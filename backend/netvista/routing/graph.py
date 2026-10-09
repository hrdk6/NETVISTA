"""networkx graph built from the topology (networkx is used only as the graph container)."""

from __future__ import annotations

import networkx as nx

from ..topology import Topology


def build_graph(topo: Topology) -> nx.Graph:
    g = nx.Graph()
    for n in topo.nodes.values():
        g.add_node(n.id, type=n.type)
    for l in topo.links.values():
        g.add_edge(l.a, l.b, link_id=l.id, delay_ms=l.delay_ms, bw_mbps=l.bw_mbps)
    return g


def path_links(g: nx.Graph, path: list[str]) -> list[str]:
    return [g.edges[u, v]["link_id"] for u, v in zip(path, path[1:])]


def core_hops(topo: Topology, path: list[str]) -> list[tuple[str, str, str]]:
    """Directed router->router hops of a path as (u, v, link_id)."""
    out = []
    for u, v in zip(path, path[1:]):
        if topo.nodes[u].type == "router" and topo.nodes[v].type == "router":
            link = topo.link_between(u, v)
            assert link is not None
            out.append((u, v, link.id))
    return out


def path_cost(g: nx.Graph, path: list[str], weight: str = "delay_ms") -> float:
    return sum(g.edges[u, v][weight] for u, v in zip(path, path[1:]))
