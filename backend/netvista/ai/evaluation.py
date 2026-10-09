"""AI evaluation: score the anomaly detector and the root-cause analysis against real faults.

Protocol (all automatic, on the live emulated network, logged as events):
  0. traffic   start the default iperf3 profile if it is not running; static routing for the
               run (so a fault stays on the flows' path instead of being routed around)
  1. quiet     60 s with no fault: every anomaly raised here is a FALSE ALARM; every
               ok -> degraded flip of the threshold health colours is a threshold false alarm
  2. per fault inject through the chaos lab, then poll every 0.5 s for up to 25 s:
                 learned detector   first anomaly that starts after the injection
                 threshold health   first link on the fault's path that turns degraded/down
                 diagnosis          root-cause analysis 4 s after the first alarm, compared with
                                    the injected fault (location = element, type = fault class)
               revert, wait until every anomaly has cleared (max 45 s)
  3. summary   detection rate and mean time-to-detect for both methods, diagnosis accuracy,
               false alarms per minute.

The diagnosis never reads the chaos lab; only this scorer does.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any

from ..routing.graph import core_hops

log = logging.getLogger(__name__)

QUIET_S = 60.0
DETECT_TIMEOUT_S = 25.0
DIAG_DELAY_S = 4.0
COOLDOWN_MAX_S = 45.0
POLL_S = 0.5

EXPECTED_TYPE = {
    "link_latency": {"latency"},
    "link_loss": {"packet_loss"},
    "link_down": {"link_down"},
    "node_down": {"node_down"},
    "link_bandwidth": {"congestion", "traffic_surge"},
    "traffic_burst": {"traffic_surge"},
}


class AIEvaluationService:
    def __init__(self, rt, ai) -> None:
        self.rt = rt
        self.ai = ai
        self.path = rt.s.runs_dir / "ai_evaluations.jsonl"
        self.runs: deque[dict] = deque(maxlen=50)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict[str, Any] = {"running": False}
        if self.path.exists():
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

    def scenarios(self) -> list[dict]:
        rt = self.rt
        topo, ctrl = rt.topo, rt.controller
        first = topo.traffic[0] if topo.traffic else None
        pair = f"{first.src}>{first.dst}" if first else next(iter(ctrl.flows))
        path = ctrl.flows[pair].static_path
        hops = core_hops(topo, path)
        link = min((lid for _, _, lid in hops), key=lambda l: topo.links[l].bw_mbps)
        lans = {r for s in rt.plan.subnets if s.kind == "lan" for r in s.routers}
        router = next((n for n in path if topo.nodes[n].type == "router" and n not in lans), None)
        cap = topo.links[link].bw_mbps
        # a second client sends to the same server, so the burst shares the bottleneck
        bsrc = next((c for c in topo.clients if c != pair.split(">")[0]), topo.clients[0])
        bdst = pair.split(">")[1]
        burst_rate = round(cap * 0.95, 1)
        out = [
            {"label": f"Subtle latency: +2 ms on {link}", "kind": "link_latency", "target": link, "params": {"add_ms": 2},
             "expect": link, "subtle": True},
            {"label": f"+25 ms latency on {link}", "kind": "link_latency", "target": link, "params": {"add_ms": 25}, "expect": link},
            {"label": f"Subtle loss: 1% on {link}", "kind": "link_loss", "target": link, "params": {"loss_pct": 1}, "expect": link,
             "subtle": True},
            {"label": f"10% loss on {link}", "kind": "link_loss", "target": link, "params": {"loss_pct": 10}, "expect": link},
            {"label": f"Bandwidth cap 4 Mbit/s on {link} (overload)", "kind": "link_bandwidth", "target": link,
             "params": {"bw_mbps": 4}, "expect": link},
            {"label": f"Traffic surge {burst_rate:g} Mbit/s {bsrc}→{bdst}", "kind": "traffic_burst", "target": f"{bsrc}>{bdst}",
             "params": {"rate_mbps": burst_rate, "duration_s": 60}, "expect": link},
            {"label": f"Link {link} down", "kind": "link_down", "target": link, "params": {}, "expect": link},
        ]
        if router:
            out.append({"label": f"Router {router} down", "kind": "node_down", "target": router, "params": {}, "expect": router})
        return out

    def start(self) -> dict:
        rt = self.rt
        if self._thread and self._thread.is_alive():
            raise ValueError("an AI evaluation is already running")
        if rt.chaos.active():
            raise ValueError("revert active faults first: the evaluation needs a clean starting state")
        for name in ("validation", "demo", "replayer"):
            ext = rt.extensions.get(name)
            if ext and ext.status().get("running"):
                raise ValueError(f"wait for the {name} job to finish first")
        sc = self.scenarios()
        self._stop.clear()
        self._status = {"running": True, "total": len(sc), "index": -1, "label": "preparing", "step": "starting",
                        "started": time.time(), "step_ends_at": None}
        self._thread = threading.Thread(target=self._job, args=(sc,), name="ai-eval", daemon=True)
        self._thread.start()
        return self.status()

    # ------------------------------------------------------------------ job
    def _sleep(self, secs: float, step: str) -> bool:
        self._status.update(step=step, step_ends_at=time.time() + secs)
        return not self._stop.wait(secs)

    def _related_links(self, sc: dict) -> list[str]:
        topo = self.rt.topo
        if sc["kind"] == "node_down":
            return [l.id for l in topo.links_of(sc["target"])]
        if sc["kind"] == "traffic_burst":
            f = self.rt.controller.flows.get(sc["target"])
            path = (f.path if f else None) or []
            return [lid for _, _, lid in core_hops(topo, path)]
        return [sc["target"]]

    def _health(self, links: list[str]) -> dict[str, str]:
        now = time.time()
        return {lid: self.rt.telemetry.link_view(lid, now)["health"] for lid in links}

    def _job(self, scenarios: list[dict]) -> None:
        rt, ai = self.rt, self.ai
        original_mode = rt.controller.mode
        result: dict[str, Any] = {"id": f"ai{int(time.time() * 1000)}", "t": time.time(), "scenarios": [], "quiet": None}
        try:
            if not any(f.kind == "background" for f in rt.traffic.running()):
                rt.events.emit("ai.eval", "AI evaluation: starting the default traffic profile")
                rt.start_default_traffic()
            if original_mode != "static":
                rt.controller.set_mode("static")
            # baselines must be learned under the same traffic + routing the faults will meet
            ai.relearn()
            if not self._sleep(30.0, "learning normal behaviour under traffic (30 s)"):
                return
            result["quiet"] = self._quiet()
            if self._stop.is_set():
                return
            for i, sc in enumerate(scenarios):
                if self._stop.is_set():
                    break
                self._status.update(index=i, label=sc["label"])
                result["scenarios"].append(self._run_one(sc, i + 1, len(scenarios)))
            if not self._stop.is_set():
                result["summary"] = summarize(result)
                result["duration_s"] = round(time.time() - result["t"])
                self.runs.append(result)
                try:
                    with self.path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(result) + "\n")
                except OSError:
                    pass
                s = result["summary"]
                rt.events.emit(
                    "ai.eval",
                    f"AI evaluation finished: learned detector caught {s['ai_detected']}/{s['faults']} faults "
                    f"(threshold rules {s['threshold_detected']}/{s['faults']}), diagnosis correct {s['diagnosis_correct']}/{s['faults']}, "
                    f"{s['false_alarms_per_min']:.2f} false alarms/min",
                    severity="success", run=result["id"],
                )
        except Exception as e:
            log.exception("AI evaluation failed")
            rt.events.emit("ai.eval", f"AI evaluation aborted: {e}", severity="error")
        finally:
            try:
                rt.chaos.revert_all(source="ai-evaluation")
            except Exception:
                pass
            if rt.controller.mode != original_mode:
                rt.controller.set_mode(original_mode)
            self._status = {"running": False, "finished": time.time()}

    def _quiet(self) -> dict:
        ai = self.ai
        links = list(self.rt.topo.links)
        t0 = time.time()
        self._status.update(index=-1, label="Quiet period (no faults)", step="measuring false alarms", step_ends_at=t0 + QUIET_S)
        before = {a.id for a in ai.detector.active()} | {a.id for a in ai.detector.recent(200)}
        prev = self._health(links)
        thr_flags = 0
        diag_flags = 0
        while time.time() - t0 < QUIET_S and not self._stop.wait(POLL_S):
            cur = self._health(links)
            thr_flags += sum(1 for l in links if prev[l] == "ok" and cur[l] in ("degraded", "down"))
            prev = cur
            if ai.diagnosis.get("causes"):
                diag_flags += 1
        dur = time.time() - t0
        raised = [a for a in ai.detector.active() + ai.detector.recent(200) if a.id not in before and a.t_start >= t0 - 1]
        return {
            "duration_s": round(dur, 1),
            "ai_false_alarms": len(raised),
            "ai_false_alarm_signals": sorted({a.label for a in raised}),
            "threshold_false_alarms": thr_flags,
            "diagnosis_false_s": round(diag_flags * POLL_S, 1),
        }

    def _run_one(self, sc: dict, pos: int, total: int) -> dict:
        rt, ai = self.rt, self.ai
        rt.events.emit("ai.eval", f"AI evaluation {pos}/{total}: {sc['label']}")
        related = self._related_links(sc)
        prev = self._health(related)
        seen_before = {a.id for a in ai.detector.active()} | {a.id for a in ai.detector.recent(200)}
        self._status["step"] = "injecting"
        inj = rt.chaos.inject(sc["kind"], sc["target"], sc["params"], source="ai-evaluation")
        t_inj = inj.t
        t_ai = t_thr = t_alarm = None
        flaps = 0
        first_signal = None
        diag: dict | None = None
        t_diag_correct = None
        exp_types = EXPECTED_TYPE[sc["kind"]]
        deadline = t_inj + DETECT_TIMEOUT_S
        self._status.update(step="waiting for detection", step_ends_at=deadline)
        while not self._stop.wait(POLL_S):
            now = time.time()
            new = [a for a in ai.detector.active() + ai.detector.recent(200) if a.id not in seen_before and a.t_start >= t_inj - 0.5]
            if new and t_ai is None:
                # detection time = when the alarm was raised, not when the deviation began
                first = min(new, key=lambda a: a.t_raised)
                t_ai, first_signal = first.t_raised, first.label
            cur = self._health(related)
            if t_thr is None and any(h in ("degraded", "down") for h in cur.values()):
                t_thr = now
            flaps += sum(1 for l in related if (prev[l] == "ok") != (cur[l] == "ok") and "unknown" not in (prev[l], cur[l]))
            prev = cur
            causes = ai.diagnosis.get("causes") or []
            if t_diag_correct is None and causes and causes[0]["element"] == sc["expect"] and causes[0]["type"] in exp_types:
                t_diag_correct = now
            alarms = [t for t in (t_ai, t_thr) if t is not None]
            if causes and t_alarm is None:
                alarms.append(now)
            if alarms and t_alarm is None:
                t_alarm = min(alarms)
                self._status.update(step="diagnosing", step_ends_at=now + DIAG_DELAY_S)
            if t_alarm is not None and now >= t_alarm + DIAG_DELAY_S and diag is None:
                diag = json.loads(json.dumps(ai.diagnosis, default=str))
            if (diag is not None and t_ai is not None and t_thr is not None) or now >= deadline:
                if diag is None:
                    diag = json.loads(json.dumps(ai.diagnosis, default=str))
                break
        # observed over the fault window: how many anomalies, how often thresholds flipped
        n_anom = len([a for a in ai.detector.active() + ai.detector.recent(200) if a.id not in seen_before and a.t_start >= t_inj - 0.5])
        self._status["step"] = "reverting"
        try:
            rt.chaos.revert(inj.id, source="ai-evaluation")
        except Exception:
            pass
        top = (diag or {}).get("causes") or []
        top0 = top[0] if top else None
        loc_ok = bool(top0 and (top0["element"] == sc["expect"] or sc["expect"] in top0.get("alternatives", [])))
        type_ok = bool(top0 and top0["type"] in exp_types)
        row = {
            "label": sc["label"], "kind": sc["kind"], "target": sc["target"], "expect_element": sc["expect"],
            "expect_type": sorted(exp_types), "subtle": bool(sc.get("subtle")),
            "ai_detected": t_ai is not None, "ai_detect_s": None if t_ai is None else round(max(0.0, t_ai - t_inj), 2),
            "ai_first_signal": first_signal, "ai_anomalies": n_anom,
            "threshold_detected": t_thr is not None, "threshold_detect_s": None if t_thr is None else round(t_thr - t_inj, 2),
            "threshold_flaps": flaps,
            "diagnosis": None if not top0 else {k: top0[k] for k in ("title", "type", "element", "confidence", "alternatives")},
            "diagnosis_other_causes": [c["title"] for c in top[1:]],
            "diagnosis_location_ok": loc_ok, "diagnosis_type_ok": type_ok, "diagnosis_correct": loc_ok and type_ok,
            "diagnosis_correct_after_s": None if t_diag_correct is None else round(t_diag_correct - t_inj, 2),
        }
        rt.events.emit(
            "ai.eval",
            f"  {sc['label']}: detector {'%.1f s' % row['ai_detect_s'] if row['ai_detected'] else 'missed'}, "
            f"thresholds {'%.1f s' % row['threshold_detect_s'] if row['threshold_detected'] else 'missed'}, diagnosis "
            + (f"'{top0['title']}' ({'correct' if row['diagnosis_correct'] else 'wrong'})" if top0 else "none"),
            severity="success" if row["diagnosis_correct"] else "warn",
        )
        # cool down: every anomaly cleared and no cause left (or give up after COOLDOWN_MAX_S)
        t0 = time.time()
        self._status.update(step="cooling down", step_ends_at=t0 + COOLDOWN_MAX_S)
        while time.time() - t0 < COOLDOWN_MAX_S and not self._stop.wait(1.0):
            if not ai.detector.active() and not ai.diagnosis.get("causes") and time.time() - t0 > 6:
                break
        self._sleep(3.0, "cooling down")
        return row


def summarize(result: dict) -> dict:
    rows = result["scenarios"]
    n = len(rows)

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 2) if xs else None

    q = result.get("quiet") or {}
    minutes = (q.get("duration_s") or 60) / 60
    subtle = [r for r in rows if r["subtle"]]
    return {
        "faults": n,
        "ai_detected": sum(r["ai_detected"] for r in rows),
        "threshold_detected": sum(r["threshold_detected"] for r in rows),
        "ai_mean_detect_s": mean(r["ai_detect_s"] for r in rows),
        "threshold_mean_detect_s": mean(r["threshold_detect_s"] for r in rows),
        "subtle_faults": len(subtle),
        "subtle_ai_detected": sum(r["ai_detected"] for r in subtle),
        "subtle_threshold_detected": sum(r["threshold_detected"] for r in subtle),
        "diagnosis_correct": sum(r["diagnosis_correct"] for r in rows),
        "diagnosis_location_ok": sum(r["diagnosis_location_ok"] for r in rows),
        "diagnosis_accuracy_pct": round(100.0 * sum(r["diagnosis_correct"] for r in rows) / n, 1) if n else None,
        "false_alarms": q.get("ai_false_alarms"),
        "false_alarms_per_min": round((q.get("ai_false_alarms") or 0) / minutes, 2),
        "threshold_false_alarms": q.get("threshold_false_alarms"),
        "threshold_flaps_during_faults": sum(r["threshold_flaps"] for r in rows),
    }
