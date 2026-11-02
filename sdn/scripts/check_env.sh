#!/usr/bin/env bash
#
# check_env.sh - preflight verification for the Mininet + Open vSwitch + os-ken
# SDN demo on this VM.
#
# This script is non-destructive: it installs nothing and modifies no
# repository files.  The only system change is a uniquely named, temporary OVS
# bridge used for the meter test; it is removed after the test and by an EXIT
# trap (so Ctrl-C / early exits also clean up).

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"

PASSES=0
FAILS=0
WARNS=0

BRIDGE=""
BRIDGE_CREATED=0
METER_SUPPORT="not tested"

pass()    { printf '[PASS] %s\n' "$*"; PASSES=$((PASSES + 1)); }
fail()    { printf '[FAIL] %s\n' "$*"; FAILS=$((FAILS + 1)); }
warn()    { printf '[WARN] %s\n' "$*"; WARNS=$((WARNS + 1)); }
info()    { printf '[INFO] %s\n' "$*"; }
section() { printf '\n== %s ==\n' "$*"; }

have() { command -v -- "$1" >/dev/null 2>&1; }

print_version() {
  local label=$1
  shift
  local out=""
  local rc=0
  out=$("$@" 2>&1) || rc=$?
  out=${out%%$'\n'*}
  if [[ -n $out ]]; then
    printf '       %-12s %s\n' "${label}:" "$out"
  else
    printf '       %-12s (no output, exit %d)\n' "${label}:" "$rc"
  fi
}

ovs_vsctl_q() {
  if (( EUID == 0 )); then
    ovs-vsctl "$@"
  else
    sudo -n ovs-vsctl "$@"
  fi
}

ovs_ofctl_q() {
  if (( EUID == 0 )); then
    ovs-ofctl "$@"
  else
    sudo -n ovs-ofctl "$@"
  fi
}

remove_bridge() {
  if (( BRIDGE_CREATED == 1 )) && [[ -n $BRIDGE ]]; then
    if ovs_vsctl_q --if-exists del-br "$BRIDGE" >/dev/null 2>&1; then
      BRIDGE_CREATED=0
      info "Cleaned up temporary OVS bridge $BRIDGE"
    else
      printf '[WARN] Could not remove temporary OVS bridge %s\n' "$BRIDGE"
    fi
  fi
}

