#!/usr/bin/env python3
"""Check the digital twin against the live network (stdlib only; backend must be running).

    python3 scripts/twin_check.py [--suite]

1. waits for the automatic calibration and prints the fitted overheads
2. predicts the current state (no change) and compares it with live measurements
3. runs one validation (default: +40 ms latency) or the whole suite with --suite
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from live_check import get, post  # noqa: E402
import live_check  # noqa: E402


def fmt(x, nd=2):
    return "  n/a " if x is None else f"{x:7.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", action="store_true")
    ap.add_argument("--url", default="http://localhost:8000")
    a = ap.parse_args()
    live_check.URL = a.url
    live_check.wait_health()

    print("1. Calibration")
    for _ in range(40):
        cal = get("/api/sim/calibration")
        if cal.get("t"):
            break
        time.sleep(1)
    else:
        cal = post("/api/sim/calibrate")
    print(f"   endpoint E = {cal['endpoint_ms']} ms, per-hop H = {cal['per_hop_ms']} ms, fit RMS = {cal['fit_rms_ms']} ms, "
          f"{cal['n_streams']} streams, noise p5..p95 = {cal['noise_p05_ms']}..{cal['noise_p95_ms']} ms, calm={cal['conditions']['calm']}")

    post("/api/chaos/revert_all")
    post("/api/traffic/start")
    time.sleep(10)

    print("2. Twin of the current state vs live (with traffic)")
    pred = post("/api/sim/predict", {"changes": [], "duration_s": 10, "include_baseline": False})
    print(f"   simulated in {pred['wall_s']:.1f} s")
    print(f"   {'pair':10s} {'metric':8s} {'twin':>8s} {'live':>8s}")
    for pair, p in pred["result"]["pairs"].items():
        live = pred["live"]["pairs"][pair]
        for m in ("rtt_p50", "rtt_p95", "rx_mbps", "loss_pct"):
            if p.get(m) is None and live.get(m) is None:
                continue
            print(f"   {pair:10s} {m:8s} {fmt(p.get(m))} {fmt(live.get(m))}")

    print("3. Validation" + (" suite" if a.suite else ": +40 ms on r2-r5"))
    if a.suite:
        st = post("/api/validation/suite")
    else:
        st = post("/api/validation/run", {"changes": [{"kind": "link_latency", "target": "r2-r5", "params": {"add_ms": 40}}], "measure_s": 12})
    n_before = len(get("/api/validation/runs"))
    while get("/api/validation/status").get("running"):
        s = get("/api/validation/status")
        print(f"   ... {s.get('index', 0) + 1}/{s.get('total')} {s.get('label')}: {s.get('step')}", flush=True)
        time.sleep(5)
    runs = get("/api/validation/runs")
    for run in reversed(runs[: len(runs) - n_before + st.get("total", 1)]):
        s = run["summary"]
        print(f"   {run['label']:38s} latency err {fmt(s['latency_mape'], 1)}%  thr err {fmt(s['throughput_mape'], 1)}%  "
              f"loss err {fmt(s['loss_mae_pp'], 2)} pp  paths {fmt(s['path_match_pct'], 0)}%")
        for r in run["rows"]:
            if r["metric"] in ("rtt_p50", "rtt_p95", "rx_mbps", "loss_pct", "path") and r["pair"] in ("c1>srv1", "c2>srv2"):
                print(f"       {r['pair']:8s} {r['metric']:8s} pred={r['predicted']} meas={r['measured']} err={r['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
