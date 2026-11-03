#!/usr/bin/env python3
"""Measure one-request-per-connection HTTP latency using only the stdlib.

Intended to run inside a Mininet student host during an iperf background load.
It is an exam-like client: a new TCP connection is opened for every request and
the response is read until the server closes it.
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import time
from collections import Counter


def parse_target(value: str) -> tuple[str, int]:
    text = value.strip()
    if text.startswith("http://"):
        text = text[7:]
    if text.startswith("["):
        end = text.find("]")
        if end < 0 or not text[end + 1 :].startswith(":"):
            raise ValueError("invalid target")
        host = text[1:end]
        port = int(text[end + 2 :])
        return host, port
    if ":" not in text:
        raise ValueError("target must be HOST:PORT")
    host, raw_port = text.rsplit(":", 1)
    if not host:
        raise ValueError("target has an empty host")
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValueError("target port must be between 1 and 65535")
    return host, port


def request_once(host: str, port: int, timeout: float) -> tuple[bool, float, str, str]:
    started = time.perf_counter()
    conn: socket.socket | None = None
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
        conn.settimeout(timeout)
        request = (
            "GET /health HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Connection: close\r\n"
            "User-Agent: exam-sdn-latency-probe/1\r\n"
            "\r\n"
        ).encode("ascii")
        conn.sendall(request)
        chunks: list[bytes] = []
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(item) for item in chunks) > 256 * 1024:
                break
        data = b"".join(chunks)
        latency_ms = (time.perf_counter() - started) * 1000.0
        header, _, body = data.partition(b"\r\n\r\n")
        first_line = header.split(b"\r\n", 1)[0].split(b" ")
        if len(first_line) < 2 or not first_line[1].isdigit():
            return False, latency_ms, "malformed_response", ""
        status = int(first_line[1])
        if status != 200:
            return False, latency_ms, f"http_{status}", ""
        try:
            parsed = json.loads(body.decode("utf-8"))
            server = str(parsed.get("server", "")) if isinstance(parsed, dict) else ""
        except (UnicodeDecodeError, json.JSONDecodeError):
            server = ""
        return True, latency_ms, "", server
    except (OSError, ValueError) as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        return False, latency_ms, f"{type(exc).__name__}: {exc}", ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description="stdlib HTTP latency probe")
    parser.add_argument("--target", required=True, help="HOST:PORT")
    parser.add_argument("--count", type=int, default=60, help="requests to send")
    parser.add_argument(
        "--interval-ms", type=float, default=50.0, help="pause between requests"
    )
    parser.add_argument("--timeout", type=float, default=2.0, help="socket timeout")
    parser.add_argument("--warmup", type=int, default=3, help="warm-up requests")
    parser.add_argument("--out", default=None, help="optional JSON output path")
    args = parser.parse_args()

    try:
        host, port = parse_target(args.target)
    except ValueError as exc:
        parser.error(str(exc))
    if args.count < 1:
        parser.error("--count must be >= 1")
    if args.interval_ms < 0:
        parser.error("--interval-ms must be >= 0")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    for _ in range(max(0, args.warmup)):
        request_once(host, port, args.timeout)
        if args.interval_ms:
            time.sleep(args.interval_ms / 1000.0)

    latencies: list[float] = []
    errors: Counter[str] = Counter()
    servers: Counter[str] = Counter()
    started = time.monotonic()
    for _ in range(args.count):
        ok, latency_ms, error, server = request_once(host, port, args.timeout)
        if ok:
            latencies.append(latency_ms)
            if server:
                servers[server] += 1
        else:
            errors[error] += 1
        if args.interval_ms:
            time.sleep(args.interval_ms / 1000.0)
    elapsed = max(1e-9, time.monotonic() - started)
    summary = {
        "target": f"{host}:{port}",
        "requests": args.count,
        "ok": len(latencies),
        "errors": sum(errors.values()),
        "error_details": dict(errors),
        "elapsed_s": round(elapsed, 3),
        "latency_ms": {
            "min": round(min(latencies), 3) if latencies else 0.0,
            "median": round(statistics.median(latencies), 3) if latencies else 0.0,
            "p95": round(percentile(latencies, 0.95), 3) if latencies else 0.0,
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "servers": dict(servers),
    }
    encoded = json.dumps(summary, separators=(",", ":"))
    print(encoded, flush=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
