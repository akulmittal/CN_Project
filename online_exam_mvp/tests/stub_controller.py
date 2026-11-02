#!/usr/bin/env python3
"""Tiny local stand-in for the Ryu REST API used by tests/integration.sh."""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    events_path = ""

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path != "/exam/event":
            self._json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "bad_length"})
            return
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "invalid_json"})
            return
        if not isinstance(parsed, dict) or "event" not in parsed:
            self._json(400, {"error": "invalid_event"})
            return
        with _lock:
            with open(self.events_path, "ab") as handle:
                handle.write(body.rstrip(b"\r\n") + b"\n")
        self._json(200, {"ok": True})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path != "/sdn/state":
            self._json(404, {"error": "not_found"})
            return
        self._json(200, {"mode": "static", "exam_active": False,
                         "blocked_hosts": [], "servers": {}})

    def log_message(self, *_args) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    directory = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(directory, exist_ok=True)
    open(args.out, "wb").close()
    Handler.events_path = os.path.abspath(args.out)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"stub controller on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
