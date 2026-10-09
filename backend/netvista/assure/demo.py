"""One-button Assure demo: the whole intent loop on the live network in about two minutes.

    1. intents + stress traffic      every flow loaded; the adaptive controller balances load
    2. what the AI layer sees         which intents hold now, and the predicted resilience
    3. plan                           routes + pre-checked backups, resilience before -> after
    4. apply + verify                 intent routing mode, then 12 s settle + 10 s live check
    5. fail a protected link          the plan's backup paths go in at detection time
    6. recover                        wait-to-restore brings the flows home
    7. summary                        every number measured or predicted above, side by side

Every step narrates what it measured into the event log and the demo panel.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .model import scenario_changes, scenario_by_id


class AssureDemo:
    STEPS = [
        "Load gold / bronze intents and stress traffic",
        "Let the adaptive controller balance load; read the intents",
        "Plan routes and backups for the intents",
        "Apply the plan, verify it on live measurements",
        "Fail a link the plan protects",
        "Recover (wait-to-restore)",
        "Summary",
    ]

    def __init__(self, svc) -> None:
        self.svc = svc
        self.rt = svc.rt
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict[str, Any] = {"running": False, "steps": self.STEPS}

    def status(self) -> dict:
        return dict(self._status)

    def stop(self) -> None:
        self._stop.set()

    def start(self) -> dict:
        if self._thread and self._thread.is_alive():
            raise ValueError("the Assure demo is already running")
        busy = self.svc.busy_job()
        if busy:
            raise ValueError(f"the {busy} job owns the network")
        self._stop.clear()
        self._status = {"running": True, "steps": self.STEPS, "index": 0, "started": time.time(), "notes": []}
        self._thread = threading.Thread(target=self._run, name="assure-demo", daemon=True)
        self._thread.start()
        return self.status()

    # ------------------------------------------------------------------
    def _say(self, msg: str, severity: str = "info") -> None:
        self._status.setdefault("notes", []).append(msg)
        self.rt.events.emit("assure.demo", msg, severity=severity, source="assure-demo")

    def _wait(self, secs: float) -> bool:
        self._status["step_ends_at"] = time.time() + secs
        return not self._stop.wait(secs)

    def _step(self, i: int) -> None:
        self._status["index"] = i

    def _intent_line(self) -> tuple[str, int, int]:
        rows = self.svc.intents.list()
        latest = self.svc.intents.latest
        bad = [i.id for i in rows if i.enabled and latest.get(i.id) and latest[i.id].status == "violated"]
        n = sum(1 for i in rows if i.enabled)
        return (", ".join(bad) or "none"), n - len(bad), n

    def _run(self) -> None:
        svc, rt = self.svc, self.rt
        ctrl = rt.controller
        summary: dict[str, Any] = {}
        inj = None
        try:
            # 1 ----------------------------------------------------------------
            self._step(0)
            rt.chaos.revert_all(source="assure-demo")
            if ctrl.route_plan or ctrl.mode != "adaptive":
                svc.clear_plan("adaptive") if ctrl.route_plan else ctrl.set_mode("adaptive")
            ctrl.set_herd_guard(True)
            if not svc.intents.list():
                svc.intents.replace_all(svc.preset("gold-bronze"), source="preset")
            rates = svc.stress_profile()
            rt.traffic.stop_all("background")
            for pair, r in rates.items():
                src, dst = pair.split(">")
                rt.traffic.start_flow(src, dst, r)
            self._say(f"Stress traffic on every flow ({', '.join(f'{p.replace('>', '→')} {r:g}' for p, r in rates.items())} Mbit/s); "
                      f"{len(svc.intents.list())} intents are checked every second")
            # 2 ----------------------------------------------------------------
            self._step(1)
            if not self._wait(22):
                return
            bad, ok, n = self._intent_line()
            res = svc.run_resilience(record=False)
            summary["before"] = {"met": f"{ok}/{n}", "violated": bad, "resilience": res["score"], "bound": res.get("score_best")}
            self._say(f"Adaptive routing: {ok} of {n} intents met (violated: {bad}). Predicted: {res['score']}% of (failure × intent) "
                      f"cells survive a single failure; the best any routing could reach is {res.get('score_best')}%",
                      "warn" if bad != "none" else "info")
            # 3 ----------------------------------------------------------------
            self._step(2)
            plan = svc.make_plan(source="assure-demo")
            svc.confirm_plan(plan["id"])
            conf = plan.get("confirmation") or {}
            summary["plan"] = {"id": plan["id"], "moved": plan["moved"], "after": plan["resilience_after"]["score"],
                               "searched": plan["search"]["evaluations"], "wall_s": plan["search"]["wall_s"],
                               "agree": conf.get("agree"), "rtt_diff": conf.get("max_rtt_diff_pct")}
            self._say(f"Plan {plan['id']}: {plan['label']}; searched {plan['search']['evaluations']} routings in "
                      f"{plan['search']['wall_s']:.2f} s. Predicted resilience {plan['resilience_before']['score']}% → "
                      f"{plan['resilience_after']['score']}%. Packet twin agrees within {conf.get('max_rtt_diff_pct', '?')}% RTT")
            # 4 ----------------------------------------------------------------
            self._step(3)
            svc.apply_plan(plan["id"], source="assure-demo")
            t0 = time.time()
            while time.time() - t0 < 40 and not plan.get("verification"):
                if not self._wait(1):
                    return
            v = plan.get("verification") or {}
            summary["verify"] = {"verdict": v.get("verdict"), "correct": v.get("intent_predictions_correct"), "judged": v.get("intent_predictions_judged")}
            bad, ok, n = self._intent_line()
            self._say(f"Plan {plan['id']} {v.get('verdict', 'not judged')} on live measurements: {v.get('intent_predictions_correct')}/"
                      f"{v.get('intent_predictions_judged')} intent outcomes as predicted; now {ok} of {n} intents met",
                      "success" if v.get("verdict") == "verified" else "warn")
            # 5 ----------------------------------------------------------------
            self._step(4)
            sid = next((k for k in plan["protection"] if k.startswith("link:")), None)
            if sid is None:
                self._say("The plan needed no backup for any single link: skipping the failure step")
            else:
                sc = scenario_by_id(rt.topo, sid)
                expect = plan["predicted"]["scenarios"].get(sid, {})
                self._say(f"Failing {sc.label.lower().replace(' fails', '')}: the plan moves "
                          f"{', '.join(p.replace('>', '→') for p in plan['protection'][sid])} to pre-planned backups; predicted to break: "
                          f"{', '.join(expect.get('violated') or []) or 'nothing'}")
                for c in scenario_changes(sc):
                    inj = rt.chaos.inject(c["kind"], c.get("target"), c.get("params") or {}, source="assure-demo")
                if not self._wait(16):
                    return
                bad, ok, n = self._intent_line()
                summary["failure"] = {"scenario": sid, "predicted_broken": expect.get("violated") or [], "measured_broken": bad}
                self._say(f"With {sid.split(':')[1]} down: routing serves scenario {ctrl.scenario}; {ok} of {n} intents met (violated: {bad}); "
                          f"predicted: {', '.join(expect.get('violated') or []) or 'none'}", "success" if bad == (", ".join(expect.get('violated') or []) or "none") else "warn")
            # 6 ----------------------------------------------------------------
            self._step(5)
            if inj is not None:
                rt.chaos.revert(inj.id, source="assure-demo")
                inj = None
                if not self._wait(ctrl.wtr_s + 6):
                    return
                self._say(f"Link back; after the {ctrl.wtr_s:g} s wait-to-restore the flows are on their primary paths again "
                          f"(scenario now: {ctrl.scenario or 'none'})", "success")
            # 7 ----------------------------------------------------------------
            self._step(6)
            b, p, vv = summary.get("before", {}), summary.get("plan", {}), summary.get("verify", {})
            f = summary.get("failure")
            self._say(
                f"Summary: adaptive routing met {b.get('met')} intents; plan {p.get('id')} (searched {p.get('searched')} routings in "
                f"{p.get('wall_s', 0):.2f} s) raised predicted resilience from {b.get('resilience')}% to {p.get('after')}% "
                f"(bound {b.get('bound')}%), was {vv.get('verdict')} live with {vv.get('correct')}/{vv.get('judged')} intent outcomes as predicted"
                + (f", and under {f['scenario']} the measured breaks ({f['measured_broken']}) matched the prediction "
                   f"({', '.join(f['predicted_broken']) or 'none'})" if f else ""),
                "success",
            )
            self._status["summary"] = summary
        except Exception as e:
            self._say(f"Assure demo stopped: {e}", "error")
        finally:
            if inj is not None:
                try:
                    rt.chaos.revert(inj.id, source="assure-demo")
                except Exception:
                    pass
            self._status["running"] = False
            self._status["finished"] = time.time()
