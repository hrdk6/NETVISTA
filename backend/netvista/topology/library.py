"""Topology library and design checks for the designer page.

Built-in topologies live in topologies/, designs saved from the UI in topologies/user/.
`design_report` is the design-time verification run on every edit, before anything boots:
structural validity (the same rules the emulation enforces), the derived address plan, path
diversity per flow (K-shortest candidates and link-disjoint paths), single points of failure,
and whether the default traffic fits.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import networkx as nx

from ..config import REPO_ROOT
from .addressing import build_address_plan
from .model import Topology, TopologyError, topology_from_dict

BUILTIN_DIR = REPO_ROOT / "topologies"
USER_DIR = BUILTIN_DIR / "user"
MAX_NODES = 40
MAX_LINKS = 80


def _rel(p: Path) -> str:
    return p.relative_to(BUILTIN_DIR).as_posix()


def resolve(file: str) -> Path:
    """A library file name -> path, refusing anything outside the topology folders."""
    p = (BUILTIN_DIR / file).resolve()
    if p.suffix != ".json" or not (p.parent == BUILTIN_DIR.resolve() or p.parent == USER_DIR.resolve()):
        raise ValueError(f"not a topology in the library: {file!r}")
    if not p.exists():
        raise ValueError(f"no such topology: {file!r}")
    return p


def list_topologies(current: Path | None) -> list[dict[str, Any]]:
    out = []
    files = sorted(BUILTIN_DIR.glob("*.json")) + sorted(USER_DIR.glob("*.json"))
    for f in files:
        row: dict[str, Any] = {"file": _rel(f), "user": f.parent == USER_DIR, "current": bool(current and f.resolve() == current.resolve())}
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            t = topology_from_dict(raw)
            row.update(name=t.name, description=t.description, nodes=len(t.nodes), links=len(t.links), routers=len(t.routers),
                       hosts=len(t.hosts), flows=len(t.flow_pairs()))
        except (OSError, ValueError) as e:
            row.update(name=f.stem, description="", error=str(e))
        out.append(row)
    return out


def read(file: str) -> dict[str, Any]:
    return json.loads(resolve(file).read_text(encoding="utf-8"))


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "design"


def save(raw: dict[str, Any]) -> str:
    topo = topology_from_dict(raw)  # never save something that cannot boot
    _limits(topo)
    USER_DIR.mkdir(parents=True, exist_ok=True)
    p = USER_DIR / f"{slug(topo.name)}.json"
    p.write_text(json.dumps(clean(raw), indent=2), encoding="utf-8")
    return _rel(p)


def clean(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields the topology format defines (the editor may carry UI state)."""
    nodes = [{k: n[k] for k in ("id", "type", "label", "x", "y") if k in n} for n in raw.get("nodes", [])]
    links = [{k: l[k] for k in ("a", "b", "bw_mbps", "delay_ms", "jitter_ms", "loss_pct", "queue_pkts") if k in l} for l in raw.get("links", [])]
    traffic = [{k: t[k] for k in ("src", "dst", "rate_mbps") if k in t} for t in raw.get("traffic", [])]
    return {"name": raw.get("name", "design"), "description": raw.get("description", ""), "nodes": nodes, "links": links, "traffic": traffic}


def _limits(topo: Topology) -> None:
    if len(topo.nodes) > MAX_NODES:
        raise TopologyError(f"{len(topo.nodes)} nodes: the emulation is sized for at most {MAX_NODES}")
    if len(topo.links) > MAX_LINKS:
        raise TopologyError(f"{len(topo.links)} links: at most {MAX_LINKS}")


def design_report(raw: dict[str, Any]) -> dict[str, Any]:
    """Everything worth knowing about a design before it boots."""
    try:
        topo = topology_from_dict(raw)
        _limits(topo)
        plan = build_address_plan(topo)
    except (TopologyError, ValueError, KeyError) as e:
        return {"ok": False, "errors": [str(e)], "warnings": []}

    from ..assure.model import spof_elements
    from ..routing.graph import build_graph
    from ..routing.yen import k_shortest_paths

    g = build_graph(topo)
    core = nx.Graph()
    core.add_nodes_from(topo.routers)
    for l in topo.links.values():
        if topo.nodes[l.a].type == "router" and topo.nodes[l.b].type == "router":
            core.add_edge(l.a, l.b)
    warnings: list[str] = []
    flows = []
    for c, s in topo.flow_pairs():
        cands = [p for _, p in k_shortest_paths(g, c, s, 8, "delay_ms")]
        # link-disjoint paths between the two sites' gateway routers (a host's access link is
        # single by definition, so diversity is a property of the routed core)
        ga, gb = plan.host_gateway[c], plan.host_gateway[s]
        disjoint: int | None = None
        if ga != gb:
            try:
                disjoint = len(list(nx.edge_disjoint_paths(core, ga, gb)))
            except nx.NetworkXException:
                disjoint = 0
        sp = cands[0] if cands else None
        rtt = 2 * sum(topo.link_between(u, v).delay_ms for u, v in zip(sp, sp[1:])) if sp else None
        bottleneck = min((topo.link_between(u, v).bw_mbps for u, v in zip(sp, sp[1:])), default=None) if sp else None
        flows.append({"pair": f"{c}>{s}", "candidates": len(cands), "disjoint_paths": disjoint, "design_rtt_ms": rtt,
                      "shortest": [n for n in sp if topo.nodes[n].type == "router"] if sp else None, "bottleneck_mbps": bottleneck})
        if len(cands) <= 1:
            warnings.append(f"{c} → {s} has a single path: any failure on it cuts the flow off")
    pairs = [f"{c}>{s}" for c, s in topo.flow_pairs()]
    spofs = spof_elements(topo, pairs)
    core_spofs = [s for s in spofs if not s["edge"]]
    for s in core_spofs:
        warnings.append(f"{s['label']}: cuts off {', '.join(p.replace('>', ' → ') for p in s['pairs'])} (a core single point of failure)")
    if not topo.traffic:
        warnings.append("no default traffic profile: Start traffic will have nothing to start")
    for t in topo.traffic:
        f = next(x for x in flows if x["pair"] == f"{t.src}>{t.dst}")
        if f["bottleneck_mbps"] and t.rate_mbps * 1242 / 1200 > f["bottleneck_mbps"]:
            warnings.append(f"default traffic {t.src} → {t.dst} ({t.rate_mbps:g} Mbit/s) exceeds its shortest path's bottleneck ({f['bottleneck_mbps']:g} Mbit/s)")
    longest = max((len(i.name) for ends in plan.link_intfs.values() for i in ends.values()), default=0)
    return {
        "ok": True, "errors": [], "warnings": warnings,
        "name": topo.name,
        "stats": {
            "routers": len(topo.routers), "switches": len(topo.switches), "clients": len(topo.clients), "servers": len(topo.servers),
            "links": len(topo.links), "core_links": sum(1 for l in topo.links.values() if topo.nodes[l.a].type == "router" and topo.nodes[l.b].type == "router"),
            "flows": len(pairs), "longest_interface_name": longest,
            "probe_streams": len(pairs) + len(topo.hosts) + sum(1 for l in topo.links.values() if topo.nodes[l.a].type == "router" and topo.nodes[l.b].type == "router"),
        },
        "flows": flows,
        "spofs": spofs,
        "subnets": [{"cidr": s.cidr, "kind": s.kind, "routers": s.routers, "hosts": s.hosts} for s in plan.subnets],
    }
