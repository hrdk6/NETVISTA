"""Intent planner: choose every flow's path, and a backup for every failure, against the intents.

The adaptive controller decides one flow at a time, from what it measures now, with a
generic score (latency + loss + load). It does not know that c1 → srv1 has a 30 ms SLO and
c1 → srv2 is bulk traffic, and it reacts to a failure only after the failure. The planner
decides all flows *jointly*, for the operator's intents, *before* anything fails:

  objective (lexicographic, smaller is better)
    1. V_now   weighted intent violation in the current state (severity: 1 + relative excess,
               x priority weight 10/3/1; "at risk" cells - point estimate inside the limit,
               upper prediction bound outside - cost a quarter)
    2. V_fail  sum over the failure scenarios of the violation of the intents that must
               survive that failure, with the best backup paths for that failure
    3. cost    the controller's own path score summed over the flows, plus a cost per flow
               moved away from its current path (stability: never move traffic for nothing)

  search
    primary    every combination of the flows' K candidate paths (Yen) when that is small
               enough (4 flows x 6 paths = 1296 here), otherwise local search with restarts;
               each combination is one fluid-model evaluation (~0.1 ms)
    backup     for each kept primary and each failure, only the flows that cross the failed
               element move (local repair, like fast reroute): every combination of their
               surviving candidates is tried; the best one becomes that failure's protection
               entry, so it is checked against the intents before it is ever needed

Hard constraints (avoid / waypoint) prune the primary candidates when some candidate can
satisfy them. Everything is deterministic: the same state and intents give the same plan.
"""

from __future__ import annotations

import itertools
import random
import time
from typing import Any

from ..simulator.fluid import FluidNet, demand_of
from .intents import Intent, View, evaluate_all, path_links, weighted_violation
from .model import Scenario, core_links, crosses, predicted_view, single_scenarios, unreachable_pairs, with_failure

MAX_ENUM = 6000  # exhaustive primary search up to this many combinations
MAX_KEEP = 48  # primaries whose failure behaviour is optimised
ALT_PER_FLOW = 4  # backup candidates tried per affected flow
DISCONNECTED_COST = 1000.0


def _net(cfg: dict) -> FluidNet:
    return FluidNet(cfg["links"], cfg.get("overhead"), int(cfg.get("payload_bytes", 1200)), int(cfg.get("wire_bytes", 1242)),
                    float(cfg.get("probe_timeout_s", 2.0)))


class Evaluator:
    """Counts evaluations; caches nothing (every call is a different routing)."""

    def __init__(self, topo, intents: list[Intent], weights: dict, margin: float, change_cost: float) -> None:
        self.topo = topo
        self.intents = intents
        self.by_id = {i.id: i for i in intents}
        self.w = weights
        self.margin = margin
        self.change_cost = change_cost
        self.core = core_links(topo)
        self.n = 0

    def perf(self, view: View) -> float:
        total = 0.0
        for st in view.pairs.values():
            wt = 1.0 if st.offered_mbps > 0 else 0.25
            if not st.alive or st.rtt.get("p50") is None:
                total += wt * DISCONNECTED_COST
                continue
            util = max((view.link_util.get(l, 0.0) for l in path_links(st.path, self.topo)), default=0.0)
            total += wt * (self.w["latency"] * st.rtt["p50"] / 2 + self.w["loss"] * (st.loss_pct or 0.0) + self.w["util"] * 100 * util)
        return total

    def score(self, net: FluidNet, paths: dict, demand: dict, failure_kind: str | None) -> tuple[float, float, View, list]:
        self.n += 1
        ev = net.evaluate(paths, demand)
        view = predicted_view(net, ev, paths, self.margin)
        results = evaluate_all(self.intents, view, self.topo, self.core, failure_kind=failure_kind)
        return weighted_violation(results, self.by_id), self.perf(view), view, results

    def moves(self, paths: dict, current: dict, demand: dict) -> float:
        return sum((1.0 if demand.get(p) else 0.15) for p in paths if paths[p] != current.get(p))


