"""Assure service: intents checked every second, resilience predicted, plans made, deployed and proven.

Plugged into the runtime as extension "assure" (services.py). The loop, once a second:

    live view (probes, iperf3, counters)  ->  every intent checked  ->  compliance history,
    debounced violation events  ->  autopilot triggers  ->  (when the state changed) a fresh
    failure analysis in the simulator's worker process, for the fragility map.

Heavy work (failure matrix, planning, packet-twin confirmation) runs in the simulator's
process pool, so a search never holds the backend's GIL while probes are being timed.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from ..runtime import sanitize
from .autopilot import Autopilot
from .benchmark import BenchmarkService
from .conformal import ResidualPool
from .demo import AssureDemo
from .drill import DrillService
from .intents import IntentStore, PairState, View, evaluate_all
from .model import core_links, single_scenarios, spof_elements
from .planner import plan_routes
from .resilience import analyze

log = logging.getLogger(__name__)
RESILIENCE_EVERY_S = 60.0


def short(path: list[str] | None, topo) -> str:
    if not path:
        return "–"
    return "-".join(n for n in path if topo.nodes[n].type == "router")


class AssureService:
    def __init__(self, rt) -> None:
        self.rt = rt
        self.topo = rt.topo
        self.core = core_links(rt.topo)
        self.pairs = list(rt.controller.flows)
        self.intents = IntentStore(self._intents_path(), rt.topo, self.pairs, self.core)
        self.pool = ResidualPool(rt.s.runs_dir / "residuals.jsonl")
        self._seed_pool()
        self.plans: OrderedDict[str, dict] = OrderedDict()
        self._plan_ids = itertools.count(1)
        self._res_ids = itertools.count(1)
        self.resilience: dict | None = None
        self.resilience_n2: dict | None = None
        self.lock = threading.RLock()
        self.tick_ms = 0.0
        self._sig: tuple | None = None
        self._res_due = 0.0
        self._res_running = False
        self.latest_view: View | None = None
        self._stop = threading.Event()
        self.autopilot = Autopilot(self)
        self.drills = DrillService(self)
        self.benchmark = BenchmarkService(self)
        self.demo = AssureDemo(self)
        threading.Thread(target=self._loop, name="assure", daemon=True).start()

    def _intents_path(self):
        """One intent file per topology (intents name its flows and links)."""
        import shutil

        from ..topology.library import slug

        d = self.rt.s.runs_dir / "intents"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{slug(self.topo.name)}.json"
        legacy = self.rt.s.runs_dir / "intents.json"  # before per-topology files
        if not path.exists() and legacy.exists():
            try:
                if json.loads(legacy.read_text(encoding="utf-8")).get("topology") == self.topo.name:
                    shutil.copyfile(legacy, path)
            except (OSError, ValueError):
                pass
        return path

    # ------------------------------------------------------------------ uncertainty pool
    def _seed_pool(self) -> None:
        """First start: seed the packet twin's residuals from the validation history."""
        if any(self.pool.count("packet", c) for c in ("rtt", "throughput", "loss")):
            return
        val = self.rt.extensions.get("validation")
        if not val:
            return
        for run in reversed(val.list_runs()):
            self.pool.add_rows("packet", run.get("rows", []), source=f"validation {run.get('id')}")
            if run.get("rows_fluid"):
                self.pool.add_rows("fluid", run["rows_fluid"], source=f"validation {run.get('id')}")

    # ------------------------------------------------------------------ live view
    def live_view(self, now: float) -> View:
        rt = self.rt
        tel, ctrl, traffic = rt.telemetry, rt.controller, rt.traffic
        pairs: dict[str, PairState] = {}
        for pair, f in ctrl.flows.items():
            pv = tel.flow_probe_view(pair, now)
            offered = traffic.offered_mbps(f.src, f.dst)
            ts = traffic.pair_stats(f.src, f.dst, 3.0, now) if offered > 0 else None
            if offered > 0 and ts is not None:
                loss, src = ts["loss_pct"], "data"
            else:
                loss, src = pv.get("probe_loss_pct"), "probe"
            pairs[pair] = PairState(
                path=list(f.path) if f.path else None, alive=pv.get("alive"),
                rtt={"p50": pv.get("rtt_p50"), "p95": pv.get("rtt_p95"), "p99": pv.get("rtt_p99")},
                loss_pct=loss, loss_source=src, offered_mbps=offered, rx_mbps=ts["rx_mbps"] if ts else None,
            )
        util, up, rate, cap = {}, {}, {}, {}
        for lid, link in self.topo.links.items():
            v = tel.link_view(lid, now)
            util[lid] = v["util"]
            up[lid] = v["health"] != "down" and v["admin_up"]
            bw = rt.chaos.effective_params(lid).bw_mbps * 1e6
            for s, r in ((link.a, link.b), (link.b, link.a)):
                rate[(s, r)] = rt.counters.get(rt.plan.intf(lid, s).name).tx_bps
                cap[(s, r)] = bw if up[lid] else 0.0
        return View("live", pairs, util, up, rate, cap)

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            t0 = time.time()
            try:
                self.step(t0)
            except Exception:
                log.exception("assure step failed")
            self.tick_ms = (time.time() - t0) * 1000

    def emit(self, kind: str, message: str, severity: str = "info", **data) -> None:
        self.rt.events.emit(kind, message, severity=severity, **data)

    def step(self, now: float) -> None:
        view = self.live_view(now)
        self.latest_view = view
        results = evaluate_all(self.intents.list(), view, self.topo, self.core)
        self.intents.record(now, results, self.emit)
        self.autopilot.tick(now, view, results)
        sig = self.state_signature()
        if sig != self._sig:
            self._sig = sig
            self._res_due = min(self._res_due or now + 2.0, now + 2.0)  # settle 2 s after a change
        if not self._res_due:
            self._res_due = now + 3.0
        if now >= self._res_due and not self._res_running:
            self._res_due = now + RESILIENCE_EVERY_S
            self._res_running = True
            threading.Thread(target=self._background_resilience, name="assure-resilience", daemon=True).start()

    def _background_resilience(self) -> None:
        try:
            self.run_resilience(record=False)
        except Exception as e:
            log.warning("background resilience analysis failed: %s", e)
        finally:
            self._res_running = False

    def state_signature(self) -> tuple:
        """What the failure analysis depends on: intents, demand, link state, routing."""
        rt = self.rt
        ctrl = rt.controller
        demand = tuple(sorted((p, round(rt.traffic.offered_mbps(f.src, f.dst), 2)) for p, f in ctrl.flows.items()))
        links = tuple((lid, rt.chaos.effective_params(lid).to_dict().__repr__(), rt.chaos.admin_up(lid)) for lid in self.topo.links)
        paths = tuple((p, tuple(f.path or ())) for p, f in ctrl.flows.items())
        plan = (ctrl.route_plan or {}).get("id")
        return (self.intents.version, demand, links, paths, ctrl.mode, plan, ctrl.herd_guard)

    # ------------------------------------------------------------------ bundles for the worker
    def bundle(self) -> dict[str, Any]:
        rt = self.rt
        sim = rt.extensions["simulator"]
        cfg, _ = sim.build_config([], "adaptive", None, None, 8.0, 1)  # current paths, as they are now
        ctrl = rt.controller
        return {
            "topo": self.topo, "cfg": cfg, "intents": self.intents.list(), "mode": ctrl.mode,
            "plan": ctrl.route_plan, "weights": ctrl.weights.to_dict(), "rtt_margin": self.pool.rtt_margin("fluid"),
        }

    def _pool(self):
        return self.rt.extensions["simulator"]._pool

    # ------------------------------------------------------------------ resilience
    def run_resilience(self, double: bool = False, record: bool = True) -> dict:
        b = self.bundle()
        b["double"] = double
        res = self._pool().submit(analyze, b).result(timeout=180)
        res["id"] = f"R{next(self._res_ids)}"
        res["rtt_margin"] = b["rtt_margin"]
        res = sanitize(res)
        with self.lock:
            if double:
                self.resilience_n2 = res
            else:
                self.resilience = res
        if record:
            worst = sorted((r for r in res["scenarios"] if r["violated"]), key=lambda r: -r["severity"])[:2]
            self.emit(
                "assure.resilience",
                f"Resilience analysis ({res['mode']} routing{', N-2' if double else ''}): {res['score']}% of failure x intent cells hold"
                + (f" (best possible {res['score_best']}%)" if res.get("score_best") is not None else "")
                + (f"; worst: {', '.join(r['scenario']['label'] for r in worst)}" if worst else ""),
                severity="info", analysis=res["id"],
            )
        return res

    # ------------------------------------------------------------------ plans
    def make_plan(self, source: str = "user") -> dict:
        b = self.bundle()
        if not b["intents"]:
            raise ValueError("add at least one intent first: the planner optimises for your intents")
        pl = self._pool().submit(plan_routes, b).result(timeout=300)
        pid = f"P{next(self._plan_ids)}"
        plan = {"id": pid, "t": time.time(), "source": source, "status": "proposed", "mode_at_creation": b["mode"], **pl}
        moved = pl["moved"]
        plan["label"] = (f"Move {', '.join(p.replace('>', '→') for p in moved)}" if moved else "Keep current paths") + \
            f", {len(pl['protection'])} backup scenario{'s' if len(pl['protection']) != 1 else ''}"
        # before / after: the same failure analysis for the current routing and for this plan
        after_cfg = dict(b["cfg"])
        after_cfg["pairs"] = {p: dict(v, path=pl["primary"][p]) for p, v in b["cfg"]["pairs"].items()}
        ctrl_plan = {"id": pid, "primary": pl["primary"], "protection": pl["protection"]}
        fut_after = self._pool().submit(analyze, dict(b, cfg=after_cfg, mode="intent", plan=ctrl_plan, bounds=False))
        fut_before = self._pool().submit(analyze, dict(b, bounds=True))
        after, before = fut_after.result(timeout=180), fut_before.result(timeout=180)
        plan["resilience_after"] = _res_brief(after)
        plan["resilience_before"] = _res_brief(before)
        plan["resilience_after"]["score_best"] = before.get("score_best")
        plan["rtt_margin"] = b["rtt_margin"]
        plan = sanitize(plan)
        with self.lock:
            self.plans[pid] = plan
            while len(self.plans) > 30:
                self.plans.popitem(last=False)
        self.emit(
            "assure.plan",
            f"Plan {pid}: {plan['label']}. Resilience {before['score']}% → {after['score']}%"
            + (f" (best possible {before['score_best']}%)" if before.get("score_best") is not None else "")
            + f"; searched {pl['search']['evaluations']} routings in {pl['search']['wall_s']:.2f} s",
            severity="info", plan=pid,
        )
        return plan

    def get_plan(self, pid: str) -> dict:
        p = self.plans.get(pid)
        if p is None:
            raise KeyError(f"unknown plan {pid}")
        return p

    def confirm_plan(self, pid: str) -> dict:
        """Second opinion: run the packet-level twin on the plan and compare with the fluid prediction."""
        plan = self.get_plan(pid)
        sim = self.rt.extensions["simulator"]
        cfg, _ = sim.build_config([], "adaptive", None, None, 10.0, 3)
        cfg["mode"] = "fixed"
        cfg["pairs"] = {p: dict(v, path=plan["primary"][p]) for p, v in cfg["pairs"].items()}
        t0 = time.time()
        res = sim.run_cfg(cfg)
        rows, worst_rtt, worst_util = [], 0.0, 0.0
        for pair, fp in plan["predicted"]["pairs"].items():
            pp = res["pairs"][pair]
            for metric, fv, pv in (("rtt_p50", fp["rtt_p50"], pp["rtt_p50"]), ("rtt_p95", fp["rtt_p95"], pp["rtt_p95"])):
                err = None if fv is None or pv is None or not pv else abs(fv - pv) / pv * 100
                rows.append({"pair": pair, "metric": metric, "fluid": fv, "packet": pv, "diff_pct": err})
                if err is not None:
                    worst_rtt = max(worst_rtt, err)
            if fp["offered_mbps"]:
                rows.append({"pair": pair, "metric": "rx_mbps", "fluid": fp["rx_mbps"], "packet": pp["rx_mbps"],
                             "diff_pct": None if not pp["rx_mbps"] else abs((fp["rx_mbps"] or 0) - pp["rx_mbps"]) / pp["rx_mbps"] * 100})
        for lid, u in plan["predicted"]["link_util"].items():
            worst_util = max(worst_util, abs(u - res["links"][lid]["util"]) * 100)
        conf = {
            "t": time.time(), "wall_s": round(time.time() - t0, 2), "rows": rows,
            "max_rtt_diff_pct": round(worst_rtt, 3), "max_util_diff_pp": round(worst_util, 3),
            "agree": worst_rtt <= 5.0 and worst_util <= 5.0,
        }
        plan["confirmation"] = sanitize(conf)
        self.emit("assure.confirm", f"Plan {pid} cross-checked with the packet-level twin: RTT within {worst_rtt:.2f}%, "
                  f"load within {worst_util:.2f} pp of the fluid model ({'agree' if conf['agree'] else 'DISAGREE'})",
                  severity="success" if conf["agree"] else "warn", plan=pid)
        return plan

    def apply_plan(self, pid: str, source: str = "user", verify: bool = True) -> dict:
        plan = self.get_plan(pid)
        busy = self.busy_job()
        if busy and not source.startswith(("benchmark", "drill", "assure-demo")):
            raise ValueError(f"the {busy} job is using the network; wait for it to finish")
        ctrl = self.rt.controller
        prev = {"plan": ctrl.route_plan, "mode": ctrl.mode}
        ctrl.apply_plan({"id": pid, "label": plan["label"], "primary": plan["primary"], "protection": plan["protection"]}, source=source)
        plan["status"] = "applied"
        plan["applied_t"] = time.time()
        plan["applied_by"] = source
        for other in self.plans.values():
            if other is not plan and other.get("status") == "applied":
                other["status"] = "superseded"
        if verify:
            self.autopilot.verify_deployment(plan, prev, source)
        return plan

    def clear_plan(self, mode: str = "adaptive") -> dict:
        self.rt.controller.clear_plan(mode)
        for p in self.plans.values():
            if p.get("status") == "applied":
                p["status"] = "withdrawn"
        return {"mode": self.rt.controller.mode}

    # ------------------------------------------------------------------ jobs
    def busy_job(self) -> str | None:
        for name in ("validation", "demo", "replayer"):
            ext = self.rt.extensions.get(name)
            if ext and ext.status().get("running"):
                return name
        ai = self.rt.extensions.get("ai")
        if ai and ai.evaluation.status().get("running"):
            return "AI evaluation"
        if self.drills.status().get("running"):
            return "drill"
        if self.benchmark.status().get("running"):
            return "benchmark"
        if self.demo.status().get("running"):
            return "Assure demo"
        return None

    # ------------------------------------------------------------------ presets
    def preset(self, name: str) -> list[dict]:
        """Sensible intents for any topology, derived from its design numbers."""
        topo = self.topo
        ctrl = self.rt.controller
        traffic_pairs = [f"{t.src}>{t.dst}" for t in topo.traffic] or self.pairs[:1]

        def design_rtt(pair: str) -> float:
            path = ctrl.flows[pair].static_path
            return 2 * sum(topo.link_between(u, v).delay_ms for u, v in zip(path, path[1:]))

        def slo(pair: str) -> float:
            return float(5 * round((design_rtt(pair) * 1.25 + 2.5) / 5))  # 25 % over design, rounded to 5 ms

        if name == "gold-bronze":
            gold = traffic_pairs
            out = [{"kind": "latency", "flows": [p], "params": {"stat": "p95", "max_ms": slo(p)}, "protect": "any", "priority": "critical",
                    "note": "gold service: low latency, must survive single failures"} for p in gold]
            out += [
                {"kind": "loss", "flows": ["*"], "params": {"max_pct": 1}, "protect": "any", "priority": "high"},
                {"kind": "max_util", "links": ["*"], "params": {"max_pct": 85}, "protect": "any", "priority": "high",
                 "note": "headroom: no core link above 85 %"},
                {"kind": "reach", "flows": ["*"], "protect": "any", "priority": "critical"},
            ]
            return out
        if name == "basic":
            return [
                {"kind": "reach", "flows": ["*"], "protect": "link", "priority": "critical"},
                {"kind": "loss", "flows": ["*"], "params": {"max_pct": 1}, "priority": "high"},
                {"kind": "max_util", "links": ["*"], "params": {"max_pct": 90}, "priority": "normal"},
            ]
        if name == "policy":
            on_shortest = {n for p in self.pairs for n in ctrl.flows[p].static_path}
            mids = [r for r in topo.routers if r not in on_shortest]
            out = self.preset("gold-bronze")
            if len(self.pairs) >= 2:
                out.append({"kind": "disjoint", "flows": self.pairs[:2], "params": {"nodes": False}, "priority": "normal",
                            "note": "diverse paths for the two main flows"})
            if mids:
                out.append({"kind": "avoid", "flows": [self.pairs[-1]], "params": {"elements": [mids[0]]}, "priority": "normal",
                            "note": f"example policy: keep {self.pairs[-1].replace('>', '→')} off {mids[0]}"})
            return out
        raise ValueError("preset must be gold-bronze, basic or policy")

    def stress_profile(self) -> dict[str, float]:
        """Benchmark demand: every managed flow carries traffic, ~40 % of the source site's uplinks in total.
        Default-profile flows weigh 3, the others 2 (so the 'gold' flows are the heaviest)."""
        topo = self.topo
        ctrl = self.rt.controller
        default = {f"{t.src}>{t.dst}" for t in topo.traffic}
        first = ctrl.flows[self.pairs[0]].static_path
        edge = next(n for n in first if topo.nodes[n].type == "router")
        uplink = sum(l.bw_mbps for l in topo.links_of(edge) if topo.nodes[l.other(edge)].type == "router")
        weights = {p: (3.0 if p in default else 2.0) for p in self.pairs}
        total = 0.4 * uplink
        s = sum(weights.values())
        return {p: round(total * w / s, 1) for p, w in weights.items()}

    # ------------------------------------------------------------------ views
    def intent_rows(self, now: float | None = None, timeline_s: float = 300) -> list[dict]:
        now = now or time.time()
        out = []
        for it in self.intents.list():
            r = self.intents.latest.get(it.id)
            a = self.intents.alert.get(it.id, {})
            out.append({
                **it.to_dict(),
                "status": r.status if r else "unknown",
                "checks": [c.to_dict() for c in r.checks] if r else [],
                "violated_since": a.get("since") if a.get("violated") else None,
                "compliance": {w: self.intents.compliance(it.id, now, s) for w, s in (("1m", 60), ("5m", 300), ("15m", 900))},
                "timeline": self.intents.timeline(it.id, now, timeline_s),
                "resilience": next((x for x in (self.resilience or {}).get("intents", []) if x["intent"] == it.id), None),
            })
        return out

    def snapshot_view(self) -> dict:
        now = time.time()
        rows = []
        counts = {"ok": 0, "violated": 0, "at_risk": 0, "unknown": 0}
        for it in self.intents.list():
            r = self.intents.latest.get(it.id)
            st = r.status if r else "unknown"
            if not it.enabled:
                st = "disabled"
            else:
                counts[st] = counts.get(st, 0) + 1
            worst = None
            if r:
                bad = [c for c in r.checks if c.ok is False] or [c for c in r.checks if c.at_risk]
                if bad:
                    w = max(bad, key=lambda c: c.severity)
                    worst = w.to_dict()
            rows.append({"id": it.id, "label": it.label, "kind": it.kind, "priority": it.priority, "protect": it.protect,
                         "status": st, "worst": worst, "compliance_5m": self.intents.compliance(it.id, now, 300)["compliance_pct"]})
        res = self.resilience
        return {
            "intents": rows,
            "counts": counts,
            "resilience": None if not res else {
                "id": res["id"], "t": res["t"], "mode": res["mode"], "plan": res.get("plan"), "score": res["score"],
                "score_best": res.get("score_best"), "criticality": res["criticality"],
                "worst": [{"scenario": r["scenario"]["id"], "label": r["scenario"]["label"], "violated": r["violated"],
                           "avoidable": r.get("avoidable", []), "severity": r["severity"]}
                          for r in sorted((r for r in res["scenarios"] if r["violated"]), key=lambda r: -r["severity"])[:4]],
            },
            "autopilot": self.autopilot.brief(),
            "tick_ms": round(self.tick_ms, 2),
        }

    def status(self) -> dict:
        d, b, m = self.drills.status(), self.benchmark.status(), self.demo.status()
        return {"running": bool(d.get("running") or b.get("running") or m.get("running")), "drill": d, "benchmark": b, "demo": m}

    def scenarios(self) -> dict:
        return {
            "scenarios": [s.to_dict() for s in single_scenarios(self.topo)],
            "spofs": spof_elements(self.topo, self.pairs),
        }

    def stop(self) -> None:
        self._stop.set()
        self.autopilot.stop()
        self.drills.stop()
        self.benchmark.stop()
        self.demo.stop()


def _res_brief(res: dict) -> dict:
    return {
        "score": res.get("score"), "score_best": res.get("score_best"), "mode": res.get("mode"),
        "intents": [{k: x[k] for k in ("intent", "scenarios", "held", "broken")} for x in res.get("intents", [])],
        "violations": {r["scenario"]["id"]: r["violated"] for r in res.get("scenarios", []) if r["violated"]},
    }


def dumps(x: Any) -> str:
    return json.dumps(sanitize(x), default=str)
