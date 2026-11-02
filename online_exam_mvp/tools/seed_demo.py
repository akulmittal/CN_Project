#!/usr/bin/env python3
"""Create demo accounts and one active published exam.

The generated users CSV contains real credentials, so it is written with mode
0600 and is git-ignored. Passwords are passed to create_user over stdin, never
on the command line.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "client"))

from exam_client import ExamClient, parse_target  # noqa: E402

PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
PASSWORD_LENGTH = 20


def generate_password() -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def create_account(db_path: str, username: str, password: str, role: str) -> bool:
    create_user = os.path.join(PROJECT_ROOT, "create_user")
    if not os.path.isfile(create_user):
        print("seed_demo: ./create_user is missing; run make first", file=sys.stderr)
        return False
    process = subprocess.run(
        [create_user, "--db", db_path, "--password-stdin", username, role],
        input=password + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        print(f"seed_demo: could not create {username}: {detail}", file=sys.stderr)
        return False
    return True


def reset_users(db_path: str, usernames: list[str]) -> None:
    try:
        connection = sqlite3.connect(db_path, timeout=5)
        placeholders = ",".join("?" for _ in usernames)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            f"DELETE FROM student_answers WHERE attempt_id IN "
            f"(SELECT id FROM exam_attempts WHERE user_id IN "
            f"(SELECT id FROM users WHERE username IN ({placeholders})))",
            usernames,
        )
        connection.execute(
            f"DELETE FROM exam_attempts WHERE user_id IN "
            f"(SELECT id FROM users WHERE username IN ({placeholders}))",
            usernames,
        )
        connection.execute(
            f"DELETE FROM sessions WHERE user_id IN "
            f"(SELECT id FROM users WHERE username IN ({placeholders}))",
            usernames,
        )
        connection.execute(
            f"DELETE FROM users WHERE username IN ({placeholders})", usernames)
        connection.commit()
        connection.close()
    except sqlite3.Error as exc:
        print(f"seed_demo: could not reset users: {exc}", file=sys.stderr)


def add_question(client: ExamClient, exam_id: int, index: int,
                 question_type: str, text: str, correct: str,
                 options: list[str] | None = None) -> bool:
    payload: dict[str, Any] = {
        "exam_id": exam_id,
        "question_text": text,
        "question_type": question_type,
        "marks": 1,
        "correct_answer": correct,
    }
    if options is not None:
        payload["options"] = options
    response = client.request("POST", "/api/v1/admin/questions", payload)
    if not response.ok:
        print(f"seed_demo: question {index} failed: "
              f"HTTP {response.status} {response.error_detail()}", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="database/exam.db", help="SQLite database path")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080",
                        help="running exam server base URL")
    parser.add_argument("--students", type=int, default=20, help="student accounts (1..50)")
    parser.add_argument("--users-file", default="users.csv", help="credential CSV output")
    parser.add_argument("--admin-user", default="examadmin")
    parser.add_argument("--student-prefix", default="student")
    parser.add_argument("--exam-title", default="SDN Socket Programming Demo Exam")
    parser.add_argument("--duration-minutes", type=int, default=120)
    parser.add_argument("--question-count", type=int, default=10,
                        help="number of questions to create (default 10)")
    parser.add_argument("--reset-users", action="store_true",
                        help="delete these demo usernames first (for repeatable local demos)")
    args = parser.parse_args()

    if not 1 <= args.students <= 50:
        parser.error("--students must be between 1 and 50")
    if args.duration_minutes < 1 or args.duration_minutes > 1440:
        parser.error("--duration-minutes must be between 1 and 1440")
    if args.question_count < 1 or args.question_count > 200:
        parser.error("--question-count must be between 1 and 200")

    db_path = os.path.abspath(args.db)
    users_path = os.path.abspath(args.users_file)
    try:
        host, port = parse_target(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))

    accounts: list[tuple[str, str, str]] = [
        (args.admin_user, generate_password(), "admin")
    ]
    for index in range(1, args.students + 1):
        accounts.append((f"{args.student_prefix}{index:02d}", generate_password(), "student"))

    if args.reset_users:
        reset_users(db_path, [username for username, _, _ in accounts])

    for username, password, role in accounts:
        if not create_account(db_path, username, password, role):
            print("seed_demo: hint: use a new --db path or --reset-users", file=sys.stderr)
            return 1

    os.makedirs(os.path.dirname(users_path), exist_ok=True)
    with open(users_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["username", "password", "role"])
        writer.writerows(accounts)
    os.chmod(users_path, 0o600)

    admin_username, admin_password, _ = accounts[0]
    client = ExamClient(host, port, timeout=10.0)
    login = client.login(admin_username, admin_password)
    if not login.ok:
        print(f"seed_demo: admin login failed: HTTP {login.status} "
              f"{login.error_detail()}", file=sys.stderr)
        return 1

    start_time = int(time.time()) - 30
    creation = client.request("POST", "/api/v1/admin/exams", {
        "title": args.exam_title,
        "start_time": start_time,
        "duration_minutes": args.duration_minutes,
    })
    if not creation.ok or not isinstance(creation.body, dict):
        print(f"seed_demo: exam creation failed: HTTP {creation.status} "
              f"{creation.error_detail()}", file=sys.stderr)
        return 1
    exam_id = int(creation.body["exam_id"])

    question_templates = [
        ("mcq", "Which protocol provides reliable byte streams?",
         "TCP", ["TCP", "UDP", "ICMP", "ARP"]),
        ("mcq", "Which OpenFlow message installs forwarding rules?",
         "FLOW_MOD", ["FLOW_MOD", "PACKET_IN", "HELLO", "BARRIER_REQUEST"]),
        ("mcq", "Which device runs the SDN control plane?",
         "Controller", ["Controller", "Switch", "Hub", "Repeater"]),
        ("mcq", "What does WAL improve in SQLite?",
         "Concurrent readers", ["Concurrent readers", "DNS", "TLS", "ARP"]),
        ("mcq", "Which header carries a session cookie?",
         "Cookie", ["Cookie", "Host", "Content-Length", "Location"]),
        ("mcq", "What is the default OpenFlow port used in this project?",
         "6653", ["6653", "80", "443", "9090"]),
        ("mcq", "Which Python module is used for exam traffic in this project?",
         "socket", ["socket", "requests", "urllib", "scapy"]),
        ("mcq", "What does the controller do when the VIP backend fails?",
         "Select a healthy server", ["Select a healthy server", "Drop all traffic",
                                      "Reboot the switch", "Disable ARP"]),
        ("integer", "How many bits are in an IPv4 address?", "32"),
        ("text", "Name one benefit of SDN in this project.", "centralized policy"),
    ]
    for index in range(1, args.question_count + 1):
        template = question_templates[(index - 1) % len(question_templates)]
        question_type = template[0]
        text = f"{template[1]} (question {index})"
        correct = template[2]
        options = template[3] if len(template) > 3 else None
        if not add_question(client, exam_id, index, question_type, text, correct, options):
            return 1

    publish = client.request("POST", f"/api/v1/admin/exams/{exam_id}/publish")
    if not publish.ok:
        print(f"seed_demo: publish failed: HTTP {publish.status} "
              f"{publish.error_detail()}", file=sys.stderr)
        return 1

    print(json.dumps({
        "exam_id": exam_id,
        "students": args.students,
        "users_file": users_path,
        "base_url": f"http://{host}:{port}",
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
