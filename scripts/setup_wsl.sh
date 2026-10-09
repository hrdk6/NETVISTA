#!/usr/bin/env bash
# One-time setup of the Linux side of NETVISTA (WSL2 Ubuntu 24.04 or any Ubuntu 22.04+ box).
# Installs Mininet, Open vSwitch, iperf3 and a Python venv with the backend dependencies.
# Must run as root:  sudo bash scripts/setup_wsl.sh   (or: wsl -d Ubuntu-24.04 -u root -- bash scripts/setup_wsl.sh)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${NETVISTA_VENV:-/opt/netvista/venv}"

if [ "$(id -u)" -ne 0 ]; then
  echo "setup_wsl.sh must run as root (it installs packages and Mininet needs root)." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
echo "==> Installing system packages (mininet, openvswitch, iperf3, ...)"
apt-get update -y
apt-get install -y --no-install-recommends \
  mininet openvswitch-switch iperf3 iproute2 iputils-ping traceroute \
  ethtool net-tools tcpdump psmisc procps bridge-utils \
  python3 python3-venv python3-pip

echo "==> Loading kernel modules"
modprobe openvswitch
modprobe sch_netem
modprobe sch_htb

echo "==> Starting Open vSwitch"
service openvswitch-switch start || /usr/share/openvswitch/scripts/ovs-ctl start

echo "==> Creating Python venv at $VENV (with system site-packages so 'import mininet' works)"
mkdir -p "$(dirname "$VENV")"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -r "$ROOT/backend/requirements.txt"

echo "==> Checking versions"
"$VENV/bin/python" -c "import mininet.net, fastapi, simpy, networkx; print('mininet OK, fastapi', fastapi.__version__, 'simpy', simpy.__version__, 'networkx', networkx.__version__)"
ovs-vsctl --version | head -1
iperf3 --version | head -1
echo "==> Setup complete."
