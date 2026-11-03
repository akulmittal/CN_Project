#!/usr/bin/env bash
# End-to-end checks for the Mininet-ready exam server.
# Requires: make, python3, curl. Does not require Mininet or Ryu.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TMP="$(mktemp -d)"
SUCCESS=0
declare -a PIDS=()

fail() {
    printf '\nFAIL: %s\n' "$*" >&2
    printf 'Temp files kept in %s\n' "$TMP" >&2
    for log in "$TMP"/*.log; do
        [ -f "$log" ] || continue
        printf '\n--- %s (tail) ---\n' "$log" >&2
        tail -40 "$log" >&2 || true
    done
    exit 1
}

cleanup() {
    for pid in "${PIDS[@]:-}"; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    if [ "$SUCCESS" -eq 1 ]; then
        rm -rf "$TMP"
    fi
}
trap cleanup EXIT

free_port() {
    python3 - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
}

wait_for_health() {
    local port="$1"
    local attempts="${2:-100}"
    for _ in $(seq 1 "$attempts"); do
        if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

wait_for_event() {
    local event="$1"
    for _ in $(seq 1 100); do
        if grep -q "\"event\":\"${event}\"" "$TMP/events.ndjson" 2>/dev/null; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

stop_server() {
    local pid="$1"
    local name="$2"
    kill -INT "$pid" 2>/dev/null || fail "$name was not running"
    if ! wait "$pid"; then
        fail "$name did not exit cleanly after SIGINT"
    fi
}

printf '1) Build\n'
make >/dev/null

printf '2) Start stub controller and server\n'
STUB_PORT="$(free_port)"
PORT="$(free_port)"
python3 tests/stub_controller.py --port "$STUB_PORT" --out "$TMP/events.ndjson" \
    >"$TMP/stub.log" 2>&1 &
STUB_PID=$!
PIDS+=("$STUB_PID")
for _ in $(seq 1 50); do
    if curl -fsS "http://127.0.0.1:${STUB_PORT}/sdn/state" >/dev/null 2>&1; then break; fi
    sleep 0.1
done

./exam_server --port "$PORT" --db "$TMP/exam.db" --addr 127.0.0.1 \
    --server-id "127.0.0.1:${PORT}" \
    --controller-url "http://127.0.0.1:${STUB_PORT}" \
    >"$TMP/server1.log" 2>&1 &
SERVER1=$!
PIDS+=("$SERVER1")
wait_for_health "$PORT" || fail "server did not become healthy"

printf '3) Seed 20 students and a 50-question active exam\n'
python3 tools/seed_demo.py --db "$TMP/exam.db" \
    --base-url "http://127.0.0.1:${PORT}" --students 20 \
    --question-count 50 --users-file "$TMP/users.csv" \
    >"$TMP/seed.json" || fail "seed_demo failed"
EXAM_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exam_id"])' "$TMP/seed.json")"
[ -n "$EXAM_ID" ] || fail "seed_demo did not print an exam id"

printf '4) Run loadgen with 20 concurrent clients\n'
python3 client/loadgen.py --target "127.0.0.1:${PORT}" --clients 20 \
    --exam-id "$EXAM_ID" --users-file "$TMP/users.csv" \
    --answers-per-client 3 --think-ms 0 --timeout 15 \
    --out "$TMP/results.csv" >"$TMP/loadgen.json" || fail "loadgen failed"

python3 - "$TMP/loadgen.json" <<'PY' || fail "loadgen summary had errors"
import json
import sys
summary = json.load(open(sys.argv[1]))
print("   ", summary)
assert summary["clients"] == 20, summary
assert summary["errors"] == 0, summary
assert summary["requests"] >= 100, summary
PY

HEADER="$(head -1 "$TMP/results.csv")"
[ "$HEADER" = "ts_start,client,op,latency_ms,status,ok" ] \
    || fail "unexpected CSV header: $HEADER"

printf '5) Verify controller events\n'
for event in server_status exam_published attempt_started attempt_submitted; do
    wait_for_event "$event" || fail "missing event: $event"
    printf '    received %s\n' "$event"
done

printf '6) SIGINT grace and immediate restart on the same port\n'
stop_server "$SERVER1" "server1"
if grep -qi 'bind' "$TMP/server1.log"; then
    fail "unexpected bind error in server log"
fi
./exam_server --port "$PORT" --db "$TMP/exam.db" --addr 127.0.0.1 \
    --server-id "127.0.0.1:${PORT}" \
    --controller-url "http://127.0.0.1:${STUB_PORT}" \
    >"$TMP/server1-restart.log" 2>&1 &
SERVER1B=$!
PIDS+=("$SERVER1B")
wait_for_health "$PORT" || fail "server could not restart on the same port"
stop_server "$SERVER1B" "restarted server1"

printf '7) Two servers sharing one WAL database\n'
SHARED_PORT_A="$(free_port)"
SHARED_PORT_B="$(free_port)"
./exam_server --port "$SHARED_PORT_A" --db "$TMP/shared.db" --addr 127.0.0.1 \
    --server-id "127.0.0.1:${SHARED_PORT_A}" >"$TMP/shared-a.log" 2>&1 &
SHARED_A=$!
PIDS+=("$SHARED_A")
wait_for_health "$SHARED_PORT_A" || fail "shared server A did not start"
python3 tools/seed_demo.py --db "$TMP/shared.db" \
    --base-url "http://127.0.0.1:${SHARED_PORT_A}" --students 5 \
    --users-file "$TMP/shared-users.csv" >"$TMP/shared-seed.json" \
    || fail "shared database seed failed"
./exam_server --port "$SHARED_PORT_B" --db "$TMP/shared.db" --addr 127.0.0.1 \
    --server-id "127.0.0.1:${SHARED_PORT_B}" >"$TMP/shared-b.log" 2>&1 &
SHARED_B=$!
PIDS+=("$SHARED_B")
wait_for_health "$SHARED_PORT_B" || fail "shared server B did not start"

python3 - "$TMP/shared-users.csv" "$SHARED_PORT_B" <<'PY' || fail "shared DB login failed"
import csv
import os
import sys
sys.path.insert(0, os.path.join(os.getcwd(), "client"))
from exam_client import ExamClient
rows = list(csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8")))
student = next(row for row in rows if row["role"] == "student")
client = ExamClient("127.0.0.1", int(sys.argv[2]), timeout=10)
response = client.login(student["username"], student["password"])
assert response.ok, (response.status, response.error_detail())
exams = client.request("GET", "/api/v1/student/exams")
assert exams.ok, (exams.status, exams.error_detail())
print("    login and exam list succeeded through server B")
PY
stop_server "$SHARED_A" "shared server A"
stop_server "$SHARED_B" "shared server B"

printf '8) Unreachable controller must not delay /health\n'
OFFLINE_PORT="$(free_port)"
./exam_server --port "$OFFLINE_PORT" --db "$TMP/offline.db" --addr 127.0.0.1 \
    --server-id "127.0.0.1:${OFFLINE_PORT}" \
    --controller-url "http://127.0.0.1:1" >"$TMP/offline.log" 2>&1 &
OFFLINE=$!
PIDS+=("$OFFLINE")
wait_for_health "$OFFLINE_PORT" || fail "offline-controller server did not start"
python3 - "$OFFLINE_PORT" <<'PY' || fail "health was delayed by unreachable controller"
import subprocess
import sys
port = sys.argv[1]
times = []
for _ in range(5):
    output = subprocess.check_output([
        "curl", "-sS", "-o", "/dev/null", "-w", "%{time_total}",
        f"http://127.0.0.1:{port}/health",
    ], text=True)
    times.append(float(output))
print("    health seconds:", times)
assert max(times) < 1.0, times
PY
stop_server "$OFFLINE" "offline-controller server"

SUCCESS=1
printf '\nAll integration checks passed.\n'
