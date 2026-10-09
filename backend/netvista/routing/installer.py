"""Install computed paths into the emulated routers as Linux policy routes.

Why Linux routes and not OVS flow rules: our routers are Linux network namespaces doing
real IP forwarding, so routes are the native mechanism - visible with `ip route`, shown hop
by hop by `traceroute`, and needing no OpenFlow controller (Ryu/OS-Ken are unmaintained on
Python 3.12). OVS stays where it fits: as the L2 site switches.

Per-flow paths use policy routing:
    ip rule  add from <client>/32 to <server>/32 lookup <T>      (on every router, once)
    ip route replace <server>/32 via <next hop> table <T>        (only on routers on the path)
The reverse direction uses table T+1 with the reversed path, so probes measure one path.
Traffic not covered by a rule (traceroute replies, router-to-router probes) uses the main
table, which holds shortest-path routes to every subnet.
"""

from __future__ import annotations

import logging
import time

import networkx as nx

from ..emulation import nsexec
from ..topology import AddressPlan, Topology
from .dijkstra import shortest_path

log = logging.getLogger(__name__)


class RouteInstaller:
    def __init__(self, topo: Topology, plan: AddressPlan, pids: dict[str, int | None]) -> None:
        self.topo = topo
        self.plan = plan
        self.pids = pids
        self.pairs = topo.flow_pairs()
        self.tables: dict[str, tuple[int, int]] = {
            f"{c}>{s}": (100 + 2 * i, 101 + 2 * i) for i, (c, s) in enumerate(self.pairs)
        }
        self.commands_run = 0

    # ------------------------------------------------------------------ helpers
    def _hop_route(self, u: str, v: str, dst_ip: str, table: int | None) -> str | None:
        """Route on router u that sends traffic for dst_ip to neighbour v (None if v not a router)."""
        if self.topo.nodes[u].type != "router" or self.topo.nodes[v].type != "router":
            return None
        link = self.topo.link_between(u, v)
        assert link is not None
        nh = self.plan.router_ip_on_link(link.id, v)
        dev = self.plan.intf(link.id, u).name
        t = f" table {table}" if table is not None else ""
        return f"route replace {dst_ip}/32 via {nh} dev {dev}{t}"

    def flow_lines(self, path: list[str], dst_ip: str, table: int) -> dict[str, str]:
        out = {}
        for u, v in zip(path, path[1:]):
            line = self._hop_route(u, v, dst_ip, table)
            if line:
                out[u] = line
        return out

    def _run(self, router: str, lines: list[str]) -> None:
        if not lines:
            return
        r = nsexec.ip_batch(self.pids[router], lines)
        self.commands_run += len(lines)
        if r.returncode != 0 and r.stderr.strip():
            log.debug("ip batch on %s: %s", router, r.stderr.strip())

    # ------------------------------------------------------------------ setup
    def setup_rules(self) -> None:
        for r in self.topo.routers:
            lines = []
            for (c, s) in self.pairs:
                tf, tr = self.tables[f"{c}>{s}"]
                cip, sip = self.plan.host_ip[c], self.plan.host_ip[s]
                lines += [
                    f"rule del pref {tf}",
                    f"rule del pref {tr}",
                    f"rule add from {cip}/32 to {sip}/32 lookup {tf} pref {tf}",
                    f"rule add from {sip}/32 to {cip}/32 lookup {tr} pref {tr}",
                    f"route flush table {tf}",
                    f"route flush table {tr}",
                ]
            self._run(r, lines)

    # ------------------------------------------------------------------ flows
    def install_flow(self, pair: str, new_path: list[str], old_path: list[str] | None) -> float:
        """Make-before-break install. Returns elapsed seconds.

        Forward routes are written from the destination side back towards the source, so by
        the time the first-hop router switches, every downstream router already knows the
        new path; reverse routes are written in the opposite order for the same reason.
        """
        t0 = time.perf_counter()
        c, s = pair.split(">")
        tf, tr = self.tables[pair]
        fwd = self.flow_lines(new_path, self.plan.host_ip[s], tf)
        rev = self.flow_lines(list(reversed(new_path)), self.plan.host_ip[c], tr)
        routers_fwd = [n for n in new_path if n in fwd]
        for r in reversed(routers_fwd):
            self._run(r, [fwd[r]])
        routers_rev = [n for n in reversed(new_path) if n in rev]
        for r in reversed(routers_rev):
            self._run(r, [rev[r]])
        if old_path:
            stale = [n for n in old_path if self.topo.nodes[n].type == "router" and n not in fwd and n not in rev]
            for r in stale:
                self._run(r, [f"route flush table {tf}", f"route flush table {tr}"])
        return time.perf_counter() - t0

    # ------------------------------------------------------------------ main table
    def main_table_lines(self, g: nx.Graph, dead_links: set[str]) -> dict[str, list[str]]:
        """Shortest-path (by configured delay) routes to every subnet, avoiding dead links."""
        rg = g.subgraph(self.topo.routers).copy()

        def w(u, v, d):
            return None if d["link_id"] in dead_links else d["delay_ms"]

        out: dict[str, list[str]] = {r: [] for r in self.topo.routers}
        for r in self.topo.routers:
            for sub in self.plan.subnets:
                if r in sub.routers:
                    continue
                best: tuple[float, list] | None = None
                for owner in sub.routers:
                    cost, path = shortest_path(rg, r, owner, w)
                    if path and (best is None or cost < best[0]):
                        best = (cost, path)
                if best is None or len(best[1]) < 2:
                    out[r].append(f"route del {sub.cidr}")
                    continue
                nxt = best[1][1]
                link = self.topo.link_between(r, nxt)
                assert link is not None
                nh = self.plan.router_ip_on_link(link.id, nxt)
                dev = self.plan.intf(link.id, r).name
                out[r].append(f"route replace {sub.cidr} via {nh} dev {dev}")
        return out

    def reconcile(self, g: nx.Graph, dead_links: set[str], paths: dict[str, list[str] | None]) -> None:
        """Idempotently re-assert every desired route (the kernel deletes routes whose
        interface went down; this puts them back once the interface is up again)."""
        per_router = self.main_table_lines(g, dead_links)
        for pair, path in paths.items():
            tf, tr = self.tables[pair]
            c, s = pair.split(">")
            if path is None:
                continue
            fwd = self.flow_lines(path, self.plan.host_ip[s], tf)
            rev = self.flow_lines(list(reversed(path)), self.plan.host_ip[c], tr)
            for r in self.topo.routers:
                if r in fwd:
                    per_router[r].append(fwd[r])
                else:
                    per_router[r].append(f"route flush table {tf}")
                if r in rev:
                    per_router[r].append(rev[r])
                else:
                    per_router[r].append(f"route flush table {tr}")
        for r, lines in per_router.items():
            self._run(r, lines)

    # ------------------------------------------------------------------ inspection
    def show_routes(self, router: str, table: int | str = "main") -> list[str]:
        r = nsexec.run(self.pids[router], ["ip", "route", "show", "table", str(table)])
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    def show_rules(self, router: str) -> list[str]:
        r = nsexec.run(self.pids[router], ["ip", "rule", "show"])
        return [ln for ln in r.stdout.splitlines() if ln.strip()]
