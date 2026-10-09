"""Assure: intents, protection plans, failure analysis, planner and prediction intervals."""

import json

import pytest

from conftest import TOPO_DIR
from netvista.assure.conformal import ResidualPool
from netvista.assure.intents import IntentError, PairState, View, evaluate_intent, validate_intent
from netvista.assure.model import core_links, react, scenario_by_id, single_scenarios, spof_elements, unreachable_pairs
from netvista.assure.planner import plan_routes
from netvista.assure.resilience import analyze
from netvista.routing.graph import build_graph
from netvista.routing.protection import desired_paths, scenario_key
from netvista.routing.yen import k_shortest_paths
from netvista.simulator import run_fluid_prediction
from netvista.topology import load_topology

TOPO = load_topology(TOPO_DIR / "default.json")
PAIRS = [f"{c}>{s}" for c, s in TOPO.flow_pairs()]
CORE = core_links(TOPO)


def mk(raw, iid="I1"):
    return validate_intent(dict(raw, id=iid), TOPO, PAIRS, CORE)


def cfg_for(rates, herd_guard=True):
    g = build_graph(TOPO)
    links = {lid: {"a": l.a, "b": l.b, **l.params(), "up": True} for lid, l in TOPO.links.items()}
    pairs = {}
    for c, s in TOPO.flow_pairs():
        cands = [p for _, p in k_shortest_paths(g, c, s, 8, "delay_ms")]
        p = f"{c}>{s}"
        fl = [{"rate_mbps": r} for r in rates.get(p, [])]
        pairs[p] = {"src": c, "dst": s, "path": cands[0], "candidates": cands, "static_path": cands[0], "flows": fl,
                    "offered_mbps": sum(f["rate_mbps"] for f in fl)}
    return {"links": links, "pairs": pairs, "routers": TOPO.routers, "mode": "adaptive", "herd_guard": herd_guard,
            "weights": {"latency": 1.0, "loss": 5.0, "util": 0.2}, "hysteresis": 0.15, "duration_s": 10, "warmup_s": 3,
            "overhead": {"per_hop_ms": 0.05, "endpoint_ms": 0.1, "noise_ms": [-0.1, 0.0, 0.1, 0.3]},
            "probe_timeout_s": 2.0, "payload_bytes": 1200, "wire_bytes": 1242}


HEAVY = {"c1>srv1": [12], "c2>srv2": [12], "c1>srv2": [8], "c2>srv1": [8]}
GOLD = [
    {"kind": "latency", "flows": ["c1>srv1", "c2>srv2"], "params": {"stat": "p95", "max_ms": 30}, "protect": "any", "priority": "critical"},
    {"kind": "loss", "flows": ["*"], "params": {"max_pct": 1}, "protect": "any", "priority": "high"},
    {"kind": "max_util", "links": ["*"], "params": {"max_pct": 85}, "protect": "any", "priority": "high"},
    {"kind": "reach", "flows": ["*"], "protect": "any", "priority": "critical"},
]


def gold():
    return [mk(r, f"I{i + 1}") for i, r in enumerate(GOLD)]


# ---------------------------------------------------------------------------- intents
def test_intent_validation_and_labels():
    it = mk({"kind": "latency", "flows": ["c1>srv1"], "params": {"max_ms": 30}, "protect": "link"})
    assert it.params == {"stat": "p95", "max_ms": 30.0}
    assert "c1 → srv1" in it.label and "single link failure" in it.label
    assert it.covers("link") and not it.covers("node")
    for bad in (
        {"kind": "latency", "flows": ["c9>srv1"], "params": {"max_ms": 30}},
        {"kind": "latency", "flows": ["c1>srv1"], "params": {"max_ms": -1}},
        {"kind": "avoid", "flows": ["c1>srv1"], "params": {"elements": ["c2"]}},
        {"kind": "disjoint", "flows": ["c1>srv1"]},
        {"kind": "teleport", "flows": ["*"]},
    ):
        with pytest.raises(IntentError):
            mk(bad)


def view_with(path, rtt=25.0, loss=0.0, util=None, offered=6.0):
    st = PairState(path=path, alive=True, rtt={"p50": rtt, "p95": rtt, "p99": rtt}, loss_pct=loss, loss_source="data",
                   offered_mbps=offered, rx_mbps=offered)
    return View("live", {"c1>srv1": st}, util or {}, {l: True for l in TOPO.links}, {}, {})


