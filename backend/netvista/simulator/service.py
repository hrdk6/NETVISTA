"""Simulator service: builds twin configs from the live state and runs what-ifs.

The twin is built from the SAME topology JSON, the SAME effective tc parameters the chaos
engine applied, the SAME traffic demand the traffic manager is generating, the SAME current
paths/candidates the controller holds - plus the calibrated overheads. Then the requested
changes are applied ONLY to that copy; the live network is untouched.
"""

from __future__ import annotations

import itertools
import json
import logging
import multiprocessing as mp
import threading
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from ..chaos import change_label, validate_change
from ..config import wire_bytes
from .calibration import calibration_model_cfg, collect_live, finish_calibration, run_calibration_sim
from .whatif import run_prediction

log = logging.getLogger(__name__)


class SimulatorService:
    def __init__(self, rt) -> None:
        self.rt = rt
        self.calibration: dict | None = None
        self.predictions: deque[dict] = deque(maxlen=40)
        self._ids = itertools.count(1)
        self._pool = ProcessPoolExecutor(max_workers=2, mp_context=mp.get_context("spawn"))
        self._busy = 0
        self.lock = threading.Lock()
        self._cal_path = rt.s.runs_dir / "calibration.json"

    # ------------------------------------------------------------------ calibration
    def calibrate(self, window_s: float = 15.0) -> dict:
        live = collect_live(self.rt, window_s)
        if not live:
            raise ValueError("not enough probe samples yet - wait ~15 s after start-up")
        base_cfg, _ = self.build_config([], None, None, None, 8.0, 7)
        sim_probes = self._pool.submit(run_calibration_sim, calibration_model_cfg(base_cfg, live)).result(timeout=120)
        cal = finish_calibration(self.rt, live, sim_probes, window_s)
        if cal["n_streams"] == 0:
            raise ValueError("calibration produced no usable streams")
        self.calibration = cal
        try:
            self._cal_path.write_text(json.dumps(cal, indent=1))
        except OSError:
            pass
        load = cal["conditions"]["offered_mbps"]
        note = f" under {load:g} Mbit/s of traffic" if load else " on an idle network"
        if not cal["conditions"]["calm"]:
            note += " (faults were active - prefer calibrating without faults)"
        self.rt.events.emit(
            "sim.calibrate",
            f"Twin calibrated from {cal['n_streams']} live probe streams{note}: endpoint {cal['endpoint_ms']:.3f} ms, "
            f"per-hop {cal['per_hop_ms']:.3f} ms, fit RMS {cal['fit_rms_ms']:.3f} ms",
            severity="success",
        )
        return self.calibration_dict()

    def calibration_dict(self) -> dict:
        if not self.calibration:
            return {"t": None}
        c = dict(self.calibration)
        c["noise_samples"] = len(c.pop("noise_ms", []))
        return c

    def auto_calibrate(self, delay_s: float = 15.0) -> None:
        def run():
            time.sleep(delay_s)
            try:
                self.calibrate()
            except Exception as e:
                log.warning("auto-calibration failed: %s", e)

        threading.Thread(target=run, name="auto-calibrate", daemon=True).start()

    # ------------------------------------------------------------------ config
    def build_config(
        self,
        changes: list[dict],
        routing_mode: str | None,
        weights: dict | None,
        flow_rates: dict[str, float] | None,
        duration_s: float,
        seed: int,
    ) -> tuple[dict, list[str]]:
        rt = self.rt
        topo, ctrl = rt.topo, rt.controller
        now = time.time()
        links: dict[str, dict] = {}
        for lid, l in topo.links.items():
            p = rt.chaos.effective_params(lid)
            links[lid] = {"a": l.a, "b": l.b, **p.to_dict(), "up": rt.chaos.admin_up(lid)}
        pairs: dict[str, dict] = {}
        for pair, f in ctrl.flows.items():
            flows = []
            for fl in rt.traffic.running():
                if fl.src != f.src or fl.dst != f.dst:
                    continue
                if fl.kind == "burst" and fl.duration_s:
                    remaining = fl.started_at + fl.duration_s - now
                    if remaining <= 0.5:
                        continue
                    flows.append({"rate_mbps": fl.rate_mbps, "stop_s": remaining})
                else:
                    flows.append({"rate_mbps": fl.rate_mbps})
            pairs[pair] = {
                "src": f.src, "dst": f.dst, "path": list(f.path or f.static_path),
                "candidates": [list(c) for c in f.candidates], "static_path": list(f.static_path), "flows": flows,
            }
        labels = []
        for ch in changes:
            kind = ch["kind"]
            target, p = validate_change(topo, kind, ch.get("target"), ch.get("params") or {})
            labels.append(change_label(kind, target, p))
            if kind == "link_latency":
                base = topo.links[target]
                links[target]["delay_ms"] = base.delay_ms + p["add_ms"]
                links[target]["jitter_ms"] = p["jitter_ms"] or base.jitter_ms
            elif kind == "link_loss":
                links[target]["loss_pct"] = p["loss_pct"]
            elif kind == "link_bandwidth":
                links[target]["bw_mbps"] = p["bw_mbps"]
            elif kind == "link_down":
                links[target]["up"] = False
            elif kind == "node_down":
                for l in topo.links_of(target):
                    links[l.id]["up"] = False
            elif kind == "traffic_burst":
                if target not in pairs:
                    raise ValueError("what-if bursts must go from a client to a server")
                pairs[target]["flows"].append({"rate_mbps": p["rate_mbps"], "stop_s": p["duration_s"]})
        for pair, rate in (flow_rates or {}).items():
            if pair not in pairs:
                raise ValueError(f"unknown flow pair {pair}")
            pairs[pair]["flows"] = [{"rate_mbps": float(rate)}] if rate and rate > 0 else []
            labels.append(f"Demand {pair.replace('>', '→')} = {float(rate):g} Mbit/s")
        for p in pairs.values():
            p["offered_mbps"] = sum(f["rate_mbps"] for f in p["flows"])
        cal = self.calibration or {}
        cfg = {
            "links": links,
            "pairs": pairs,
            "routers": topo.routers,
            "mode": routing_mode or ctrl.mode,
            "weights": weights or ctrl.weights.to_dict(),
            "hysteresis": ctrl.hysteresis,
            "duration_s": duration_s,
            # long enough for an overloaded 1000-packet netem buffer to fill (steady state)
            "warmup_s": min(5.0, duration_s / 3),
            "seed": seed,
            "overhead": {
                "per_hop_ms": cal.get("per_hop_ms", 0.0),
                "endpoint_ms": cal.get("endpoint_ms", 0.0),
                "noise_ms": cal.get("noise_ms", []),
            },
            "probe_interval_s": rt.s.probe_interval_s,
            "probe_timeout_s": rt.s.probe_timeout_s,
            "payload_bytes": rt.s.iperf_payload_bytes,
            "wire_bytes": wire_bytes(rt.s.iperf_payload_bytes),
        }
        return cfg, labels

    # ------------------------------------------------------------------ live view (for side by side)
    def live_view(self) -> dict:
        rt = self.rt
        now = time.time()
        pairs = {}
        for pair, f in rt.controller.flows.items():
            pv = rt.telemetry.flow_probe_view(pair, now)
            ts = rt.traffic.pair_stats(f.src, f.dst, 5.0, now)
            pairs[pair] = {
                "path": f.path, "offered_mbps": rt.traffic.offered_mbps(f.src, f.dst),
                "rtt_p50": pv.get("rtt_p50"), "rtt_p95": pv.get("rtt_p95"), "rtt_p99": pv.get("rtt_p99"),
                "probe_loss_pct": pv.get("probe_loss_pct"),
                "rx_mbps": ts["rx_mbps"] if ts else None, "loss_pct": ts["loss_pct"] if ts else None,
            }
        links = {}
        for lid in rt.topo.links:
            v = rt.telemetry.link_view(lid, now)
            links[lid] = {"util": v["util"], "latency_ms": (v["rtt_ms"] / 2) if v["rtt_ms"] else None,
                          "loss_pct": v["loss_pct"], "health": v["health"]}
        return {"t": now, "pairs": pairs, "links": links}

    # ------------------------------------------------------------------ predict
    def run_cfg(self, cfg: dict, timeout: float = 240.0) -> dict:
        with self.lock:
            self._busy += 1
        try:
            return self._pool.submit(run_prediction, cfg).result(timeout=timeout)
        finally:
            with self.lock:
                self._busy -= 1

    def predict(
        self,
        changes: list[dict],
        duration_s: float = 8.0,
        routing_mode: str | None = None,
        weights: dict | None = None,
        flow_rates: dict[str, float] | None = None,
        include_baseline: bool = True,
        seed: int = 1,
        record: bool = True,
    ) -> dict:
        if routing_mode not in (None, "static", "adaptive"):
            raise ValueError("routing_mode must be static or adaptive")
        if self.calibration is None:
            try:
                self.calibrate()
            except ValueError:
                pass
        cfg, labels = self.build_config(changes, routing_mode, weights, flow_rates, duration_s, seed)
        live = self.live_view()
        t0 = time.time()
        fut_main = self._pool.submit(run_prediction, cfg)
        fut_base = None
        if include_baseline:
            base_cfg, _ = self.build_config([], routing_mode, weights, None, duration_s, seed)
            fut_base = self._pool.submit(run_prediction, base_cfg)
        with self.lock:
            self._busy += 1
        try:
            res = fut_main.result(timeout=240)
            base = fut_base.result(timeout=240) if fut_base else None
        finally:
            with self.lock:
                self._busy -= 1
        pred = {
            "id": f"p{next(self._ids)}",
            "t": t0,
            "label": "; ".join(labels) or "No change (twin of the current state)",
            "changes": changes,
            "mode": cfg["mode"],
            "weights": cfg["weights"],
            "duration_s": duration_s,
            "calibration_t": (self.calibration or {}).get("t"),
            "result": res,
            "baseline": base,
            "live": live,
            "wall_s": time.time() - t0,
            "label_kind": "SIMULATION",
        }
        if record:
            self.predictions.append(pred)
            moved = [p for p, v in res["pairs"].items() if v["rerouted"]]
            self.rt.events.emit(
                "sim.predict",
                f"Twin prediction '{pred['label']}' ({cfg['mode']}) in {pred['wall_s']:.1f}s"
                + (f"; predicts re-route of {', '.join(moved)}" if moved else ""),
                prediction=pred["id"],
            )
        return pred

    def list_predictions(self) -> list[dict]:
        return list(self.predictions)[::-1]

    def status(self) -> dict:
        return {"busy": self._busy > 0, "calibrated_at": (self.calibration or {}).get("t"), "n_predictions": len(self.predictions)}

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
