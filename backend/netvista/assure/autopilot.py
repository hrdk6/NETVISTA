"""Autopilot: closed-loop intent assurance with a safety gate, verification and rollback.

    observe (1 Hz) -> trigger -> plan (fluid search) -> safety gate -> act -> verify -> keep / roll back
                                                        (+ packet-twin cross-check)

Modes
  off       nothing happens on its own; plans are made and applied by hand
  shadow    plans are made on every trigger and logged as "would have applied" - nothing
            changes. Safe to run next to the adaptive controller, and the way to build trust
  approve   plans that pass the gate wait for an operator's Approve
  auto      plans that pass the gate are applied immediately

Triggers: the intents changed; an intent has been violated for 5 s; the offered traffic
changed; a link's parameters or state changed; or every 60 s as a periodic check. After a
trigger the loop waits 3 s for the state to settle, and never deploys twice within 20 s.

Safety gate (deterministic; any failure blocks the plan and says why):
  * no other job (validation, AI evaluation, drill, benchmark, demo) owns the network
  * every primary path is alive right now
  * the plan is strictly better than staying put (fewer weighted violations now, or the same
    now and fewer under failures, or the same and a clearly lower cost)
  * no critical intent is predicted violated that the current routing satisfies
  * predicted peak link load <= 95 %
  * the packet-level twin agrees with the fluid prediction (RTT within 5 %, load within 5 pp)

Verification (also for plans applied by hand): settle 12 s (longer than the 10 s RTT window
the latency intents are measured over, so the old paths have left the window), measure 10 s, compare every
intent with its state before the change and with the prediction. A deployment is rolled back
automatically when an intent that held before is violated after, or a critical intent that
was predicted to hold is measured violated. Every comparison goes into the ledger and its
residuals into the uncertainty pool (conformal.py).
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from collections import deque
from typing import Any

from ..runtime import sanitize
from ..telemetry.health import percentile
from .intents import evaluate_all

log = logging.getLogger(__name__)
MODES = ("off", "shadow", "approve", "auto")
SETTLE_S = 3.0
VIOLATION_TRIGGER_S = 5.0
PERIODIC_S = 60.0
MIN_GAP_S = 20.0
VERIFY_SETTLE_S = 12.0  # > the 10 s RTT window the latency intents use: judge only the new state
VERIFY_MEASURE_S = 10.0


class Autopilot:
    def __init__(self, svc) -> None:
        self.svc = svc
        self.rt = svc.rt
        self.mode = "off"
        self.state = "idle"
        self.decisions: deque[dict] = deque(maxlen=60)
        self.ledger: deque[dict] = deque(maxlen=200)
        self._ids = itertools.count(1)
        self._led_ids = itertools.count(1)
        self.lock = threading.RLock()
        self._pending: dict[str, Any] | None = None  # trigger waiting to settle
        self._last_plan_t = 0.0
        self._last_deploy_t = 0.0
        self._last_sig: tuple | None = None
        self._violated_since: float | None = None
        self._busy = False
        self._stop = threading.Event()
        self.ledger_path = self.rt.s.runs_dir / "assure_ledger.jsonl"
        self._load_ledger()

    def _load_ledger(self) -> None:
        try:
            for line in self.ledger_path.read_text(encoding="utf-8").splitlines()[-200:]:
                self.ledger.append(json.loads(line))
        except (OSError, ValueError):
            pass
        nums = [int(e["id"][1:]) for e in self.ledger if str(e.get("id", "")).startswith("L") and e["id"][1:].isdigit()]
        self._led_ids = itertools.count(max(nums, default=0) + 1)

    # ------------------------------------------------------------------ control
    def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise ValueError(f"autopilot mode must be one of {', '.join(MODES)}")
        if mode in ("approve", "auto") and not self.svc.intents.list():
            raise ValueError("add intents first: the autopilot works towards your intents")
        prev, self.mode = self.mode, mode
        self.svc.emit("assure.autopilot", f"Autopilot {prev} → {mode}" + {
            "off": "", "shadow": ": plans are logged, nothing is changed",
            "approve": ": plans that pass the safety gate wait for your approval",
            "auto": ": plans that pass the safety gate are applied and verified automatically",
        }[mode], severity="info", mode=mode)
        if mode != "off":
            self._pending = {"reason": f"autopilot switched to {mode}", "t": time.time()}
        return self.brief()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ triggers (1 Hz, from the assure loop)
    def _signature(self) -> tuple:
        rt = self.rt
        demand = tuple(sorted((p, round(rt.traffic.offered_mbps(f.src, f.dst), 1)) for p, f in rt.controller.flows.items()))
        links = tuple((lid, repr(rt.chaos.effective_params(lid)), rt.chaos.admin_up(lid), rt.controller.link_alive.get(lid, True))
                      for lid in rt.topo.links)
        return (self.svc.intents.version, demand, links)

    def tick(self, now: float, view, results) -> None:
        if self.mode == "off" or self._busy:
            self._last_sig = self._signature()
            return
        sig = self._signature()
        if self._last_sig is not None and sig != self._last_sig:
            what = []
            if sig[0] != self._last_sig[0]:
                what.append("intents changed")
            if sig[1] != self._last_sig[1]:
                what.append("traffic changed")
            if sig[2] != self._last_sig[2]:
                what.append("link state changed")
            self._pending = {"reason": ", ".join(what) or "state changed", "t": now}
        self._last_sig = sig
        violated = [r.intent for r in results if r.status == "violated"]
        if violated:
            self._violated_since = self._violated_since or now
            if now - self._violated_since >= VIOLATION_TRIGGER_S and now - self._last_plan_t >= 15.0 and not self._pending:
                self._pending = {"reason": f"intent{'s' if len(violated) > 1 else ''} {', '.join(violated)} violated for "
                                           f"{now - self._violated_since:.0f} s", "t": now - SETTLE_S}
        else:
            self._violated_since = None
        if not self._pending and now - self._last_plan_t >= PERIODIC_S:
            self._pending = {"reason": "periodic check", "t": now - SETTLE_S}
        if self._pending and now - self._pending["t"] >= SETTLE_S and now - self._last_plan_t >= 5.0:
            trig = self._pending
            self._pending = None
            self._busy = True
            threading.Thread(target=self._decide, args=(trig,), name="autopilot", daemon=True).start()

    # ------------------------------------------------------------------ decision
    def _decide(self, trig: dict) -> None:
        try:
            self.state = "planning"
            self._last_plan_t = time.time()
            busy = self.svc.busy_job()
            if busy:
                self._record({"trigger": trig["reason"], "status": "skipped", "summary": f"the {busy} job owns the network"}, quiet=True)
                return
            plan = self.svc.make_plan(source=f"autopilot ({trig['reason']})")
            ctrl = self.rt.controller
            cur = ctrl.route_plan
            same_primary = cur is not None and cur.get("primary") == plan["primary"] and ctrl.mode == "intent"
            same_prot = cur is not None and cur.get("protection") == plan["protection"]
            if same_primary and same_prot:
                plan["status"] = "unchanged"
                self._record({"trigger": trig["reason"], "plan": plan["id"], "status": "no change",
                              "summary": "the routing in place is already the best plan"}, quiet=True)
                return
            gate = self.gate(plan)
            d = {"trigger": trig["reason"], "plan": plan["id"], "gate": gate, "summary": plan["label"],
                 "objective": plan["objective"], "current": plan["current"],
                 "resilience": {"before": plan["resilience_before"]["score"], "after": plan["resilience_after"]["score"]}}
            if not gate["ok"]:
                plan["status"] = "blocked"
                self._record(dict(d, status="blocked"), severity="warn")
                return
            if self.mode == "shadow":
                plan["status"] = "shadow"
                self._record(dict(d, status="shadow"), severity="info")
            elif self.mode == "approve":
                plan["status"] = "awaiting approval"
                self.state = "awaiting approval"
                self._record(dict(d, status="awaiting approval"), severity="warn")
            elif self.mode == "auto":
                dec = self._record(dict(d, status="applying"), severity="info")
                self._apply(dec, plan)
        except Exception as e:
            log.exception("autopilot decision failed")
            self._record({"trigger": trig.get("reason"), "status": "error", "summary": str(e)}, severity="error")
        finally:
            if self.state == "planning":
                self.state = "idle"
            self._busy = False

    def gate(self, plan: dict) -> dict:
        reasons: list[str] = []
        checks: list[dict] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"check": name, "ok": ok, "detail": detail})
            if not ok:
                reasons.append(detail)

        busy = self.svc.busy_job()
        check("network free", busy is None, f"the {busy} job owns the network" if busy else "no other job is running")
        ctrl = self.rt.controller
        dead = {l for l, a in ctrl.link_alive.items() if not a}
        from ..routing.protection import path_link_ids

        broken = [p for p, path in plan["primary"].items() if path_link_ids(path, self.rt.topo) & dead]
        check("paths alive", not broken, f"primary path of {', '.join(broken)} crosses a dead link" if broken else "every primary path is alive")
        o, c = plan["objective"], plan["current"]
        better = (o["v_now"] < c["v_now"] - 1e-6) or (abs(o["v_now"] - c["v_now"]) <= 1e-6 and o["v_fail"] < c["v_fail"] - 1e-6) or (
            abs(o["v_now"] - c["v_now"]) <= 1e-6 and abs(o["v_fail"] - c["v_fail"]) <= 1e-6 and o["cost"] < c["cost"] * 0.9)
        check("improves", better, "better than the current routing" if better else "not better than staying put")
        latest = {r.intent: r.status for r in self.svc.intents.latest.values()}
        crit = {i.id for i in self.svc.intents.list() if i.priority == "critical" and i.enabled}
        pred = {r["intent"]: r["status"] for r in plan["predicted"]["intents"]}
        regress = [i for i in crit if latest.get(i) in ("ok", "at_risk") and pred.get(i) == "violated"]
        check("no regression", not regress, f"would break critical {', '.join(regress)}" if regress else "no critical intent gets worse")
        peak = max(plan["predicted"]["link_util"].values(), default=0.0)
        check("headroom", peak <= 0.95, f"predicted peak load {peak * 100:.0f}% (limit 95%)")
        if "confirmation" not in plan:
            try:
                self.svc.confirm_plan(plan["id"])
            except Exception as e:
                plan["confirmation"] = {"agree": False, "error": str(e)}
        conf = plan.get("confirmation") or {}
        check("twins agree", bool(conf.get("agree")),
              f"packet twin vs fluid: RTT {conf.get('max_rtt_diff_pct', '?')}%, load {conf.get('max_util_diff_pp', '?')} pp"
              if "error" not in conf else f"packet twin failed: {conf['error']}")
        since = time.time() - self._last_deploy_t
        check("rate limit", since >= MIN_GAP_S, f"last deployment {since:.0f} s ago (min {MIN_GAP_S:.0f} s)")
        return {"ok": not reasons, "reasons": reasons, "checks": checks}

    def approve(self, did: str) -> dict:
        d = self._find(did)
        if d.get("status") != "awaiting approval":
            raise ValueError(f"decision {did} is {d.get('status')}, not awaiting approval")
        plan = self.svc.get_plan(d["plan"])
        gate = self.gate(plan)  # the network may have changed since the plan was made
        d["gate"] = gate
        if not gate["ok"]:
            d["status"] = "blocked"
            raise ValueError("the safety gate now blocks it: " + "; ".join(gate["reasons"]))
        d["status"] = "applying"
        d["approved_t"] = time.time()
        d["_prev"] = {"plan": self.rt.controller.route_plan, "mode": self.rt.controller.mode}
        threading.Thread(target=self._apply, args=(d, plan, "operator"), daemon=True).start()
        return d

    def dismiss(self, did: str) -> dict:
        d = self._find(did)
        if d.get("status") == "awaiting approval":
            d["status"] = "dismissed"
            self.state = "idle"
            try:
                self.svc.get_plan(d["plan"])["status"] = "dismissed"
            except KeyError:
                pass
        return d

    def _find(self, did: str) -> dict:
        for d in self.decisions:
            if d["id"] == did:
                return d
        raise KeyError(f"unknown decision {did}")

    def _apply(self, d: dict, plan: dict, by: str = "autopilot") -> None:
        try:
            self.state = "applying"
            self.svc.apply_plan(plan["id"], source=f"{by}:{d['id']}", verify=False)
            self._last_deploy_t = time.time()
            d["status"] = "verifying"
            ctrl = self.rt.controller
            prev = d.get("_prev") or {"plan": None, "mode": "adaptive"}
            self.verify_deployment(plan, prev, f"{by}:{d['id']}", decision=d, wait=True)
        except Exception as e:
            d["status"] = "error"
            d["error"] = str(e)
            self.svc.emit("assure.autopilot", f"Autopilot could not apply plan {plan['id']}: {e}", severity="error")
        finally:
            if self.state in ("applying", "verifying"):
                self.state = "idle"

    def _record(self, d: dict, severity: str = "info", quiet: bool = False) -> dict:
        d = {"id": f"D{next(self._ids)}", "t": time.time(), "mode": self.mode, **d}
        if d.get("status") == "applying":
            ctrl = self.rt.controller
            d["_prev"] = {"plan": ctrl.route_plan, "mode": ctrl.mode}
        with self.lock:
            self.decisions.append(d)
        if not quiet:
            what = {"shadow": "would apply", "awaiting approval": "waiting for approval:", "blocked": "blocked",
                    "applying": "applying", "error": "error:"}.get(d["status"], d["status"])
            extra = ""
            if d["status"] == "blocked":
                extra = " (" + "; ".join(d["gate"]["reasons"]) + ")"
            elif d.get("resilience"):
                extra = f" (resilience {d['resilience']['before']}% → {d['resilience']['after']}%)"
            self.svc.emit("assure.autopilot", f"Autopilot [{self.mode}] {what} plan {d.get('plan', '')} – {d.get('summary', '')}{extra}; "
                                              f"trigger: {d.get('trigger')}", severity=severity, decision=d["id"])
        return d

    # ------------------------------------------------------------------ verification + rollback
    def verify_deployment(self, plan: dict, prev: dict, source: str, decision: dict | None = None, wait: bool = False) -> None:
        if wait:
            self._verify(plan, prev, source, decision)
        else:
            threading.Thread(target=self._verify, args=(plan, prev, source, decision), name="assure-verify", daemon=True).start()

    def _verify(self, plan: dict, prev: dict, source: str, decision: dict | None) -> None:
        svc, rt = self.svc, self.rt
        self.state = "verifying"
        before = {r.intent: r.status for r in svc.intents.latest.values()}
        t_apply = time.time()
        if self._stop.wait(VERIFY_SETTLE_S):
            return
        t0 = time.time()
        samples: list[dict[str, str]] = []
        while time.time() - t0 < VERIFY_MEASURE_S:
            if self._stop.wait(1.0):
                return
            if rt.controller.route_plan is None or rt.controller.route_plan.get("id") != plan["id"]:
                break  # superseded by another plan / mode change: nothing to judge
            samples.append({r.intent: r.status for r in svc.intents.latest.values()})
        t1 = time.time()
        if not samples:
            self._ledger({"plan": plan["id"], "source": source, "verdict": "not judged", "reason": "routing changed during verification"})
            return
        after = {}
        for iid in before:
            sts = [s.get(iid) for s in samples if s.get(iid)]
            bad = sum(1 for s in sts if s == "violated")
            after[iid] = "violated" if sts and bad >= len(sts) / 2 else ("ok" if sts else "unknown")
        predicted = {r["intent"]: r["status"] for r in plan["predicted"]["intents"]}
        intents = {i.id: i for i in svc.intents.list()}
        regressions = [i for i, s in after.items() if s == "violated" and before.get(i) in ("ok", "at_risk")]
        surprises = [i for i, s in after.items() if s == "violated" and predicted.get(i) in ("ok", "at_risk")
                     and intents.get(i) and intents[i].priority == "critical"]
        # predicted vs measured, per flow: residuals for the uncertainty pool
        meas = self._measure(t0, t1)
        rows = []
        for pair, fp in plan["predicted"]["pairs"].items():
            m = meas.get(pair, {})
            for metric, pv, mv in (("rtt_p50", fp.get("rtt_p50"), m.get("rtt_p50")), ("rtt_p95", fp.get("rtt_p95"), m.get("rtt_p95"))):
                rows.append({"pair": pair, "metric": metric, "kind": "relative", "predicted": pv, "measured": mv,
                             "error": None if pv is None or not mv else abs(pv - mv) / mv * 100})
            if fp.get("offered_mbps"):
                rows.append({"pair": pair, "metric": "rx_mbps", "kind": "relative", "predicted": fp.get("rx_mbps"), "measured": m.get("rx_mbps"),
                             "error": None if fp.get("rx_mbps") is None or not m.get("rx_mbps") else abs(fp["rx_mbps"] - m["rx_mbps"]) / m["rx_mbps"] * 100})
                rows.append({"pair": pair, "metric": "loss_pct", "kind": "absolute", "predicted": fp.get("loss_pct"), "measured": m.get("loss_pct"),
                             "error": None if fp.get("loss_pct") is None or m.get("loss_pct") is None else abs(fp["loss_pct"] - m["loss_pct"])})
        svc.pool.add_rows("fluid", rows, source=f"deployment {plan['id']}")
        matched = sum(1 for i, s in after.items() if predicted.get(i) and (s == "violated") == (predicted[i] == "violated"))
        judged = sum(1 for i in after if predicted.get(i) and after[i] != "unknown")
        verdict = "rolled back" if (regressions or surprises) else "verified"
        entry = {
            "plan": plan["id"], "label": plan.get("label"), "source": source, "t_apply": t_apply, "window": [t0, t1],
            "before": before, "after": after, "predicted": predicted, "regressions": regressions, "surprises": surprises,
            "intent_predictions_correct": matched, "intent_predictions_judged": judged, "rows": rows, "verdict": verdict,
        }
        if verdict == "rolled back":
            rt.controller.restore(prev.get("plan"), prev.get("mode") or "adaptive", source="rollback")
            plan["status"] = "rolled back"
            why = (f"broke {', '.join(regressions)}" if regressions else "") + \
                  ("; " if regressions and surprises else "") + (f"critical {', '.join(surprises)} violated against prediction" if surprises else "")
            svc.emit("assure.rollback", f"Plan {plan['id']} rolled back automatically: {why}. Restored "
                     f"{(prev.get('plan') or {}).get('id') or prev.get('mode')}", severity="error", plan=plan["id"])
        else:
            plan["status"] = "verified"
            errs = [r["error"] for r in rows if r["metric"].startswith("rtt") and r["error"] is not None]
            svc.emit("assure.verified", f"Plan {plan['id']} verified on the live network: {matched}/{judged} intent outcomes as predicted"
                     + (f", RTT within {max(errs):.1f}% of the prediction" if errs else ""), severity="success", plan=plan["id"])
        plan["verification"] = sanitize({k: v for k, v in entry.items() if k != "rows"})
        if decision is not None:
            decision["status"] = verdict
            decision["verification"] = plan["verification"]
        self._ledger(entry)

    def _measure(self, t0: float, t1: float) -> dict[str, dict]:
        rt = self.rt
        out = {}
        for pair, f in rt.controller.flows.items():
            s = rt.store.get(f"F:{pair}")
            xs = sorted(s.rtt_samples(t0, t1)) if s else []
            ts = rt.traffic.pair_stats(f.src, f.dst, t1 - t0, t1) if rt.traffic.offered_mbps(f.src, f.dst) > 0 else None
            out[pair] = {
                "rtt_p50": percentile(xs, 50) if xs else None, "rtt_p95": percentile(xs, 95) if xs else None,
                "rx_mbps": ts["rx_mbps"] if ts else None, "loss_pct": ts["loss_pct"] if ts else None,
            }
        return out

    def _ledger(self, entry: dict) -> None:
        entry = sanitize({"id": f"L{next(self._led_ids)}", "t": time.time(), "topology": self.rt.topo.name, **entry})
        with self.lock:
            self.ledger.append(entry)
        try:
            with self.ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ views
    def brief(self) -> dict:
        pending = next((d for d in reversed(self.decisions) if d.get("status") == "awaiting approval"), None)
        last = self.decisions[-1] if self.decisions else None
        return {
            "mode": self.mode, "state": self.state,
            "pending": None if not pending else {"id": pending["id"], "plan": pending.get("plan"), "summary": pending.get("summary"),
                                                  "resilience": pending.get("resilience")},
            "last": None if not last else {"id": last["id"], "t": last["t"], "status": last.get("status"), "summary": last.get("summary"),
                                            "trigger": last.get("trigger")},
            "deployments": sum(1 for e in self.ledger if e.get("verdict") in ("verified", "rolled back")),
            "rollbacks": sum(1 for e in self.ledger if e.get("verdict") == "rolled back"),
        }

    def view(self) -> dict:
        with self.lock:
            decisions = [{k: v for k, v in d.items() if not k.startswith("_")} for d in reversed(self.decisions)]
            name = self.rt.topo.name
            ledger = [e for e in reversed(self.ledger) if e.get("topology", "netvista-default") == name]
        return sanitize({"brief": self.brief(), "decisions": decisions, "ledger": ledger})