def test_intent_checks_on_a_view():
    top = ["c1", "sw1", "r1", "r2", "r5", "sw2", "srv1"]
    lat = mk({"kind": "latency", "flows": ["c1>srv1"], "params": {"max_ms": 30}})
    assert evaluate_intent(lat, view_with(top, rtt=25), TOPO, CORE).status == "ok"
    r = evaluate_intent(lat, view_with(top, rtt=36), TOPO, CORE)
    assert r.status == "violated" and r.checks[0].severity == pytest.approx(1.2)
    v = view_with(top, rtt=29.5)
    v.rtt_margin = 0.05  # 29.5 * 1.05 > 30: inside the limit, but not with its prediction interval
    assert evaluate_intent(lat, v, TOPO, CORE).status == "at_risk"
    avoid = mk({"kind": "avoid", "flows": ["c1>srv1"], "params": {"elements": ["r2-r5"]}})
    assert evaluate_intent(avoid, view_with(top), TOPO, CORE).status == "violated"
    way = mk({"kind": "waypoint", "flows": ["c1>srv1"], "params": {"node": "r3"}})
    assert evaluate_intent(way, view_with(top), TOPO, CORE).status == "violated"
    util = mk({"kind": "max_util", "links": ["*"], "params": {"max_pct": 85}})
    assert evaluate_intent(util, view_with(top, util={"r2-r5": 0.9}), TOPO, CORE).status == "violated"


# ---------------------------------------------------------------------------- protection
def test_scenario_keys():
    assert scenario_key(set(), TOPO) is None
    assert scenario_key({"r2-r5"}, TOPO) == "link:r2-r5"
    assert scenario_key({"r1-r2", "r2-r5"}, TOPO) == "node:r2"  # every dead link touches r2
    assert scenario_key({"r1-r2", "r3-r5"}, TOPO) == "multi:r1-r2+r3-r5"


def test_desired_paths_use_backups_and_drop_dead_ones():
    a = ["c1", "sw1", "r1", "r2", "r5", "sw2", "srv1"]
    b = ["c1", "sw1", "r1", "r3", "r5", "sw2", "srv1"]
    plan = {"primary": {"c1>srv1": a}, "protection": {"link:r2-r5": {"c1>srv1": b}}}
    assert desired_paths(plan, set(), TOPO) == (None, {"c1>srv1": a})
    assert desired_paths(plan, {"r2-r5"}, TOPO) == ("link:r2-r5", {"c1>srv1": b})
    # an unplanned failure of the primary: no usable plan path, the controller falls back
    assert desired_paths(plan, {"r1-r2"}, TOPO) == ("link:r1-r2", {"c1>srv1": None})


def test_spofs_are_the_site_gateways_and_access():
    cut = {s["scenario"] for s in spof_elements(TOPO, PAIRS)}
    assert cut == {"node:r1", "node:r5"}
    assert unreachable_pairs(TOPO, scenario_by_id(TOPO, "link:r2-r5"), PAIRS) == set()


# ---------------------------------------------------------------------------- analysis + planning
def test_static_routing_cannot_survive_and_adaptive_reacts():
    cfg = cfg_for({"c1>srv1": [6], "c2>srv2": [6]})
    sc = scenario_by_id(TOPO, "link:r2-r5")
    assert react(cfg, sc, "static", None, TOPO)["paths"]["c1>srv1"][3] == "r2"
    moved = react(cfg, sc, "adaptive", None, TOPO)["paths"]["c1>srv1"]
    assert "r5" in moved and moved[3:5] != ["r2", "r5"]


