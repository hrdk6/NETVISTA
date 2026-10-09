"""Predictive resilience: which intents would break if any single element failed?

For every failure scenario (each core link, each router; optionally every pair of core links)
the analysis

  1. takes the live state (link parameters, demand, current paths) as the twin sees it,
  2. predicts how the routing reacts: static paths, the adaptive controller's own scoring
     rule round by round, or the plan's pre-planned backups (model.react),
  3. predicts the resulting latency, loss and load with the fluid model,
  4. checks every intent whose `protect` covers that failure,
  5. separates what routing could fix from what it cannot: if the failure cuts a flow off
     entirely (an edge router, a site's only uplink) no routing helps - that is a single point
     of failure of the topology, reported as such.

The adaptive controller is also judged on its transient: the assignment right after the
fail-over (when flows decide against stale counters) can break intents that the re-balanced
assignment satisfies a few seconds later.

Output: a scenario x intent matrix, per-element criticality for the fragility map, and a
resilience score = weighted share of (scenario, intent) cells that hold. Seconds of CPU for
the whole matrix, so it can run on every change; the worst scenarios can be confirmed with
the packet-level twin and then drilled on the live network (drill.py).
"""

from __future__ import annotations

import itertools
import time
from typing import Any

from ..simulator.fluid import demand_of
from .intents import Intent, evaluate_all, weighted_violation
from .model import (
    Scenario, core_links, crosses, double_link_scenarios, evaluate_paths, react, single_scenarios, spof_elements,
    unreachable_pairs, with_failure,
)


def _summ(results, intents_by_id) -> list[dict]:
    return [dict(r.to_dict(), weight=intents_by_id[r.intent].weight) for r in results]


def best_achievable(topo, cfg_f: dict, intents: list[Intent], sc: Scenario, margin: float) -> dict:
    """The least-violating routing after failure `sc`: every combination of surviving candidate
    paths when that is small enough, otherwise local search. Here K-shortest covers every simple
    path between the sites, so an intent this misses is impossible to meet by routing alone."""
    from .planner import MAX_ENUM, Evaluator, _local_search, _net

    pairs = list(cfg_f["pairs"])
    cands = {}
    for p in pairs:
        alive = [list(c) for c in cfg_f["pairs"][p]["candidates"] if not crosses(c, sc, topo)]
        cands[p] = alive or [None]
    ev = Evaluator(topo, intents, cfg_f.get("weights") or {"latency": 1.0, "loss": 5.0, "util": 0.2}, margin, 0.0)
    net = _net(cfg_f)
    demand = demand_of(cfg_f)
    space = 1
    for p in pairs:
        space *= len(cands[p])
    best = None
    if space <= MAX_ENUM:
        for combo in itertools.product(*(cands[p] for p in pairs)):
            paths = dict(zip(pairs, combo))
            v, perf, _, res = ev.score(net, paths, demand, sc.failure_class)
            key = (round(v, 6), perf)
            if best is None or key < best[0]:
                best = (key, paths, res)
        method = "exhaustive"
    else:
        start = {p: cands[p][0] for p in pairs}
        top = _local_search(ev, net, pairs, {p: [c for c in cands[p] if c] or [None] for p in pairs}, start, demand)
        key, paths = top[0]
        _, _, _, res = ev.score(net, paths, demand, sc.failure_class)
        best = (key, paths, res)
        method = "local search"
    return {
        "violated": [r.intent for r in best[2] if r.status == "violated"],
        "severity": best[0][0],
        "paths": best[1],
        "method": method,
        "combinations": space,
    }


