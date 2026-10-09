#!/usr/bin/env python3
"""End-to-end acceptance check against a RUNNING NETVISTA backend (stdlib only).

    python3 scripts/live_check.py [--url http://localhost:8000] [--quick]

It drives the real emulated network through: traffic -> link failure -> router crash ->
latency injection -> recovery, and prints the measured detection / reroute / recovery
times. Exit code 0 = every check passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

URL = "http://localhost:8000"
FAILS: list[str] = []


def get(path: str):
    with urllib.request.urlopen(URL + path, timeout=30) as r:
        return json.load(r)


def post(path: str, body: dict | None = None):
    req = urllib.request.Request(URL + path, data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"POST {path} -> HTTP {e.code}: {e.read().decode()[:300]}")


def check(cond: bool, msg: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def paths() -> dict[str, str]:
    return {p: "-".join(f["path"] or []) for p, f in get("/api/routing")["flows"].items()}


def wait_health(timeout: float = 90) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if get("/api/health")["status"] == "running":
                return
        except Exception:
            pass
        time.sleep(1)
    raise SystemExit("backend did not become ready")


def main() -> int:
    global URL
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    URL = a.url.rstrip("/")
    wait_health()
    post("/api/chaos/revert_all")
    post("/api/routing/mode", {"mode": "adaptive"})
    time.sleep(3)

    print("1. Baseline telemetry")
    s = get("/api/state")
    for lid in ("r1-r2", "r2-r5", "r4-r5"):
        l = s["links"][lid]
        ok = l["rtt_ms"] is not None and abs(l["rtt_ms"] - l["expected_rtt_ms"]) < 2.0
        check(ok, f"{lid}: probe RTT {l['rtt_ms']} ms vs designed {l['expected_rtt_ms']} ms")
    check(all(s["agents"].values()), "all probe agents alive")

    print("2. Real traffic (iperf3)")
    post("/api/traffic/start")
    time.sleep(6)
    s = get("/api/state")
    for pair in ("c1>srv1", "c2>srv2"):
        t = s["flows"][pair]["traffic"]
        check(t is not None and t["rx_mbps"] > 5.0, f"{pair}: iperf3 receiver reports {t and round(t['rx_mbps'], 2)} Mbit/s (offered 6)")
    busiest = max(s["links"].values(), key=lambda l: l["util"])
    check(busiest["util"] > 0.1, f"interface counters show load: busiest link {busiest['id']} at {busiest['util'] * 100:.1f}%")

    print("3. Link failure r2-r5 (adaptive routing)")
    before = paths()
    n0 = len(get("/api/routing")["incidents"])
    post("/api/chaos/inject", {"kind": "link_down", "target": "r2-r5"})
    time.sleep(5)
    after = paths()
    check(all("r2-r5" not in p for p in after.values()), f"no flow uses r2-r5 any more: {after}")
    incs = [i for i in get("/api/routing")["incidents"][n0:] if i["kind"] == "failure"]
    check(bool(incs), "failure incidents recorded")
    for i in incs:
        print(f"        {i['pair']}: detection {i['detection_ms']} ms, reroute {i['reroute_ms']} ms, recovery {i['recovery_ms']} ms")
        check(i["status"] == "recovered", f"{i['pair']} recovered")
    post("/api/chaos/revert_all")
    time.sleep(6)

    print("4. Router crash r2")
    n0 = len(get("/api/routing")["incidents"])
    post("/api/chaos/inject", {"kind": "node_down", "target": "r2"})
    time.sleep(6)
    after = paths()
    check(all("-r2-" not in p for p in after.values()), f"no flow crosses r2: {after}")
    incs = [i for i in get("/api/routing")["incidents"][n0:] if i["kind"] == "failure"]
    reroutes = {}
    for i in incs:
        reroutes[i["pair"]] = reroutes.get(i["pair"], 0) + 1
        print(f"        {i['pair']} via {i['link']}: detection {i['detection_ms']} ms, reroute {i['reroute_ms']} ms, recovery {i['recovery_ms']} ms")
    check(all(n == 1 for n in reroutes.values()), f"each affected flow re-routed exactly once (suspect-link avoidance): {reroutes}")
    post("/api/chaos/revert_all")
    time.sleep(8)
    s = get("/api/state")
    check(all(s["links"][l]["health"] != "down" for l in ("r1-r2", "r2-r4", "r2-r5")), "r2 links healthy again after revert")

    if not a.quick:
        print("5. Latency injection +60 ms on the link c1>srv1 uses")
        time.sleep(4)
        r = get("/api/routing")
        path = r["flows"]["c1>srv1"]["path"]
        links = s["links"]
        core = [lid for u, v in zip(path, path[1:]) for lid in (f"{u}-{v}", f"{v}-{u}")
                if lid in links and u.startswith("r") and v.startswith("r")]
        target = core[-1]
        post("/api/chaos/inject", {"kind": "link_latency", "target": target, "params": {"add_ms": 60}})
        time.sleep(3.0)  # let the 2 s RTT window fill with post-injection samples
        l = get("/api/state")["links"][target]
        check(bool(l["rtt_ms"]) and l["rtt_ms"] > 100, f"{target}: measured RTT rose to {l['rtt_ms']} ms (netem +60 ms each way)")
        time.sleep(5)
        new = get("/api/routing")["flows"]["c1>srv1"]["path"]
        check(new != path, f"c1>srv1 moved off {target}: {'-'.join(path)} -> {'-'.join(new)}")
        post("/api/chaos/revert_all")

    print("6. Static mode does not react")
    post("/api/routing/mode", {"mode": "static"})
    time.sleep(2)
    p_static = paths()
    post("/api/chaos/inject", {"kind": "link_down", "target": "r2-r5"})
    time.sleep(4)
    check(paths() == p_static, "static routing kept its paths during the failure")
    s = get("/api/state")
    check(s["flows"]["c1>srv1"]["probe"]["alive"] is False, "c1>srv1 end-to-end probes are dead (outage visible)")
    post("/api/chaos/revert_all")
    time.sleep(5)
    s = get("/api/state")
    check(s["flows"]["c1>srv1"]["probe"]["alive"] is True, "c1>srv1 back after repair (routes re-asserted by reconcile)")
    post("/api/routing/mode", {"mode": "adaptive"})

    print()
    print("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