cleanup() {
  remove_bridge
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------
# 1. OS, kernel, Python
# ---------------------------------------------------------------------------
section "1. OS, kernel, and Python"

OS_PRETTY="unknown"
if [[ -r /etc/os-release ]]; then
  OS_PRETTY=$(
    # shellcheck disable=SC1091
    . /etc/os-release
    printf '%s' "${PRETTY_NAME:-unknown}"
  )
  pass "OS: $OS_PRETTY"
else
  warn "/etc/os-release is not readable"
fi

KERNEL="$(uname -r)"
pass "Kernel: $KERNEL"

PY_VERSION="missing"
if have python3; then
  PY_VERSION="$(python3 --version 2>&1)"
  PY_NUM="$(python3 -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])')"
  pass "Python: $PY_VERSION ($(command -v -- python3))"
  if (( PY_NUM < 312 )); then
    warn "$PY_VERSION is older than 3.12; os-ken/eventlet may misbehave"
  elif (( PY_NUM >= 315 )); then
    warn "$PY_VERSION is 3.15 or newer; os-ken/eventlet support may lag"
  fi
else
  fail "python3 was not found in PATH"
fi

# ---------------------------------------------------------------------------
# 2. Repository layout
# ---------------------------------------------------------------------------
section "2. Repository layout"

info "Repo root: $ROOT"
for required in \
  "$ROOT/sdn/topology.py" \
  "$ROOT/sdn/controller/exam_controller.py"
do
  if [[ -f $required ]]; then
    pass "Found $required"
  else
    warn "Missing $required (expected once the topology/controller are added)"
  fi
done

# ---------------------------------------------------------------------------
# 3. SDN virtual environment (os-ken + eventlet)
# ---------------------------------------------------------------------------
section "3. SDN virtual environment (os-ken + eventlet)"

VENV="$ROOT/sdn/.venv"
VENV_PY="$VENV/bin/python"
OSKEN_VER="unknown"
EVENTLET_VER="unknown"

if [[ -x $VENV_PY ]]; then
  VENV_PY_VERSION=$("$VENV_PY" --version 2>&1) || VENV_PY_VERSION="unknown"
  pass "Venv Python: $VENV_PY_VERSION ($VENV_PY)"

  VENV_RC=0
  VENV_OUT=$(
    EVENTLET_NO_GREENDNS=yes PYTHONNOUSERSITE=1 "$VENV_PY" -c \
      'import os_ken, eventlet; print("OSKEN=%s" % os_ken.__version__); print("EVENTLET=%s" % eventlet.__version__)' \
      2>&1
  ) || VENV_RC=$?

  if (( VENV_RC == 0 )); then
    OSKEN_VER=$(printf '%s\n' "$VENV_OUT" | sed -n 's/^OSKEN=//p')
    EVENTLET_VER=$(printf '%s\n' "$VENV_OUT" | sed -n 's/^EVENTLET=//p')
    pass "os-ken version: $OSKEN_VER"
    pass "eventlet version: $EVENTLET_VER"

    if [[ $OSKEN_VER != "3.1.1" ]]; then
      fail "os-ken is $OSKEN_VER; expected exactly 3.1.1"
    fi
    if [[ $EVENTLET_VER != "0.41.3" ]]; then
      fail "eventlet is $EVENTLET_VER; expected exactly 0.41.3"
    fi
  else
    fail "os_ken/eventlet import failed (exit $VENV_RC): ${VENV_OUT//$'\n'/ }"
  fi
else
  fail "Venv Python not found: $VENV_PY (create $VENV with os-ken 3.1.1 + eventlet 0.41.3)"
fi

if [[ -x "$VENV/bin/osken-manager" ]]; then
  pass "osken-manager: $VENV/bin/osken-manager"
else
  fail "osken-manager not found or not executable: $VENV/bin/osken-manager"
fi

# ---------------------------------------------------------------------------
# 4. Required commands
# ---------------------------------------------------------------------------
section "4. Required commands"

for cmd in mn ovs-vsctl ovs-ofctl iperf3 tc ip sqlite3 curl; do
  if have "$cmd"; then
    pass "Command: $cmd ($(command -v -- "$cmd"))"
  else
    fail "Required command not found: $cmd"
  fi
done

for cmd in iperf jq; do
  if have "$cmd"; then
    pass "Optional command: $cmd ($(command -v -- "$cmd"))"
  else
    warn "Optional command not found: $cmd"
  fi
done

MININET_VER="not found"
if have mn; then
  MININET_VER=$(mn --version 2>&1) || MININET_VER="unknown"
  MININET_VER=${MININET_VER%%$'\n'*}
fi

OVS_VER="not found"
if have ovs-vsctl; then
  OVS_VER=$(ovs-vsctl --version 2>&1) || OVS_VER="unknown"
  OVS_VER=${OVS_VER%%$'\n'*}
fi

printf '       Command versions:\n'
if have mn;        then print_version "mn"         mn --version;        fi
if have ovs-vsctl; then print_version "ovs-vsctl"  ovs-vsctl --version; fi
if have ovs-ofctl; then print_version "ovs-ofctl"  ovs-ofctl --version; fi
if have iperf;     then print_version "iperf"      iperf --version;     fi
if have iperf3;    then print_version "iperf3"     iperf3 --version;    fi
if have tc;        then print_version "tc"         tc -V;               fi
if have ip;        then print_version "ip"         ip -V;               fi
if have jq;        then print_version "jq"         jq --version;        fi
if have sqlite3;   then print_version "sqlite3"    sqlite3 --version;   fi
if have curl;      then print_version "curl"       curl --version;      fi

# ---------------------------------------------------------------------------
# 5. C build dependencies and demo binaries
# ---------------------------------------------------------------------------
section "5. C build dependencies and demo binaries"

if have pkg-config; then
  for mod in sqlite3 libsodium libcjson; do
    if mod_ver=$(pkg-config --modversion "$mod" 2>/dev/null); then
      pass "pkg-config $mod: $mod_ver"
    else
      fail "pkg-config module missing: $mod"
    fi
  done
else
  fail "pkg-config not found; cannot verify sqlite3, libsodium, libcjson"
fi

EXAM_DIR="$ROOT/online_exam_mvp"
if [[ -f "$EXAM_DIR/Makefile" ]]; then
  pass "Found $EXAM_DIR/Makefile"
else
  warn "Missing $EXAM_DIR/Makefile"
fi

for bin in exam_server create_user; do
  if [[ -f "$EXAM_DIR/$bin" ]]; then
    if [[ -x "$EXAM_DIR/$bin" ]]; then
      pass "Built binary: $EXAM_DIR/$bin"
    else
      warn "Binary exists but is not executable: $EXAM_DIR/$bin"
    fi
  else
    warn "Not built: $EXAM_DIR/$bin (build with: make -C \"$EXAM_DIR\")"
  fi
done

# ---------------------------------------------------------------------------
# 6. OVS kernel module
# ---------------------------------------------------------------------------
section "6. OVS kernel module"

MODULE_LOADED=0
if [[ -d /sys/module/openvswitch ]]; then
  MODULE_LOADED=1
elif have lsmod && lsmod 2>/dev/null | awk '$1 == "openvswitch" { found = 1 } END { exit(found ? 0 : 1) }'; then
  MODULE_LOADED=1
fi

if (( MODULE_LOADED == 1 )); then
  pass "openvswitch kernel module is loaded"
else
  fail "openvswitch kernel module is not loaded. Fix with: sudo modprobe openvswitch; sudo systemctl enable --now openvswitch-switch"
fi

# ---------------------------------------------------------------------------
# 7. OVS service and database access
# ---------------------------------------------------------------------------
section "7. OVS service and database"

if have systemctl; then
  SVC_STATE=$(systemctl is-active openvswitch-switch 2>/dev/null || true)
  case $SVC_STATE in
    active)
      pass "openvswitch-switch service: active"
      ;;
    "")
      warn "openvswitch-switch service state is unknown"
      ;;
    *)
      warn "openvswitch-switch service: $SVC_STATE (expected active)"
      ;;
  esac
