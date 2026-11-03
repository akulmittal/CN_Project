#!/usr/bin/env python3
"""Best-effort UDP flood generator for the SDN abuse-blocking demo.

This is deliberately a tiny stdlib-only script so it can be launched inside a
Mininet host with ``python3 sdn/scripts/flood.py ...``.
"""

from __future__ import annotations

import argparse
import json
import socket
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="UDP flood helper")
    parser.add_argument("--target", required=True, help="destination IPv4 address")
    parser.add_argument("--port", type=int, default=9, help="destination UDP port")
    parser.add_argument(
        "--duration", type=float, default=15.0, help="seconds to send"
    )
    parser.add_argument(
        "--size", type=int, default=1200, help="payload bytes per datagram"
    )
    args = parser.parse_args()

    if not 0 < args.port < 65536:
        parser.error("--port must be between 1 and 65535")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if not 0 <= args.size <= 65507:
        parser.error("--size must be between 0 and 65507")

    payload = b"x" * args.size
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    destination = (args.target, args.port)
    deadline = time.monotonic() + args.duration
    sent = 0
    errors = 0
    started = time.monotonic()
    try:
        while time.monotonic() < deadline:
            try:
                sock.sendto(payload, destination)
                sent += 1
            except OSError:
                errors += 1
                if errors > 10000:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = max(1e-6, time.monotonic() - started)
        sock.close()
    print(
        json.dumps(
            {
                "target": f"{args.target}:{args.port}",
                "duration_s": round(elapsed, 3),
                "datagrams": sent,
                "errors": errors,
                "pps": round(sent / elapsed, 1),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
