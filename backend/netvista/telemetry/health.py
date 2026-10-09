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
from typing import Callable


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


AGENT_STALE_S = 3.0  # an agent silent this long is itself in trouble: judge on the backend's clock

# Host stalls. On WSL2 the packet path of *every* namespace freezes for ~1.2-1.9 s about every 32 s,
# reproduced with two bare namespaces and a veth, no NETVISTA code (DESIGN.md section 11.10). A real
# failure silences the probes that cross it; a stall silences all of them at the same instant. So:
STALL_MUTE_S = 0.5        # a stream is mute after 5 missed probe intervals
STALL_SHARE = 0.8         # a host stall is >= 80% of all streams mute at once (worst real N-1: ~55%)
STALL_CLEAR_SHARE = 0.5   # ...and it is over once at least half of them answer again
STALL_MIN_STREAMS = 6     # with fewer streams a stall cannot be told from an outage
STALL_MAX_S = 4.0         # longer than any stall measured (1.9 s): a real mass outage, judged normally


@dataclass
class HostStall:
    start: float               # the moment the streams went mute (median of their last echoes)
    streams: int               # how many streams were being watched
    mute: int                  # how many went mute together
    end: float | None = None   # None while it lasts
    outage: bool = False       # lasted past STALL_MAX_S: not a stall, its time counts as silence

    def to_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": None if self.end is None else round(self.end, 3),
                "duration_s": None if self.end is None else round(self.end - self.start, 3),
                "streams": self.streams, "mute": self.mute, "outage": self.outage}


def stalled_between(stalls, a: float, b: float) -> float:
    """Seconds of [a, b] that fell inside host stalls (an open stall extends to b)."""
    total = 0.0
    for st in reversed(stalls):
        end = b if st.end is None else st.end
        if end <= a:
            break
        if not st.outage:
            total += max(0.0, min(b, end) - max(a, st.start))
    return total


def overlaps_stall(stalls, a: float, b: float) -> bool:
    for st in reversed(stalls):
        end = math.inf if st.end is None else st.end
        if end <= a:
            return False
        if not st.outage and st.start < b:
            return True
    return False


class ProbeStream:
    def __init__(self, sid: str, created_t: float, window: int = 600, stalls: deque | None = None) -> None:
        self.sid = sid
        self.created_t = created_t
        self.outcomes: deque[tuple[float, float | None]] = deque(maxlen=window)  # (t, rtt_ms or None=lost)
        self.last_reply_t: float | None = None
        self.seen_until: float | None = None  # agent wall time of its latest report on this stream
        self.srtt: float | None = None
        self.replies = 0
        self.losses = 0
        self.stalls: deque[HostStall] = stalls if stalls is not None else deque()  # shared with the store
        self.excluded = 0  # outcomes that crossed a host stall: they measure the host, not the network

    def observed_now(self, now: float) -> float:
        """The latest moment we have the agent's word for. Silence is only measured up to here, so a
        late report (backend stall) delays a verdict instead of faking a failure."""
        if self.seen_until is None or now - self.seen_until > AGENT_STALE_S:
            return now
        return min(now, self.seen_until)

    def silence(self, now: float) -> float | None:
        """Seconds without an echo, up to what the agent has reported, host stalls not counted."""
        if self.last_reply_t is None:
            return None
        t1 = self.observed_now(now)
        return max(0.0, t1 - self.last_reply_t - stalled_between(self.stalls, self.last_reply_t, t1))

    def on_reply(self, t: float, rtt: float) -> None:
        self.last_reply_t = t if self.last_reply_t is None else max(t, self.last_reply_t)
        self.replies += 1
        if self.stalls and overlaps_stall(self.stalls, t - rtt / 1000.0, t):
            self.excluded += 1  # still proof of life, but its RTT is the host's freeze
            return
        self.outcomes.append((t, rtt))
        self.srtt = rtt if self.srtt is None else 0.8 * self.srtt + 0.2 * rtt

    def on_loss(self, t: float, until: float | None = None) -> None:
        """t: when the lost probe was sent; until: when it was given up on."""
        self.losses += 1
        if self.stalls and overlaps_stall(self.stalls, t, until if until is not None else t):
            self.excluded += 1
            return
        self.outcomes.append((t, None))

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
        t1 = self.observed_now(now)
        dead = t1 - ref - stalled_between(self.stalls, ref, t1) > self.dead_interval(dead_min, dead_mult)
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
        self.stalls: deque[HostStall] = deque(maxlen=256)
        self.on_stall: Callable[[HostStall], None] | None = None  # called when a stall ends

    def ensure(self, sid: str, t: float) -> ProbeStream:
        with self.lock:
            s = self.streams.get(sid)
            if s is None:
                s = self.streams[sid] = ProbeStream(sid, t, stalls=self.stalls)
            return s

    def get(self, sid: str) -> ProbeStream | None:
        return self.streams.get(sid)

    def check_stall(self, t: float) -> HostStall | None:
        """Called after every agent report. Opens a stall when most streams went mute together,
        closes it when they answer again. Returns a stall that just ended."""
        with self.lock:
            live = [s.last_reply_t for s in self.streams.values() if s.last_reply_t is not None]
            if len(live) < STALL_MIN_STREAMS:
                return None
            mute = sorted(x for x in live if t - x > STALL_MUTE_S)
            share = len(mute) / len(live)
            cur = self.stalls[-1] if self.stalls and self.stalls[-1].end is None else None
            if cur is None:
                if share >= STALL_SHARE:
                    self.stalls.append(HostStall(start=mute[len(mute) // 2], streams=len(live), mute=len(mute)))
                return None
            cur.mute = max(cur.mute, len(mute))
            if not cur.outage and t - cur.start > STALL_MAX_S:
                cur.outage = True  # everything is really quiet: stop excusing it
            if share >= STALL_CLEAR_SHARE:
                return None
            cur.end = t
        if self.on_stall:
            self.on_stall(cur)
        return cur

    def stall_summary(self) -> dict:
        done = [s for s in list(self.stalls) if s.end is not None and not s.outage]
        durs = [s.end - s.start for s in done]
        gaps = [b.start - a.start for a, b in zip(done, done[1:])]
        cur = self.stalls[-1] if self.stalls and self.stalls[-1].end is None else None
        return {
            "count": len(done),
            "total_s": round(sum(durs), 2),
            "longest_s": round(max(durs), 3) if durs else None,
            "median_period_s": round(sorted(gaps)[len(gaps) // 2], 1) if gaps else None,
            "excluded_samples": sum(s.excluded for s in list(self.streams.values())),
            "active": cur.to_dict() if cur else None,
            "last": done[-1].to_dict() if done else None,
        }
