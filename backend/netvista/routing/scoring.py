"""Network-aware path score.

    score = w_latency * latency_ms + w_loss * loss_pct + w_util * util_pct

  latency_ms : sum of measured one-way link latencies along the path
  loss_pct   : 100 * (1 - prod(1 - p_i))   - end-to-end loss of independent links
  util_pct   : 100 * max(projected utilisation) - the bottleneck after placing this flow

All three terms are in "millisecond-equivalents": with the default weights 1 % loss costs
as much as 5 ms of latency and 10 % extra utilisation costs 2 ms. A path containing a dead
link is infeasible (score = inf).

"Projected" utilisation = (measured rate on the link - this flow's own rate if it is
already there + this flow's rate) / capacity. Without that correction a flow would see its
own traffic on the current path, move away, see the old path empty, move back... (flapping).
Hysteresis + hold-down add a second line of defence.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field


@dataclass
class Weights:
    latency: float = 1.0
    loss: float = 5.0
    util: float = 0.2

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HopMetrics:
    link_id: str
    u: str
    v: str
    latency_ms: float
    loss: float  # one-way loss probability 0..1
    util: float  # projected utilisation 0..(can exceed 1 when overloaded)
    alive: bool


@dataclass
class PathEval:
    path: list[str]
    hops: list[HopMetrics] = field(default_factory=list)
    latency_ms: float = 0.0
    loss_pct: float = 0.0
    util_pct: float = 0.0
    feasible: bool = True
    score: float = math.inf
    bottleneck: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "links": [h.link_id for h in self.hops],
            "latency_ms": round(self.latency_ms, 3),
            "loss_pct": round(self.loss_pct, 3),
            "util_pct": round(self.util_pct, 2),
            "feasible": self.feasible,
            "score": None if math.isinf(self.score) else round(self.score, 3),
            "bottleneck": self.bottleneck,
            "dead_links": [h.link_id for h in self.hops if not h.alive],
        }


def evaluate(path: list[str], hops: list[HopMetrics], w: Weights) -> PathEval:
    ev = PathEval(path=path, hops=hops)
    ev.latency_ms = sum(h.latency_ms for h in hops)
    keep = 1.0
    for h in hops:
        keep *= 1.0 - min(1.0, max(0.0, h.loss))
    ev.loss_pct = 100.0 * (1.0 - keep)
    if hops:
        b = max(hops, key=lambda h: h.util)
        ev.util_pct = 100.0 * b.util
        ev.bottleneck = b.link_id
    ev.feasible = all(h.alive for h in hops)
    ev.score = (w.latency * ev.latency_ms + w.loss * ev.loss_pct + w.util * ev.util_pct) if ev.feasible else math.inf
    return ev


def best(cands: list[PathEval]) -> PathEval | None:
    feasible = [c for c in cands if c.feasible]
    if not feasible:
        return None
    return min(feasible, key=lambda c: (round(c.score, 6), len(c.path), c.path))


def decide(current: PathEval | None, cands: list[PathEval], hysteresis: float, hold_ok: bool) -> tuple[PathEval | None, str]:
    """Pick the path to use. Returns (chosen, reason).

    reasons: "initial", "failover" (current path broken), "better" (voluntary switch),
             "keep" (no change), "no-path" (nothing feasible; keep what we have)
    """
    b = best(cands)
    if current is None:
        return (b, "initial") if b else (None, "no-path")
    if not current.feasible:
        return (b, "failover") if b else (current, "no-path")
    if b is None or b.path == current.path:
        return current, "keep"
    if hold_ok and b.score < current.score * (1.0 - hysteresis):
        return b, "better"
    return current, "keep"
