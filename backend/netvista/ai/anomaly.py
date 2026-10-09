"""Learned-baseline anomaly detection: each signal learns its own normal, online.

Why not just thresholds? The health colours use fixed rules (loss >= 1 %, RTT > 1.5 x design
+ 2 ms, load >= 85 %). Those miss a link that is 3 ms slower than it has ever been, and they
flap on low, bursty loss. The detector instead learns, per signal, an exponentially weighted
mean and variance of what that signal *normally* measures, and flags a sustained deviation:

    scale = max(std, floor, rel_floor * |mean|)      floors stop a perfectly flat emulated
                                                     link from turning 0.05 ms of jitter into an alarm
    z     = (value - mean) / scale                   (only increases count for RTT/loss/load)
    raise   when z >= 4 for 3 consecutive samples (or z >= 8 for 2): ~2-3 s at 1 Hz
    clear   when z <  2 for 5 consecutive samples
    learn   only from samples that are not anomalous, so a fault never becomes "normal"
            (until the operator presses Re-learn)

This is unsupervised: nothing is trained on labelled faults. The evaluation suite
(ai/evaluation.py) measures what it catches against the real chaos lab.
"""

from __future__ import annotations

import itertools
import math
import threading
from collections import deque
from dataclasses import dataclass, field

from .signals import SignalSpec

WARMUP = 20  # samples (seconds) before a signal may raise anything
ALPHA = 0.05  # EWMA weight: ~20 s memory
Z_ON = 4.0
Z_STRONG = 8.0
Z_OFF = 2.0
N_ON = 3
N_ON_STRONG = 2
N_OFF = 5
HISTORY = 300  # seconds of (value, band) kept per signal for the charts


@dataclass
class Anomaly:
    id: int
    signal: str
    kind: str
    entity: str
    metric: str
    unit: str
    label: str
    t_start: float
    baseline_mean: float
    baseline_scale: float
    value: float
    z: float
    peak_value: float
    peak_z: float
    t_raised: float = 0.0  # when the alarm was declared (t_start is when the deviation began)
    t_end: float | None = None

    @property
    def active(self) -> bool:
        return self.t_end is None

    def to_dict(self) -> dict:
        def r(x: float) -> float:
            return round(x, 4 if self.unit == "ratio" else 3)

        return {
            "id": self.id, "signal": self.signal, "kind": self.kind, "entity": self.entity, "metric": self.metric,
            "unit": self.unit, "label": self.label, "t_start": self.t_start, "t_raised": self.t_raised, "t_end": self.t_end,
            "active": self.active,
            "value": r(self.value), "z": round(self.z, 1), "peak_value": r(self.peak_value), "peak_z": round(self.peak_z, 1),
            "normal_mean": r(self.baseline_mean), "normal_scale": r(self.baseline_scale),
            "normal_upper": r(self.baseline_mean + Z_ON * self.baseline_scale),
            "severity": "major" if self.peak_z >= Z_STRONG else "minor",
        }


@dataclass
class Series:
    spec: SignalSpec
    n: int = 0
    mean: float = 0.0
    var: float = 0.0
    state: str = "learning"  # learning | normal | anomalous
    hi: int = 0
    lo: int = 0
    hi_since: float | None = None
    last_value: float | None = None
    last_z: float | None = None
    last_t: float | None = None
    anomaly: Anomaly | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY))

    def scale(self) -> float:
        s = self.spec
        return max(math.sqrt(max(self.var, 0.0)), s.floor, s.rel_floor * abs(self.mean))

    def upper(self) -> float:
        up = self.mean + Z_ON * self.scale()
        return max(up, self.spec.min_level) if self.spec.min_level is not None else up

    def score(self, x: float) -> float:
        z = (x - self.mean) / self.scale()
        if self.spec.direction == "both":
            z = abs(z)
        if self.spec.min_level is not None and x < self.spec.min_level:
            z = min(z, Z_OFF - 0.01)  # below the level that matters: never anomalous
        return z

    def learn(self, x: float) -> None:
        self.n += 1
        if self.n == 1:
            self.mean, self.var = x, 0.0
            return
        a = max(ALPHA, 1.0 / self.n)  # cumulative average during warm-up, then EWMA
        d = x - self.mean
        self.mean += a * d
        self.var = (1 - a) * (self.var + a * d * d)


