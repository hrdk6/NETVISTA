"""Fluid model of the emulated network: the fast twin.

The SimPy twin (model.py) moves every packet. That is accurate (0.2 % RTT error against the
live network) but costs about a second of CPU per prediction. Failure analysis and route
planning need thousands of predictions, so this module computes the same steady state in
closed form, from the same configuration, in tens of microseconds:

  per directed hop (one tc egress: netem loss -> netem buffer of Q packets -> delay D -> HTB at C)
    offered       lambda   = sum of the wire rates of the flows that reach the hop
    after loss    lambda'  = lambda * (1 - p)
    accepted      a        = min(lambda', C, Q * s / (D + s / C))
                             the third term is netem's buffer: it counts packets that are
                             still in the delay line, so a long delay at a high rate drops
                             packets even below capacity (the packet twin models the same)
    latency       full buffer (lambda' >= C):  T = Q * s / C            (Little's law, N = Q)
                  otherwise:                   T = D + W, W from Kingman's G/D/1 formula
                                               W = rho / (1 - rho) * ca^2 / 2 * s / C
    ca^2          Whitt's QNA superposition of the flows' arrival processes (each a CBR source
                  with +-30 % gap jitter, ci^2 = 0.3^2 / 3)
  flows crossing an overloaded hop all lose the same fraction (fair tail drop), and the
  reduced rates are propagated downstream until the loads stop changing (fixed point).

  per pair (probe RTT, the quantity the live dashboard measures)
    RTT_q = sum of hop latencies both ways + calibrated overheads (E + H * 2n)
            + q-quantile of the calibrated noise + z_q * sqrt(sum jitter^2 + sum W^2)

It returns results in the same shape as whatif.simulate(), so the routing prediction loop,
the validation comparisons and the UI work with either engine. Its error against the packet
twin is measured in tests/test_fluid.py and against the live network by the validation runs.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any


Z = {50: 0.0, 95: 1.6449, 99: 2.3263}
SOURCE_CV2 = 0.3 * 0.3 / 3.0  # CBR gap jitter +-30 % (uniform) -> squared coefficient of variation


def quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    pos = (len(sorted_vals) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


@dataclass
class Hop:
    link_id: str
    sender: str
    receiver: str
    cap_bps: float
    delay_s: float
    jitter_s: float
    loss: float
    limit: int
    up: bool


@dataclass
class HopState:
    offered_bps: float = 0.0  # arriving wire bits/s (before netem loss)
    accepted_bps: float = 0.0  # what HTB sends
    pass_frac: float = 1.0  # fraction of arriving packets that leave (netem loss and buffer)
    latency_s: float = 0.0  # time from arrival to departure for packets that pass
    wait_s: float = 0.0  # waiting for the shaper (queueing)
    saturated: bool = False


@dataclass
class PairEval:
    pair: str
    alive: bool
    offered_mbps: float
    rx_mbps: float
    loss_pct: float | None
    probe_loss_pct: float
    rtt_p50: float | None
    rtt_p95: float | None
    rtt_p99: float | None
    owd_ms: float | None
    bottleneck: str | None = None
    max_util: float = 0.0


@dataclass
class FluidEval:
    pairs: dict[str, PairEval] = field(default_factory=dict)
    hops: dict[tuple[str, str], HopState] = field(default_factory=dict)

    def link_util(self, net: "FluidNet") -> dict[str, float]:
        out: dict[str, float] = {}
        for (u, v), st in self.hops.items():
            h = net.hops[(u, v)]
            util = st.accepted_bps / h.cap_bps if h.cap_bps > 0 else 0.0
            out[h.link_id] = max(out.get(h.link_id, 0.0), util)
        return out


class FluidNet:
    """The network state (link parameters, up/down) prepared for fast repeated evaluation."""

    def __init__(
        self,
        links: dict[str, dict],
        overhead: dict | None = None,
        payload_bytes: int = 1200,
        wire_bytes: int = 1242,
        probe_timeout_s: float = 2.0,
    ) -> None:
        self.links = links
        self.payload = payload_bytes
        self.wire = wire_bytes
        self.wire_factor = wire_bytes / payload_bytes
        self.probe_timeout_s = probe_timeout_s
        ov = overhead or {}
        self.per_hop_s = float(ov.get("per_hop_ms", 0.0)) / 1000.0
        self.endpoint_s = float(ov.get("endpoint_ms", 0.0)) / 1000.0
        noise = sorted(float(x) / 1000.0 for x in ov.get("noise_ms", []) or [])
        self.noise_q = {q: (quantile(noise, q) if noise else 0.0) for q in Z}
        self.hops: dict[tuple[str, str], Hop] = {}
        for lid, l in links.items():
            for s, r in ((l["a"], l["b"]), (l["b"], l["a"])):
                self.hops[(s, r)] = Hop(
                    link_id=lid, sender=s, receiver=r, cap_bps=float(l["bw_mbps"]) * 1e6, delay_s=float(l["delay_ms"]) / 1000.0,
                    jitter_s=float(l.get("jitter_ms", 0.0)) / 1000.0, loss=float(l.get("loss_pct", 0.0)) / 100.0,
                    limit=int(l.get("queue_pkts", 1000)), up=bool(l.get("up", True)),
                )

    # ------------------------------------------------------------------ helpers
    def hops_of(self, path: list[str]) -> list[tuple[str, str]]:
        return list(zip(path, path[1:]))

    def _hop_state(self, h: Hop, flows_bps: list[float]) -> HopState:
        st = HopState()
        lam = sum(flows_bps)
        st.offered_bps = lam
        if not h.up:
            st.pass_frac = 0.0
            return st
        s_bits = self.wire * 8.0
        tx = s_bits / h.cap_bps  # serialisation time of one data packet
        after_loss = lam * (1.0 - h.loss)
        buffer_bound = h.limit * s_bits / (h.delay_s + tx)  # netem's limit includes the delay line
        accepted = min(after_loss, h.cap_bps, buffer_bound)
        st.accepted_bps = accepted
        st.pass_frac = (1.0 - h.loss) * (accepted / after_loss if after_loss > 0 else 1.0)
        full_latency = max(h.limit * s_bits / h.cap_bps, h.delay_s + tx)
        if after_loss >= h.cap_bps * 0.999 and buffer_bound >= h.cap_bps:
            st.saturated = True
            st.latency_s = full_latency
            st.wait_s = full_latency - h.delay_s
            return st
        rho = min(accepted / h.cap_bps, 0.999)
        if rho > 0 and flows_bps:
            shares = [f / lam for f in flows_bps if f > 0]
            v = 1.0 / sum(x * x for x in shares)
            w = 1.0 / (1.0 + 4.0 * (1.0 - rho) ** 2 * (v - 1.0))
            ca2 = w * SOURCE_CV2 + (1.0 - w)
            wait = rho / (1.0 - rho) * ca2 / 2.0 * tx
        else:
            wait = 0.0
        wait = min(wait, max(0.0, full_latency - h.delay_s))
        st.wait_s = wait
        st.latency_s = h.delay_s + wait
        return st

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, paths: dict[str, list[str] | None], demand: dict[str, list[float]] | None = None) -> FluidEval:
        """paths: pair -> node path (probes and data follow it, the reply the reverse path).
        demand: pair -> payload rates (Mbit/s) of the CBR flows on that pair."""
        demand = demand or {}
        hop_lists: dict[str, list[tuple[str, str]]] = {p: self.hops_of(path) for p, path in paths.items() if path}
        # flows as (pair, hop list, payload bps); rates at each hop start at the source rate
        flows: list[tuple[str, list[tuple[str, str]], float]] = []
        for pair, rates in demand.items():
            hl = hop_lists.get(pair)
            if not hl:
                continue
            for r in rates:
                if r > 0:
                    flows.append((pair, hl, r * 1e6))
        states: dict[tuple[str, str], HopState] = {}
        pass_frac: dict[tuple[str, str], float] = {}
        for _ in range(12):  # loads only fall as drops propagate: a few passes reach the fixed point
            arriving: dict[tuple[str, str], list[float]] = {}
            for _, hl, bps in flows:
                rate = bps * self.wire_factor
                for hop in hl:
                    arriving.setdefault(hop, []).append(rate)
                    rate *= pass_frac.get(hop, 1.0)
            new_states = {hop: self._hop_state(self.hops[hop], arr) for hop, arr in arriving.items()}
            changed = any(abs(new_states[h].pass_frac - pass_frac.get(h, 1.0)) > 1e-9 for h in new_states)
            states = new_states
            pass_frac = {h: s.pass_frac for h, s in states.items()}
            if not changed:
                break
        # hops with probes only (no data): netem loss and delay, no queueing
        for hl in hop_lists.values():
            for hop in hl + [(v, u) for u, v in hl]:
                if hop not in states:
                    states[hop] = self._hop_state(self.hops[hop], [])

        out = FluidEval(hops=states)
        for pair, path in paths.items():
            rates = [r for r in demand.get(pair, []) if r > 0]
            offered = sum(rates)
            if not path:
                out.pairs[pair] = PairEval(pair, False, offered, 0.0, 100.0 if offered else None, 100.0, None, None, None, None)
                continue
            fwd = hop_lists[pair]
            rev = [(v, u) for u, v in reversed(fwd)]
            alive = all(self.hops[h].up for h in fwd + rev)
            deliver = 1.0
            for h in fwd:
                deliver *= states[h].pass_frac
            rt_pass = deliver
            for h in rev:
                rt_pass *= states[h].pass_frac
            n = len(fwd)
            rx = offered * deliver
            ev = PairEval(
                pair=pair, alive=alive, offered_mbps=offered, rx_mbps=rx,
                loss_pct=(100.0 * (1.0 - deliver)) if offered > 0 else None,
                probe_loss_pct=100.0 * (1.0 - rt_pass), rtt_p50=None, rtt_p95=None, rtt_p99=None, owd_ms=None,
            )
            if alive and rt_pass > 0:
                lat_f = sum(states[h].latency_s for h in fwd)
                lat_r = sum(states[h].latency_s for h in rev)
                base = lat_f + lat_r + 2 * n * self.per_hop_s + self.endpoint_s
                # a full buffer drains at a constant rate (deterministic delay); below saturation the
                # waiting time is roughly exponential, so its spread is about its mean
                var = sum(self.hops[h].jitter_s ** 2 + (0.0 if states[h].saturated else states[h].wait_s ** 2) for h in fwd + rev)
                sd = math.sqrt(var)
                for q in Z:
                    rtt = base + self.noise_q[q] + Z[q] * sd
                    setattr(ev, f"rtt_p{q}", rtt * 1000.0 if rtt <= self.probe_timeout_s else None)
                ev.owd_ms = (lat_f + n * self.per_hop_s + self.endpoint_s / 2) * 1000.0
                if ev.rtt_p50 is None:  # every echo would arrive after the probe timeout
                    ev.probe_loss_pct = 100.0
            utils = [(states[h].accepted_bps / self.hops[h].cap_bps if self.hops[h].cap_bps else 0.0, self.hops[h].link_id) for h in fwd + rev]
            if utils:
                ev.max_util, ev.bottleneck = max(utils)
            out.pairs[pair] = ev
        return out


# ---------------------------------------------------------------------------- twin-compatible API
def demand_of(cfg: dict) -> dict[str, list[float]]:
    return {pair: [float(f["rate_mbps"]) for f in p.get("flows", []) if f.get("rate_mbps", 0) > 0] for pair, p in cfg["pairs"].items()}


def net_of(cfg: dict) -> FluidNet:
    return FluidNet(
        cfg["links"], cfg.get("overhead"), int(cfg.get("payload_bytes", 1200)), int(cfg.get("wire_bytes", 1242)),
        float(cfg.get("probe_timeout_s", 2.0)),
    )


def fluid_simulate(cfg: dict[str, Any], paths: dict[str, list[str]], duration: float | None = None, seed: int | None = None) -> dict[str, Any]:
    """Same result shape as whatif.simulate(), computed analytically (duration/seed are unused)."""
    net = net_of(cfg)
    ev = net.evaluate(paths, demand_of(cfg))
    payload = net.payload
    flows: dict[str, dict] = {}
    for pair, p in cfg["pairs"].items():
        pe = ev.pairs[pair]
        deliver = (pe.rx_mbps / pe.offered_mbps) if pe.offered_mbps else 0.0
        for k, f in enumerate(p.get("flows", [])):
            rate = float(f["rate_mbps"])
            if rate <= 0:
                continue
            pps = rate * 1e6 / (payload * 8)
            flows[f"{pair}#{k}"] = {
                "sent": pps, "received": pps * deliver, "loss_pct": 100.0 * (1.0 - deliver),
                "rx_mbps": rate * deliver, "owd_p50_ms": pe.owd_ms if deliver > 0 else None, "owd_p95_ms": None,
            }
    probes = {
        pair: {"sent": 10.0, "lost": pe.probe_loss_pct / 10.0, "loss_pct": pe.probe_loss_pct,
               "rtt_mean": pe.rtt_p50, "rtt_p50": pe.rtt_p50, "rtt_p95": pe.rtt_p95, "rtt_p99": pe.rtt_p99}
        for pair, pe in ev.pairs.items()
    }
    links: dict[str, dict] = {}
    for (s, r), h in net.hops.items():
        st = ev.hops.get((s, r)) or net._hop_state(h, [])
        loss_frac = 1.0 - st.pass_frac if h.up else 1.0
        links.setdefault(h.link_id, {})[f"{s}>{r}"] = {
            "util": st.accepted_bps / h.cap_bps if h.cap_bps else 0.0,
            "rate_bps": st.accepted_bps,
            "queue_ms": st.wait_s * 1000.0,
            "loss_frac": loss_frac,
            "drops": None,
            "drop_queue": None,
            "probe_latency_ms": st.latency_s * 1000.0 if h.up else None,
            "up": h.up,
            "sender": s,
        }
    return {"flows": flows, "probes": probes, "links": links, "window_s": None, "engine": "fluid"}


def run_fluid_prediction(cfg: dict[str, Any]) -> dict[str, Any]:
    """whatif.run_prediction() with the fluid engine: same routing prediction, same output."""
    from .whatif import predict_with

    t0 = time.perf_counter()
    out = predict_with(cfg, lambda c, paths, duration, seed: fluid_simulate(c, paths))
    out["engine"] = "fluid"
    out["wall_s"] = time.perf_counter() - t0
    return out
