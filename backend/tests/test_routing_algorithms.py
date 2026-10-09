"""Our own Dijkstra and Yen implementations, checked against networkx's reference ones."""

import itertools
import math
import random

import networkx as nx
import pytest

from netvista.routing import build_graph, dijkstra, k_shortest_paths, path_cost, shortest_path
from netvista.topology import load_topology
from conftest import TOPO_DIR


def random_graph(seed: int, n: int = 12, p: float = 0.3) -> nx.Graph:
    rng = random.Random(seed)
    g = nx.gnp_random_graph(n, p, seed=seed)
    for u, v in g.edges:
        g.edges[u, v]["delay_ms"] = rng.randint(1, 20)
    return g


@pytest.mark.parametrize("seed", range(25))
def test_dijkstra_distances_match_networkx(seed):
    g = random_graph(seed)
    dist, _ = dijkstra(g, 0, "delay_ms")
    ref = nx.single_source_dijkstra_path_length(g, 0, weight="delay_ms")
    assert set(dist) == set(ref)
    for v in ref:
        assert dist[v] == pytest.approx(ref[v])


@pytest.mark.parametrize("seed", range(25))
def test_shortest_path_is_valid_and_optimal(seed):
    g = random_graph(seed)
    for t in g.nodes:
        cost, path = shortest_path(g, 0, t, "delay_ms")
        if not nx.has_path(g, 0, t):
            assert path is None and math.isinf(cost)
            continue
        assert path[0] == 0 and path[-1] == t
        assert all(g.has_edge(u, v) for u, v in zip(path, path[1:]))
        assert path_cost(g, path) == pytest.approx(cost)
        assert cost == pytest.approx(nx.dijkstra_path_length(g, 0, t, weight="delay_ms"))


def test_tie_break_is_deterministic():
    g = nx.Graph()
    for u, v in [("s", "a"), ("a", "t"), ("s", "b"), ("b", "t")]:
        g.add_edge(u, v, delay_ms=1)
    paths = {tuple(shortest_path(g, "s", "t")[1]) for _ in range(20)}
    assert paths == {("s", "a", "t")}  # lexicographically smallest of the equal-cost paths


def test_dead_edges_are_avoided_via_callable_weight():
    topo = load_topology(TOPO_DIR / "default.json")
    g = build_graph(topo)
    _, p = shortest_path(g, "c1", "srv1", "delay_ms")
    assert p == ["c1", "sw1", "r1", "r2", "r5", "sw2", "srv1"]
    dead = {"r2-r5"}
    _, p2 = shortest_path(g, "c1", "srv1", lambda u, v, d: None if d["link_id"] in dead else d["delay_ms"])
    assert p2 == ["c1", "sw1", "r1", "r2", "r4", "r5", "sw2", "srv1"]


def test_negative_weight_rejected():
    g = nx.Graph()
    g.add_edge(1, 2, delay_ms=-1)
    with pytest.raises(ValueError):
        dijkstra(g, 1)


@pytest.mark.parametrize("seed", range(15))
def test_yen_matches_networkx_simple_paths(seed):
    g = random_graph(seed, n=9, p=0.4)
    if not nx.has_path(g, 0, 8):
        pytest.skip("disconnected sample")
    ours = k_shortest_paths(g, 0, 8, 6, "delay_ms")
    ref = list(itertools.islice(nx.shortest_simple_paths(g, 0, 8, weight="delay_ms"), 6))
    assert [c for c, _ in ours] == pytest.approx([path_cost(g, p) for p in ref])
    for _, p in ours:
        assert len(p) == len(set(p)), "paths must be loop-free"
    assert len({tuple(p) for _, p in ours}) == len(ours), "paths must be distinct"


def test_yen_on_default_topology_finds_all_six_core_paths():
    topo = load_topology(TOPO_DIR / "default.json")
    paths = k_shortest_paths(build_graph(topo), "c1", "srv1", 10)
    cores = ["-".join(p[2:-2]) for _, p in paths]
    assert cores == ["r1-r2-r5", "r1-r2-r4-r5", "r1-r3-r4-r5", "r1-r3-r5", "r1-r2-r4-r3-r5", "r1-r3-r4-r2-r5"]
    costs = [c for c, _ in paths]
    assert costs == sorted(costs)
