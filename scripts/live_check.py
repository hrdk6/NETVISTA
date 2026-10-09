#!/usr/bin/env python3
"""End-to-end acceptance check against a RUNNING NETVISTA backend (stdlib only).

    python3 scripts/live_check.py [--url http://localhost:8000] [--quick]

It drives the real emulated network through: traffic -> link failure -> router crash ->
latency injection -> recovery -> a subtle fault for the AI layer, and prints the measured detection / reroute / recovery
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


def last_incident() -> int:
    return max((i["id"] for i in get("/api/routing")["incidents"]), default=0)


def new_incidents(after_id: int) -> list[dict]:
    # by id, not by position: the API returns only the latest 30 incidents
    return [i for i in get("/api/routing")["incidents"] if i["id"] > after_id and i["kind"] == "failure"]


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
    post("/api/traffic/stop")  # the baseline is measured on a quiet network, whatever ran before
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
    post("/api/traffic/start")  # the default 6 Mbit/s per flow
    time.sleep(6)
    s = get("/api/state")
    for pair in ("c1>srv1", "c2>srv2"):
        t = s["flows"][pair]["traffic"]
        check(t is not None and t["rx_mbps"] > 5.0, f"{pair}: iperf3 receiver reports {t and round(t['rx_mbps'], 2)} Mbit/s (offered 6)")
    busiest = max(s["links"].values(), key=lambda l: l["util"])
    check(busiest["util"] > 0.1, f"interface counters show load: busiest link {busiest['id']} at {busiest['util'] * 100:.1f}%")

    print("3. Link failure r2-r5 (adaptive routing)")
    before = paths()
    n0 = last_incident()
    post("/api/chaos/inject", {"kind": "link_down", "target": "r2-r5"})
    time.sleep(5)
    after = paths()
    check(all("r2-r5" not in p for p in after.values()), f"no flow uses r2-r5 any more: {after}")
    incs = new_incidents(n0)
    check(bool(incs), "failure incidents recorded")
    for i in incs:
        print(f"        {i['pair']}: detection {i['detection_ms']} ms, reroute {i['reroute_ms']} ms, recovery {i['recovery_ms']} ms")
        check(i["status"] == "recovered", f"{i['pair']} recovered")
    post("/api/chaos/revert_all")
    time.sleep(6)

    print("4. Router crash r2")
    n0 = last_incident()
    post("/api/chaos/inject", {"kind": "node_down", "target": "r2"})
    time.sleep(6)
    after = paths()
    check(all("-r2-" not in p for p in after.values()), f"no flow crosses r2: {after}")
    incs = new_incidents(n0)
    check(bool(incs), "failure incidents recorded")
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

    print("7. AI layer: learned baselines + root-cause analysis")
    st = get("/api/ai/status")
    det = st["detector"]
    check(det["normal"] + det["anomalous"] >= det["signals"] - 6, f"detector has learned {det['normal']} of {det['signals']} signals")
    t_end = time.time() + 40
    while time.time() < t_end and (get("/api/ai/insights")["anomalies"] or get("/api/ai/insights")["diagnosis"]["causes"]):
        time.sleep(2)  # let the previous steps' anomalies clear
    check(not get("/api/ai/insights")["diagnosis"]["causes"], "no diagnosis while the network is healthy")
    # +2 ms each way: below the threshold rule (1.5 x design + 2 ms), well above the learned spread
    inj = post("/api/chaos/inject", {"kind": "link_latency", "target": "r2-r5", "params": {"add_ms": 2}})
    t0, found, health = time.time(), None, None
    while time.time() - t0 < 15 and not found:
        time.sleep(0.5)
        ins = get("/api/ai/insights")
        found = next((c for c in ins["diagnosis"]["causes"] if c["element"] == "r2-r5"), None)
    health = get("/api/state")["links"]["r2-r5"]["health"]
    check(found is not None and found["type"] == "latency",
          f"subtle +2 ms on r2-r5 diagnosed as '{found and found['title']}' after {time.time() - t0:.1f} s (threshold health says '{health}')")
    post(f"/api/chaos/revert/{inj['id']}")
    print(f"     copilot: {st['copilot']['label']}" + ("" if st["copilot"]["available"] else f" ({st['copilot']['reason']})"))

    print("8. Assure: intents, failure analysis, plan, verification, drill")
    intents = post("/api/assure/intents/preset", {"name": "gold-bronze", "replace": True})
    check(len(intents) >= 4, f"gold/bronze preset loaded: {len(intents)} intents")
    time.sleep(4)
    rows = get("/api/assure/intents")
    check(all(r["status"] in ("ok", "at_risk") for r in rows), "every intent met with the default traffic: " + ", ".join(f"{r['id']} {r['status']}" for r in rows))
    res = post("/api/assure/resilience", {"double": False})
    check(len(res["scenarios"]) == 12 and res["score"] is not None, f"failure analysis: 12 single failures in {res['wall_s']:.2f} s, {res['score']}% hold (best {res['score_best']}%)")
    check({s["scenario"] for s in res["spofs"]} == {"node:r1", "node:r5"}, "single points of failure are the two site gateways")
    plan = post("/api/assure/plan")
    check(plan["resilience_after"]["score"] >= plan["resilience_before"]["score"], f"plan {plan['id']}: resilience {plan['resilience_before']['score']}% -> {plan['resilience_after']['score']}%")
    post(f"/api/assure/plans/{plan['id']}/apply")
    t0, verdict = time.time(), None
    while time.time() - t0 < 45 and verdict is None:
        time.sleep(2)
        verdict = (get(f"/api/assure/plans/{plan['id']}").get("verification") or {}).get("verdict")
    check(verdict == "verified", f"plan {plan['id']} verified on live measurements after {time.time() - t0:.0f} s ({verdict})")
    time.sleep(3)
    post("/api/assure/drill", {"scenario": "link:r2-r5"})
    t0 = time.time()
    while time.time() - t0 < 90 and get("/api/state")["jobs"]["assure"]["drill"].get("running"):
        time.sleep(2)
    d = get("/api/assure/drills")[0]
    check(d["paths_match_fluid"], f"drill r2-r5 ({d['mode']}): backup paths as predicted")
    check((d["intent_accuracy"] or 0) >= 99, f"drill r2-r5: {d['intent_accuracy']}% of intent outcomes as predicted")
    post("/api/assure/plan/clear", {"mode": "adaptive"})

    print()
    print("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
