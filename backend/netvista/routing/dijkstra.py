"""Dijkstra's shortest path, implemented from scratch (binary heap, O((V+E) log V)).

networkx is only the graph container; no networkx path algorithm is used here.
Ties are broken deterministically (lexicographically smallest node sequence wins), so the
same topology always yields the same "static" routes - important for reproducible demos.
"""

from __future__ import annotations

import heapq
import math
from typing import Callable, Hashable, Iterable, Union

import networkx as nx

Weight = Union[str, Callable[[Hashable, Hashable, dict], float | None]]


def _weight_fn(weight: Weight) -> Callable[[Hashable, Hashable, dict], float | None]:
    if callable(weight):
        return weight
    return lambda u, v, d: d.get(weight, 1.0)


def dijkstra(
    g: nx.Graph,
    source: Hashable,
    weight: Weight = "delay_ms",
    banned_nodes: Iterable[Hashable] = (),
    banned_edges: Iterable[frozenset] = (),
) -> tuple[dict[Hashable, float], dict[Hashable, Hashable]]:
    """Single-source shortest paths. Returns (dist, prev).

    `weight` may be an edge attribute name or f(u, v, data) -> cost; a cost of None or inf
    makes the edge unusable (e.g. a link that is down). Negative costs are rejected.
    """
    wf = _weight_fn(weight)
    banned_n = set(banned_nodes)
    banned_e = set(banned_edges)
    if source in banned_n:
        return {}, {}
    dist: dict[Hashable, float] = {source: 0.0}
    # path label used only for deterministic tie-breaking
    label: dict[Hashable, tuple] = {source: (str(source),)}
    prev: dict[Hashable, Hashable] = {}
    done: set[Hashable] = set()
    heap: list[tuple[float, tuple, Hashable]] = [(0.0, label[source], source)]
    while heap:
        d, lab, u = heapq.heappop(heap)
        if u in done or d > dist.get(u, math.inf) or lab != label.get(u):
            continue
        done.add(u)
        for v, data in g[u].items():
            if v in banned_n or v in done or frozenset((u, v)) in banned_e:
                continue
            w = wf(u, v, data)
            if w is None or math.isinf(w):
                continue
            if w < 0:
                raise ValueError(f"negative edge weight on {u}-{v}")
            nd = d + w
            nlab = lab + (str(v),)
            old = dist.get(v, math.inf)
            if nd < old - 1e-12 or (abs(nd - old) <= 1e-12 and nlab < label[v]):
                dist[v] = nd
                label[v] = nlab
                prev[v] = u
                heapq.heappush(heap, (nd, nlab, v))
    return dist, prev


def shortest_path(
    g: nx.Graph,
    source: Hashable,
    target: Hashable,
    weight: Weight = "delay_ms",
    banned_nodes: Iterable[Hashable] = (),
    banned_edges: Iterable[frozenset] = (),
) -> tuple[float, list | None]:
    """(cost, path) from source to target, or (inf, None) if unreachable."""
    if source == target:
        return 0.0, [source]
    dist, prev = dijkstra(g, source, weight, banned_nodes, banned_edges)
    if target not in dist:
        return math.inf, None
    path = [target]
    while path[-1] != source:
        path.append(prev[path[-1]])
    path.reverse()
    return dist[target], path
