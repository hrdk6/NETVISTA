"""Per-adjacency probe statistics: RTT percentiles, windowed loss and liveness.

A "stream" is one probe target as seen by one agent, e.g.
    L:r1-r2      r1 probing r2 across link r1-r2       (router adjacency)
    G:c1         host c1 probing its gateway router    (access segment)
    F:c1>srv1    client c1 probing server srv1         (end-to-end flow path)
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass


def percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile (same definition numpy uses by default)."""
    if not sorted_vals:
        return math.nan
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


@dataclass
class RttStats:
    n: int
    mean: float
    p50: float
    p95: float
    p99: float

    def to_dict(self) -> dict:
        return {"n": self.n, "mean": self.mean, "p50": self.p50, "p95": self.p95, "p99": self.p99}


class ProbeStream:
    def __init__(self, sid: str, created_t: float, window: int = 600) -> None:
        self.sid = sid
        self.created_t = created_t
        self.outcomes: deque[tuple[float, float | None]] = deque(maxlen=window)  # (t, rtt_ms or None=lost)
        self.last_reply_t: float | None = None
        self.srtt: float | None = None
        self.replies = 0
        self.losses = 0

    def on_reply(self, t: float, rtt: float) -> None:
        self.outcomes.append((t, rtt))
        self.last_reply_t = t
        self.replies += 1
        self.srtt = rtt if self.srtt is None else 0.8 * self.srtt + 0.2 * rtt

    def on_loss(self, t: float) -> None:
        self.outcomes.append((t, None))
        self.losses += 1

    # ---- statistics ----------------------------------------------------------
    def window(self, t0: float, t1: float) -> list[tuple[float, float | None]]:
        return [o for o in list(self.outcomes) if t0 <= o[0] <= t1]

    def loss_pct(self, now: float, window_s: float = 5.0) -> float | None:
        w = self.window(now - window_s, now)
        if not w:
            return None
        return 100.0 * sum(1 for _, r in w if r is None) / len(w)

    def rtt_samples(self, t0: float, t1: float) -> list[float]:
        return [r for t, r in list(self.outcomes) if r is not None and t0 <= t <= t1]

    def rtt_stats(self, now: float, window_s: float = 10.0) -> RttStats | None:
        s = sorted(self.rtt_samples(now - window_s, now))
        if not s:
            return None
        return RttStats(n=len(s), mean=sum(s) / len(s), p50=percentile(s, 50), p95=percentile(s, 95), p99=percentile(s, 99))

    def recent_rtt(self, now: float, window_s: float = 2.0) -> float | None:
        s = self.rtt_samples(now - window_s, now)
        return sum(s) / len(s) if s else self.srtt

    def dead_interval(self, dead_min: float, dead_mult: float) -> float:
        return max(dead_min, dead_mult * (self.srtt or 0.0) / 1000.0)

    def is_dead(self, now: float, dead_min: float, dead_mult: float) -> bool | None:
        """True once no echo has arrived for the dead interval. None = not enough data yet."""
        ref = self.last_reply_t if self.last_reply_t is not None else self.created_t
        dead = now - ref > self.dead_interval(dead_min, dead_mult)
        if self.last_reply_t is None and not dead:
            return None
        return dead

    def first_reply_after(self, t: float) -> float | None:
        for ts, r in list(self.outcomes):
            if ts > t and r is not None:
                return ts
        return None


class ProbeStore:
    """All probe streams, filled by agent reader threads, read by controller/collector."""

    def __init__(self) -> None:
        self.streams: dict[str, ProbeStream] = {}
        self.lock = threading.RLock()

    def ensure(self, sid: str, t: float) -> ProbeStream:
        with self.lock:
            s = self.streams.get(sid)
            if s is None:
                s = self.streams[sid] = ProbeStream(sid, t)
            return s

    def get(self, sid: str) -> ProbeStream | None:
        return self.streams.get(sid)
