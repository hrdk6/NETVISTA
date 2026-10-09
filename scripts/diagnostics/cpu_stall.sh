#!/usr/bin/env bash
# Control for netns_stall.sh: is it the CPU / VM that freezes? (DESIGN.md section 11.10, finding 6)
#
#   bash scripts/diagnostics/cpu_stall.sh [SECONDS=70]
#
# One busy timing loop pinned to every vCPU, plus one process sleeping in 10 ms steps; each prints
# every gap of more than 100 ms in its own progress. On the development machine all were empty
# while the namespaced packet path stalled every ~33 s.
DUR=${1:-70}
LOG=$(mktemp -d)
for c in $(seq 0 $(($(nproc) - 1))); do
  taskset -c "$c" python3 - "$c" "$DUR" > "$LOG/cpu_$c.txt" <<'EOF' &
import sys, time
end = time.monotonic() + float(sys.argv[2]); last = time.monotonic(); gaps = []
while last < end:
    now = time.monotonic()
    if now - last > 0.1:
        gaps.append(round(now - last, 3))
    last = now
print(f"cpu {sys.argv[1]:>2}: gaps > 100 ms {gaps}")
EOF
done
python3 - "$DUR" > "$LOG/sleeper.txt" <<'EOF' &
import sys, time
end = time.monotonic() + float(sys.argv[1]); last = time.monotonic(); gaps = []
while last < end:
    time.sleep(0.01)
    now = time.monotonic()
    if now - last > 0.1:
        gaps.append(round(now - last, 3))
    last = now
print(f"sleeper: gaps > 100 ms {gaps}")
EOF
wait
sort -V "$LOG"/cpu_*.txt; cat "$LOG/sleeper.txt"
rm -rf "$LOG"
