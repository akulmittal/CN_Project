#!/usr/bin/env python3
"""Stdlib-only stand-in for the C exam backend until the real server exists.

The stub accepts one HTTP/1.1 request per TCP connection, serves ``/health``
and ``/api/v1/health``, and can POST ``server_status`` heartbeats to a Ryu-style
SDN controller. It deliberately uses raw sockets instead of ``http.server`` so
the connection lifecycle matches the C server: accept, read one request, send
one response with ``Connection: close``, then close.

Only the Python standard library is used. Example::

    python3 sdn/stub_server.py --host 127.0.0.1 --port 8080 \
        --active-attempts 3 --controller-url http://127.0.0.1:9090
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import socket
import sys
import threading
import time
from typing import Any, BinaryIO, Sequence, TextIO

SERVICE_NAME = "exam-backend-stub"
HEALTH_PATHS = frozenset({"/health", "/api/v1/health"})

ACCEPT_POLL_S = 0.5
CLIENT_TIMEOUT_S = 5.0
MAX_REQUEST_LINE_BYTES = 8192
MAX_HEADER_LINE_BYTES = 8192
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024
NOTIFIER_TIMEOUT_S = 0.3
NOTIFIER_FAILURE_LOG_INTERVAL_S = 10.0
CONNECTION_JOIN_TIMEOUT_S = 1.0
NOTIFIER_JOIN_TIMEOUT_S = 1.0

_STATUS_REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    501: "Not Implemented",
}


class BadRequest(Exception):
    """Raised when an incoming HTTP request is malformed or truncated."""


class PayloadTooLarge(Exception):
    """Raised when Content-Length exceeds the stub's body limit."""


def _json_body(payload: dict[str, Any]) -> bytes:
    """Return compact JSON with the required trailing newline."""
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return encoded.encode("utf-8") + b"\n"


def _format_peer(peer: tuple[Any, ...]) -> str:
    """Format an accept() address as HOST:PORT, bracketing IPv6 literals."""
    host = str(peer[0])
    port = int(peer[1])
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _discard_exact(reader: BinaryIO, size: int) -> None:
    """Read exactly *size* bytes from *reader* and discard them."""
    remaining = size
    while remaining > 0:
        chunk = reader.read(min(remaining, 64 * 1024))
        if not chunk:
            raise BadRequest("truncated request body")
        remaining -= len(chunk)


