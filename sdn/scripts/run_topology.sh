#!/usr/bin/env bash
# Start the Mininet topology as root.  This wrapper only adds sudo when needed;
# all topology flags are passed through unchanged.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOPOLOGY="$ROOT/sdn/topology.py"

if [[ ! -f "$TOPOLOGY" ]]; then
    printf 'run_topology: missing %s\n' "$TOPOLOGY" >&2
    exit 1
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    exec python3 "$TOPOLOGY" --help
fi

if [[ "$(id -u)" -eq 0 ]]; then
    exec python3 "$TOPOLOGY" "$@"
fi

printf 'Starting Mininet as root (sudo may prompt for a password).\n'
exec sudo -E python3 "$TOPOLOGY" "$@"