def test_planner_meets_every_intent_now_and_reaches_the_failure_bound():
    cfg = cfg_for(HEAVY)
    adaptive = run_fluid_prediction(cfg)
    cfg["pairs"] = {p: dict(v, path=adaptive["pairs"][p]["path"]) for p, v in cfg["pairs"].items()}
    intents = gold()
    plan = plan_routes({"topo": TOPO, "cfg": cfg, "intents": intents, "weights": cfg["weights"]})
    assert plan["objective"]["v_now"] == 0  # every intent met in normal operation
    assert plan["current"]["v_now"] > 0  # the load-balancing controller broke the gold SLO
    assert plan["search"]["method"] == "exhaustive" and plan["search"]["space"] == 1296
    # the gold flows get low-latency paths (via r2, never the 34 ms detour over r3) within their SLO;
    # backups exist for the failures that hit them
    for p in ("c1>srv1", "c2>srv2"):
        assert "r2" in plan["primary"][p] and "r3" not in plan["primary"][p]
        assert plan["predicted"]["pairs"][p]["rtt_p95"] <= 30
    assert "node:r2" in plan["protection"]

    before = analyze({"topo": TOPO, "cfg": cfg, "intents": intents, "mode": "adaptive", "plan": None})
    planned_cfg = dict(cfg, pairs={p: dict(v, path=plan["primary"][p]) for p, v in cfg["pairs"].items()})
    ctrl_plan = {"id": "P1", "primary": plan["primary"], "protection": plan["protection"]}
    after = analyze({"topo": TOPO, "cfg": planned_cfg, "intents": intents, "mode": "intent", "plan": ctrl_plan})
    assert after["score"] > before["score"]
    # nothing the planner leaves broken was avoidable: it reaches the exhaustive per-failure bound
    assert after["score"] == pytest.approx(after["score_best"])
    for row in after["scenarios"]:
        assert row["avoidable"] == []
    # what remains is physics (no path under 30 ms survives r1-r2) or a single point of failure
    r1r2 = next(r for r in after["scenarios"] if r["scenario"]["id"] == "link:r1-r2")
    assert r1r2["violated"] == ["I1"] and r1r2["unavoidable"] == ["I1"]
    r1 = next(r for r in after["scenarios"] if r["scenario"]["id"] == "node:r1")
    assert set(r1["unprotectable"]) == {"I1", "I2", "I4"}


def test_hard_path_constraints_prune_candidates():
    cfg = cfg_for({"c1>srv1": [6]})
    intents = [mk({"kind": "avoid", "flows": ["c1>srv1"], "params": {"elements": ["r2"]}})]
    plan = plan_routes({"topo": TOPO, "cfg": cfg, "intents": intents})
    assert "r2" not in plan["primary"]["c1>srv1"]
    assert plan["search"]["pruned_candidates"]["c1>srv1"] > 0


def test_resilience_matrix_covers_every_single_failure():
    res = analyze({"topo": TOPO, "cfg": cfg_for({"c1>srv1": [6], "c2>srv2": [6]}), "intents": gold(), "mode": "adaptive", "plan": None})
    ids = {r["scenario"]["id"] for r in res["scenarios"]}
    assert ids == {s.id for s in single_scenarios(TOPO)}
    assert set(res["criticality"]) == {f"link:{l}" for l in CORE} | {f"node:{r}" for r in TOPO.routers}
    assert res["criticality"]["node:r1"]["unreachable"]  # the edge router cuts every flow off


