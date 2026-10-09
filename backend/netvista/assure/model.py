"""Shared machinery for failure analysis and planning: scenarios, predicted views, routing reactions.

Everything here is a pure function of picklable inputs (the topology, a twin config, intents),
so the heavy searches run in the simulator's worker process and never stall the probes.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from ..routing.protection import desired_paths, path_link_ids
from ..simulator.fluid import FluidEval, FluidNet, demand_of, fluid_simulate
from ..simulator.whatif import predict_with
from .intents import PairState, View, edge_routers


@dataclass(frozen=True)
class Scenario:
    id: str  # link:r2-r5 | node:r2 | double:r1-r2+r3-r5
    kind: str  # link | node | double
    label: str
    links: frozenset[str]  # every link the failure takes down
    nodes: frozenset[str]  # failed routers

    @property
    def failure_class(self) -> str:
        """Which `protect` setting must cover it: a router failure is 'node', anything else 'link'."""
        return "node" if self.kind == "node" else "link"

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label, "links": sorted(self.links), "nodes": sorted(self.nodes)}


def core_links(topo) -> list[str]:
    return [l.id for l in topo.links.values() if topo.nodes[l.a].type == "router" and topo.nodes[l.b].type == "router"]


def single_scenarios(topo, kinds: tuple[str, ...] = ("link", "node")) -> list[Scenario]:
    out = []
    if "link" in kinds:
        for lid in core_links(topo):
            out.append(Scenario(f"link:{lid}", "link", f"Link {lid} fails", frozenset({lid}), frozenset()))
    if "node" in kinds:
        for r in topo.routers:
            out.append(Scenario(f"node:{r}", "node", f"Router {r} fails", frozenset(l.id for l in topo.links_of(r)), frozenset({r})))
    return out


def double_link_scenarios(topo) -> list[Scenario]:
    return [
        Scenario(f"double:{a}+{b}", "double", f"Links {a} and {b} fail together", frozenset({a, b}), frozenset())
        for a, b in combinations(core_links(topo), 2)
    ]


def scenario_by_id(topo, sid: str) -> Scenario:
    kind, _, ref = sid.partition(":")
    if kind == "link" and ref in topo.links:
        return Scenario(sid, "link", f"Link {ref} fails", frozenset({ref}), frozenset())
    if kind == "node" and ref in topo.nodes:
        return Scenario(sid, "node", f"Router {ref} fails", frozenset(l.id for l in topo.links_of(ref)), frozenset({ref}))
    if kind == "double":
        a, _, b = ref.partition("+")
        if a in topo.links and b in topo.links:
            return Scenario(sid, "double", f"Links {a} and {b} fail together", frozenset({a, b}), frozenset())
    raise ValueError(f"unknown failure scenario {sid!r}")


def scenario_changes(sc: Scenario) -> list[dict]:
    """The chaos-lab changes that create this failure on the live network."""
    if sc.kind == "node":
        return [{"kind": "node_down", "target": next(iter(sc.nodes)), "params": {}}]
    return [{"kind": "link_down", "target": l, "params": {}} for l in sorted(sc.links)]


def with_failure(cfg: dict, sc: Scenario | None) -> dict:
    if sc is None:
        return cfg
    links = {lid: (dict(l, up=False) if lid in sc.links else l) for lid, l in cfg["links"].items()}
    return {**cfg, "links": links}


def crosses(path: list[str] | None, sc: Scenario, topo) -> bool:
    if not path:
        return False
    return bool(path_link_ids(path, topo) & sc.links) or bool(set(path) & sc.nodes)


def unreachable_pairs(topo, sc: Scenario, pairs: list[str]) -> set[str]:
    """Pairs that no routing can reconnect: the failure cut them off (single points of failure)."""
    adj: dict[str, set[str]] = {n: set() for n in topo.nodes}
    for l in topo.links.values():
        if l.id in sc.links or l.a in sc.nodes or l.b in sc.nodes:
            continue
        adj[l.a].add(l.b)
        adj[l.b].add(l.a)
    out = set()
    for pair in pairs:
        src, dst = pair.split(">")
        seen, stack = {src}, [src]
        while stack:
            u = stack.pop()
            for v in adj[u]:
                if v not in seen and not (topo.nodes[v].is_host and v != dst):
                    seen.add(v)
                    stack.append(v)
        if dst not in seen:
            out.add(pair)
    return out


def spof_elements(topo, pairs: list[str]) -> list[dict]:
    """Elements whose single failure disconnects some pair (structural, no routing can help)."""
    out = []
    for sc in single_scenarios(topo):
        cut = unreachable_pairs(topo, sc, pairs)
        if cut:
            out.append({"scenario": sc.id, "label": sc.label, "pairs": sorted(cut)})
    gw = edge_routers(topo)
    return [dict(o, edge=o["scenario"].split(":", 1)[1] in gw) for o in out]


# ---------------------------------------------------------------------------- views
def predicted_view(net: FluidNet, ev: FluidEval, paths: dict[str, list[str] | None], rtt_margin: float = 0.0) -> View:
    pairs = {}
    for pair, pe in ev.pairs.items():
        path = paths.get(pair)
        offered = pe.offered_mbps
        if offered > 0:
            loss, src = pe.loss_pct, "data"
        else:
            loss, src = pe.probe_loss_pct, "probe"
        pairs[pair] = PairState(
            path=list(path) if path else None, alive=bool(path) and pe.alive,
            rtt={"p50": pe.rtt_p50, "p95": pe.rtt_p95, "p99": pe.rtt_p99},
            loss_pct=loss, loss_source=src, offered_mbps=offered, rx_mbps=pe.rx_mbps if offered > 0 else None,
        )
    util: dict[str, float] = {}
    up: dict[str, bool] = {}
    rate: dict[tuple[str, str], float] = {}
    cap: dict[tuple[str, str], float] = {}
    for hop, h in net.hops.items():
        st = ev.hops.get(hop)
        acc = st.accepted_bps if st else 0.0
        rate[hop] = acc
        cap[hop] = h.cap_bps if h.up else 0.0
        util[h.link_id] = max(util.get(h.link_id, 0.0), acc / h.cap_bps if h.cap_bps else 0.0)
        up[h.link_id] = h.up
    return View("predicted", pairs, util, up, rate, cap, rtt_margin=rtt_margin, wire_factor=net.wire_factor)


def evaluate_paths(cfg: dict, paths: dict[str, list[str] | None], rtt_margin: float = 0.0, net: FluidNet | None = None) -> tuple[View, FluidEval]:
    net = net or FluidNet(cfg["links"], cfg.get("overhead"), int(cfg.get("payload_bytes", 1200)), int(cfg.get("wire_bytes", 1242)),
                          float(cfg.get("probe_timeout_s", 2.0)))
    ev = net.evaluate(paths, demand_of(cfg))
    return predicted_view(net, ev, paths, rtt_margin), ev


# ---------------------------------------------------------------------------- routing reactions
def react(cfg: dict, sc: Scenario | None, mode: str, plan: dict | None, topo) -> dict:
    """Which paths the controller ends up on after failure `sc` (fluid prediction).

    static    flows stay on their Dijkstra paths (and lose traffic on a dead link)
    adaptive  the controller's own scoring rule, round by round (herd guard as configured);
              'first' is the assignment right after the fail-over, before re-balancing
    intent    the plan's pre-planned backups; the adaptive rule for flows the plan leaves open
    """
    cfg_f = with_failure(cfg, sc)
    pairs = list(cfg["pairs"])
    current = {p: list(v["path"]) for p, v in cfg["pairs"].items()}
    if mode == "static":
        final = {p: list(v["static_path"]) for p, v in cfg["pairs"].items()}
        return {"paths": final, "first": final, "rounds": 0, "planned": False}
    if mode == "intent" and plan:
        failed = set(sc.links & set(core_links(topo))) if sc else set()
        _, want = desired_paths(plan, failed, topo)
        if all(want.get(p) for p in pairs):
            return {"paths": want, "first": want, "rounds": 0, "planned": True}
        # unplanned: the adaptive rule decides the open flows; the others stay pinned to the plan
        cfg_f = {**cfg_f, "pairs": {
            p: (dict(v, path=want[p], candidates=[want[p]]) if want.get(p) else dict(v, path=current[p]))
            for p, v in cfg_f["pairs"].items()
        }}
    out = predict_with({**cfg_f, "mode": "adaptive"}, lambda c, paths, d, s: fluid_simulate(c, paths))
    final = {p: v["path"] for p, v in out["pairs"].items()}
    rounds = out["routing_rounds"]
    first = dict(final)
    if rounds:
        first = {p: (rounds[0].get(p, {}).get("chosen") or cfg_f["pairs"][p]["path"]) for p in pairs}
    return {"paths": final, "first": first, "rounds": len(rounds), "planned": False}