else
  warn "systemctl not found; cannot report openvswitch-switch service state"
fi

OVS_ACCESSIBLE=0
OVS_SHOW=""
if ! have ovs-vsctl; then
  warn "Skipping OVS DB check because ovs-vsctl is not installed"
elif (( EUID == 0 )); then
  if OVS_SHOW=$(ovs-vsctl show 2>&1); then
    OVS_ACCESSIBLE=1
    pass "OVS DB is accessible as root"
  else
    fail "ovs-vsctl show failed: ${OVS_SHOW//$'\n'/ }"
  fi
elif ! have sudo; then
  warn "sudo not found; skipping live OVS DB and meter test"
else
  OVS_SHOW_RC=0
  OVS_SHOW=$(sudo -n ovs-vsctl show 2>&1) || OVS_SHOW_RC=$?
  if (( OVS_SHOW_RC == 0 )); then
    OVS_ACCESSIBLE=1
    pass "OVS DB is accessible via passwordless sudo"
  else
    warn "OVS DB is not accessible without a sudo password; skipping live OVS/meter test"
    info "sudo reported: ${OVS_SHOW//$'\n'/ }"
    info "Run this script with sudo for the full check, or configure passwordless sudo for ovs-vsctl/ovs-ofctl"
  fi
fi

if (( OVS_ACCESSIBLE == 1 )); then
  info "ovs-vsctl show:"
  if [[ -n $OVS_SHOW ]]; then
    printf '%s\n' "$OVS_SHOW" | sed 's/^/       /'
  else
    printf '       (no bridges configured)\n'
  fi
fi

# ---------------------------------------------------------------------------
# 8. OpenFlow 13 meter support (temporary bridge)
# ---------------------------------------------------------------------------
section "8. OpenFlow 13 meter support (temporary bridge)"

if (( OVS_ACCESSIBLE == 0 )); then
  warn "Meter test skipped because the OVS DB is not accessible (no privilege)"
  METER_SUPPORT="not tested (OVS DB inaccessible)"
