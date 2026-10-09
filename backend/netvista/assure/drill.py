"""Resilience drills: fail an element on the live network and check the prediction.

The failure analysis says, for example, "if r2 fails, I1 breaks and I2-I4 hold". A drill
proves or refutes that on the real emulation, the way chaos engineering does in production:

  1. predict   fluid model (the analysis' own prediction) and packet-level twin, frozen
  2. inject    the failure through the chaos lab (link down / router crash)
  3. watch     every intent each second from the moment of injection (the transient:
               detection, fail-over, re-balancing)
  4. measure   12 s of steady state after an 8 s settle
  5. revert    and wait until the links answer again (+ wait-to-restore in intent mode)
  6. compare   paths, every intent's outcome (predicted violated / held vs measured), and
               RTT / throughput / loss errors for both engines; residuals feed the
               uncertainty pool

The drill refuses to start while faults are active or another job owns the network.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any

from ..runtime import sanitize
from ..simulator.fluid import run_fluid_prediction
from ..validation.runner import compare
from .intents import evaluate_all
from .model import evaluate_paths, react, scenario_by_id, scenario_changes, with_failure

log = logging.getLogger(__name__)
SETTLE_S = 8.0
MEASURE_S = 12.0


class DrillService:
    def __init__(self, svc) -> None:
        self.svc = svc
        self.rt = svc.rt
        self.path = self.rt.s.runs_dir / "drills.jsonl"
        self.runs: deque[dict] = deque(maxlen=100)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict[str, Any] = {"running": False}
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines()[-100:]:
                self.runs.append(json.loads(line))
        except (OSError, ValueError):
            pass

    def status(self) -> dict:
        return dict(self._status)

    def list_runs(self) -> list[dict]:
        name = self.rt.topo.name
        return [r for r in reversed(self.runs) if r.get("topology", "netvista-default") == name]

    def stop(self) -> None:
        self._stop.set()

    def start(self, scenario_id: str) -> dict:
        sc = scenario_by_id(self.rt.topo, scenario_id)
        if self._thread and self._thread.is_alive():
            raise ValueError("a drill is already running")
        if self.rt.chaos.active():
            raise ValueError("revert the active faults first: a drill starts from a clean network")
        busy = self.svc.busy_job()
        if busy:
            raise ValueError(f"the {busy} job owns the network")
        self._stop.clear()
        self._status = {"running": True, "scenario": sc.id, "label": sc.label, "step": "predicting", "started": time.time(), "step_ends_at": None}
        self._thread = threading.Thread(target=self._job, args=(sc,), name="drill", daemon=True)
        self._thread.start()
        return self.status()

    def _sleep(self, secs: float, step: str) -> bool:
        self._status.update(step=step, step_ends_at=time.time() + secs)
        return not self._stop.wait(secs)

    def _job(self, sc) -> None:
        try:
            self._run(sc)
        except Exception as e:
            log.exception("drill failed")
            self.svc.emit("assure.drill", f"Drill '{sc.label}' failed: {e}", severity="error")
        finally:
            self._status = {"running": False, "finished": time.time()}

    def _run(self, sc) -> None:
        svc, rt = self.svc, self.rt
        sim = rt.extensions["simulator"]
        inj_ids: list[str] = []
        try:
            changes = scenario_changes(sc)
            b = svc.bundle()
            topo = rt.topo
            mode = rt.controller.mode
            # ---- 1. predict (frozen before anything is touched)
            rx = react(b["cfg"], sc, mode, b["plan"], topo)
            view, _ = evaluate_paths(with_failure(b["cfg"], sc), rx["paths"], b["rtt_margin"])
            pred_intents = {r.intent: r.status for r in evaluate_all(b["intents"], view, topo, svc.core)}
            cfg, _ = sim.build_config(changes, None, None, None, 12.0, 5)
            fluid = run_fluid_prediction(cfg)
            self._status["step"] = "predicting (packet twin)"
            packet = sim.predict(changes, duration_s=12.0, include_baseline=False, record=False)["result"]
            # ---- 2. inject
            self._status["step"] = "injecting"
            before = {r.intent: r.status for r in svc.intents.latest.values()}
            t_inj = time.time()
            for c in changes:
                inj_ids.append(rt.chaos.inject(c["kind"], c.get("target"), c.get("params") or {}, source="drill").id)
            # ---- 3/4. watch the transient, then measure
            timeline: list[tuple[float, dict[str, str]]] = []
            t_end = t_inj + SETTLE_S + MEASURE_S
            self._status.update(step="watching the failure", step_ends_at=t_end)
            t0 = t_inj + SETTLE_S
            while time.time() < t_end:
                if self._stop.wait(1.0):
                    return
                timeline.append((round(time.time() - t_inj, 1), {r.intent: r.status for r in svc.intents.latest.values()}))
                if time.time() >= t0:
                    self._status["step"] = "measuring steady state"
            t1 = time.time()
            meas = rt.extensions["validation"].measure(t0, t1)
        finally:
            self._status["step"] = "reverting"
            for iid in inj_ids:
                try:
                    rt.chaos.revert(iid, source="drill")
                except Exception:
                    pass
        if not inj_ids:
            return
        self._recover()
        measured_intents: dict[str, str] = {}
        steady = [s for t, s in timeline if t >= SETTLE_S]
        for iid in pred_intents:
            sts = [s.get(iid) for s in steady if s.get(iid) and s.get(iid) != "unknown"]
            measured_intents[iid] = "unknown" if not sts else ("violated" if sum(1 for s in sts if s == "violated") >= len(sts) / 2 else "held")
        cmp_intents = []
        for iid, p in pred_intents.items():
            pv = "violated" if p == "violated" else "held"
            m = measured_intents.get(iid, "unknown")
            cmp_intents.append({"intent": iid, "predicted": pv, "measured": m, "match": None if m == "unknown" else pv == m})
        viol_s = {iid: sum(1 for _, s in timeline if s.get(iid) == "violated") for iid in pred_intents}
        # settled: the first second from which no intent is violated that the prediction expected to hold
        def clean(s):
            return all(s.get(i) != "violated" or pred_intents.get(i) == "violated" for i in pred_intents)

        first_ok = None
        for k, (t, _) in enumerate(timeline):
            if all(clean(s) for _, s in timeline[k:]):
                first_ok = t
                break
        rows_f, sum_f = compare(fluid["pairs"], meas)
        rows_p, sum_p = compare(packet["pairs"], meas)
        svc.pool.add_rows("fluid", rows_f, source=f"drill {sc.id}")
        svc.pool.add_rows("packet", rows_p, source=f"drill {sc.id}")
        judged = [c for c in cmp_intents if c["match"] is not None]
        run = sanitize({
            "id": f"d{int(time.time() * 1000)}", "t": time.time(), "topology": rt.topo.name, "scenario": sc.to_dict(), "mode": mode,
            "plan": (b["plan"] or {}).get("id"), "changes": changes, "t_inject": t_inj, "window": [t0, t1],
            "predicted": {"paths_fluid": rx["paths"], "paths_packet": {p: v["path"] for p, v in packet["pairs"].items()},
                          "intents": pred_intents},
            "measured": {"paths": {p: v["path"] for p, v in meas.items()}, "intents": measured_intents, "pairs": meas},
            "before": before,
            "intents": cmp_intents,
            "intent_accuracy": None if not judged else round(100.0 * sum(1 for c in judged if c["match"]) / len(judged), 1),
            "paths_match_fluid": all(rx["paths"].get(p) == meas[p]["path"] for p in meas),
            "paths_match_packet": all(packet["pairs"][p]["path"] == meas[p]["path"] for p in meas),
            "transient": {"timeline": timeline, "violation_s": viol_s, "settled_after_s": first_ok},
            "rows_fluid": rows_f, "summary_fluid": sum_f, "rows_packet": rows_p, "summary_packet": sum_p,
        })
        self.runs.append(run)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(run, default=str) + "\n")
        except OSError:
            pass

        def fm(x, u):
            return "n/a" if x is None else f"{x:.1f}{u}"

        svc.emit(
            "assure.drill",
            f"Drill '{sc.label}' ({mode}): {sum(1 for c in judged if c['match'])}/{len(judged)} intent outcomes as predicted; "
            f"paths {'as predicted' if run['paths_match_fluid'] else 'DIFFERENT from the prediction'}; "
            f"RTT error fluid {fm(sum_f.get('latency_p50_mape'), '%')}, packet {fm(sum_p.get('latency_p50_mape'), '%')}",
            severity="success" if run["paths_match_fluid"] and (run["intent_accuracy"] or 0) >= 99 else "warn", drill=run["id"],
        )

    def _recover(self, timeout: float = 30.0) -> None:
        """Wait until every core link answers probes again (plus wait-to-restore in intent mode)."""
        rt = self.rt
        self._status["step"] = "waiting for the network to recover"
        t0 = time.time()
        while time.time() - t0 < timeout:
            if all(rt.controller.link_alive.values()):
                break
            if self._stop.wait(0.5):
                return
        extra = rt.controller.wtr_s + 2.0 if rt.controller.mode == "intent" else 3.0
        self._stop.wait(extra)
