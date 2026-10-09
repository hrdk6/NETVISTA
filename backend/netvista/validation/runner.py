"""Validation: does the twin predict what the real emulated network then does?

Protocol for one scenario (all automatic, logged as events):
  1. PREDICT  - run the twin on a copy of the current live state + the scenario's changes
                (prediction is frozen before anything is touched)
  2. APPLY    - inject exactly the same changes into the live network through the chaos engine
  3. SETTLE   - wait (default 6 s) for re-routing and queues to reach steady state
  4. MEASURE  - collect live probe RTTs and iperf3 receiver reports for a window (default 15 s)
  5. REVERT   - undo the changes, cool down
  6. COMPARE  - per flow and metric:
                  latency (RTT p50/p95/p99), throughput  -> relative error  |pred - meas| / meas
                  loss                                   -> absolute error in percentage points
                (relative error is meaningless when the true loss is ~0 %)
                  path                                   -> did the twin predict the live path?
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any

from ..routing.graph import core_hops
from ..telemetry.health import percentile

log = logging.getLogger(__name__)
LAT_METRICS = ("rtt_p50", "rtt_p95", "rtt_p99")


def rel_err(pred: float | None, meas: float | None) -> float | None:
    if pred is None or meas is None or abs(meas) < 1e-9:
        return None
    return abs(pred - meas) / abs(meas) * 100.0


def compare(pred_pairs: dict[str, dict], meas_pairs: dict[str, dict]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    for pair, m in meas_pairs.items():
        p = pred_pairs.get(pair)
        if p is None:
            continue
        active = (m.get("offered_mbps") or 0) > 0
        for metric in LAT_METRICS:
            rows.append({"pair": pair, "metric": metric, "unit": "ms", "kind": "relative",
                         "predicted": p.get(metric), "measured": m.get(metric), "error": rel_err(p.get(metric), m.get(metric))})
        rows.append({"pair": pair, "metric": "probe_loss_pct", "unit": "%", "kind": "absolute",
                     "predicted": p.get("probe_loss_pct"), "measured": m.get("probe_loss_pct"),
                     "error": None if p.get("probe_loss_pct") is None or m.get("probe_loss_pct") is None
                     else abs(p["probe_loss_pct"] - m["probe_loss_pct"])})
        if active:
            rows.append({"pair": pair, "metric": "rx_mbps", "unit": "Mbit/s", "kind": "relative",
                         "predicted": p.get("rx_mbps"), "measured": m.get("rx_mbps"), "error": rel_err(p.get("rx_mbps"), m.get("rx_mbps"))})
            rows.append({"pair": pair, "metric": "loss_pct", "unit": "%", "kind": "absolute",
                         "predicted": p.get("loss_pct"), "measured": m.get("loss_pct"),
                         "error": None if p.get("loss_pct") is None or m.get("loss_pct") is None else abs(p["loss_pct"] - m["loss_pct"])})
        rows.append({"pair": pair, "metric": "path", "unit": "", "kind": "match",
                     "predicted": "-".join(p.get("path") or []), "measured": "-".join(m.get("path") or []),
                     "error": 0.0 if p.get("path") == m.get("path") else 100.0})

    # aggregate over all traffic-carrying flows: what a shared bottleneck delivers in total.
    # Under tail-drop overload the per-flow split depends on packet micro-timing (NAPI batches,
    # timer phases) that no queueing model sees, while the total is governed by capacity.
    active = [p for p, m in meas_pairs.items() if (m.get("offered_mbps") or 0) > 0 and p in pred_pairs]
    agg = {}
    if active:
        off = {p: meas_pairs[p]["offered_mbps"] for p in active}
        tot = sum(off.values())

        def wloss(src):
            vals = [(off[p], src[p].get("loss_pct")) for p in active]
            return None if any(v is None for _, v in vals) else sum(o * v for o, v in vals) / tot

        def total(src):
            vals = [src[p].get("rx_mbps") for p in active]
            return None if any(v is None for v in vals) else sum(vals)

        pt, mt = total(pred_pairs), total(meas_pairs)
        pl, ml = wloss(pred_pairs), wloss(meas_pairs)
        rows.append({"pair": "all", "metric": "rx_mbps", "unit": "Mbit/s", "kind": "relative", "predicted": pt, "measured": mt, "error": rel_err(pt, mt)})
        rows.append({"pair": "all", "metric": "loss_pct", "unit": "%", "kind": "absolute", "predicted": pl, "measured": ml,
                     "error": None if pl is None or ml is None else abs(pl - ml)})
        agg = {"aggregate_throughput_err": rel_err(pt, mt), "aggregate_loss_err_pp": None if pl is None or ml is None else abs(pl - ml)}

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if xs else None

    summary = {
        **agg,
        "latency_mape": mean(r["error"] for r in rows if r["metric"] in LAT_METRICS),
        "latency_p50_mape": mean(r["error"] for r in rows if r["metric"] == "rtt_p50"),
        "latency_p95_mape": mean(r["error"] for r in rows if r["metric"] == "rtt_p95"),
        "throughput_mape": mean(r["error"] for r in rows if r["metric"] == "rx_mbps" and r["pair"] != "all"),
        "loss_mae_pp": mean(r["error"] for r in rows if r["metric"] == "loss_pct" and r["pair"] != "all"),
        "probe_loss_mae_pp": mean(r["error"] for r in rows if r["metric"] == "probe_loss_pct"),
        "path_match_pct": mean(100.0 - r["error"] for r in rows if r["metric"] == "path"),
        "n_rows": len(rows),
    }
    return rows, summary


class ValidationService:
    def __init__(self, rt) -> None:
        self.rt = rt
        self.path = rt.s.runs_dir / "validation_runs.jsonl"
        self.runs: deque[dict] = deque(maxlen=200)
        self._load()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict[str, Any] = {"running": False}

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                self.runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # ------------------------------------------------------------------ public
    def status(self) -> dict:
        return dict(self._status)

    def list_runs(self) -> list[dict]:
        return list(self.runs)[::-1]

    def stop(self) -> None:
        self._stop.set()

    def default_suite(self) -> list[dict]:
        topo, ctrl = self.rt.topo, self.rt.controller
        first = topo.traffic[0] if topo.traffic else None
        pair = f"{first.src}>{first.dst}" if first else next(iter(ctrl.flows))
        path = ctrl.flows[pair].static_path
        hops = core_hops(topo, path)
        link = min((lid for _, _, lid in hops), key=lambda l: topo.links[l].bw_mbps)
        lans = {r for s in self.rt.plan.subnets if s.kind == "lan" for r in s.routers}
        router = next((n for n in path if topo.nodes[n].type == "router" and n not in lans), None)
        bclient = topo.clients[-1]
        bserver = topo.servers[0]
        lat = [{"kind": "link_latency", "target": link, "params": {"add_ms": 40}}]
        loss = [{"kind": "link_loss", "target": link, "params": {"loss_pct": 5}}]
        burst = [{"kind": "traffic_burst", "target": f"{bclient}>{bserver}", "params": {"rate_mbps": 20, "duration_s": 90}}]
        # static rows keep the flows ON the impaired link -> they test the queueing/loss model;
        # adaptive rows let the controller react -> they test the twin's routing prediction
        suite = [
            {"label": "Baseline (no change)", "mode": "adaptive", "changes": []},
            {"label": f"+40 ms latency on {link}", "mode": "static", "changes": lat},
            {"label": f"+40 ms latency on {link}", "mode": "adaptive", "changes": lat},
            {"label": f"5% loss on {link}", "mode": "static", "changes": loss},
            {"label": f"5% loss on {link}", "mode": "adaptive", "changes": loss},
            {"label": f"Bandwidth cap 14 Mbit/s on {link} (89% load)", "mode": "static",
             "changes": [{"kind": "link_bandwidth", "target": link, "params": {"bw_mbps": 14}}]},
            {"label": f"Bandwidth cap 8 Mbit/s on {link} (overload)", "mode": "static",
             "changes": [{"kind": "link_bandwidth", "target": link, "params": {"bw_mbps": 8}}]},
            {"label": f"Burst 20 Mbit/s {bclient}→{bserver} (overload)", "mode": "static", "changes": burst},
            {"label": f"Burst 20 Mbit/s {bclient}→{bserver}", "mode": "adaptive", "changes": burst},
            {"label": f"Link {link} down", "mode": "adaptive", "changes": [{"kind": "link_down", "target": link}]},
        ]
        if router:
            suite.append({"label": f"Router {router} down", "mode": "adaptive", "changes": [{"kind": "node_down", "target": router}]})
        return suite

    def start_run(self, changes: list[dict], label: str | None, settle_s: float = 6.0, measure_s: float = 15.0, duration_s: float = 12.0) -> dict:
        from ..chaos import change_label, validate_change

        labels = []
        for c in changes:
            t, p = validate_change(self.rt.topo, c["kind"], c.get("target"), c.get("params") or {})
            labels.append(change_label(c["kind"], t, p))
        sc = {"label": label or ("; ".join(labels) or "Baseline (no change)"), "changes": changes}
        return self._start([sc], settle_s, measure_s, duration_s, suite=False)

    def start_suite(self, settle_s: float = 8.0, measure_s: float = 12.0, duration_s: float = 14.0) -> dict:
        return self._start(self.default_suite(), settle_s, measure_s, duration_s, suite=True)

    def _start(self, scenarios: list[dict], settle_s: float, measure_s: float, duration_s: float, suite: bool) -> dict:
        if self._thread and self._thread.is_alive():
            raise ValueError("a validation job is already running")
        if self.rt.chaos.active():
            raise ValueError("revert active faults first - validation needs a clean starting state")
        self._stop.clear()
        self._status = {"running": True, "suite": suite, "total": len(scenarios), "index": 0, "label": scenarios[0]["label"],
                        "step": "starting", "started": time.time(), "step_ends_at": None}
        self._thread = threading.Thread(target=self._job, args=(scenarios, settle_s, measure_s, duration_s), name="validation", daemon=True)
        self._thread.start()
        return self.status()

    # ------------------------------------------------------------------ job
    def _sleep(self, secs: float, step: str) -> bool:
        self._status.update(step=step, step_ends_at=time.time() + secs)
        return not self._stop.wait(secs)

    def _job(self, scenarios: list[dict], settle_s: float, measure_s: float, duration_s: float) -> None:
        rt = self.rt
        sim = rt.extensions["simulator"]
        try:
            if not any(f.kind == "background" for f in rt.traffic.running()):
                rt.events.emit("validation.info", "Starting the default traffic profile for validation")
                rt.start_default_traffic()
                if not self._sleep(16.0, "warming up traffic"):
                    return
            # calibrate in the same operating regime the scenarios will be measured in
            self._status["step"] = "calibrating twin (live, under traffic)"
            try:
                sim.calibrate()
            except ValueError as e:  # not steady: the previous calibration (or none) stays in use
                log.warning("calibration before validation failed: %s", e)
            original_mode = rt.controller.mode
            try:
                for i, sc in enumerate(scenarios):
                    if self._stop.is_set():
                        break
                    self._status.update(index=i, label=sc["label"], mode=sc.get("mode") or rt.controller.mode)
                    want = sc.get("mode")
                    if want and want != rt.controller.mode:
                        rt.controller.set_mode(want)
                        if not self._sleep(6.0, f"switching to {want} routing"):
                            break
                    self._run_one(sc, settle_s, measure_s, duration_s, suite_pos=(i + 1, len(scenarios)))
                    if i < len(scenarios) - 1 and not self._sleep(10.0, "cooling down"):
                        break
            finally:
                if rt.controller.mode != original_mode:
                    rt.controller.set_mode(original_mode)
        except Exception as e:
            log.exception("validation failed")
            rt.events.emit("validation.error", f"Validation aborted: {e}", severity="error")
            rt.chaos.revert_all(source="validation")
        finally:
            self._status = {"running": False, "finished": time.time()}

    def _run_one(self, sc: dict, settle_s: float, measure_s: float, duration_s: float, suite_pos: tuple[int, int]) -> None:
        rt = self.rt
        sim = rt.extensions["simulator"]
        rt.events.emit("validation.start", f"Validation {suite_pos[0]}/{suite_pos[1]}: {sc['label']} ({rt.controller.mode} routing)")
        self._status["step"] = "predicting (twin only)"
        pred = sim.predict(sc["changes"], duration_s=duration_s, include_baseline=False)
        # the fluid model's prediction of the same scenario, so both engines are scored live
        try:
            from ..simulator.fluid import run_fluid_prediction

            cfg_f, _ = sim.build_config(sc["changes"], None, None, None, duration_s, 1)
            fluid = run_fluid_prediction(cfg_f)
        except Exception:
            log.exception("fluid prediction failed")
            fluid = None
        self._status["step"] = "applying live"
        inj_ids = []
        try:
            for c in sc["changes"]:
                inj_ids.append(rt.chaos.inject(c["kind"], c.get("target"), c.get("params") or {}, source="validation").id)
            if not self._sleep(settle_s, "settling"):
                return
            t0 = time.time()
            if not self._sleep(measure_s, "measuring live"):
                return
            t1 = time.time()
            meas = self.measure(t0, t1)
        finally:
            self._status["step"] = "reverting"
            for iid in inj_ids:
                try:
                    rt.chaos.revert(iid, source="validation")
                except Exception:
                    pass
        rows, summary = compare(pred["result"]["pairs"], meas)
        rows_f, summary_f = compare(fluid["pairs"], meas) if fluid else ([], {})
        assure = rt.extensions.get("assure")
        if assure is not None:
            assure.pool.add_rows("packet", rows, source=f"validation {sc['label']}")
            if rows_f:
                assure.pool.add_rows("fluid", rows_f, source=f"validation {sc['label']}")
        run = {
            "id": f"v{int(time.time() * 1000)}",
            "t": time.time(),
            "label": sc["label"],
            "changes": sc["changes"],
            "mode": pred["mode"],
            "prediction_id": pred["id"],
            "settle_s": settle_s,
            "measure_window": [t0, t1],
            "sim_duration_s": duration_s,
            "sim_wall_s": pred["wall_s"],
            "calibration_t": pred["calibration_t"],
            "predicted": pred["result"]["pairs"],
            "measured": meas,
            "rows": rows,
            "summary": summary,
            "predicted_fluid": fluid["pairs"] if fluid else None,
            "fluid_wall_s": fluid["wall_s"] if fluid else None,
            "rows_fluid": rows_f,
            "summary_fluid": summary_f,
        }
        self.runs.append(run)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(run) + "\n")
        except OSError:
            pass

        def fmt(x, unit):
            return "n/a" if x is None else f"{x:.1f}{unit}"

        rt.events.emit(
            "validation.result",
            f"Validated '{sc['label']}': latency error {fmt(summary['latency_mape'], '%')}, throughput error "
            f"{fmt(summary['throughput_mape'], '%')}, loss error {fmt(summary['loss_mae_pp'], ' pp')}, paths matched {fmt(summary['path_match_pct'], '%')}",
            severity="success", run=run["id"],
        )

    def measure(self, t0: float, t1: float) -> dict[str, dict]:
        rt = self.rt
        out = {}
        hist = [h for h in list(rt.history) if t0 <= h["t"] <= t1]
        for pair, f in rt.controller.flows.items():
            s = rt.store.get(f"F:{pair}")
            samples = sorted(s.rtt_samples(t0, t1)) if s else []
            w = s.window(t0, t1) if s else []
            ts = rt.traffic.pair_stats(f.src, f.dst, t1 - t0, t1)
            seen = sorted({h["flows"][pair]["path"] for h in hist if pair in h["flows"] and h["flows"][pair]["path"]})
            out[pair] = {
                "path": list(f.path) if f.path else None,
                "paths_seen": seen,
                "offered_mbps": rt.traffic.offered_mbps(f.src, f.dst),
                "n_rtt": len(samples),
                "rtt_mean": sum(samples) / len(samples) if samples else None,
                "rtt_p50": percentile(samples, 50) if samples else None,
                "rtt_p95": percentile(samples, 95) if samples else None,
                "rtt_p99": percentile(samples, 99) if samples else None,
                "probe_loss_pct": 100.0 * sum(1 for _, r in w if r is None) / len(w) if w else None,
                "rx_mbps": ts["rx_mbps"] if ts else None,
                "loss_pct": ts["loss_pct"] if ts else None,
            }
        return out
