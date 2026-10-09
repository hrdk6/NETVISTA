"""The signals the AIOps layer watches, sampled once a second from live telemetry only.

Every signal is one scalar measured by the probe agents or the interface counters:

    link:<id>:rtt       mean probe RTT over 2 s on a router-router link   (L:<id> stream)
    link:<id>:loss      probe loss over the last 10 s on that link
    link:<id>:util      max(tx a->b, tx b->a) / shaped capacity            (every link)
    access:<host>:rtt   host -> gateway probe RTT                          (G:<host> stream)
    access:<host>:loss  host -> gateway probe loss
    flow:<pair>:rtt     end-to-end client -> server probe RTT              (F:<pair> stream)
    flow:<pair>:loss    end-to-end probe loss
    flow:<pair>:data_loss  iperf3 receiver loss of the pair's traffic (only while it carries traffic)

It never reads the chaos lab's fault list: what the detector knows is what the network shows.
Each probe signal also carries the set of network elements its packets traverse
("link:r2-r5", "node:r2"), which is what the root-cause analysis reasons over.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..topology import Topology

LOSS_WINDOW_S = 10.0
RTT_WINDOW_S = 2.0
# lost probes are only known after the probe timeout, and are stamped with their send time
LOSS_LAG_S = 2.0


@dataclass(frozen=True)
class SignalSpec:
    id: str
    kind: str  # link | access | flow
    entity: str  # link id | host | pair
    metric: str  # rtt | loss | util | data_loss
    unit: str  # ms | % | ratio
    stream: str | None  # probe stream measured by this signal (None for counters)
    direction: str  # "up": only increases are anomalous | "both"
    floor: float  # smallest scale (in the signal's unit) a deviation is measured against
    rel_floor: float  # ... or this fraction of the learned mean, whichever is larger
    min_level: float | None = None  # value must also reach this level to be anomalous

    @property
    def label(self) -> str:
        what = {"rtt": "round trip", "loss": "probe loss", "util": "load", "data_loss": "data loss"}[self.metric]
        ent = {"link": self.entity, "access": f"{self.entity} → gateway", "flow": self.entity.replace(">", " → ")}[self.kind]
        return f"{ent} {what}"


def element_link(lid: str) -> str:
    return f"link:{lid}"


def element_node(n: str) -> str:
    return f"node:{n}"


def path_elements(topo: Topology, path: list[str] | None) -> frozenset[str]:
    """Nodes and links a packet following `path` traverses (hosts excluded: they are endpoints)."""
    if not path:
        return frozenset()
    out = {element_node(n) for n in path if not topo.nodes[n].is_host}
    for u, v in zip(path, path[1:]):
        link = topo.link_between(u, v)
        if link:
            out.add(element_link(link.id))
    return frozenset(out)


def gateway_path(topo: Topology, host: str, gateway: str) -> list[str]:
    link = topo.links_of(host)[0]
    peer = link.other(host)
    if topo.nodes[peer].type == "switch":
        return [host, peer, gateway]
    return [host, peer]


def build_specs(topo: Topology) -> list[SignalSpec]:
    specs: list[SignalSpec] = []
    for link in topo.links.values():
        core = topo.nodes[link.a].type == "router" and topo.nodes[link.b].type == "router"
        if core:
            specs.append(SignalSpec(f"link:{link.id}:rtt", "link", link.id, "rtt", "ms", f"L:{link.id}", "up", 0.3, 0.05))
            specs.append(SignalSpec(f"link:{link.id}:loss", "link", link.id, "loss", "%", f"L:{link.id}", "up", 0.4, 0.0))
        # load is only an anomaly when it is unusually high AND near capacity (congestion risk);
        # an operator starting traffic is a change, not a fault
        specs.append(SignalSpec(f"link:{link.id}:util", "link", link.id, "util", "ratio", None, "up", 0.05, 0.1, min_level=0.7))
    for h in topo.hosts:
        specs.append(SignalSpec(f"access:{h}:rtt", "access", h, "rtt", "ms", f"G:{h}", "up", 0.3, 0.05))
        specs.append(SignalSpec(f"access:{h}:loss", "access", h, "loss", "%", f"G:{h}", "up", 0.4, 0.0))
    for c, s in topo.flow_pairs():
        pair = f"{c}>{s}"
        specs.append(SignalSpec(f"flow:{pair}:rtt", "flow", pair, "rtt", "ms", f"F:{pair}", "up", 0.5, 0.06))
        specs.append(SignalSpec(f"flow:{pair}:loss", "flow", pair, "loss", "%", f"F:{pair}", "up", 0.5, 0.0))
        # iperf3 counts every datagram (~625/s per 6 Mbit/s flow), so its loss is far less noisy
        # than 10 probes/s: 1 % loss is ~19 packets per 3 s report, never zero
        specs.append(SignalSpec(f"flow:{pair}:data_loss", "flow", pair, "data_loss", "%", None, "up", 0.25, 0.0))
    return specs


def _stream_rtt(stream, now: float) -> float | None:
    vals = stream.rtt_samples(now - RTT_WINDOW_S, now)
    return sum(vals) / len(vals) if vals else None


def _stream_loss(stream, now: float) -> float | None:
    # shift the window back by the probe timeout so every probe in it has a known outcome
    t1 = now - LOSS_LAG_S
    w = stream.window(t1 - LOSS_WINDOW_S, t1)
    if len(w) < 10:
        return None
    return 100.0 * sum(1 for _, r in w if r is None) / len(w)


def sample(rt, specs: list[SignalSpec], now: float) -> dict[str, float | None]:
    """One value per signal, read from the probe store, counters and iperf3 receivers."""
    out: dict[str, float | None] = {}
    utils: dict[str, float] = {}
    for spec in specs:
        if spec.metric == "util":
            if spec.entity not in utils:
                link = rt.topo.links[spec.entity]
                cap = rt.telemetry.link_params(link.id).bw_mbps  # interface rate, like SNMP ifSpeed
                ab = rt.telemetry.direction(link.id, link.a, cap)["util"]
                ba = rt.telemetry.direction(link.id, link.b, cap)["util"]
                utils[spec.entity] = max(ab, ba)
            out[spec.id] = utils[spec.entity]
        elif spec.metric == "data_loss":
            src, dst = spec.entity.split(">")
            if rt.traffic.offered_mbps(src, dst) <= 0:
                out[spec.id] = None
                continue
            st = rt.traffic.pair_stats(src, dst, 3.0, now)
            out[spec.id] = st["loss_pct"] if st else None
        else:
            stream = rt.store.get(spec.stream) if spec.stream else None
            if stream is None:
                out[spec.id] = None
            elif spec.metric == "rtt":
                out[spec.id] = _stream_rtt(stream, now)
            else:
                out[spec.id] = _stream_loss(stream, now)
    for k, v in out.items():
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            out[k] = None
    return out