class JsonLineLogger:
    """Thread-safe JSON-lines logger writing to stdout and an optional file."""

    def __init__(self, path: str | None) -> None:
        self._lock = threading.Lock()
        self._stream: TextIO | None = None
        self._closed = False
        if path is not None:
            self._stream = open(path, "a", encoding="utf-8")

    def log(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {"ts": int(time.time()), "event": event}
        record.update(fields)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
        with self._lock:
            if self._closed:
                return
            try:
                print(line, flush=True)
            except (BrokenPipeError, OSError):
                pass
            if self._stream is not None:
                try:
                    self._stream.write(line + "\n")
                    self._stream.flush()
                except OSError:
                    pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._stream is not None:
                try:
                    self._stream.close()
                except OSError:
                    pass
                self._stream = None


class StubServer:
    """One-request-per-connection threaded HTTP server."""

    def __init__(
        self,
        host: str,
        port: int,
        server_id: str,
        active_attempts: int,
        logger: JsonLineLogger,
        stop_event: threading.Event,
    ) -> None:
        self.host = host
        self.port = port
        self.server_id = server_id
        self.active_attempts = active_attempts
        self._logger = logger
        self._stop = stop_event
        self._listener: socket.socket | None = None
        self._bound_port = port
        self._state_lock = threading.Lock()
        self._active_connections = 0
        self._threads: set[threading.Thread] = set()

    @property
    def bound_port(self) -> int:
        return self._bound_port

    def active_connections(self) -> int:
        with self._state_lock:
            return self._active_connections

    def bind(self) -> None:
        """Bind and listen; raises OSError if the address cannot be used."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.host, self.port))
            listener.listen(128)
            listener.settimeout(ACCEPT_POLL_S)
        except OSError:
            listener.close()
            raise
        self._listener = listener
        self._bound_port = int(listener.getsockname()[1])

    def serve_forever(self) -> None:
        listener = self._listener
        if listener is None:
            raise RuntimeError("server is not bound")
        try:
            while not self._stop.is_set():
                try:
                    conn, peer = listener.accept()
                except socket.timeout:
                    continue
                except (OSError, ValueError):
                    if self._stop.is_set():
                        break
                    raise
                self._start_connection(conn, peer)
        finally:
            self._listener = None
            try:
                listener.close()
            except OSError:
                pass

    def request_stop(self) -> None:
        """Signal the server to stop and unblock accept(). Safe for handlers."""
        self._stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def join_connections(self, timeout: float) -> None:
        """Join connection workers for at most *timeout* seconds total."""
        deadline = time.monotonic() + timeout
        with self._state_lock:
            threads = list(self._threads)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            thread.join(remaining)

    def _start_connection(self, conn: socket.socket, peer: tuple[Any, ...]) -> None:
        with self._state_lock:
            self._active_connections += 1
            active = self._active_connections
        peer_text = _format_peer(peer)
        self._logger.log(
            "connection_accepted",
            peer=peer_text,
            active_connections=active,
        )
        thread = threading.Thread(
            target=self._serve_connection,
            args=(conn, peer_text),
            name="stub-connection",
            daemon=True,
        )
        with self._state_lock:
            self._threads.add(thread)
        try:
            thread.start()
        except RuntimeError:
            with self._state_lock:
                self._threads.discard(thread)
                self._active_connections -= 1
            try:
                conn.close()
            except OSError:
                pass
            raise

    def _serve_connection(self, conn: socket.socket, peer: str) -> None:
        try:
            conn.settimeout(CLIENT_TIMEOUT_S)
            self._handle_request(conn, peer)
        except (OSError, ValueError):
            pass
        except Exception as exc:  # keep one client from killing the process
            self._logger.log(
                "connection_error",
                peer=peer,
                error=type(exc).__name__,
            )
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._state_lock:
                if self._active_connections > 0:
                    self._active_connections -= 1
                self._threads.discard(threading.current_thread())

    def _handle_request(self, conn: socket.socket, peer: str) -> None:
        reader = conn.makefile("rb")
        try:
            try:
                request = self._read_request(reader)
            except BadRequest:
                self._send_json(conn, 400, {"error": "bad_request"})
                return
            except PayloadTooLarge:
                self._send_json(conn, 413, {"error": "payload_too_large"})
                return

            if request is None:
                return

            method, target = request
            path = target.split("?", 1)[0] or "/"

            if method == "GET":
                if path in HEALTH_PATHS:
                    active = self.active_connections()
                    self._send_json(
                        conn,
                        200,
                        {
                            "status": "ok",
                            "service": SERVICE_NAME,
                            "server": self.server_id,
                            "active_attempts": self.active_attempts,
                            "active_connections": active,
                        },
                    )
                    self._logger.log(
                        "health_request",
                        peer=peer,
                        path=path,
                        status=200,
                        active_connections=active,
                    )
                else:
                    self._send_json(conn, 404, {"error": "not_found"})
            elif method == "POST" and path.startswith("/api/v1/"):
                self._send_json(conn, 501, {"error": "stub_only"})
            else:
                self._send_json(conn, 405, {"error": "method_not_allowed"})
        finally:
            try:
                reader.close()
            except OSError:
                pass

    @staticmethod
    def _read_request(reader: BinaryIO) -> tuple[str, str] | None:
        request_line = reader.readline(MAX_REQUEST_LINE_BYTES + 1)
        if not request_line:
            return None
        if (
            len(request_line) > MAX_REQUEST_LINE_BYTES
            or not request_line.endswith(b"\n")
        ):
            raise BadRequest("invalid request line")

        parts = request_line.rstrip(b"\r\n").split(b" ")
        if len(parts) != 3:
            raise BadRequest("malformed request line")
        method_raw, target_raw, version_raw = parts
        if not version_raw.startswith(b"HTTP/"):
            raise BadRequest("unsupported HTTP version")
        try:
            method = method_raw.decode("ascii")
        except UnicodeDecodeError:
            raise BadRequest("non-ASCII method") from None
        if not method:
            raise BadRequest("empty method")
        target = target_raw.decode("latin-1")
        if not target:
            raise BadRequest("empty target")

        headers: dict[str, str] = {}
        header_bytes = 0
        while True:
            line = reader.readline(MAX_HEADER_LINE_BYTES + 1)
            header_bytes += len(line)
            if header_bytes > MAX_HEADER_BYTES:
                raise BadRequest("headers too large")
            if not line:
                raise BadRequest("truncated headers")
            if (
                len(line) > MAX_HEADER_LINE_BYTES
                or not line.endswith(b"\n")
            ):
                raise BadRequest("invalid header line")
            if line in (b"\r\n", b"\n"):
                break

            name_raw, separator, value_raw = line.partition(b":")
            if not separator or not name_raw.strip():
                raise BadRequest("malformed header")
            try:
                name = name_raw.strip().decode("ascii").lower()
            except UnicodeDecodeError:
                raise BadRequest("non-ASCII header name") from None
            value = value_raw.strip().decode("latin-1")
            if name == "content-length" and name in headers:
                if headers[name] != value:
                    raise BadRequest("conflicting Content-Length")
            headers[name] = value

        raw_length = headers.get("content-length", "0")
        if not (raw_length.isascii() and raw_length.isdigit()):
            raise BadRequest("invalid Content-Length")
        content_length = int(raw_length)
        if content_length > MAX_BODY_BYTES:
            raise PayloadTooLarge
        # The body is ignored, but it must still be consumed by Content-Length
        # so the client observes a normal one-request-per-connection exchange.
        _discard_exact(reader, content_length)
        return method, target

    @staticmethod
    def _send_json(conn: socket.socket, status: int, payload: dict[str, Any]) -> None:
        body = _json_body(payload)
        reason = _STATUS_REASONS.get(status, "Error")
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        conn.sendall(header)
        conn.sendall(body)


class ControllerNotifier:
    """Daemon-thread server_status sender using a raw stdlib socket POST."""

    def __init__(
        self,
        host: str,
        port: int,
        url: str,
        server_id: str,
        exam_id: int | None,
        active_attempts: int,
        heartbeat_s: float,
        server: StubServer,
        logger: JsonLineLogger,
        stop_event: threading.Event,
    ) -> None:
        self._host = host
        self._port = port
        self._url = url
        self._server_id = server_id
        self._exam_id = exam_id
        self._active_attempts = active_attempts
        self._heartbeat_s = heartbeat_s
        self._server = server
        self._logger = logger
        self._stop = stop_event
        self._failure_lock = threading.Lock()
        self._last_failure_log = float("-inf")

    def run(self) -> None:
        while not self._stop.is_set():
            error = self._post_status()
            if error is not None:
                self._log_failure(error)
            if self._stop.wait(self._heartbeat_s):
                return

    def _post_status(self) -> str | None:
        payload = {
            "event": "server_status",
            "server": self._server_id,
            "exam_id": self._exam_id,
            "active_attempts": self._active_attempts,
            "active_connections": self._server.active_connections(),
            "ts": int(time.time()),
        }
        body = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        if ":" in self._host:
            host_header = f"[{self._host}]:{self._port}"
        else:
            host_header = f"{self._host}:{self._port}"
        request = (
            "POST /exam/event HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "User-Agent: exam-backend-stub/1\r\n"
            "\r\n"
        ).encode("ascii") + body

        try:
            conn = socket.create_connection(
                (self._host, self._port),
                timeout=NOTIFIER_TIMEOUT_S,
            )
        except OSError as exc:
            return f"connect failed: {exc}"

        try:
            conn.settimeout(NOTIFIER_TIMEOUT_S)
            conn.sendall(request)
            response = conn.recv(512)
        except OSError as exc:
            return f"request failed: {exc}"
        finally:
            try:
                conn.close()
            except OSError:
                pass

        if not response:
            return "empty controller response"
        first_line = response.split(b"\r\n", 1)[0]
        fields = first_line.split(b" ")
        if len(fields) < 2 or not fields[1].isdigit():
            return "malformed controller response"
        status = int(fields[1])
        if status < 200 or status >= 300:
            return f"controller returned HTTP {status}"
        return None

    def _log_failure(self, error: str) -> None:
        with self._failure_lock:
            now = time.monotonic()
            if now - self._last_failure_log < NOTIFIER_FAILURE_LOG_INTERVAL_S:
                return
            self._last_failure_log = now
        self._logger.log(
            "notifier_failure",
            controller=self._url,
            error=error,
        )


def _parse_controller_url(value: str) -> tuple[str, int]:
    prefix = "http://"
    if value[: len(prefix)].lower() != prefix:
        raise ValueError("--controller-url must be http://HOST:PORT")
    authority = value[len(prefix) :]
    if not authority:
        raise ValueError("--controller-url must include a host")
    if any(char in authority for char in "?#@"):
        raise ValueError("--controller-url must be http://HOST:PORT")
    if "/" in authority:
        host_port, tail = authority.split("/", 1)
        if tail:
            raise ValueError("--controller-url must not include a path or query")
    else:
        host_port = authority

    if host_port.startswith("["):
        closing = host_port.find("]")
        if closing < 0:
            raise ValueError("--controller-url has an invalid IPv6 host")
        host = host_port[1:closing]
        remainder = host_port[closing + 1 :]
        if remainder and not remainder.startswith(":"):
            raise ValueError("--controller-url has an invalid port")
        port_text = remainder[1:] if remainder else ""
    else:
        if host_port.count(":") > 1:
            raise ValueError("--controller-url must bracket IPv6 hosts")
        if ":" in host_port:
            host, port_text = host_port.rsplit(":", 1)
        else:
            host, port_text = host_port, ""

    if not host:
        raise ValueError("--controller-url must include a host")
    if port_text == "":
        port = 80
    elif port_text.isascii() and port_text.isdigit():
        port = int(port_text)
    else:
        raise ValueError("--controller-url has an invalid port")
    if port < 1 or port > 65535:
        raise ValueError("--controller-url port must be between 1 and 65535")
    return host, port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stdlib-only exam backend HTTP/controller stub.",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="bind address (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="bind port (default: %(default)s)",
    )
    parser.add_argument(
        "--server-id",
        default=None,
        help=(
            "server identifier sent to the controller; defaults to HOST:PORT, "
            "with 0.0.0.0 mapped to 127.0.0.1"
        ),
    )
    parser.add_argument(
        "--controller-url",
        default=None,
        metavar="http://HOST:PORT",
        help="optional SDN controller URL that receives server_status events",
    )
    parser.add_argument(
        "--exam-id",
        type=int,
        default=None,
        help="optional exam id included in server_status events",
    )
    parser.add_argument(
        "--active-attempts",
        type=int,
        default=0,
        help="reported active attempt count (default: %(default)s)",
    )
    parser.add_argument(
        "--heartbeat-s",
        type=float,
        default=2.0,
        help="controller heartbeat interval in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="optional file that receives the same JSON-lines logs as stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.port < 1 or args.port > 65535:
        parser.error("--port must be between 1 and 65535")
    if args.active_attempts < 0:
        parser.error("--active-attempts must be >= 0")
    if not math.isfinite(args.heartbeat_s) or args.heartbeat_s <= 0.0:
        parser.error("--heartbeat-s must be a positive finite number")

    controller: tuple[str, int] | None = None
    if args.controller_url is not None:
        try:
            controller = _parse_controller_url(args.controller_url)
        except ValueError as exc:
            parser.error(str(exc))

    server_id = args.server_id
    if not server_id:
        display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
        server_id = f"{display_host}:{args.port}"

    try:
        logger = JsonLineLogger(args.log_file)
    except OSError as exc:
        print(
            f"stub_server: cannot open log file {args.log_file!r}: {exc}",
            file=sys.stderr,
        )
        return 2

    stop_event = threading.Event()
    server = StubServer(
        host=args.host,
        port=args.port,
        server_id=server_id,
        active_attempts=args.active_attempts,
        logger=logger,
        stop_event=stop_event,
    )

    def handle_signal(_signum: int, _frame: Any) -> None:
        # Keep the handler lock-free; it may interrupt the main thread anywhere.
        stop_event.set()
        server.request_stop()

    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, handle_signal)

    notifier_thread: threading.Thread | None = None
    exit_code = 0
    try:
        server.bind()
        logger.log(
            "listening",
            host=args.host,
            port=server.bound_port,
            server=server_id,
            controller_url=args.controller_url,
            active_attempts=args.active_attempts,
            heartbeat_s=args.heartbeat_s,
        )
        if controller is not None:
            notifier = ControllerNotifier(
                host=controller[0],
                port=controller[1],
                url=args.controller_url,
                server_id=server_id,
                exam_id=args.exam_id,
                active_attempts=args.active_attempts,
                heartbeat_s=args.heartbeat_s,
                server=server,
                logger=logger,
                stop_event=stop_event,
            )
            notifier_thread = threading.Thread(
                target=notifier.run,
                name="stub-notifier",
                daemon=True,
            )
            notifier_thread.start()
        server.serve_forever()
    except OSError as exc:
        if not stop_event.is_set():
            print(f"stub_server: {exc}", file=sys.stderr)
            exit_code = 1
    finally:
        stop_event.set()
        server.request_stop()
        server.join_connections(CONNECTION_JOIN_TIMEOUT_S)
        if notifier_thread is not None:
            notifier_thread.join(NOTIFIER_JOIN_TIMEOUT_S)
        if exit_code == 0:
            logger.log("shutdown")
        logger.close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
