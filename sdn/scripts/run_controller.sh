#!/usr/bin/env bash
# Start the os-ken exam controller with the project-local virtualenv.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$ROOT/sdn/.venv"
MANAGER="$VENV/bin/osken-manager"
RUN_DIR="$ROOT/run"

MODE="${EXAM_SDN_MODE:-sdn}"
OF_PORT="${EXAM_SDN_OF_PORT:-6653}"
REST_PORT="${EXAM_SDN_REST_PORT:-9090}"
REST_HOST="${EXAM_SDN_REST_HOST:-0.0.0.0}"
METADATA="${EXAM_SDN_METADATA:-$RUN_DIR/topology.json}"
LOG_FILE="${EXAM_SDN_LOG:-$RUN_DIR/controller.ndjson}"
MONITOR_INTERVAL="${EXAM_SDN_MONITOR_INTERVAL:-2.0}"
BLOCK_SECONDS="${EXAM_SDN_BLOCK_SECONDS:-20.0}"
HEALTH_INTERVAL="${EXAM_SDN_HEALTH_INTERVAL:-2.0}"
HEALTH_FAILURES="${EXAM_SDN_HEALTH_FAILURES:-2}"
QOS_BACKEND="${EXAM_SDN_QOS_BACKEND:-auto}"
EXAM_RATE="${EXAM_SDN_EXAM_RATE_MBPS:-8.0}"
BG_RATE="${EXAM_SDN_BACKGROUND_RATE_MBPS:-2.0}"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Start the exam SDN controller.  The OpenFlow port defaults to 6653 and the
REST API to 0.0.0.0:9090.  This script is safe to run as a normal user; if OVS
meters are unsupported, the optional HTB queue fallback needs root.

Options:
  --mode static|sdn            initial mode (default: $MODE)
  --of-port PORT               OpenFlow listen port (default: $OF_PORT)
  --rest-host HOST             REST bind address (default: $REST_HOST)
  --rest-port PORT             REST port (default: $REST_PORT)
  --metadata PATH              topology.json path
  --log-file PATH              JSONL decision log path
  --monitor-interval SECONDS   flow/port stats interval (default: $MONITOR_INTERVAL)
  --block-seconds SECONDS      abuse block timeout (default: $BLOCK_SECONDS)
  --health-interval SECONDS    backend health probe interval (default: $HEALTH_INTERVAL)
  --health-failures COUNT      failed probes before failover (default: $HEALTH_FAILURES)
  --qos-backend MODE           auto|meter|queue|priority (default: $QOS_BACKEND)
  --exam-rate-mbps MBPS        exam meter rate (default: $EXAM_RATE)
  --background-rate-mbps MBPS  background meter rate (default: $BG_RATE)
  -h, --help                   show this help

Environment variables with the EXAM_SDN_ prefix can also be used.  Start the
controller before the topology; it retries reading run/topology.json.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) MODE="${2:?--mode needs a value}"; shift 2 ;;
        --of-port) OF_PORT="${2:?--of-port needs a value}"; shift 2 ;;
        --rest-host) REST_HOST="${2:?--rest-host needs a value}"; shift 2 ;;
        --rest-port) REST_PORT="${2:?--rest-port needs a value}"; shift 2 ;;
        --metadata) METADATA="${2:?--metadata needs a value}"; shift 2 ;;
        --log-file) LOG_FILE="${2:?--log-file needs a value}"; shift 2 ;;
        --monitor-interval) MONITOR_INTERVAL="${2:?--monitor-interval needs a value}"; shift 2 ;;
        --block-seconds) BLOCK_SECONDS="${2:?--block-seconds needs a value}"; shift 2 ;;
        --health-interval) HEALTH_INTERVAL="${2:?--health-interval needs a value}"; shift 2 ;;
        --health-failures) HEALTH_FAILURES="${2:?--health-failures needs a value}"; shift 2 ;;
        --qos-backend) QOS_BACKEND="${2:?--qos-backend needs a value}"; shift 2 ;;
        --exam-rate-mbps) EXAM_RATE="${2:?--exam-rate-mbps needs a value}"; shift 2 ;;
        --background-rate-mbps) BG_RATE="${2:?--background-rate-mbps needs a value}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'run_controller: unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$MODE" in
    static|sdn) ;;
    *) printf 'run_controller: --mode must be static or sdn\n' >&2; exit 2 ;;
esac
case "$QOS_BACKEND" in
    auto|meter|queue|priority) ;;
    *) printf 'run_controller: --qos-backend must be auto, meter, queue or priority\n' >&2; exit 2 ;;
esac

if [[ ! -x "$MANAGER" ]]; then
    printf 'run_controller: missing %s\n' "$MANAGER" >&2
    printf 'Create the runtime with:\n' >&2
    printf '  python3 -m venv %s\n' "$VENV" >&2
    printf '  %s/bin/pip install -r %s/sdn/requirements.txt\n' "$VENV" "$ROOT" >&2
    exit 1
fi

mkdir -p "$RUN_DIR" "$(dirname "$LOG_FILE")" "$(dirname "$METADATA")"

printf 'Controller: mode=%s openflow=%s rest=%s:%s\n' \
    "$MODE" "$OF_PORT" "$REST_HOST" "$REST_PORT"
printf 'Metadata:   %s\n' "$METADATA"
printf 'Decision log: %s\n' "$LOG_FILE"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONNOUSERSITE=1
export EVENTLET_NO_GREENDNS=yes
export EXAM_SDN_MODE="$MODE"
export EXAM_SDN_REST_HOST="$REST_HOST"
export EXAM_SDN_REST_PORT="$REST_PORT"
export EXAM_SDN_METADATA="$METADATA"
export EXAM_SDN_LOG="$LOG_FILE"
export EXAM_SDN_MONITOR_INTERVAL="$MONITOR_INTERVAL"
export EXAM_SDN_BLOCK_SECONDS="$BLOCK_SECONDS"
export EXAM_SDN_HEALTH_INTERVAL="$HEALTH_INTERVAL"
export EXAM_SDN_HEALTH_FAILURES="$HEALTH_FAILURES"
export EXAM_SDN_QOS_BACKEND="$QOS_BACKEND"
export EXAM_SDN_EXAM_RATE_MBPS="$EXAM_RATE"
export EXAM_SDN_BACKGROUND_RATE_MBPS="$BG_RATE"

# os-ken uses a module path, not a file path.  The controller reads its own
# settings from the environment above.
exec "$MANAGER" --ofp-tcp-listen-port "$OF_PORT" sdn.controller.exam_controller
