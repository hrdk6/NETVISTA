"""Live benchmark: does intent planning actually beat the controllers we already had?

The same failures are injected into the real emulated network under each routing strategy,
with the same traffic and the same intents, and every intent is checked every second:

  static          Dijkstra shortest paths, never reacts
  adaptive-raw    the adaptive controller as originally built (herd guard off)
  adaptive        the adaptive controller with the herd guard
  intent          the planner's primary paths + pre-planned, pre-checked backups

Workload: the "stress" profile (every managed flow carries traffic, ~40 % of the source site's
uplinks in total) unless the caller passes rates. Intents: the operator's current intents.

Per (strategy, failure):
  violation-seconds   sum over intents of (priority weight x seconds violated) in the 20 s
                      after the failure, minus nothing: the steady state before the failure
                      is measured separately (baseline window), so a strategy that already
                      violates intents in normal operation pays for it there
  time to compliance  first second after which no intent is violated that was not violated
                      before the failure (None if it never happens within the window)
  data lost           Mbit offered minus Mbit received (iperf3) during the window
  route changes       path changes made by the controller during the window
  prediction check    the failure analysis' prediction (made just before the failure, for
                      that strategy) of which intents break, against what was measured

A full run (4 strategies x every core link and transit router) takes about half an hour.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any

from ..runtime import sanitize
from .model import scenario_by_id, scenario_changes, single_scenarios
from .resilience import analyze

log = logging.getLogger(__name__)
STRATEGIES = ("static", "adaptive-raw", "adaptive", "intent")
BASELINE_S = 8.0
WATCH_S = 20.0
WARMUP_S = 15.0


class BenchmarkService:
    def __init__(self, svc) -> None:
        self.svc = svc
        self.rt = svc.rt
        self.path = self.rt.s.runs_dir / "benchmarks.jsonl"
        self.runs: deque[dict] = deque(maxlen=20)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict[str, Any] = {"running": False}
        self._switches = 0
        self.rt.controller.on_switch.append(self._count_switch)
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines()[-20:]:
                self.runs.append(json.loads(line))
        except (OSError, ValueError):
            pass

    def _count_switch(self, *_a) -> None:
        self._switches += 1

    def status(self) -> dict:
        return dict(self._status)

    def list_runs(self) -> list[dict]:
        name = self.rt.topo.name
        return [r for r in reversed(self.runs) if r.get("topology", "netvista-default") == name]

    def stop(self) -> None:
        self._stop.set()

    def default_scenarios(self) -> list[str]:
        """Every core link, and every router that is not a site gateway (those are SPOFs)."""
        from .intents import edge_routers

        edge = edge_routers(self.rt.topo)
        return [s.id for s in single_scenarios(self.rt.topo) if not (s.kind == "node" and next(iter(s.nodes)) in edge)]

    def start(self, strategies: list[str] | None = None, scenarios: list[str] | None = None, rates: dict[str, float] | None = None) -> dict:
        if self._thread and self._thread.is_alive():
            raise ValueError("a benchmark is already running")
        if self.rt.chaos.active():
            raise ValueError("revert the active faults first")
        busy = self.svc.busy_job()
        if busy:
            raise ValueError(f"the {busy} job owns the network")
        if not self.svc.intents.list():
            raise ValueError("add intents first: the benchmark scores every strategy against your intents")
        strategies = strategies or list(STRATEGIES)
        for s in strategies:
            if s not in STRATEGIES:
                raise ValueError(f"unknown strategy {s!r}; use {', '.join(STRATEGIES)}")
        scenarios = scenarios or self.default_scenarios()
        for sid in scenarios:
            scenario_by_id(self.rt.topo, sid)
        rates = rates or self.svc.stress_profile()
        self._stop.clear()
        total = len(strategies) * len(scenarios)
        self._status = {"running": True, "strategies": strategies, "scenarios": scenarios, "rates": rates, "index": 0, "total": total,
                        "strategy": None, "scenario": None, "step": "starting", "started": time.time(), "step_ends_at": None,
                        "eta_s": total * (BASELINE_S + WATCH_S + 18) + len(strategies) * (WARMUP_S + 10)}
        self._thread = threading.Thread(target=self._job, args=(strategies, scenarios, rates), name="benchmark", daemon=True)
        self._thread.start()
        return self.status()

    # ------------------------------------------------------------------ helpers
    def _sleep(self, secs: float, step: str) -> bool:
        self._status.update(step=step, step_ends_at=time.time() + secs)
        return not self._stop.wait(secs)

    def _set_traffic(self, rates: dict[str, float]) -> None:
        tr = self.rt.traffic
        tr.stop_all("background")
        for pair, r in rates.items():
            if r > 0:
                src, dst = pair.split(">")
                tr.start_flow(src, dst, r)

    def _setup(self, strategy: str) -> str | None:
        ctrl = self.rt.controller
        if strategy == "static":
            ctrl.clear_plan("static") if ctrl.route_plan else ctrl.set_mode("static")
            return None
        if strategy in ("adaptive-raw", "adaptive"):
            ctrl.set_herd_guard(strategy == "adaptive")
            ctrl.clear_plan("adaptive") if ctrl.route_plan else ctrl.set_mode("adaptive")
            return None
        ctrl.set_herd_guard(True)
        if ctrl.mode != "adaptive":
            ctrl.clear_plan("adaptive") if ctrl.route_plan else ctrl.set_mode("adaptive")
        plan = self.svc.make_plan(source="benchmark")
        self.svc.apply_plan(plan["id"], source="benchmark", verify=False)
        return plan["id"]

    def _sample(self, t_rel: float) -> dict:
        svc = self.svc
        return {"t": round(t_rel, 1), "status": {r.intent: r.status for r in svc.intents.latest.values()}}

    def _window(self, secs: float, t_ref: float, step: str) -> list[dict] | None:
        out = []
        t_end = time.time() + secs
        self._status.update(step=step, step_ends_at=t_end)
        while time.time() < t_end:
            if self._stop.wait(1.0):
                return None
            out.append(self._sample(time.time() - t_ref))
        return out

    def _data(self, t0: float, t1: float) -> tuple[float, float]:
        """(Mbit offered, Mbit received) by the iperf3 flows in [t0, t1]."""
        tr = self.rt.traffic
        offered = received = 0.0
        for f in list(tr.flows.values()):
            if f.kind != "background" or f.started_at > t1 or (f.ended_at and f.ended_at < t0):
                continue
            seconds = [s for s in list(f.samples) if t0 < s[0] <= t1]
            received += sum(s[1] for s in seconds)
            offered += f.rate_mbps * len(seconds)
        return offered, received

    # ------------------------------------------------------------------ job
    def _job(self, strategies: list[str], scenarios: list[str], rates: dict[str, float]) -> None:
        rt, svc = self.rt, self.svc
        ctrl = rt.controller
        orig = {"mode": ctrl.mode, "plan": ctrl.route_plan, "herd_guard": ctrl.herd_guard}
        weights = {i.id: i.weight for i in svc.intents.list() if i.enabled}
        results: list[dict] = []
        t_start = time.time()
        try:
            rt.events.emit("assure.benchmark", f"Benchmark started: {', '.join(strategies)} x {len(scenarios)} failures, "
                           f"traffic {', '.join(f'{p} {r:g}' for p, r in rates.items())} Mbit/s", severity="info")
            self._status["step"] = "starting the benchmark traffic"
            self._set_traffic(rates)
            idx = 0
            for strategy in strategies:
                if self._stop.is_set():
                    break
                self._status.update(strategy=strategy, scenario=None)
                if not self._sleep(3.0, f"{strategy}: preparing"):
                    break
                plan_id = self._setup(strategy)
                if not self._sleep(WARMUP_S, f"{strategy}: settling"):
                    break
                for sid in scenarios:
                    if self._stop.is_set():
                        break
                    idx += 1
                    self._status.update(index=idx, scenario=sid)
                    sc = scenario_by_id(rt.topo, sid)
                    row = self._one(strategy, sc, weights)
                    if row is None:
                        break
                    row["plan"] = plan_id
                    results.append(row)
                    self._status["last"] = {k: row[k] for k in ("strategy", "scenario", "violation_s", "time_to_compliance_s")}
        except Exception as e:
            log.exception("benchmark failed")
            rt.events.emit("assure.benchmark", f"Benchmark aborted: {e}", severity="error")
        finally:
            try:
                rt.chaos.revert_all(source="benchmark")
                ctrl.set_herd_guard(orig["herd_guard"])
                ctrl.restore(orig["plan"], orig["mode"], source="benchmark end")
                rt.traffic.stop_all("background")
            except Exception:
                log.exception("benchmark cleanup")
            if results:
                self._finish(strategies, scenarios, rates, results, t_start)
            self._status = {"running": False, "finished": time.time()}

    def _one(self, strategy: str, sc, weights: dict[str, float]) -> dict | None:
        rt, svc = self.rt, self.svc
        label = f"{strategy}: {sc.label}"
        # the failure analysis' prediction for this strategy, frozen before the failure
        try:
            b = svc.bundle()
            b["bounds"] = False
            pred = svc._pool().submit(analyze, b).result(timeout=120)
            prow = next((r for r in pred["scenarios"] if r["scenario"]["id"] == sc.id), None)
        except Exception:
            prow = None
        base = self._window(BASELINE_S, time.time() + BASELINE_S, f"{label} - baseline")
        if base is None:
            return None
        before_bad = {i for s in base for i, st in s["status"].items() if st == "violated"}
        base_viol = sum(weights.get(i, 1.0) for s in base for i, st in s["status"].items() if st == "violated")
        sw0 = self._switches
        t_inj = time.time()
        inj_ids = []
        try:
            for c in scenario_changes(sc):
                inj_ids.append(rt.chaos.inject(c["kind"], c.get("target"), c.get("params") or {}, source="benchmark").id)
            watch = self._window(WATCH_S, t_inj, f"{label} - watching")
        finally:
            for iid in inj_ids:
                try:
                    rt.chaos.revert(iid, source="benchmark")
                except Exception:
                    pass
        if watch is None:
            return None
        t_end = time.time()
        offered, received = self._data(t_inj, t_end)
        switches = self._switches - sw0
        viol = sum(weights.get(i, 1.0) for s in watch for i, st in s["status"].items() if st == "violated")
        new_viol = sum(weights.get(i, 1.0) for s in watch for i, st in s["status"].items() if st == "violated" and i not in before_bad)
        ttc = None
        for k, s in enumerate(watch):
            if all(watch[j]["status"].get(i) != "violated" or i in before_bad for j in range(k, len(watch)) for i in watch[j]["status"]):
                ttc = s["t"]
                break
        steady = watch[-8:]
        measured_broken = sorted({i for i in weights if sum(1 for s in steady if s["status"].get(i) == "violated") >= len(steady) / 2})
        pred_broken = sorted(prow["violated"]) if prow else None
        # recover before the next failure: links answering, then every intent back to how it was
        # before this failure. At least 12 s: the latency intents use a 10 s RTT window, and a
        # shorter wait leaks this failure into the next one's baseline (found in the first run)
        self._status["step"] = f"{label} - recovering"
        t0 = time.time()
        while time.time() - t0 < 30 and not all(rt.controller.link_alive.values()):
            if self._stop.wait(0.5):
                return None
        if not self._sleep(max(12.0, rt.controller.wtr_s + 7.0 if strategy == "intent" else 12.0), f"{label} - recovering"):
            return None
        t1 = time.time()
        while time.time() - t1 < 30:
            now_bad = {r.intent for r in svc.intents.latest.values() if r.status == "violated"}
            if now_bad <= before_bad:
                break
            if self._stop.wait(1.0):
                return None
        return {
            "strategy": strategy, "scenario": sc.id, "label": sc.label,
            "baseline_violation_s": round(base_viol, 2), "violation_s": round(viol, 2), "new_violation_s": round(new_viol, 2),
            "time_to_compliance_s": ttc, "route_changes": switches,
            "offered_mbit": round(offered, 2), "received_mbit": round(received, 2), "lost_mbit": round(max(0.0, offered - received), 2),
            "measured_broken": measured_broken, "predicted_broken": pred_broken,
            "prediction_match": None if pred_broken is None else (set(pred_broken) == set(measured_broken)),
            "timeline": watch,
        }

    def _finish(self, strategies, scenarios, rates, results, t_start) -> None:
        summary = {}
        for s in strategies:
            rows = [r for r in results if r["strategy"] == s]
            if not rows:
                continue
            ttc = [r["time_to_compliance_s"] for r in rows if r["time_to_compliance_s"] is not None]
            pm = [r["prediction_match"] for r in rows if r["prediction_match"] is not None]
            summary[s] = {
                "failures": len(rows),
                "violation_s": round(sum(r["violation_s"] for r in rows), 1),
                "new_violation_s": round(sum(r["new_violation_s"] for r in rows), 1),
                "baseline_violation_s": round(sum(r["baseline_violation_s"] for r in rows), 1),
                "mean_time_to_compliance_s": round(sum(ttc) / len(ttc), 2) if ttc else None,
                "never_compliant": sum(1 for r in rows if r["time_to_compliance_s"] is None),
                "route_changes": sum(r["route_changes"] for r in rows),
                "lost_mbit": round(sum(r["lost_mbit"] for r in rows), 1),
                "delivered_pct": round(100.0 * sum(r["received_mbit"] for r in rows) / max(1e-9, sum(r["offered_mbit"] for r in rows)), 2),
                "prediction_accuracy_pct": round(100.0 * sum(pm) / len(pm), 1) if pm else None,
            }
        run = sanitize({
            "id": f"b{int(time.time() * 1000)}", "t": time.time(), "topology": self.rt.topo.name, "duration_s": round(time.time() - t_start, 1),
            "strategies": strategies, "scenarios": scenarios, "rates": rates,
            "intents": [i.to_dict() for i in self.svc.intents.list()],
            "results": results, "summary": summary,
        })
        self.runs.append(run)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(run, default=str) + "\n")
        except OSError:
            pass
        best = min(summary.items(), key=lambda kv: kv[1]["violation_s"])[0] if summary else None
        self.rt.events.emit(
            "assure.benchmark",
            "Benchmark finished: weighted violation-seconds " + ", ".join(f"{s} {v['violation_s']:g}" for s, v in summary.items())
            + (f"; best: {best}" if best else ""), severity="success", benchmark=run["id"],
        )
