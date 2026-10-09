"""SimPy discrete-event model of the emulated network (packet level).

Each link direction mirrors the real tc pipeline on the sender's egress (see emulation/tc.py):

    arrival --(netem loss p)--> [netem buffer, limit Q packets] --delay D (+jitter)--> HTB token bucket @ R bit/s --> next hop

  * loss is drawn first, then the buffer check - the order netem_enqueue() uses
  * the buffer counts every packet accepted but not yet transmitted (in delay OR waiting
    for the rate limiter), exactly like netem's `limit` under an HTB parent
  * the rate limiter is HTB's token bucket, FIFO: tokens refill at R up to the bucket size
    (tc's default HTB burst, 1600 bytes); a packet leaves as soon as its predecessor has
    left AND enough tokens exist. Under load this converges to Lindley's recursion at rate
    R; at light load a small probe is not delayed by the data packet in front of it (a
    pure FIFO-server model would wrongly add that serialisation wait - measured, see DESIGN.md)

Sources mirror the live traffic: iperf3-like UDP constant bit rate flows (1200 B payload,
1242 B on the wire) and the probe agents' echo probes (10/s, 106 B on the wire).
All randomness comes from one seeded RNG, so a prediction is reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

import simpy

from ..telemetry.health import percentile

PROBE_WIRE_BYTES = 64 + 8 + 20 + 14


@dataclass
class DirLink:
    link_id: str
    sender: str
    receiver: str
    bw_bps: float
    delay_s: float
    jitter_s: float
    loss: float
    limit: int
    up: bool = True
    bucket: float = 1600.0  # HTB burst in bytes (tc default for these rates)
    tokens: float = 1600.0
    tok_t: float = 0.0
    last_dep: float = 0.0
    in_system: int = 0
    # statistics (only for packets that *arrive* inside the measurement window)
    arrivals: int = 0
    tx_pkts: int = 0
    tx_bytes: int = 0
    drop_loss: int = 0
    drop_queue: int = 0
    drop_down: int = 0
    wait_sum: float = 0.0  # time spent waiting for the rate limiter (queueing)
    probe_lat_sum: float = 0.0
    probe_lat_n: int = 0


@dataclass
class FlowResult:
    sent: int = 0
    received: int = 0
    rx_payload_bytes: int = 0
    owd: list[float] = field(default_factory=list)


@dataclass
class ProbeResult:
    sent: int = 0
    rtts: list[float] = field(default_factory=list)
    lost: int = 0


class NetworkModel:
    """cfg keys: links, flows, probes, duration_s, warmup_s, seed, overhead (see whatif.py)."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.env = simpy.Environment()
        self.rng = random.Random(cfg.get("seed", 1))
        self.duration = float(cfg["duration_s"])
        self.warmup = float(cfg.get("warmup_s", 3.0))
        ov = cfg.get("overhead", {})
        self.per_hop_s = float(ov.get("per_hop_ms", 0.0)) / 1000.0
        self.endpoint_s = float(ov.get("endpoint_ms", 0.0)) / 1000.0
        self.noise = [x / 1000.0 for x in ov.get("noise_ms", [])]
        self.probe_timeout = float(cfg.get("probe_timeout_s", 2.0))
        self.source_jitter = float(cfg.get("source_jitter", 0.3))
        self.probe_jitter = float(cfg.get("probe_jitter", 0.02))
        self.dir: dict[tuple[str, str], DirLink] = {}
        for lid, l in cfg["links"].items():
            for s, r in ((l["a"], l["b"]), (l["b"], l["a"])):
                self.dir[(s, r)] = DirLink(
                    link_id=lid, sender=s, receiver=r,
                    bw_bps=l["bw_mbps"] * 1e6, delay_s=l["delay_ms"] / 1000.0, jitter_s=l.get("jitter_ms", 0.0) / 1000.0,
                    loss=l.get("loss_pct", 0.0) / 100.0, limit=int(l.get("queue_pkts", 1000)), up=bool(l.get("up", True)),
                    bucket=float(l.get("burst_bytes", 1600)), tokens=float(l.get("burst_bytes", 1600)),
                )
        self.flow_res: dict[str, FlowResult] = {}
        self.probe_res: dict[str, ProbeResult] = {}

    # ------------------------------------------------------------------ mechanics
    def _in_window(self, t: float) -> bool:
        return self.warmup <= t < self.duration

    def _hop(self, dl: DirLink, size: int, is_probe: bool):
        env = self.env
        t_arr = env.now
        counted = self._in_window(t_arr)
        if counted:
            dl.arrivals += 1
        if not dl.up:
            if counted:
                dl.drop_down += 1
            return False
        if dl.loss > 0 and self.rng.random() < dl.loss:
            if counted:
                dl.drop_loss += 1
            return False
        if dl.in_system >= dl.limit:
            if counted:
                dl.drop_queue += 1
            return False
        dl.in_system += 1
        d = dl.delay_s
        if dl.jitter_s > 0:
            d = max(0.0, self.rng.gauss(dl.delay_s, dl.jitter_s))
        if d > 0:
            yield env.timeout(d)
        ready = env.now
        start = max(ready, dl.last_dep)  # FIFO: never overtake the packet in front
        dl.tokens = min(dl.bucket, dl.tokens + (start - dl.tok_t) * dl.bw_bps / 8.0)
        dl.tok_t = start
        if dl.tokens >= size:
            dep = start
            dl.tokens -= size
        else:
            dep = start + (size - dl.tokens) * 8.0 / dl.bw_bps
            dl.tokens = 0.0
            dl.tok_t = dep
        dl.last_dep = dep
        if dep > env.now:
            yield env.timeout(dep - env.now)
        dl.in_system -= 1
        if counted:
            dl.tx_pkts += 1
            dl.tx_bytes += size
            dl.wait_sum += dep - ready
            if is_probe:
                dl.probe_lat_sum += env.now - t_arr
                dl.probe_lat_n += 1
        if self.per_hop_s > 0:
            yield env.timeout(self.per_hop_s)
        return True

    def _traverse(self, hops: list[DirLink], size: int, is_probe: bool):
        for dl in hops:
            ok = yield from self._hop(dl, size, is_probe)
            if not ok:
                return False
        return True

    def hops_of(self, path: list[str]) -> list[DirLink]:
        return [self.dir[(u, v)] for u, v in zip(path, path[1:])]

    # ------------------------------------------------------------------ sources
    def _data_packet(self, res: FlowResult, hops: list[DirLink], wire: int, payload: int):
        t0 = self.env.now
        ok = yield from self._traverse(hops, wire, False)
        if self._in_window(t0):
            res.sent += 1
            if ok:
                res.received += 1
                res.rx_payload_bytes += payload
                res.owd.append(self.env.now - t0)

    def _gap(self, interval: float, jitter: float) -> float:
        # real senders are not perfectly periodic (iperf3 paces on a 1 ms timer, the OS adds
        # wake-up noise); a mean-preserving jitter stops the twin's sources from phase-locking
        # with each other at a tail-drop queue, which would split a bottleneck unrealistically
        return interval * (1.0 + self.rng.uniform(-jitter, jitter)) if jitter > 0 else interval

    def _cbr(self, fid: str, path: list[str], rate_mbps: float, payload: int, wire: int, start: float, stop: float):
        res = self.flow_res.setdefault(fid, FlowResult())
        hops = self.hops_of(path)
        interval = payload * 8.0 / (rate_mbps * 1e6)
        yield self.env.timeout(start + self.rng.random() * interval)
        while self.env.now < stop:
            self.env.process(self._data_packet(res, hops, wire, payload))
            yield self.env.timeout(self._gap(interval, self.source_jitter))

    def _probe_once(self, res: ProbeResult, fwd: list[DirLink], rev: list[DirLink]):
        t0 = self.env.now
        ok = yield from self._traverse(fwd, PROBE_WIRE_BYTES, True)
        if ok:
            ok = yield from self._traverse(rev, PROBE_WIRE_BYTES, True)
        if not self._in_window(t0):
            return
        res.sent += 1
        if not ok:
            res.lost += 1
            return
        rtt = self.env.now - t0 + self.endpoint_s
        if self.noise:
            rtt = max(0.0, rtt + self.rng.choice(self.noise))
        if rtt > self.probe_timeout:
            res.lost += 1  # the live agent would have declared it lost
        else:
            res.rtts.append(rtt)

    def _prober(self, pid: str, path: list[str], interval: float):
        res = self.probe_res.setdefault(pid, ProbeResult())
        fwd = self.hops_of(path)
        rev = self.hops_of(list(reversed(path)))
        yield self.env.timeout(self.rng.random() * interval)
        while self.env.now < self.duration:
            self.env.process(self._probe_once(res, fwd, rev))
            yield self.env.timeout(self._gap(interval, self.probe_jitter))

    # ------------------------------------------------------------------ run
    def run(self) -> dict[str, Any]:
        for f in self.cfg.get("flows", []):
            if f["rate_mbps"] <= 0:
                continue
            self.env.process(self._cbr(
                f["id"], f["path"], f["rate_mbps"], int(f.get("payload_bytes", 1200)), int(f.get("wire_bytes", 1242)),
                float(f.get("start_s", 0.0)), float(f.get("stop_s", self.duration)),
            ))
        for p in self.cfg.get("probes", []):
            self.env.process(self._prober(p["id"], p["path"], float(p.get("interval_s", 0.1))))
        drain = 3.0
        self.env.run(until=self.duration + drain)
        return self.results()

    def results(self) -> dict[str, Any]:
        win = self.duration - self.warmup
        flows = {}
        for fid, r in self.flow_res.items():
            owd = sorted(r.owd)
            flows[fid] = {
                "sent": r.sent,
                "received": r.received,
                "loss_pct": 100.0 * (r.sent - r.received) / r.sent if r.sent else 0.0,
                "rx_mbps": r.rx_payload_bytes * 8 / win / 1e6,
                "owd_p50_ms": percentile(owd, 50) * 1000 if owd else None,
                "owd_p95_ms": percentile(owd, 95) * 1000 if owd else None,
            }
        probes = {}
        for pid, r in self.probe_res.items():
            s = sorted(r.rtts)
            probes[pid] = {
                "sent": r.sent,
                "lost": r.lost,
                "loss_pct": 100.0 * r.lost / r.sent if r.sent else 0.0,
                "rtt_mean": sum(s) / len(s) * 1000 if s else None,
                "rtt_p50": percentile(s, 50) * 1000 if s else None,
                "rtt_p95": percentile(s, 95) * 1000 if s else None,
                "rtt_p99": percentile(s, 99) * 1000 if s else None,
            }
        links: dict[str, dict] = {}
        for (s, r), dl in self.dir.items():
            lv = links.setdefault(dl.link_id, {})
            drops = dl.drop_loss + dl.drop_queue + dl.drop_down
            lv[f"{s}>{r}"] = {
                "util": dl.tx_bytes * 8 / win / dl.bw_bps if dl.bw_bps else 0.0,
                "rate_bps": dl.tx_bytes * 8 / win,
                "queue_ms": dl.wait_sum / dl.tx_pkts * 1000 if dl.tx_pkts else 0.0,
                "loss_frac": drops / dl.arrivals if dl.arrivals else (0.0 if dl.up else 1.0),
                "drops": drops,
                "drop_queue": dl.drop_queue,
                "probe_latency_ms": dl.probe_lat_sum / dl.probe_lat_n * 1000 if dl.probe_lat_n else None,
                "up": dl.up,
                "sender": s,
            }
        return {"flows": flows, "probes": probes, "links": links, "window_s": win}

