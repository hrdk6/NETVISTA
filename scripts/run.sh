#!/usr/bin/env bash
# Start NETVISTA (Linux side). Needs root because Mininet creates namespaces/veths/qdiscs.
#   WSL:    wsl -d Ubuntu-24.04 -u root -- bash /mnt/c/dev/NETVISTA/scripts/run.sh
#   Linux:  sudo bash scripts/run.sh [--topology topologies/small.json] [--port 8000]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${NETVISTA_VENV:-/opt/netvista/venv}"

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E bash "$0" "$@"
fi

if [ ! -x "$VENV/bin/python" ] || ! command -v mn >/dev/null 2>&1; then
  echo "==> First run: installing dependencies"
  bash "$ROOT/scripts/setup_wsl.sh"
fi

modprobe openvswitch 2>/dev/null || true
modprobe sch_netem 2>/dev/null || true
modprobe sch_htb 2>/dev/null || true
service openvswitch-switch start >/dev/null 2>&1 || /usr/share/openvswitch/scripts/ovs-ctl start >/dev/null

echo "==> Cleaning up any previous run"
pkill -f netvista-agent 2>/dev/null || true
pkill -x iperf3 2>/dev/null || true
mn -c >/dev/null 2>&1 || true

if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  echo "!! frontend/dist not found - the API will run, but build the UI first (scripts/run.ps1 does this on Windows)."
fi

cd "$ROOT/backend"
echo "==> NETVISTA on http://localhost:${NETVISTA_PORT:-8000}  (Ctrl+C to stop)"
exec "$VENV/bin/python" -m netvista --port "${NETVISTA_PORT:-8000}" "$@"