def _allowed(cands: list[list[str]], pair: str, intents: list[Intent], topo) -> list[list[str]]:
    """Drop candidates that break a hard path constraint (avoid / waypoint) - if any satisfy it."""
    ok = list(cands)
    for it in intents:
        if not it.enabled or it.kind not in ("avoid", "waypoint"):
            continue
        if "*" not in it.flows and pair not in it.flows:
            continue
        if it.kind == "avoid":
            bad = set(it.params["elements"])
            keep = [c for c in ok if not (bad & (set(c) | set(path_links(c, topo))))]
        else:
            keep = [c for c in ok if it.params["node"] in c]
        if keep:
            ok = keep
    return ok


def _protect(ev: Evaluator, net_f: FluidNet, sc: Scenario, primary: dict, cands: dict, demand: dict, topo, cut: set[str]):
    """Best backup paths for the flows that cross the failed element (local repair)."""
    affected = [p for p in primary if crosses(primary[p], sc, topo)]
    if not affected:
        v, perf, _, _ = ev.score(net_f, primary, demand, sc.failure_class)
        return {}, v, perf
    options = []
    for p in affected:
        alts = [c for c in cands[p] if not crosses(c, sc, topo)][:ALT_PER_FLOW]
        options.append(alts or [None])
    best = None
    for combo in itertools.product(*options):
        paths = dict(primary)
        for p, c in zip(affected, combo):
            paths[p] = c
        v, perf, _, _ = ev.score(net_f, paths, demand, sc.failure_class)
        key = (round(v, 6), perf)
        if best is None or key < best[0]:
            best = (key, {p: c for p, c in zip(affected, combo) if c is not None}, v, perf)
    return best[1], best[2], best[3]


def _local_search(ev: Evaluator, net0: FluidNet, pairs: list[str], cands: dict, current: dict, demand: dict, seed: int = 7) -> list[tuple]:
    """Best-improvement coordinate descent from several starts (used when enumeration is too big)."""
    rng = random.Random(seed)
    starts = [dict(current), {p: cands[p][0] for p in pairs}]
    for _ in range(6):
        starts.append({p: rng.choice(cands[p]) for p in pairs})
    found: dict[tuple, tuple] = {}

    def key_of(paths):
        v, perf, _, _ = ev.score(net0, paths, demand, None)
        return (round(v, 6), perf + ev.change_cost * ev.moves(paths, current, demand))

    for start in starts:
        cur = dict(start)
        cur_key = key_of(cur)
        for _ in range(30):
            improved = False
            for p in pairs:
                for c in cands[p]:
                    if c == cur[p]:
                        continue
                    trial = dict(cur)
                    trial[p] = c
                    k = key_of(trial)
                    found[tuple(tuple(trial[q]) for q in pairs)] = (k, trial)
                    if k < cur_key:
                        cur, cur_key, improved = trial, k, True
            if not improved:
                break
        found[tuple(tuple(cur[q]) for q in pairs)] = (cur_key, cur)
    return sorted(found.values(), key=lambda x: x[0])


