"""Fuse probes + counters into per-link / per-node / per-flow views.

Health is always *measured*: a link is "down" because its probes stopped coming back,
not because we know we injected a failure. That is what makes detection time meaningful.
"""

from __future__ import annotations

import math
from typing import Callable

from ..config import Settings
from ..emulation.tc import LinkParams
from ..topology import AddressPlan, Topology
from .counters import CounterPoller
from .health import ProbeStore, ProbeStream, RttStats, percentile

DEGRADED_LOSS_PCT = 1.0
DEGRADED_UTIL = 0.85


def _clean(x: float | None, nd: int = 3) -> float | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(x, nd)


class Telemetry:
    def __init__(
        self,
        topo: Topology,
        plan: AddressPlan,
        settings: Settings,
        store: ProbeStore,
        counters: CounterPoller,
        link_params: Callable[[str], LinkParams],
        link_admin_up: Callable[[str], bool],
    ) -> None:
        self.topo = topo
        self.plan = plan
        self.s = settings
        self.store = store
        self.counters = counters
        self.link_params = link_params
        self.link_admin_up = link_admin_up
        self.link_streams: dict[str, list[str]] = {}
        self.link_span: dict[str, str] = {}
        self.expected_rtt: dict[str, float] = {}  # stream -> design RTT (2 x base one-way delay)
        self._map_streams()

    # ------------------------------------------------------------------ stream mapping
    def _map_streams(self) -> None:
        t = self.topo
        for h in t.hosts:
            link = t.links_of(h)[0]
            peer = link.other(h)
            if t.nodes[peer].type == "switch":
                up = next(l for l in t.links_of(peer) if t.nodes[l.other(peer)].type == "router")
                self.expected_rtt[f"G:{h}"] = 2 * (link.delay_ms + up.delay_ms)
            else:
                self.expected_rtt[f"G:{h}"] = 2 * link.delay_ms
        for link in t.links.values():
            ta, tb = t.nodes[link.a].type, t.nodes[link.b].type
            if ta == "router" and tb == "router":
                sid = f"L:{link.id}"
                self.link_streams[link.id] = [sid]
                self.link_span[link.id] = f"{link.a} → {link.b}"
                self.expected_rtt[sid] = 2 * link.delay_ms
            elif "switch" in (ta, tb) and "router" in (ta, tb):
                sw = link.a if ta == "switch" else link.b
                hosts = [x for x in t.neighbors(sw) if t.nodes[x].is_host]
                self.link_streams[link.id] = [f"G:{h}" for h in hosts]
                self.link_span[link.id] = f"hosts on {sw} → gateway"
            else:
                h = link.a if t.nodes[link.a].is_host else link.b
                self.link_streams[link.id] = [f"G:{h}"]
                self.link_span[link.id] = f"{h} → gateway {self.plan.host_gateway[h]}"

    def streams(self, ids: list[str]) -> list[ProbeStream]:
        return [s for s in (self.store.get(i) for i in ids) if s is not None]

    # ------------------------------------------------------------------ pooled stats
    def _liveness(self, streams: list[ProbeStream], now: float) -> bool | None:
        states = [s.is_dead(now, self.s.dead_min_s, self.s.dead_mult) for s in streams]
        if any(d is False for d in states):
            return True
        if states and all(d is True for d in states):
            return False
        return None

    @staticmethod
    def _pooled_loss(streams: list[ProbeStream], now: float, window_s: float) -> float | None:
        w = [o for s in streams for o in s.window(now - window_s, now)]
        if not w:
            return None
        return 100.0 * sum(1 for _, r in w if r is None) / len(w)

    @staticmethod
    def _pooled_rtt(streams: list[ProbeStream], now: float, window_s: float) -> RttStats | None:
        vals = sorted(r for s in streams for r in s.rtt_samples(now - window_s, now))
        if not vals:
            return None
        return RttStats(len(vals), sum(vals) / len(vals), percentile(vals, 50), percentile(vals, 95), percentile(vals, 99))

    # ------------------------------------------------------------------ views
    def direction(self, link_id: str, sender: str, cap_mbps: float) -> dict:
        r = self.counters.get(self.plan.intf(link_id, sender).name)
        util = r.tx_bps / (cap_mbps * 1e6) if cap_mbps > 0 else 0.0
        return {
            "bps": round(r.tx_bps, 1),
            "pps": round(r.tx_pps, 2),
            "util": round(util, 4),
            "drops_ps": round(r.qdisc_drops_ps, 2),
            "drops_total": r.qdisc_drops_total + r.rx_drops_total,
            "backlog_pkts": r.backlog_pkts,
            "tx_bytes_total": r.tx_bytes_total,
        }

    def link_view(self, link_id: str, now: float) -> dict:
        link = self.topo.links[link_id]
        p = self.link_params(link_id)
        streams = self.streams(self.link_streams[link_id])
        alive = self._liveness(streams, now)
        rtt = self._pooled_rtt(streams, now, 10.0)
        recent = self._pooled_rtt(streams, now, 2.0)
        loss = self._pooled_loss(streams, now, 5.0)
        ab = self.direction(link_id, link.a, p.bw_mbps)
        ba = self.direction(link_id, link.b, p.bw_mbps)
        util = max(ab["util"], ba["util"])
        expected = sum(self.expected_rtt.get(s.sid, 0) for s in streams) / max(1, len(streams))
        if alive is None:
            health = "unknown"
        elif not alive:
            health = "down"
        elif (loss or 0) >= DEGRADED_LOSS_PCT or util >= DEGRADED_UTIL or (
            recent is not None and recent.mean > 1.5 * expected + 2.0
        ):
            health = "degraded"
        else:
            health = "ok"
        return {
            "id": link_id,
            "a": link.a,
            "b": link.b,
            "health": health,
            "alive": alive,
            "admin_up": self.link_admin_up(link_id),
            "probe_span": self.link_span[link_id],
            "probe_streams": [s.sid for s in streams],
            "rtt_ms": _clean(recent.mean if recent else None),
            "rtt_p50": _clean(rtt.p50 if rtt else None),
            "rtt_p95": _clean(rtt.p95 if rtt else None),
            "rtt_p99": _clean(rtt.p99 if rtt else None),
            "expected_rtt_ms": round(expected, 3),
            "loss_pct": _clean(loss, 2),
            "ab": ab,
            "ba": ba,
            "util": round(util, 4),
            "cfg": p.to_dict(),
            "base": link.params(),
        }

    def node_view(self, node: str, link_views: dict[str, dict]) -> dict:
        n = self.topo.nodes[node]
        views = [link_views[l.id] for l in self.topo.links_of(node)]
        states = [v["health"] for v in views]
        if states and all(s == "down" for s in states):
            health = "down"
        elif any(s in ("down", "degraded") for s in states):
            health = "degraded"
        elif all(s == "unknown" for s in states):
            health = "unknown"
        else:
            health = "ok"
        rx = tx = drops = 0.0
        intfs = []
        for intf in self.plan.intfs_of(node):
            r = self.counters.get(intf.name)
            rx += r.rx_bps
            tx += r.tx_bps
            drops += r.qdisc_drops_ps
            intfs.append({
                "name": intf.name, "ip": intf.cidr, "link": intf.link_id,
                "rx_bps": round(r.rx_bps, 1), "tx_bps": round(r.tx_bps, 1),
                "rx_pps": round(r.rx_pps, 2), "tx_pps": round(r.tx_pps, 2),
                "drops_total": r.qdisc_drops_total + r.rx_drops_total, "backlog_pkts": r.backlog_pkts,
            })
        return {
            "id": node,
            "type": n.type,
            "health": health,
            "rx_bps": round(rx, 1),
            "tx_bps": round(tx, 1),
            "drops_ps": round(drops, 2),
            "interfaces": intfs,
        }

    def flow_probe_view(self, pair: str, now: float, window_s: float = 10.0) -> dict:
        s = self.store.get(f"F:{pair}")
        if s is None:
            return {"alive": None}
        st = s.rtt_stats(now, window_s)
        dead = s.is_dead(now, self.s.dead_min_s, self.s.dead_mult)
        return {
            "alive": None if dead is None else not dead,
            "rtt_ms": _clean(s.recent_rtt(now, 2.0)),
            "rtt_p50": _clean(st.p50 if st else None),
            "rtt_p95": _clean(st.p95 if st else None),
            "rtt_p99": _clean(st.p99 if st else None),
            "probe_loss_pct": _clean(s.loss_pct(now, 5.0), 2),
        }

    # ------------------------------------------------------------------ routing inputs
    def routing_link_metrics(self, link_id: str, now: float) -> dict:
        """Inputs for the path score of a router-router link.

        latency: one-way estimate = RTT/2 over the last 2 s (falls back to configured delay
                 before the first sample); loss: one-way estimate from round-trip probe loss,
                 1 - sqrt(1 - p_rt); alive: probe liveness.
        """
        streams = self.streams(self.link_streams[link_id])
        alive = self._liveness(streams, now)
        recent = self._pooled_rtt(streams, now, 2.0)
        loss = self._pooled_loss(streams, now, 3.0)
        p = self.link_params(link_id)
        lat = recent.mean / 2 if recent else p.delay_ms
        rt = (loss or 0.0) / 100.0
        one_way = 1 - math.sqrt(max(0.0, 1 - rt))
        return {"latency_ms": lat, "loss": one_way, "alive": alive is not False, "cap_mbps": p.bw_mbps}
