"""Packet journey: the installed path between two hosts, with each router's live kernel decision.

Shared by the REST API (Packet journey page) and the AI copilot (trace_path tool).
"""

from __future__ import annotations

import time
from typing import Any

from .routing.graph import core_hops


class NoPathError(LookupError):
    pass


def packet_journey(rt, src: str, dst: str) -> dict[str, Any]:
    topo, plan = rt.topo, rt.plan
    if src not in plan.host_ip or dst not in plan.host_ip or src == dst:
        raise ValueError("src and dst must be two different hosts")
    pair, rev = f"{src}>{dst}", f"{dst}>{src}"
    if pair in rt.controller.flows:
        path, table, source = rt.controller.flows[pair].path, rt.installer.tables[pair][0], "policy table (forward)"
    elif rev in rt.controller.flows:
        fp = rt.controller.flows[rev].path
        path, table, source = (list(reversed(fp)) if fp else None), rt.installer.tables[rev][1], "policy table (reverse)"
    else:
        from .routing.dijkstra import shortest_path

        dead = {l for l, a in rt.controller.link_alive.items() if not a}
        _, path = shortest_path(rt.controller.g, src, dst, lambda u, v, d: None if d["link_id"] in dead else d["delay_ms"])
        table, source = "main", "main table (shortest path)"
    if not path:
        raise NoPathError("no path currently installed")
    now = time.time()
    src_ip, dst_ip = plan.host_ip[src], plan.host_ip[dst]
    hops = []
    for i, node in enumerate(path):
        n = topo.nodes[node]
        hop: dict[str, Any] = {"node": node, "type": n.type, "label": n.label}
        if i > 0:
            l_in = topo.link_between(path[i - 1], node)
            hop["in"] = {"link": l_in.id, "intf": plan.intf(l_in.id, node).name, "ip": plan.intf(l_in.id, node).ip}
        if i < len(path) - 1:
            l_out = topo.link_between(node, path[i + 1])
            v = rt.telemetry.link_view(l_out.id, now)
            d = v["ab"] if l_out.a == node else v["ba"]
            hop["out"] = {
                "link": l_out.id, "intf": plan.intf(l_out.id, node).name, "ip": plan.intf(l_out.id, node).ip,
                "health": v["health"], "rtt_ms": v["rtt_ms"], "loss_pct": v["loss_pct"],
                "util": d["util"], "bps": d["bps"], "pps": d["pps"], "drops_ps": d["drops_ps"],
                "cfg": v["cfg"], "probe_span": v["probe_span"],
            }
        if n.type == "router" and "in" in hop:
            res = rt.net.node_run(node, ["ip", "route", "get", dst_ip, "from", src_ip, "iif", hop["in"]["intf"]], timeout=3)
            hop["kernel_decision"] = (res.stdout or res.stderr).strip().splitlines()[0] if (res.stdout or res.stderr) else ""
            hop["table_routes"] = rt.installer.show_routes(node, table)
        hops.append(hop)
    core = [lid for _, _, lid in core_hops(topo, path)]
    return {"src": src, "dst": dst, "src_ip": src_ip, "dst_ip": dst_ip, "path": path, "core_links": core,
            "table": table, "route_source": source, "hops": hops, "t": now}
