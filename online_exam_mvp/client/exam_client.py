#!/usr/bin/env python3
"""Small HTTP/1.1 client for the exam server using only the stdlib socket API.

The server intentionally speaks one request per TCP connection and responds
with ``Connection: close``. This client therefore opens a fresh socket for
every operation, which also makes connection setup part of the measured
latency. No requests/urllib/http.client are used for exam traffic.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass
class ExamResponse:
    status: int
    ok: bool
    latency_ms: float
    body: Any = None
    raw_body: bytes = b""
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)

    def error_detail(self) -> str:
        if self.error:
            return self.error
        if isinstance(self.body, dict) and "error" in self.body:
            return str(self.body["error"])
        return ""


def parse_target(target: str) -> tuple[str, int]:
    """Parse HOST:PORT, with an optional http:// prefix."""
    value = target.strip()
    if value.startswith("http://"):
        value = value[7:]
    if value.startswith("["):
        raise ValueError("IPv6 targets are not supported by this demo client")
    if ":" not in value:
        raise ValueError("target must be HOST:PORT")
    host, port_text = value.rsplit(":", 1)
    if not host:
        raise ValueError("target host is empty")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("target port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("target port must be between 1 and 65535")
    return host, port


def _parse_response(data: bytes) -> tuple[int, dict[str, str], bytes]:
    separator = data.find(b"\r\n\r\n")
    if separator < 0:
        raise ValueError("incomplete HTTP response")
    header_block = data[:separator].decode("iso-8859-1")
    body = data[separator + 4:]
    lines = header_block.split("\r\n")
    status_parts = lines[0].split(" ", 2)
    if len(status_parts) < 2 or not status_parts[1].isdigit():
        raise ValueError("malformed HTTP status line")
    status = int(status_parts[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    content_length = headers.get("content-length")
    if content_length is not None:
        try:
            expected = int(content_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if expected >= 0 and len(body) != expected:
            if len(body) > expected:
                body = body[:expected]
            else:
                raise ValueError("short HTTP response body")
    return status, headers, body


class ExamClient:
    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.cookie: Optional[str] = None
        self.last_response: Optional[ExamResponse] = None

    def _request_bytes(self, method: str, path: str, body: Optional[dict[str, Any]]) -> bytes:
        payload = b""
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = [
            f"{method} {path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "User-Agent: exam-client/1",
            "Accept: application/json",
            "Content-Type: application/json",
            "Connection: close",
            f"Content-Length: {len(payload)}",
        ]
        if self.cookie:
            request.append(f"Cookie: exam_session={self.cookie}")
        head = ("\r\n".join(request) + "\r\n\r\n").encode("ascii")
        return head + payload

    def request(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> ExamResponse:
        started_at = time.time()
        started = time.perf_counter()
        status = 0
        response_body: Any = None
        raw_body = b""
        error: Optional[str] = None

        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(self._request_bytes(method, path, body))
                chunks: list[bytes] = []
                received = 0
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_RESPONSE_BYTES:
                        raise ValueError("response exceeds client limit")
                    chunks.append(chunk)
                raw = b"".join(chunks)
            status, headers, raw_body = _parse_response(raw)

            set_cookie = headers.get("set-cookie")
            if set_cookie:
                for part in set_cookie.split(";"):
                    name, separator, value = part.strip().partition("=")
                    if separator and name == "exam_session":
                        self.cookie = value
                        break

            if raw_body:
                try:
                    response_body = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    response_body = raw_body.decode("utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            error = str(exc)

        latency_ms = (time.perf_counter() - started) * 1000.0
        ok = error is None and 200 <= status < 300
        result = ExamResponse(
            status=status,
            ok=ok,
            latency_ms=latency_ms,
            body=response_body,
            raw_body=raw_body,
            error=error,
            started_at=started_at,
        )
        self.last_response = result
        return result

    def health(self) -> ExamResponse:
        return self.request("GET", "/health")

    def login(self, username: str, password: str) -> ExamResponse:
        return self.request("POST", "/api/v1/login",
                            {"username": username, "password": password})

    def me(self) -> ExamResponse:
        return self.request("GET", "/api/v1/me")

    def logout(self) -> ExamResponse:
        return self.request("POST", "/api/v1/logout")

    def start(self, exam_id: int) -> ExamResponse:
        return self.request("POST", f"/api/v1/student/exams/{int(exam_id)}/start")

    def questions(self, exam_id: int) -> ExamResponse:
        return self.request("GET", f"/api/v1/student/exams/{int(exam_id)}/questions")

    def answer(self, question_id: int, answer_text: str) -> ExamResponse:
        return self.request("POST", "/api/v1/student/answers",
                            {"question_id": int(question_id), "answer": answer_text})

    def submit(self, exam_id: int) -> ExamResponse:
        return self.request("POST", f"/api/v1/student/exams/{int(exam_id)}/submit")

    def close(self) -> None:
        """Compatibility no-op: this client never keeps a connection open."""
        self.cookie = None

    def __enter__(self) -> "ExamClient":
        return self

    def __exit__(self, *_args) -> None:
        self.close()
