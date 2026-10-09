#!/usr/bin/env bash
# Stop a running NETVISTA backend and clean up Mininet leftovers (run as root).
pkill -INT -f "python -m netvista" 2>/dev/null || true
for _ in $(seq 1 40); do
  pgrep -f "python -m netvista" >/dev/null || break
  sleep 0.25
done
pkill -9 -f "python -m netvista" 2>/dev/null || true
pkill -f netvista-agent 2>/dev/null || true
pkill -x iperf3 2>/dev/null || true
mn -c >/dev/null 2>&1 || true
echo "NETVISTA stopped."
