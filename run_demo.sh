#!/usr/bin/env bash
# ==============================================================================
# CN Project - Full Demo Runner
# Runs: build -> db init -> users -> exam server -> seed -> SDN controller -> Mininet -> demo
# ==============================================================================

set -euo pipefail

# ---- Configuration ----
PROJECT_ROOT="/home/shankar-nag/CN_Project"
EXAM_DIR="$PROJECT_ROOT/online_exam_mvp"
SDN_DIR="$PROJECT_ROOT/sdn"
RUN_DIR="$PROJECT_ROOT/run"
DEMO_DIR="$RUN_DIR/demo"

# Defaults
STUDENTS="${STUDENTS:-10}"
BW_MBPS="${BW_MBPS:-10}"
QUESTION_COUNT="${QUESTION_COUNT:-10}"
EXAM_DURATION="${EXAM_DURATION:-120}"
EXAM_TITLE="${EXAM_TITLE:-CN Demo Exam}"
MODE="${MODE:-sdn}"
NO_FLOOD="${NO_FLOOD:-false}"
NO_FAILOVER="${NO_FAILOVER:-false}"

# Ports
EXAM_PORT=8080
OF_PORT=6653
REST_PORT=9090
REST_HOST="0.0.0.0"

# PIDs for cleanup
EXAM_SERVER_PID=""
CONTROLLER_PID=""
MININET_PID=""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*"; }
ok()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn(){ echo -e "${YELLOW}[WARN]${NC} $*"; }
err() { echo -e "${RED}[ERR]${NC} $*"; }

