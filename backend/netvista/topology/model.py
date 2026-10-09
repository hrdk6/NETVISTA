"""Topology model loaded from JSON.

The same Topology object feeds the Mininet builder, the routing graph and the SimPy
simulator, so live and simulated networks can never drift apart structurally.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NODE_TYPES = ("router", "switch", "client", "server")
HOST_TYPES = ("client", "server")
# Linux interface names are limited to 15 chars; ours are "<node>-eth<N>", so node ids stay short.
_ID_RE = re.compile(r"^[a-z][a-z0-9]{0,9}$")


class TopologyError(ValueError):
    """Raised when a topology JSON file is structurally invalid."""


@dataclass(frozen=True)
class NodeSpec:
    id: str
    type: str
    label: str
    x: float
    y: float

    @property
    def is_host(self) -> bool:
        return self.type in HOST_TYPES


@dataclass(frozen=True)
class LinkSpec:
    id: str
    a: str
    b: str
    bw_mbps: float
    delay_ms: float
    jitter_ms: float = 0.0
    loss_pct: float = 0.0
    queue_pkts: int = 1000

    def other(self, node: str) -> str:
        if node == self.a:
            return self.b
        if node == self.b:
            return self.a
        raise KeyError(f"{node} is not an endpoint of link {self.id}")

    def params(self) -> dict[str, float]:
        return {
            "bw_mbps": self.bw_mbps,
            "delay_ms": self.delay_ms,
            "jitter_ms": self.jitter_ms,
            "loss_pct": self.loss_pct,
            "queue_pkts": self.queue_pkts,
        }


@dataclass(frozen=True)
class TrafficSpec:
    src: str
    dst: str
    rate_mbps: float


@dataclass
class Topology:
    name: str
    description: str
    nodes: dict[str, NodeSpec]
    links: dict[str, LinkSpec]
    traffic: list[TrafficSpec] = field(default_factory=list)
    source: dict[str, Any] = field(default_factory=dict)

    # ---- queries -------------------------------------------------------
    def of_type(self, *types: str) -> list[str]:
        return [n.id for n in self.nodes.values() if n.type in types]

    @property
    def routers(self) -> list[str]:
        return self.of_type("router")

    @property
    def switches(self) -> list[str]:
        return self.of_type("switch")

    @property
    def hosts(self) -> list[str]:
        return self.of_type(*HOST_TYPES)

    @property
    def clients(self) -> list[str]:
        return self.of_type("client")

    @property
    def servers(self) -> list[str]:
        return self.of_type("server")

    def links_of(self, node: str) -> list[LinkSpec]:
        """Links touching `node`, in JSON order (this order defines interface numbering)."""
        return [l for l in self.links.values() if node in (l.a, l.b)]

    def neighbors(self, node: str) -> list[str]:
        return [l.other(node) for l in self.links_of(node)]

    def link_between(self, u: str, v: str) -> LinkSpec | None:
        for l in self.links.values():
            if {l.a, l.b} == {u, v}:
                return l
        return None

    def flow_pairs(self) -> list[tuple[str, str]]:
        """Every client->server pair is a routed 'flow' the controller manages."""
        return [(c, s) for c in self.clients for s in self.servers]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "nodes": [vars(n) for n in self.nodes.values()],
            "links": [vars(l) for l in self.links.values()],
            "traffic": [vars(t) for t in self.traffic],
        }


def _num(d: dict[str, Any], key: str, default: float | None, lo: float, hi: float, where: str) -> float:
    if key not in d:
        if default is None:
            raise TopologyError(f"{where}: missing '{key}'")
        return default
    v = d[key]
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise TopologyError(f"{where}: '{key}' must be a number")
    if not lo <= v <= hi:
        raise TopologyError(f"{where}: '{key}'={v} out of range [{lo}, {hi}]")
    return float(v)


def topology_from_dict(data: dict[str, Any]) -> Topology:
    if not isinstance(data, dict):
        raise TopologyError("topology must be a JSON object")
    defaults = data.get("defaults", {})
    nodes: dict[str, NodeSpec] = {}
    for i, raw in enumerate(data.get("nodes", [])):
        where = f"nodes[{i}]"
        nid = raw.get("id")
        if not isinstance(nid, str) or not _ID_RE.match(nid):
            raise TopologyError(f"{where}: id must match {_ID_RE.pattern} (got {nid!r})")
        if nid in nodes:
            raise TopologyError(f"{where}: duplicate node id {nid!r}")
        ntype = raw.get("type")
        if ntype not in NODE_TYPES:
            raise TopologyError(f"{where}: type must be one of {NODE_TYPES}")
        nodes[nid] = NodeSpec(
            id=nid,
            type=ntype,
            label=str(raw.get("label", nid)),
            x=float(raw.get("x", 0.0)),
            y=float(raw.get("y", 0.0)),
        )
    if not nodes:
        raise TopologyError("topology has no nodes")

    links: dict[str, LinkSpec] = {}
    pairs: set[frozenset[str]] = set()
    for i, raw in enumerate(data.get("links", [])):
        where = f"links[{i}]"
        a, b = raw.get("a"), raw.get("b")
        if a not in nodes or b not in nodes:
            raise TopologyError(f"{where}: unknown endpoint(s) {a!r}, {b!r}")
        if a == b:
            raise TopologyError(f"{where}: self-loop on {a}")
        pair = frozenset((a, b))
        if pair in pairs:
            raise TopologyError(f"{where}: duplicate link between {a} and {b}")
        pairs.add(pair)
        lid = raw.get("id", f"{a}-{b}")
        if lid in links:
            raise TopologyError(f"{where}: duplicate link id {lid!r}")
        links[lid] = LinkSpec(
            id=lid,
            a=a,
            b=b,
            bw_mbps=_num(raw, "bw_mbps", defaults.get("bw_mbps", 100.0), 0.1, 1000, where),
            delay_ms=_num(raw, "delay_ms", defaults.get("delay_ms", 1.0), 0, 1000, where),
            jitter_ms=_num(raw, "jitter_ms", defaults.get("jitter_ms", 0.0), 0, 500, where),
            loss_pct=_num(raw, "loss_pct", defaults.get("loss_pct", 0.0), 0, 100, where),
            queue_pkts=int(_num(raw, "queue_pkts", defaults.get("queue_pkts", 1000), 10, 100000, where)),
        )

    topo = Topology(
        name=str(data.get("name", "unnamed")),
        description=str(data.get("description", "")),
        nodes=nodes,
        links=links,
        source=data,
    )
    for i, raw in enumerate(data.get("traffic", [])):
        src, dst = raw.get("src"), raw.get("dst")
        if src not in topo.clients or dst not in topo.servers:
            raise TopologyError(f"traffic[{i}]: src must be a client and dst a server")
        topo.traffic.append(TrafficSpec(src, dst, _num(raw, "rate_mbps", None, 0.01, 1000, f"traffic[{i}]")))
    _validate_structure(topo)
    return topo


def _validate_structure(topo: Topology) -> None:
    for n in topo.nodes.values():
        nbrs = topo.neighbors(n.id)
        if n.is_host:
            if len(nbrs) != 1:
                raise TopologyError(f"host {n.id} must have exactly one link (has {len(nbrs)})")
            if topo.nodes[nbrs[0]].type not in ("router", "switch"):
                raise TopologyError(f"host {n.id} must attach to a router or switch")
        elif n.type == "switch":
            routers = [x for x in nbrs if topo.nodes[x].type == "router"]
            if len(routers) != 1:
                raise TopologyError(f"switch {n.id} must have exactly one router (its LAN gateway), has {len(routers)}")
            if any(topo.nodes[x].type == "switch" for x in nbrs):
                raise TopologyError(f"switch {n.id}: switch-to-switch links are not supported")
        if len(topo.links_of(n.id)) > 32:
            raise TopologyError(f"node {n.id} has too many links")
    # connectivity
    seen: set[str] = set()
    stack = [next(iter(topo.nodes))]
    while stack:
        u = stack.pop()
        if u in seen:
            continue
        seen.add(u)
        stack.extend(topo.neighbors(u))
    if len(seen) != len(topo.nodes):
        missing = sorted(set(topo.nodes) - seen)
        raise TopologyError(f"topology is not connected; unreachable: {missing}")
    if not topo.clients or not topo.servers:
        raise TopologyError("topology needs at least one client and one server")


def load_topology(path: str | Path) -> Topology:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TopologyError(f"{p}: invalid JSON: {e}") from e
    return topology_from_dict(data)