# ---------------------------------------------------------------------------- uncertainty
def test_conformal_interval_and_online_coverage(tmp_path):
    pool = ResidualPool(tmp_path / "res.jsonl")
    assert pool.half_width("fluid", "rtt")[0] is None  # too few residuals: no interval yet
    for i in range(1, 21):
        pool.add("fluid", "rtt_p50", 100.0 + i * 0.1, 100.0)  # relative errors 0.001 .. 0.02
    q, n, src = pool.half_width("fluid", "rtt")
    assert n == 20 and src == "fluid"
    assert q == pytest.approx(0.019, abs=1e-9)  # ceil(21 * 0.9) = 19th smallest
    s = pool.summary()["fluid:rtt"]
    assert s["coverage_tested"] == 11 and 0 <= s["coverage"] <= 1  # each new point tested before joining
    reloaded = ResidualPool(tmp_path / "res.jsonl")
    assert reloaded.count("fluid", "rtt") == 20
    lines = (tmp_path / "res.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["cls"] == "rtt"


# ---------------------------------------------------------------------------- copilot tools
def test_copilot_can_propose_an_intent_but_only_the_user_adds_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from netvista.ai.copilot.agent import Copilot
    from netvista.assure.intents import IntentStore
    from netvista.config import Settings
    from netvista.topology import build_address_plan

    monkeypatch.setenv("NETVISTA_AI_PROVIDER", "off")
    store = IntentStore(tmp_path / "intents.json", TOPO, PAIRS, CORE)
    events = []
    rt = SimpleNamespace(topo=TOPO, plan=build_address_plan(TOPO), s=Settings(), events=SimpleNamespace(emit=lambda *a, **k: events.append(a)),
                         extensions={"assure": SimpleNamespace(pairs=PAIRS, core=CORE, intents=store)})
    cp = Copilot(rt, ai=SimpleNamespace())
    kinds = {n: t.kind for n, t in cp.toolbox.tools.items()}
    assert kinds["get_intents"] == "read" and kinds["get_resilience"] == "read" and kinds["make_plan"] == "simulate"
    assert kinds["propose_intent"] == "propose" and kinds["propose_plan"] == "propose"
    assert not any(n.startswith(("apply", "set_", "add_")) for n in kinds)  # still no tool that changes anything

    ok, bad = cp.toolbox.run("propose_intent", {"kind": "latency", "flows": ["c9>srv1"], "params": {"max_ms": 30}})
    assert not ok and "unknown flow" in bad["error"]
    ok, res = cp.toolbox.run("propose_intent", {"kind": "latency", "flows": ["c1>srv1"], "params": {"max_ms": 30}, "protect": "link"})
    assert ok and store.list() == []  # proposed, not added
    applied = cp.apply_proposal(res["proposal_id"])
    assert applied["status"] == "applied" and [i.label for i in store.list()] == ["c1 → srv1: round trip p95 ≤ 30 ms, even after any single link failure"]


# ---------------------------------------------------------------------------- autopilot safety gate
def _gate_fixture(tmp_path, *, dead=(), latest=None, v_now=(0.0, 12.0), v_fail=(100.0, 120.0), agree=True, peak=0.83):
    from types import SimpleNamespace

    from netvista.assure.autopilot import Autopilot
    from netvista.assure.intents import IntentResult
    from netvista.config import Settings

    intents = gold()
    latest = latest or {"I1": "ok", "I2": "ok", "I3": "ok", "I4": "ok"}
    ctrl = SimpleNamespace(link_alive={l: l not in dead for l in CORE}, route_plan=None, mode="adaptive")
    rt = SimpleNamespace(topo=TOPO, controller=ctrl, s=Settings(runs_dir=tmp_path),
                         events=SimpleNamespace(emit=lambda *a, **k: None))
    store = SimpleNamespace(list=lambda: intents, latest={k: IntentResult(k, v, []) for k, v in latest.items()})
    svc = SimpleNamespace(rt=rt, intents=store, busy_job=lambda: None, emit=lambda *a, **k: None,
                          confirm_plan=lambda pid: None)
    ap = Autopilot(svc)
    primary = {p: ["c1" if p.startswith("c1") else "c2", "sw1", "r1", "r2", "r5", "sw2", p.split(">")[1]] for p in PAIRS}
    plan = {
        "id": "P9", "primary": primary,
        "objective": {"v_now": v_now[0], "v_fail": v_fail[0], "cost": 100.0},
        "current": {"v_now": v_now[1], "v_fail": v_fail[1], "cost": 100.0},
        "predicted": {"intents": [{"intent": i, "status": "ok"} for i in latest], "link_util": {"r2-r5": peak}},
        "confirmation": {"agree": agree, "max_rtt_diff_pct": 1.0 if agree else 9.0, "max_util_diff_pp": 0.2},
    }
    return ap, plan


def test_gate_passes_a_strictly_better_confirmed_plan(tmp_path):
    ap, plan = _gate_fixture(tmp_path)
    g = ap.gate(plan)
    assert g["ok"], g["reasons"]
    assert [c["check"] for c in g["checks"]] == ["network free", "paths alive", "improves", "no regression", "headroom", "twins agree", "rate limit"]


@pytest.mark.parametrize("kw,check", [
    ({"dead": ("r2-r5",)}, "paths alive"),
    ({"v_now": (5.0, 5.0), "v_fail": (100.0, 100.0)}, "improves"),
    ({"agree": False}, "twins agree"),
    ({"peak": 0.97}, "headroom"),
])
def test_gate_blocks_and_says_why(tmp_path, kw, check):
    ap, plan = _gate_fixture(tmp_path, **kw)
    g = ap.gate(plan)
    assert not g["ok"]
    failed = [c["check"] for c in g["checks"] if not c["ok"]]
    assert failed == [check] and g["reasons"]


def test_gate_refuses_to_break_a_critical_intent_that_holds(tmp_path):
    ap, plan = _gate_fixture(tmp_path)
    plan["predicted"]["intents"] = [{"intent": "I1", "status": "violated"}] + [{"intent": i, "status": "ok"} for i in ("I2", "I3", "I4")]
    g = ap.gate(plan)
    assert not g["ok"] and "I1" in " ".join(g["reasons"])