# ---- Cleanup on exit ----
cleanup() {
    log "Cleaning up processes..."
    [[ -n "$EXAM_SERVER_PID" ]] && kill "$EXAM_SERVER_PID" 2>/dev/null || true
    [[ -n "$CONTROLLER_PID" ]] && kill "$CONTROLLER_PID" 2>/dev/null || true
    [[ -n "$MININET_PID" ]] && sudo kill "$MININET_PID" 2>/dev/null || true
    # Clean up any remaining mininet
    sudo mn -c 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---- Step functions ----

step_build() {
    log "=== Step 1: Build C backend ==="
    cd "$EXAM_DIR"
    make clean
    make
    ok "Build complete: exam_server and create_user created"
}

step_db_init() {
    log "=== Step 2: Initialize database ==="
    cd "$EXAM_DIR"
    sqlite3 database/exam.db < database/schema.sql
    ok "Database schema applied"
}

step_create_users() {
    log "=== Step 3: Create admin and student users ==="
    cd "$EXAM_DIR"
    
    # Admin
    read -rsp 'Admin password (min 12 chars): ' ADMIN_PW; echo
    printf '%s\n' "$ADMIN_PW" | ./create_user --password-stdin examadmin admin
    unset ADMIN_PW
    
    # Student
    read -rsp 'Student password (min 12 chars): ' STUDENT_PW; echo
    printf '%s\n' "$STUDENT_PW" | ./create_user --password-stdin student01 student
    unset STUDENT_PW
    
    ok "Users created"
}

step_nginx_setup() {
    log "=== Step 4: Setup Nginx Reverse Proxy ==="
    if ! command -v nginx &>/dev/null; then
        warn "nginx not installed. Install with: sudo apt install nginx"
        return 1
    fi
    
    # Install config
    sudo cp "$EXAM_DIR/config/nginx-exam.conf" /etc/nginx/sites-available/exam
    sudo ln -sf /etc/nginx/sites-available/exam /etc/nginx/sites-enabled/
    
    # Copy frontend to nginx doc root
    sudo cp "$EXAM_DIR/web/index.html" /var/www/html/index.html
    
    # Test and reload
    if sudo nginx -t; then
        sudo systemctl reload nginx
        ok "Nginx configured: frontend at http://localhost, API at http://localhost/backend/"
    else
        err "Nginx config test failed"
        return 1
    fi
}

step_start_exam_server() {
    log "=== Step 5: Start exam server (background) ==="
    cd "$EXAM_DIR"
    ./exam_server --port "$EXAM_PORT" --addr 0.0.0.0 \
        --db database/exam.db \
        --server-id "127.0.0.1:$EXAM_PORT" \
        --controller-url "http://127.0.0.1:$REST_PORT" \
        > "$RUN_DIR/exam_server.log" 2>&1 &
    EXAM_SERVER_PID=$!
    sleep 2
    
    # Verify it's up
    if curl -sf "http://127.0.0.1:$EXAM_PORT/health" >/dev/null; then
        ok "Exam server running on PID $EXAM_SERVER_PID (port $EXAM_PORT)"
    else
        err "Exam server failed to start. Check $RUN_DIR/exam_server.log"
        return 1
    fi
}

step_seed_demo() {
    log "=== Step 5: Seed demo data (admin, students, exam, questions) ==="
    cd "$EXAM_DIR"
    
    python3 tools/seed_demo.py \
        --students "$STUDENTS" \
        --question-count "$QUESTION_COUNT" \
        --exam-title "$EXAM_TITLE" \
        --duration-minutes "$EXAM_DURATION" \
        --users-file users.csv \
        --base-url "http://127.0.0.1:$EXAM_PORT"
    
    ok "Demo data seeded. Credentials saved to users.csv (mode 600)"
    log "Exam ID and user count shown above"
}

step_sdn_venv() {
    log "=== Step 6: Setup SDN Python venv ==="
    cd "$SDN_DIR"
    if [[ ! -x ".venv/bin/osken-manager" ]]; then
        python3 -m venv .venv
        .venv/bin/pip install -r requirements.txt
        ok "SDN venv created and dependencies installed"
    else
        ok "SDN venv already exists"
    fi
}

step_start_controller() {
    log "=== Step 7: Start SDN Controller ==="
    mkdir -p "$RUN_DIR" "$DEMO_DIR"
    
    cd "$PROJECT_ROOT"
    ./sdn/scripts/run_controller.sh \
        --mode "$MODE" \
        --of-port "$OF_PORT" \
        --rest-host "$REST_HOST" \
        --rest-port "$REST_PORT" \
        --log-file "$DEMO_DIR/controller.ndjson" \
        --monitor-interval 2.0 \
        --block-seconds 10.0 \
        --health-interval 2.0 \
        --health-failures 2 \
        > "$DEMO_DIR/controller.log" 2>&1 &
    CONTROLLER_PID=$!
    
    # Wait for controller health
    log "Waiting for controller REST API on port $REST_PORT..."
    for i in {1..30}; do
        if curl -sf "http://127.0.0.1:$REST_PORT/health" >/dev/null 2>&1; then
            ok "Controller running on PID $CONTROLLER_PID (REST: $REST_PORT, OF: $OF_PORT)"
            return 0
        fi
        sleep 0.5
    done
    err "Controller failed to start. Check $DEMO_DIR/controller.log"
    return 1
}

step_start_mininet() {
    log "=== Step 8: Start Mininet Topology ==="
    log "This requires sudo. Mininet will create: $STUDENTS students, 2 servers, VIP, bg1, bg2, atk"
    
    cd "$PROJECT_ROOT"
    sudo -E python3 sdn/topology.py \
        --students "$STUDENTS" \
        --bw "$BW_MBPS" \
        --run-dir "$RUN_DIR" \
        --of-port "$OF_PORT" \
        --controller-ip "10.0.0.254" \
        > "$DEMO_DIR/mininet.log" 2>&1 &
    MININET_PID=$!
    
    sleep 5
    ok "Mininet topology started (PID $MININET_PID). Switches connecting to controller..."
    sleep 10
    ok "Topology should be ready. Check $DEMO_DIR/mininet.log"
}

step_start_backends_in_mininet() {
    log "=== Step 9: Start real exam servers inside Mininet (srv1, srv2) ==="
    log "Run these commands in the Mininet CLI that opened:"
    echo
    echo "  mininet> srv1 $EXAM_DIR/exam_server --addr 0.0.0.0 --port 8080 --db $EXAM_DIR/database/exam.db --server-id 10.0.0.1:8080 --controller-url http://10.0.0.254:9090 &"
    echo "  mininet> srv2 $EXAM_DIR/exam_server --addr 0.0.0.0 --port 8080 --db $EXAM_DIR/database/exam.db --server-id 10.0.0.2:8080 --controller-url http://10.0.0.254:9090 &"
    echo
    warn "PRESS ENTER after starting both servers in Mininet CLI..."
    read -r
    ok "Assuming backends started in Mininet"
}

step_run_demo() {
    log "=== Step 10: Run full automated SDN demo ==="
    cd "$PROJECT_ROOT"
    
    DEMO_ARGS=(
        --students "$STUDENTS"
        --bw "$BW_MBPS"
        --of-port "$OF_PORT"
        --rest-port "$REST_PORT"
        --rest-host "$REST_HOST"
    )
    
    [[ "$NO_FLOOD" == "true" ]] && DEMO_ARGS+=(--no-flood)
    [[ "$NO_FAILOVER" == "true" ]] && DEMO_ARGS+=(--no-failover)
    
    sudo -E python3 sdn/scripts/demo.py "${DEMO_ARGS[@]}"
    ok "Demo complete! Artifacts in $DEMO_DIR"
}

step_test_frontend() {
    log "=== Quick Frontend Test ==="
    log "Testing VIP endpoint (SDN load-balanced)..."
    
    # Test health via VIP
    if curl -sf "http://10.0.0.100:8080/health"; then
        ok "VIP health check passed"
    else
        warn "VIP health check failed - may need backends started in Mininet"
    fi
    
    log "Testing direct backend (srv1)..."
    curl -sf "http://10.0.0.1:8080/health" && ok "srv1 health OK" || warn "srv1 not responding"
    
    log "Testing direct backend (srv2)..."
    curl -sf "http://10.0.0.2:8080/health" && ok "srv2 health OK" || warn "srv2 not responding"
}

# ---- Main menu ----
show_menu() {
    echo
    echo "============================================"
    echo "  CN Project Demo Runner"
    echo "============================================"
    echo "  1) Full automated run (all steps)"
    echo "  2) Build only"
    echo "  3) Database + Users only"
    echo "  4) Nginx setup (frontend + proxy)"
    echo "  5) Exam server only (local, no SDN)"
    echo "  6) SDN Controller only"
    echo "  7) Mininet topology only"
    echo "  8) Full SDN demo (controller + mininet + demo.py)"
    echo "  9) Test frontend (VIP + backends)"
    echo "  10) Seed demo data (requires server running)"
    echo "  q) Quit"
    echo "============================================"
    echo
    echo "Env vars: STUDENTS=$STUDENTS BW_MBPS=$BW_MBPS QUESTION_COUNT=$QUESTION_COUNT"
    echo "          MODE=$MODE NO_FLOOD=$NO_FLOOD NO_FAILOVER=$NO_FAILOVER"
    echo
}

run_full() {
    step_build
    step_db_init
    step_create_users
    step_nginx_setup
    step_start_exam_server
    step_seed_demo
    step_sdn_venv
    step_start_controller
    step_start_mininet
    step_start_backends_in_mininet
    step_run_demo
}

# ---- Entry point ----
main() {
    mkdir -p "$RUN_DIR" "$DEMO_DIR"
    
    if [[ $# -gt 0 ]]; then
        # Non-interactive mode: run specific step
        case "$1" in
            build) step_build ;;
            db) step_db_init ;;
            users) step_create_users ;;
            nginx) step_nginx_setup ;;
            server) step_start_exam_server ;;
            seed) step_seed_demo ;;
            sdn-venv) step_sdn_venv ;;
            controller) step_start_controller ;;
            mininet) step_start_mininet ;;
            backends) step_start_backends_in_mininet ;;
            demo) step_run_demo ;;
            test) step_test_frontend ;;
            full) run_full ;;
            *) err "Unknown step: $1"; exit 1 ;;
        esac
    else
        # Interactive menu
        while true; do
            show_menu
            read -rp "Select option: " choice
            case "$choice" in
                1) run_full ;;
                2) step_build ;;
                3) step_db_init && step_create_users ;;
                4) step_nginx_setup ;;
                5) step_start_exam_server ;;
                6) step_sdn_venv && step_start_controller ;;
                7) step_start_mininet ;;
                8) step_sdn_venv && step_start_controller && step_start_mininet && step_start_backends_in_mininet && step_run_demo ;;
                9) step_test_frontend ;;
                10) step_seed_demo ;;
                q|Q) log "Exiting..."; exit 0 ;;
                *) warn "Invalid option" ;;
            esac
        done
    fi
}

main "$@"