def plan_routes(bundle: dict[str, Any]) -> dict[str, Any]:
    """bundle: topo, cfg, intents, weights, kinds (failure classes to pre-plan), rtt_margin, change_cost."""
    t0 = time.perf_counter()
    topo = bundle["topo"]
    cfg = bundle["cfg"]
    intents = [i for i in bundle["intents"] if i.enabled]
    weights = bundle.get("weights") or cfg.get("weights") or {"latency": 1.0, "loss": 5.0, "util": 0.2}
    ev = Evaluator(topo, intents, weights, float(bundle.get("rtt_margin", 0.0)), float(bundle.get("change_cost", 3.0)))
    pairs = list(cfg["pairs"])
    current = {p: list(cfg["pairs"][p]["path"]) for p in pairs}
    all_cands = {p: [list(c) for c in cfg["pairs"][p]["candidates"]] for p in pairs}
    cands = {p: _allowed(all_cands[p], p, intents, topo) for p in pairs}
    demand = demand_of(cfg)
    net0 = _net(cfg)

    # ---- 1. primary assignments, ranked by the normal state
    space = 1
    for p in pairs:
        space *= len(cands[p])
    ranked: list[tuple] = []
    if space <= MAX_ENUM:
        method = "exhaustive"
        for combo in itertools.product(*(cands[p] for p in pairs)):
            paths = dict(zip(pairs, combo))
            v, perf, _, _ = ev.score(net0, paths, demand, None)
            ranked.append(((round(v, 6), perf + ev.change_cost * ev.moves(paths, current, demand)), paths))
        ranked.sort(key=lambda x: x[0])
    else:
        method = "local search"
        ranked = _local_search(ev, net0, pairs, cands, current, demand)
    v_cur, perf_cur, _, _ = ev.score(net0, current, demand, None)
    cur_key = (round(v_cur, 6), perf_cur)
    best_v = ranked[0][0][0]
    keep = [r for r in ranked if r[0][0] <= best_v + 1e-9][:MAX_KEEP]
    keep += [r for r in ranked[:8] if r not in keep]
    if not any(r[1] == current for r in keep):
        keep.append((cur_key, current))

    # ---- 2. failure scenarios and their backups
    kinds = tuple(bundle.get("kinds") or ("link", "node"))
    scenarios = single_scenarios(topo, kinds)
    nets = {sc.id: _net(with_failure(cfg, sc)) for sc in scenarios}
    cuts = {sc.id: unreachable_pairs(topo, sc, pairs) for sc in scenarios}
    evaluated = []
    for key_now, primary in keep:
        prot: dict[str, dict] = {}
        v_fail = perf_fail = 0.0
        per_sc = {}
        for sc in scenarios:
            entry, v, perf = _protect(ev, nets[sc.id], sc, primary, all_cands, demand, topo, cuts[sc.id])
            if entry:
                prot[sc.id] = entry
            v_fail += v
            perf_fail += perf
            per_sc[sc.id] = round(v, 4)
        evaluated.append({"key": (key_now[0], round(v_fail, 6), key_now[1]), "primary": primary, "protection": prot,
                          "v_now": key_now[0], "v_fail": v_fail, "cost": key_now[1], "perf_fail": perf_fail, "per_scenario": per_sc})
    evaluated.sort(key=lambda e: e["key"])
    best = evaluated[0]
    cur_eval = next(e for e in evaluated if e["primary"] == current)

    # ---- 3. predicted outcome of the chosen plan (normal state + every planned failure)
    v, perf, view, results = ev.score(net0, best["primary"], demand, None)
    scen_out = {}
    for sc in scenarios:
        paths = dict(best["primary"])
        paths.update(best["protection"].get(sc.id, {}))
        for p in pairs:
            if crosses(paths[p], sc, topo):
                paths[p] = None  # no surviving path: cut off
        vf, _, viewf, resf = ev.score(nets[sc.id], paths, demand, sc.failure_class)
        scen_out[sc.id] = {
            "label": sc.label, "kind": sc.kind, "severity": round(vf, 4),
            "violated": [r.intent for r in resf if r.status == "violated"],
            "unreachable": sorted(cuts[sc.id]), "moved": sorted(best["protection"].get(sc.id, {})),
            "max_util": round(max(viewf.link_util.values(), default=0.0), 4),
        }
    moved = [p for p in pairs if best["primary"][p] != current[p]]
    return {
        "primary": best["primary"],
        "protection": best["protection"],
        "moved": moved,
        "objective": {"v_now": round(best["v_now"], 4), "v_fail": round(best["v_fail"], 4), "cost": round(best["cost"], 3)},
        "current": {"v_now": round(cur_eval["v_now"], 4), "v_fail": round(cur_eval["v_fail"], 4), "cost": round(cur_eval["cost"], 3)},
        "predicted": {
            "intents": [r.to_dict() for r in results],
            "pairs": {p: {"path": s.path, "rtt_p50": s.rtt.get("p50"), "rtt_p95": s.rtt.get("p95"), "loss_pct": s.loss_pct,
                          "loss_source": s.loss_source, "offered_mbps": s.offered_mbps, "rx_mbps": s.rx_mbps} for p, s in view.pairs.items()},
            "link_util": {l: round(u, 4) for l, u in view.link_util.items()},
            "scenarios": scen_out,
        },
        "alternatives": [
            {"primary": e["primary"], "v_now": round(e["v_now"], 4), "v_fail": round(e["v_fail"], 4), "cost": round(e["cost"], 3),
             "is_current": e["primary"] == current}
            for e in evaluated[:6]
        ],
        "search": {
            "method": method, "space": space, "primaries_ranked": len(ranked), "primaries_kept": len(keep),
            "scenarios": len(scenarios), "evaluations": ev.n, "wall_s": round(time.perf_counter() - t0, 3),
            "pruned_candidates": {p: len(all_cands[p]) - len(cands[p]) for p in pairs if len(cands[p]) < len(all_cands[p])},
        },
    }