class AnomalyDetector:
    def __init__(self, specs: list[SignalSpec], on_raise=None, on_clear=None) -> None:
        self.series: dict[str, Series] = {s.id: Series(s) for s in specs}
        self.closed: deque[Anomaly] = deque(maxlen=200)
        self._ids = itertools.count(1)
        self.lock = threading.RLock()
        self.on_raise = on_raise
        self.on_clear = on_clear
        self.learned_since: float | None = None

    # ------------------------------------------------------------------ update
    def update(self, t: float, values: dict[str, float | None]) -> None:
        with self.lock:
            if self.learned_since is None:
                self.learned_since = t
            for sid, x in values.items():
                s = self.series.get(sid)
                if s is not None:
                    self._step(s, t, x)

    def _step(self, s: Series, t: float, x: float | None) -> None:
        s.last_t = t
        if x is None:
            # no measurement (no traffic, or no echo at all): keep the state, the loss signal
            # of the same stream carries a dead path
            s.last_value, s.last_z = None, None
            s.history.append((t, None, s.mean if s.n else None, s.upper() if s.n else None, s.state == "anomalous"))
            return
        s.last_value = x
        if s.state == "learning":
            s.learn(x)
            s.last_z = None
            if s.n >= WARMUP:
                s.state = "normal"
        elif s.state == "normal":
            z = s.score(x)
            s.last_z = z
            if z >= Z_ON:
                if s.hi == 0:
                    s.hi_since = t
                s.hi += 1
                if s.hi >= N_ON or (z >= Z_STRONG and s.hi >= N_ON_STRONG):
                    self._raise(s, t, x, z)
            else:
                s.hi, s.hi_since = 0, None
                s.learn(x)
        else:  # anomalous: frozen baseline, waiting for a sustained return to normal
            z = s.score(x)
            s.last_z = z
            a = s.anomaly
            if a is not None:
                a.value, a.z = x, z
                if z > a.peak_z:
                    a.peak_z, a.peak_value = z, x
            if z < Z_OFF:
                s.lo += 1
                if s.lo >= N_OFF:
                    self._clear(s, t)
            else:
                s.lo = 0
        s.history.append((t, x, s.mean, s.upper(), s.state == "anomalous"))

    def _raise(self, s: Series, t: float, x: float, z: float) -> None:
        spec = s.spec
        a = Anomaly(
            id=next(self._ids), signal=spec.id, kind=spec.kind, entity=spec.entity, metric=spec.metric, unit=spec.unit,
            label=spec.label, t_start=s.hi_since or t, baseline_mean=s.mean, baseline_scale=s.scale(),
            value=x, z=z, peak_value=x, peak_z=z, t_raised=t,
        )
        s.state, s.anomaly, s.hi, s.lo = "anomalous", a, 0, 0
        if self.on_raise:
            self.on_raise(a)

    def _clear(self, s: Series, t: float) -> None:
        a = s.anomaly
        s.state, s.anomaly, s.lo, s.hi, s.hi_since = "normal", None, 0, 0, None
        if a is not None:
            a.t_end = t
            self.closed.append(a)
            if self.on_clear:
                self.on_clear(a)

    def relearn(self) -> None:
        """Forget every baseline (e.g. after a deliberate, permanent change to the network)."""
        with self.lock:
            for sid, s in list(self.series.items()):
                if s.anomaly is not None:
                    s.anomaly.t_end = s.last_t
                    self.closed.append(s.anomaly)
                self.series[sid] = Series(s.spec)
            self.learned_since = None

    # ------------------------------------------------------------------ queries
    def active(self) -> list[Anomaly]:
        with self.lock:
            return [s.anomaly for s in self.series.values() if s.anomaly is not None]

    def recent(self, limit: int = 50) -> list[Anomaly]:
        with self.lock:
            return list(self.closed)[-limit:]

    def state_of(self, sid: str) -> Series | None:
        return self.series.get(sid)

    def summary(self) -> dict:
        with self.lock:
            states = [s.state for s in self.series.values()]
            learning = [s for s in self.series.values() if s.state == "learning"]
            # signals that never produce a value (e.g. data loss with no traffic) don't hold up warm-up
            fed = [s for s in learning if s.n > 0]
            return {
                "signals": len(states),
                "learning": states.count("learning"),
                "normal": states.count("normal"),
                "anomalous": states.count("anomalous"),
                "warmup_s": WARMUP,
                "warmup_progress": None if not fed else min(s.n for s in fed) / WARMUP,
                "learned_since": self.learned_since,
                "params": {"alpha": ALPHA, "z_on": Z_ON, "z_strong": Z_STRONG, "z_off": Z_OFF, "n_on": N_ON, "n_off": N_OFF},
            }

    def signal_table(self) -> list[dict]:
        with self.lock:
            out = []
            for s in self.series.values():
                spec = s.spec
                out.append({
                    "signal": spec.id, "kind": spec.kind, "entity": spec.entity, "metric": spec.metric, "unit": spec.unit,
                    "label": spec.label, "state": s.state, "samples": s.n,
                    "value": None if s.last_value is None else round(s.last_value, 4),
                    "normal_mean": round(s.mean, 4) if s.n else None,
                    "normal_scale": round(s.scale(), 4) if s.n else None,
                    "normal_upper": round(s.upper(), 4) if s.n else None,
                    "z": None if s.last_z is None else round(s.last_z, 2),
                })
            return out

    def history(self, sid: str, seconds: float = 300) -> list[dict]:
        with self.lock:
            s = self.series.get(sid)
            if s is None:
                raise KeyError(sid)
            if not s.history:
                return []
            t_end = s.history[-1][0]
            return [
                {"t": t, "value": v, "normal_mean": m, "normal_upper": u, "anomalous": an}
                for t, v, m, u, an in s.history if t >= t_end - seconds
            ]
