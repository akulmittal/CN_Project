#!/usr/bin/env python3
"""Concurrent exam load generator using only the Python stdlib socket client.

CSV columns: ts_start,client,op,latency_ms,status,ok
The final stdout line is one JSON summary object. Diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exam_client import ExamClient, ExamResponse, parse_target  # noqa: E402


@dataclass
class User:
    username: str
    password: str


def read_users(path: str) -> list[User]:
    with open(path, newline="", encoding="utf-8") as handle:
        raw = [row for row in csv.reader(handle) if row and not row[0].lstrip().startswith("#")]
    if not raw:
        raise ValueError("users file is empty")

    header = [cell.strip().lower() for cell in raw[0]]
    if "username" in header and "password" in header:
        username_index = header.index("username")
        password_index = header.index("password")
        role_index = header.index("role") if "role" in header else None
        rows = raw[1:]
    else:
        username_index, password_index, role_index, rows = 0, 1, None, raw

    users: list[User] = []
    for row in rows:
        if len(row) <= max(username_index, password_index):
            continue
        if role_index is not None and len(row) > role_index:
            role = row[role_index].strip().lower()
            if role and role != "student":
                continue
        username = row[username_index].strip()
        password = row[password_index].strip()
        if username and password:
            users.append(User(username=username, password=password))
    if not users:
        raise ValueError("users file contains no student accounts")
    return users


def pick_answer(question: dict[str, Any]) -> str:
    options = question.get("options")
    if isinstance(options, list) and options:
        return str(options[0])
    if question.get("question_type") == "integer":
        return "0"
    return "answer"


class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def add(self, username: str, operation: str, response: ExamResponse) -> None:
        row = {
            "ts_start": round(response.started_at, 6),
            "client": username,
            "op": operation,
            "latency_ms": round(response.latency_ms, 3),
            "status": response.status,
            "ok": 1 if response.ok else 0,
        }
        with self.lock:
            self.rows.append(row)


def run_client(
    client: ExamClient,
    user: User,
    exam_id: int,
    answers_per_client: int,
    think_ms: int,
    recorder: Recorder,
    barrier: threading.Barrier,
) -> None:
    try:
        barrier.wait(timeout=30)
    except threading.BrokenBarrierError:
        return

    login = client.login(user.username, user.password)
    recorder.add(user.username, "login", login)
    if not login.ok:
        return

    started = client.start(exam_id)
    recorder.add(user.username, "start", started)
    if not started.ok:
        return

    paper = client.questions(exam_id)
    recorder.add(user.username, "questions", paper)
    questions = []
    if paper.ok and isinstance(paper.body, dict):
        candidate = paper.body.get("questions")
        if isinstance(candidate, list):
            questions = [q for q in candidate if isinstance(q, dict) and "id" in q]

    for index in range(answers_per_client):
        if not questions:
            break
        question = questions[index % len(questions)]
        saved = client.answer(int(question["id"]), pick_answer(question))
        recorder.add(user.username, "answer", saved)
        if think_ms > 0:
            time.sleep(think_ms / 1000.0)

    submitted = client.submit(exam_id)
    recorder.add(user.username, "submit", submitted)


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100.0 * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="HOST:PORT of the exam server or VIP")
    parser.add_argument("--clients", type=int, default=1, help="number of concurrent student clients")
    parser.add_argument("--exam-id", type=int, required=True, help="published exam id")
    parser.add_argument("--users-file", required=True, help="CSV with username,password[,role]")
    parser.add_argument("--answers-per-client", type=int, default=5, help="answers saved per client")
    parser.add_argument("--think-ms", type=int, default=0, help="think time between answers")
    parser.add_argument("--out", required=True, help="results CSV path")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request socket timeout (s)")
    args = parser.parse_args(argv)

    if args.clients < 1:
        parser.error("--clients must be at least 1")
    if args.answers_per_client < 0:
        parser.error("--answers-per-client must not be negative")
    if args.think_ms < 0:
        parser.error("--think-ms must not be negative")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    try:
        host, port = parse_target(args.target)
        users = read_users(args.users_file)
    except (OSError, ValueError) as exc:
        print(f"loadgen: {exc}", file=sys.stderr)
        return 2

    if len(users) < args.clients:
        print(f"loadgen: users file has {len(users)} students, need {args.clients}",
              file=sys.stderr)
        return 2

    users = users[:args.clients]
    recorder = Recorder()
    barrier = threading.Barrier(args.clients)
    threads: list[threading.Thread] = []
    wall_start = time.perf_counter()
    for index, user in enumerate(users):
        client = ExamClient(host, port, timeout=args.timeout)
        thread = threading.Thread(
            target=run_client,
            name=f"client-{index}",
            args=(client, user, args.exam_id, args.answers_per_client,
                  args.think_ms, recorder, barrier),
            daemon=True,
        )
        threads.append(thread)

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    duration_s = time.perf_counter() - wall_start

    rows = sorted(recorder.rows, key=lambda row: (row["ts_start"], row["client"]))
    output_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(output_dir, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["ts_start", "client", "op", "latency_ms", "status", "ok"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    successful = [row for row in rows if row["ok"]]
    latencies = [float(row["latency_ms"]) for row in successful]
    requests = len(rows)
    errors = requests - len(successful)
    summary = {
        "clients": args.clients,
        "requests": requests,
        "errors": errors,
        "p50_ms": round(percentile(latencies, 50), 3),
        "p95_ms": round(percentile(latencies, 95), 3),
        "p99_ms": round(percentile(latencies, 99), 3),
        "throughput_rps": round(len(successful) / duration_s, 3) if duration_s > 0 else 0.0,
        "duration_s": round(duration_s, 3),
    }
    print(json.dumps(summary, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