def analyze(bundle: dict[str, Any]) -> dict[str, Any]:
    """bundle: topo, cfg (twin config of the live state), intents, mode, plan, kinds, double, rtt_margin."""
    t0 = time.perf_counter()
    topo = bundle["topo"]
    cfg = bundle["cfg"]
    intents: list[Intent] = [i for i in bundle["intents"] if i.enabled]
    by_id = {i.id: i for i in intents}
    mode = bundle.get("mode", "adaptive")
    plan = bundle.get("plan")
    margin = float(bundle.get("rtt_margin", 0.0))
    core = core_links(topo)
    pairs = list(cfg["pairs"])
    current = {p: list(v["path"]) for p, v in cfg["pairs"].items()}

    scenarios: list[Scenario] = single_scenarios(topo, tuple(bundle.get("kinds", ("link", "node"))))
    if bundle.get("double"):
        scenarios += double_link_scenarios(topo)

    view0, ev0 = evaluate_paths(cfg, current, margin)
    now_results = evaluate_all(intents, view0, topo, core)
    bounds = bundle.get("bounds", True)

    rows = []
    for sc in scenarios:
        cut = unreachable_pairs(topo, sc, pairs)
        rx = react(cfg, sc, mode, plan, topo)
        cfg_f = with_failure(cfg, sc)
        view, ev = evaluate_paths(cfg_f, rx["paths"], margin)
        results = evaluate_all(intents, view, topo, core, failure_kind=sc.failure_class)
        best = best_achievable(topo, cfg_f, intents, sc, margin) if bounds else None
        transient = None
        if rx["first"] != rx["paths"]:
            tview, _ = evaluate_paths(cfg_f, rx["first"], margin)
            tres = evaluate_all(intents, tview, topo, core, failure_kind=sc.failure_class)
            transient = {
                "paths": rx["first"],
                "violated": [r.intent for r in tres if r.status == "violated"],
                "max_util": max(tview.link_util.values(), default=0.0),
            }
        violated = [r for r in results if r.status == "violated"]
        unprotectable = []
        for r in violated:
            bad = [c for c in r.checks if c.ok is False]
            subjects = {c.subject for c in bad}
            if bad and subjects <= cut:
                unprotectable.append(r.intent)
        # a violation is "avoidable" when the best routing for this failure satisfies that intent
        unavoidable = [i for i in (best["violated"] if best else []) if i not in unprotectable]
        avoidable = [r.intent for r in violated if best is not None and r.intent not in best["violated"]]
        offered = sum(p.offered_mbps for p in ev.pairs.values())
        delivered = sum(p.rx_mbps for p in ev.pairs.values() if p.offered_mbps > 0)
        rows.append({
            "scenario": sc.to_dict(),
            "affected": [p for p in pairs if crosses(current[p], sc, topo)],
            "moved": [p for p in pairs if rx["paths"].get(p) != current[p]],
            "paths": rx["paths"],
            "planned": rx["planned"],
            "rounds": rx["rounds"],
            "unreachable": sorted(cut),
            "intents": _summ(results, by_id),
            "violated": [r.intent for r in violated],
            "at_risk": [r.intent for r in results if r.status == "at_risk"],
            "unprotectable": unprotectable,
            "unavoidable": unavoidable,
            "avoidable": avoidable,
            "best": best,
            "severity": round(weighted_violation(results, by_id, risk_weight=0.0), 4),
            "max_util": round(max(view.link_util.values(), default=0.0), 4),
            "lost_mbps": round(max(0.0, offered - delivered), 3),
            "transient": transient,
            "rtt": {p: s.rtt.get("p95") for p, s in view.pairs.items()},
        })

    # per intent: how many of the failures it must survive does it survive?
    per_intent = []
    total_w = held_w = best_w = 0.0
    for it in intents:
        cells = [r for r in rows if it.covers(r["scenario"]["kind"] if r["scenario"]["kind"] == "node" else "link")]
        broken = [r["scenario"]["id"] for r in cells if it.id in r["violated"]]
        unprot = [r["scenario"]["id"] for r in cells if it.id in r["unprotectable"]]
        unavoid = [r["scenario"]["id"] for r in cells if it.id in r["unavoidable"]]
        avoid = [r["scenario"]["id"] for r in cells if it.id in r["avoidable"]]
        transient = [r["scenario"]["id"] for r in cells if r["transient"] and it.id in r["transient"]["violated"] and it.id not in r["violated"]]
        n = len(cells)
        total_w += it.weight * n
        held_w += it.weight * (n - len(broken))
        best_w += it.weight * (n - len(unprot) - len(unavoid)) if bounds else 0.0
        per_intent.append({
            "intent": it.id, "label": it.label, "protect": it.protect, "weight": it.weight, "scenarios": n,
            "held": n - len(broken), "broken": broken, "avoidable": avoid, "unavoidable": unavoid, "unprotectable": unprot,
            "transient_only": transient,
        })

    # per element: how bad is it if this one fails? (drives the fragility map)
    criticality = {}
    # share of the intents (by priority weight) that break when this element fails: 1 = everything breaks
    total_weight = sum(i.weight for i in intents) or 1.0
    for r in rows:
        sc = r["scenario"]
        el = f"link:{sc['links'][0]}" if sc["kind"] == "link" else (f"node:{sc['nodes'][0]}" if sc["kind"] == "node" else None)
        if el is None:
            continue
        criticality[el] = {
            "scenario": sc["id"], "severity": r["severity"],
            "norm": round(sum(by_id[i].weight for i in r["violated"]) / total_weight, 4),
            "violated": r["violated"], "unprotectable": r["unprotectable"], "unreachable": r["unreachable"],
            "lost_mbps": r["lost_mbps"],
        }

    return {
        "t": time.time(),
        "mode": mode,
        "plan": plan.get("id") if plan else None,
        "engine": "fluid",
        "scenarios": rows,
        "intents": per_intent,
        "criticality": criticality,
        "spofs": spof_elements(topo, pairs),
        "now": {"intents": _summ(now_results, by_id), "max_util": round(max(view0.link_util.values(), default=0.0), 4)},
        # share of (failure, intent) cells that hold; and the best any routing could reach (the bound)
        "score": round(100.0 * held_w / total_w, 1) if total_w else None,
        "score_best": round(100.0 * best_w / total_w, 1) if (total_w and bounds) else None,
        "cells": sum(len(r["intents"]) for r in rows),
        "wall_s": round(time.perf_counter() - t0, 4),
    }