else
  BRIDGE="sdn-check-$$"
  PROTO_OK=0

  if BR_ERR=$(ovs_vsctl_q add-br "$BRIDGE" 2>&1); then
    BRIDGE_CREATED=1
    pass "Created temporary OVS bridge: $BRIDGE"
  else
    fail "Could not create temporary OVS bridge $BRIDGE: ${BR_ERR:-unknown error}"
    METER_SUPPORT="no"
  fi

  if (( BRIDGE_CREATED == 1 )); then
    if PROTO_ERR=$(ovs_vsctl_q set bridge "$BRIDGE" protocols=OpenFlow13 2>&1); then
      PROTO_OK=1
      pass "Set $BRIDGE to OpenFlow13"
    else
      fail "Could not set OpenFlow13 on $BRIDGE: ${PROTO_ERR:-unknown error}; controller will use HTB/QoS fallback"
      METER_SUPPORT="no"
    fi
  fi

  if (( BRIDGE_CREATED == 1 && PROTO_OK == 1 )); then
    METER_ADDED=0
    METER_ERR=""
    if METER_ERR=$(ovs_ofctl_q -O OpenFlow13 add-meter "$BRIDGE" \
      'meter=1,kbps,burst,band=type=drop,rate=1000,burst=1000' 2>&1); then
      METER_ADDED=1
      pass "OpenFlow13 meter add succeeded"
    else
      fail "add-meter failed on $BRIDGE: ${METER_ERR:-unknown error}; controller will use HTB/QoS fallback"
      METER_SUPPORT="no"
    fi

    DUMP_RC=0
    DUMP_OUT=$(ovs_ofctl_q -O OpenFlow13 dump-meters "$BRIDGE" 2>&1) || DUMP_RC=$?
    info "ovs-ofctl -O OpenFlow13 dump-meters $BRIDGE:"
    if [[ -n $DUMP_OUT ]]; then
      printf '%s\n' "$DUMP_OUT" | sed 's/^/       /'
    else
      printf '       (no output)\n'
    fi

    if (( DUMP_RC != 0 )); then
      fail "dump-meters failed (exit $DUMP_RC): ${DUMP_OUT//$'\n'/ }"
      METER_SUPPORT="no"
    elif (( METER_ADDED == 1 )); then
      if grep -Eq 'meter[:=]1([^0-9]|$)' <<< "$DUMP_OUT"; then
        pass "Meter 1 is present in dump-meters"
        METER_SUPPORT="yes"
      else
        fail "Meter 1 did not appear in dump-meters; controller will use HTB/QoS fallback"
        METER_SUPPORT="no"
      fi
    fi
  fi

  remove_bridge
fi

# ---------------------------------------------------------------------------
# 9. Mininet Python API
# ---------------------------------------------------------------------------
section "9. Mininet Python API"

MININET_RC=0
MININET_OUT=$(python3 -c \
  'from mininet.net import Mininet; from mininet.node import OVSSwitch, RemoteController; from mininet.link import TCLink; print("mininet OK")' \
  2>&1) || MININET_RC=$?

if (( MININET_RC == 0 )); then
  pass "Mininet import: $MININET_OUT"
else
  fail "Mininet import failed (exit $MININET_RC): ${MININET_OUT//$'\n'/ }"
fi

ROOTNS_UTIL="absent"
if python3 -c 'from mininet.util import connectToRootNS' >/dev/null 2>&1; then
  ROOTNS_UTIL="present"
fi

ROOTNS_EXAMPLE="absent"
if python3 -c 'from mininet.examples.sshd import connectToRootNS' >/dev/null 2>&1; then
  ROOTNS_EXAMPLE="present"
fi

if [[ $ROOTNS_UTIL == "present" ]]; then
  info "mininet.util.connectToRootNS is present"
else
  info "mininet.util.connectToRootNS is absent (expected for Mininet 2.3.0; not a failure)"
fi

if [[ $ROOTNS_EXAMPLE == "present" ]]; then
  pass "mininet.examples.sshd.connectToRootNS is available"
else
  warn "mininet.examples.sshd.connectToRootNS is not available"
fi

# ---------------------------------------------------------------------------
# 10. Summary
# ---------------------------------------------------------------------------
section "10. Summary"

printf '  %-20s %s\n' "Repo root:"    "$ROOT"
printf '  %-20s %s\n' "OS:"           "$OS_PRETTY"
printf '  %-20s %s\n' "Kernel:"       "$KERNEL"
printf '  %-20s %s\n' "Python:"       "$PY_VERSION"
printf '  %-20s %s\n' "Mininet:"      "$MININET_VER"
printf '  %-20s %s\n' "Open vSwitch:" "$OVS_VER"
printf '  %-20s %s\n' "os-ken:"       "$OSKEN_VER"
printf '  %-20s %s\n' "eventlet:"     "$EVENTLET_VER"
printf '  %-20s %s\n' "Meter support:" "$METER_SUPPORT"
printf '  %-20s %s\n' "Checks:"       "$PASSES passed, $WARNS warnings, $FAILS failures"

printf '\n  Next commands:\n'
printf '    %q\n' "$ROOT/sdn/scripts/run_controller.sh"
printf '    sudo %q --students 10\n' "$ROOT/sdn/scripts/run_topology.sh"

if (( FAILS > 0 )); then
  printf '\n[FAIL] %d hard failure(s) detected; fix them before running the demo.\n' "$FAILS"
  exit 1
fi

printf '\n[PASS] Environment looks ready (%d checks passed, %d warning(s)).\n' "$PASSES" "$WARNS"
exit 0
