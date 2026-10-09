#!/usr/bin/env bash
# Reproduce the WSL2 packet-path stall with no NETVISTA code (DESIGN.md section 11.10, finding 6).
#
#   sudo bash scripts/diagnostics/netns_stall.sh [LOAD=1|0] [MODE=htb|netem|none] [ARP=dynamic|static|fast]
#
# Two network namespaces joined by one veth, optionally shaped like a NETVISTA link (HTB + netem
# 5 ms), optionally loaded with a 6 Mbit/s UDP stream, and a 20 Hz ping for 100 s. Prints every
# gap of more than 200 ms between echo replies, the largest RTT, and the neighbour-cache events.
# Stop NETVISTA first (the namespace names do not clash, but the measurement should be quiet).
set -u
LOAD=${1:-1}
MODE=${2:-htb}
ARP=${3:-dynamic}
LOG=$(mktemp -d)
ip netns del nva 2>/dev/null; ip netns del nvb 2>/dev/null
ip netns add nva; ip netns add nvb
ip link add va type veth peer name vb
ip link set va netns nva; ip link set vb netns nvb
ip -n nva addr add 10.99.0.1/24 dev va; ip -n nvb addr add 10.99.0.2/24 dev vb
ip -n nva link set va up; ip -n nvb link set vb up; ip -n nva link set lo up; ip -n nvb link set lo up
if [ "$ARP" = static ]; then
  ma=$(ip -n nva -o link show va | grep -o 'link/ether [0-9a-f:]*' | cut -d' ' -f2)
  mb=$(ip -n nvb -o link show vb | grep -o 'link/ether [0-9a-f:]*' | cut -d' ' -f2)
  ip -n nva neigh replace 10.99.0.2 lladdr "$mb" dev va nud permanent
  ip -n nvb neigh replace 10.99.0.1 lladdr "$ma" dev vb nud permanent
elif [ "$ARP" = fast ]; then
  ip netns exec nva sysctl -qw net.ipv4.neigh.va.retrans_time_ms=200
  ip netns exec nvb sysctl -qw net.ipv4.neigh.vb.retrans_time_ms=200
fi
for ns in nva nvb; do
  dev=$([ $ns = nva ] && echo va || echo vb)
  case "$MODE" in
    netem) ip netns exec $ns tc qdisc replace dev $dev root netem limit 1000 delay 5ms ;;
    htb)
      ip netns exec $ns tc qdisc replace dev $dev root handle 1: htb default 1
      ip netns exec $ns tc class replace dev $dev parent 1: classid 1:1 htb rate 50mbit ceil 50mbit quantum 1514
      ip netns exec $ns tc qdisc replace dev $dev parent 1:1 handle 10: netem limit 1000 delay 5ms ;;
  esac
done
ip -n nva -ts monitor neigh > "$LOG/neigh.txt" 2>&1 &
MON=$!
if [ "$LOAD" = 1 ]; then
  ip netns exec nvb iperf3 -s -p 5999 -D
  sleep 0.5
  ip netns exec nva iperf3 -c 10.99.0.2 -u -b 6M -l 1200 -t 110 -p 5999 > /dev/null 2>&1 &
fi
echo "load=$LOAD qdisc=$MODE arp=$ARP: pinging for 100 s ..."
ip netns exec nva timeout 100 ping -O -D -i 0.05 -W 2 10.99.0.2 > "$LOG/ping.txt" 2>&1
python3 - "$LOG/ping.txt" <<'EOF'
import datetime, re, sys
ts, unanswered = [], 0
for line in open(sys.argv[1]):
    unanswered += "no answer" in line
    m = re.match(r"\[(\d+\.\d+)\].*icmp_seq=(\d+).*time=([\d.]+)", line)
    if m:
        ts.append((float(m.group(1)), int(m.group(2)), float(m.group(3))))
print(f"replies {len(ts)}, unanswered {unanswered}, max RTT {max(t[2] for t in ts) if ts else None} ms")
for a, b in zip(ts, ts[1:]):
    if b[0] - a[0] > 0.2:
        at = datetime.datetime.fromtimestamp(a[0]).strftime("%H:%M:%S.%f")[:-3]
        print(f"  gap of {b[0] - a[0]:.3f} s after {at}")
EOF
kill $MON 2>/dev/null
echo "neighbour-cache events on nva:"; grep -v '^$' "$LOG/neigh.txt" | sed 's/ lladdr [0-9a-f:]*//'
pkill -f "iperf3 -s -p 5999" 2>/dev/null
ip netns del nva; ip netns del nvb
rm -rf "$LOG"
