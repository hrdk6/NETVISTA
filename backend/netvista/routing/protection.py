"""Plans and protection: the routing data an intent plan hands to the controller.

A plan (made by assure/planner.py, checked by the twin, approved by the operator or the
autopilot) is

    {"id": "P3", "label": ..., "primary": {pair: path},
     "protection": {scenario: {pair: path}}}

    scenario  "link:r2-r5"      one core link failed
              "node:r2"         one router failed (every failed link touches r2)

In "intent" routing mode the controller pins every flow to its primary path. When links die
it looks up the scenario that explains the dead set and moves the flows to the backup paths
that were computed, and checked against the intents, in advance - like fast reroute with
pre-verified repair paths, instead of a greedy choice under pressure. A failure the plan
does not cover falls back to the adaptive score. When the links come back the flows return
to their primary paths only after the links have stayed up for the wait-to-restore time
(revertive protection with a WTR timer, as in ITU-T G.8031), so a flapping link cannot
drag traffic back and forth.
"""

from __future__ import annotations

from ..topology import Topology


def scenario_key(failed_links: set[str] | frozenset[str], topo: Topology) -> str | None:
    """Name the failure that explains a set of failed core links (None if nothing failed)."""
    if not failed_links:
        return None
    if len(failed_links) == 1:
        return f"link:{next(iter(failed_links))}"
    common = None
    for lid in failed_links:
        ends = {topo.links[lid].a, topo.links[lid].b}
        common = ends if common is None else common & ends
    if common and len(common) == 1:
        return f"node:{next(iter(common))}"
    return "multi:" + "+".join(sorted(failed_links))


def scenario_links(key: str, topo: Topology) -> set[str]:
    """The links a scenario takes down (both ends of a link; every link of a router)."""
    kind, _, ref = key.partition(":")
    if kind == "link":
        return {ref}
    if kind == "node":
        return {l.id for l in topo.links_of(ref)}
    return set(ref.split("+"))


def path_link_ids(path: list[str] | None, topo: Topology) -> set[str]:
    if not path:
        return set()
    out = set()
    for u, v in zip(path, path[1:]):
        l = topo.link_between(u, v)
        if l:
            out.add(l.id)
    return out


def validate_path(path: list[str], src: str, dst: str, topo: Topology) -> None:
    if not path or path[0] != src or path[-1] != dst:
        raise ValueError(f"path must run from {src} to {dst}")
    if len(set(path)) != len(path):
        raise ValueError("path has a loop")
    for u, v in zip(path, path[1:]):
        if topo.link_between(u, v) is None:
            raise ValueError(f"no link between {u} and {v}")
    for n in path[1:-1]:
        if topo.nodes[n].is_host:
            raise ValueError(f"path cannot transit host {n}")


def desired_paths(plan: dict, failed_links: set[str], topo: Topology) -> tuple[str | None, dict[str, list[str] | None]]:
    """Paths the plan wants for the current failure. A pair maps to None when the plan has no
    usable path for it (the caller falls back to the adaptive score)."""
    key = scenario_key(failed_links, topo)
    prot = (plan.get("protection") or {}).get(key, {}) if key else {}
    out: dict[str, list[str] | None] = {}
    for pair, primary in (plan.get("primary") or {}).items():
        path = prot.get(pair, primary)
        out[pair] = None if path_link_ids(path, topo) & failed_links else path
    return key, out
