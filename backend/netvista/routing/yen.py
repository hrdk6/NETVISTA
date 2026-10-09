"""Yen's algorithm for the K shortest loop-free paths, built on our own Dijkstra.

Used to enumerate *candidate* paths; the network-aware score then picks among them.
(The score uses max-utilisation and multiplicative loss, which are not additive edge
weights, so it cannot be optimised by Dijkstra directly - hence candidates + scoring.)
"""

from __future__ import annotations

import heapq
import itertools
import math
from typing import Hashable

import networkx as nx

from .dijkstra import Weight, _weight_fn, shortest_path


def _cost(g: nx.Graph, path: list, weight: Weight) -> float:
    wf = _weight_fn(weight)
    total = 0.0
    for u, v in zip(path, path[1:]):
        w = wf(u, v, g.edges[u, v])
        if w is None or math.isinf(w):
            return math.inf
        total += w
    return total


def k_shortest_paths(g: nx.Graph, source: Hashable, target: Hashable, k: int, weight: Weight = "delay_ms") -> list[tuple[float, list]]:
    """Return up to k (cost, path) tuples in non-decreasing cost order."""
    if k <= 0:
        return []
    cost, first = shortest_path(g, source, target, weight)
    if first is None:
        return []
    found: list[tuple[float, list]] = [(cost, first)]
    seen = {tuple(first)}
    tie = itertools.count()
    candidates: list[tuple[float, tuple, int, list]] = []
    while len(found) < k:
        last = found[-1][1]
        for i in range(len(last) - 1):
            spur = last[i]
            root = last[: i + 1]
            banned_edges = set()
            for _, p in found:
                if len(p) > i and p[: i + 1] == root:
                    banned_edges.add(frozenset((p[i], p[i + 1])))
            banned_nodes = set(root[:-1])
            _, spur_path = shortest_path(g, spur, target, weight, banned_nodes, banned_edges)
            if spur_path is None:
                continue
            total = root[:-1] + spur_path
            key = tuple(total)
            if key in seen:
                continue
            seen.add(key)
            c = _cost(g, total, weight)
            heapq.heappush(candidates, (c, tuple(map(str, total)), next(tie), total))
        if not candidates:
            break
        c, _, _, p = heapq.heappop(candidates)
        found.append((c, p))
    return found
