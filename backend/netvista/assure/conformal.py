"""Prediction intervals from the twin's own track record (split-conformal style), with honest coverage.

Every time a prediction meets reality - a validation run, a drill, an autopilot deployment -
the residual goes into a pool, per engine (packet / fluid) and metric class:

    rtt         relative error  (pred - meas) / meas      of RTT p50 / p95 / p99
    throughput  relative error                             of received Mbit/s
    loss        absolute error in percentage points        of data loss

The interval for a new prediction at level 1 - a uses the ceil((n + 1)(1 - a))-th smallest
absolute residual q:  RTT in [pred / (1 + q), pred * (1 + q)] (relative), loss in pred +- q.
That is split conformal prediction, and its coverage guarantee assumes the new case is
exchangeable with the pooled ones. Scenarios here are not drawn from one distribution (a
router crash is not like a 2 ms latency step), so the guarantee does not strictly apply.
Instead of claiming it, the pool *measures* coverage online: each new residual is first
tested against the interval computed from the residuals before it. The UI shows that
measured coverage next to the nominal level.

The planner and the intent checks use the RTT half-width as a safety margin: a predicted
latency whose upper bound crosses the limit is "at risk".
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

CLASSES = ("rtt", "throughput", "loss")
LEVEL = 0.9
MIN_N = 9  # ceil((n+1) * 0.9) <= n needs n >= 9


def metric_class(metric: str) -> str | None:
    if metric in ("rtt_p50", "rtt_p95", "rtt_p99"):
        return "rtt"
    if metric == "rx_mbps":
        return "throughput"
    if metric == "loss_pct":
        return "loss"
    return None


def residual(cls: str, pred: float, meas: float) -> float | None:
    if pred is None or meas is None:
        return None
    if cls == "loss":
        return pred - meas
    if abs(meas) < 1e-9:
        return None
    return (pred - meas) / abs(meas)


class ResidualPool:
    def __init__(self, path: Path, maxlen: int = 2000) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.items: dict[tuple[str, str], deque] = {}
        self.hits: dict[tuple[str, str], list[int]] = {}  # online coverage: [inside, tested]
        self.maxlen = maxlen
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                r = json.loads(line)
                self._add(r["engine"], r["cls"], float(r["r"]), persist=False)
            except (ValueError, KeyError, TypeError):
                continue

    def _add(self, engine: str, cls: str, r: float, persist: bool = True, meta: dict | None = None) -> None:
        key = (engine, cls)
        with self.lock:
            q = self._quantile_locked(key, LEVEL)
            if q is not None:
                h = self.hits.setdefault(key, [0, 0])
                h[1] += 1
                if abs(r) <= q + 1e-12:
                    h[0] += 1
            self.items.setdefault(key, deque(maxlen=self.maxlen)).append(r)
        if persist:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": time.time(), "engine": engine, "cls": cls, "r": round(r, 6), **(meta or {})}) + "\n")
            except OSError:
                pass

    def add(self, engine: str, metric: str, pred: float | None, meas: float | None, source: str = "") -> None:
        cls = metric_class(metric)
        if cls is None:
            return
        r = residual(cls, pred, meas)
        if r is None or not math.isfinite(r):
            return
        self._add(engine, cls, r, meta={"metric": metric, "source": source})

    def add_rows(self, engine: str, rows: list[dict], source: str) -> int:
        """Validation-style comparison rows (pair, metric, predicted, measured)."""
        n = 0
        for row in rows:
            if row.get("pair") == "all" or row.get("kind") == "match":
                continue
            p, m = row.get("predicted"), row.get("measured")
            if isinstance(p, (int, float)) and isinstance(m, (int, float)):
                before = self.count(engine, metric_class(row["metric"]) or "")
                self.add(engine, row["metric"], float(p), float(m), source)
                n += self.count(engine, metric_class(row["metric"]) or "") - before
        return n

    # ---------------------------------------------------------------- queries
    def count(self, engine: str, cls: str) -> int:
        return len(self.items.get((engine, cls), ()))

    def _quantile_locked(self, key: tuple[str, str], level: float) -> float | None:
        xs = sorted(abs(x) for x in self.items.get(key, ()))
        n = len(xs)
        k = math.ceil((n + 1) * level)
        if n < MIN_N or k > n:
            return None
        return xs[k - 1]

    def half_width(self, engine: str, cls: str, level: float = LEVEL) -> tuple[float | None, int, str]:
        """(q, n, source): falls back to the other engine's pool while this one is too small."""
        with self.lock:
            q = self._quantile_locked((engine, cls), level)
            if q is not None:
                return q, len(self.items[(engine, cls)]), engine
            other = "packet" if engine == "fluid" else "fluid"
            q = self._quantile_locked((other, cls), level)
            if q is not None:
                return q, len(self.items[(other, cls)]), other
        return None, 0, "none"

    def rtt_margin(self, engine: str = "fluid") -> float:
        q, _, _ = self.half_width(engine, "rtt")
        return float(q) if q is not None else 0.0

    def summary(self) -> dict[str, Any]:
        out = {}
        with self.lock:
            keys = sorted(self.items)
        for engine, cls in keys:
            xs = list(self.items[(engine, cls)])
            q, n, src = self.half_width(engine, cls)
            hit = self.hits.get((engine, cls), [0, 0])
            bias = sum(xs) / len(xs) if xs else None
            out[f"{engine}:{cls}"] = {
                "engine": engine, "cls": cls, "n": len(xs), "level": LEVEL,
                "half_width": None if q is None else round(q, 5), "half_width_source": src,
                "unit": "pp" if cls == "loss" else "relative",
                "bias": None if bias is None else round(bias, 5),
                "coverage": None if not hit[1] else round(hit[0] / hit[1], 4), "coverage_tested": hit[1],
            }
        return out
