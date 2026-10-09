#!/usr/bin/env python3
"""NETVISTA probe agent. Runs INSIDE one node's network namespace. Standard library only.

* UDP echo responder on --port (every node runs one, so any node can be probed).
* Sends one probe per --interval to every target; each probe carries (target, seq, send time).
* An echo returning computes RTT with the agent's own monotonic clock (no clock sync needed).
* A probe with no echo after --timeout is reported as lost.
* Results are streamed to stdout as JSON lines, one line per tick:
      {"t": <wall time>, "s": <last seq>, "r": [[target_idx, seq, rtt_ms], ...], "l": [[target_idx, seq], ...]}
* Exits when stdin closes (i.e. when the NETVISTA backend dies), so no orphans are left behind.
"""

import argparse
import json
import os
import socket
import struct
import sys
import threading
import time

MAGIC = b"NVP1"
HDR = struct.Struct("!4sHIq")  # magic, target index, sequence number, send time (monotonic ns)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=47000)
    ap.add_argument("--interval", type=float, default=0.1)
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--payload", type=int, default=64)
    ap.add_argument("--targets", default="[]", help='JSON list: [{"ip": "10.10.1.2"}, ...]')
    ap.add_argument("--tag", default="netvista-agent")
    args = ap.parse_args()
    targets = json.loads(args.targets)

    out_lock = threading.Lock()

    def emit(obj) -> None:
        line = json.dumps(obj, separators=(",", ":"))
        with out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    # --- echo responder -------------------------------------------------
    echo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    echo.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    echo.bind(("0.0.0.0", args.port))

    def echo_loop() -> None:
        while True:
            try:
                data, addr = echo.recvfrom(4096)
            except OSError:
                continue
            if data[:4] == MAGIC:
                try:
                    echo.sendto(data, addr)
                except OSError:
                    pass

    threading.Thread(target=echo_loop, daemon=True).start()

    # --- die with the parent ----------------------------------------------
    def stdin_watch() -> None:
        try:
            while sys.stdin.read(1):
                pass
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=stdin_watch, daemon=True).start()

    emit({"k": "hello", "pid": os.getpid(), "targets": len(targets)})
    if not targets:
        while True:
            time.sleep(3600)

    # --- prober -------------------------------------------------------------
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    pending: dict = {}
    plock = threading.Lock()
    replies: list = []
    rlock = threading.Lock()

    def recv_loop() -> None:
        while True:
            try:
                data, _ = sock.recvfrom(4096)
            except OSError:
                continue
            now = time.monotonic_ns()
            if len(data) < HDR.size or data[:4] != MAGIC:
                continue
            _, idx, seq, sent = HDR.unpack_from(data)
            with plock:
                if pending.pop((idx, seq), None) is None:
                    continue  # late echo of a probe already declared lost, or a duplicate
            with rlock:
                replies.append([idx, seq, round((now - sent) / 1e6, 4)])

    threading.Thread(target=recv_loop, daemon=True).start()

    pad = b"\0" * max(0, args.payload - HDR.size)
    addrs = [(t["ip"], args.port) for t in targets]
    timeout_ns = int(args.timeout * 1e9)
    seq = 0
    next_t = time.monotonic()
    last_emit = 0.0
    while True:
        seq += 1
        now = time.monotonic_ns()
        for idx, addr in enumerate(addrs):
            pkt = HDR.pack(MAGIC, idx, seq, now) + pad
            with plock:
                pending[(idx, seq)] = now
            try:
                sock.sendto(pkt, addr)
            except OSError:
                pass  # e.g. ENETUNREACH while a link is down: it will time out and count as lost
        cutoff = now - timeout_ns
        with plock:
            expired = [k for k, v in pending.items() if v < cutoff]
            for k in expired:
                del pending[k]
        with rlock:
            got = replies[:]
            replies.clear()
        wall = time.time()
        if got or expired or wall - last_emit > 1.0:
            emit({"t": wall, "s": seq, "r": got, "l": [list(k) for k in expired]})
            last_emit = wall
        next_t += args.interval
        delay = next_t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.monotonic()  # fell behind (e.g. CPU starved); resync instead of bursting


if __name__ == "__main__":
    main()
