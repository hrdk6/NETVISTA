"""The routing controller: detects failures/degradation from probes and re-routes flows.

Modes
  static   : every flow uses its Dijkstra shortest path (configured delay); never reacts.
  adaptive : every second each flow's K candidate paths are scored from live telemetry and
             the flow moves if another path is >= hysteresis better (and hold-down expired);
             a link declared dead triggers an immediate fail-over of every flow using it.
  intent   : flows follow a plan made against the operator's intents (assure/planner.py):
             pinned primary paths, and for each failure scenario backup paths that were
             checked in advance (routing/protection.py). Unplanned failures fall back to the
             adaptive score; restored links are trusted again after a wait-to-restore time.

Herd guard (adaptive): flows decide one after another within a tick, but the interface
counters only refresh twice a second, so every flow sees the same stale loads. When several
flows leave a failed or congested path they all pick the same "empty" alternative and
overload it - the oscillation that hit delay-based ARPANET routing in 1979. With the guard,
each decision adds the moving flow's rate to the counters the next flow sees.

Timings recorded per incident (all from wall-clock timestamps in this one process):
  detection  = link declared dead  - failure injected     (probe silence > dead interval)
  reroute    = new routes installed - link declared dead   (path compute + ip route writes)
  recovery   = first end-to-end echo after reroute - failure injected   (service restored)
"""

from __future__ import annotations

import itertools
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Settings, wire_bytes
from ..events import EventBus
from ..telemetry import Telemetry
from ..topology import AddressPlan, Topology
from .graph import build_graph, core_hops
from .installer import RouteInstaller
from .protection import desired_paths, path_link_ids, scenario_key, validate_path
from .scoring import HopMetrics, PathEval, Weights, decide, evaluate
from .yen import k_shortest_paths

log = logging.getLogger(__name__)
SUSPECT_SILENCE_S = 0.5
MODES = ("static", "adaptive", "intent")


def fmt_path(p: list[str] | None) -> str:
    return "–".join(p) if p else "∅"


@dataclass
class FlowRoute:
    pair: str
    src: str
    dst: str
    candidates: list[list[str]]
    static_path: list[str]
    path: list[str] | None = None
    since: float = 0.0
    changes: int = 0
    last_reason: str = "initial"
    evals: list[PathEval] = field(default_factory=list)


@dataclass
class Incident:
    id: int
    kind: str  # failure | degradation
    pair: str
    link: str | None
    cause: str | None
    mode: str
    t_inject: float | None
    t_detect: float
    from_path: list[str] | None
    to_path: list[str] | None = None
    t_reroute: float | None = None
    install_ms: float | None = None
    t_recover: float | None = None
    status: str = "open"  # open | recovered | unrecovered
    note: str = ""
    baseline_rtt: float | None = None
    cause_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        def ms(a, b):
            return None if a is None or b is None else round((a - b) * 1000, 1)

        return {
            "id": self.id,
            "kind": self.kind,
            "pair": self.pair,
            "link": self.link,
            "cause": self.cause,
            "mode": self.mode,
            "status": self.status,
            "note": self.note,
            "t_inject": self.t_inject,
            "t_detect": self.t_detect,
            "t_reroute": self.t_reroute,
            "t_recover": self.t_recover,
            "from_path": self.from_path,
            "to_path": self.to_path,
            "detection_ms": ms(self.t_detect, self.t_inject),
            "reroute_ms": ms(self.t_reroute, self.t_detect),
            "install_ms": None if self.install_ms is None else round(self.install_ms, 1),
            "recovery_ms": ms(self.t_recover, self.t_inject if self.t_inject else self.t_detect),
        }


class RoutingController:
    def __init__(
        self,
        topo: Topology,
        plan: AddressPlan,
        settings: Settings,
        telemetry: Telemetry,
        installer: RouteInstaller,
        events: EventBus,
        offered_mbps: Callable[[str, str], float],
        find_cause: Callable[[str, float, tuple[str, ...]], Any],
        tx_bps: Callable[[str, str], float],
    ) -> None:
        self.topo = topo
        self.plan = plan
        self.s = settings
        self.tel = telemetry
        self.inst = installer
        self.events = events
        self.offered_mbps = offered_mbps
        self.find_cause = find_cause
        self.tx_bps = tx_bps  # (link_id, sender node) -> measured bits/s
        self.g = build_graph(topo)
        self.mode = "adaptive"
        self.weights = Weights()
        self.hysteresis = settings.hysteresis
        self.hold_down_s = settings.hold_down_s
        self.core_links = [l.id for l in topo.links.values() if topo.nodes[l.a].type == "router" and topo.nodes[l.b].type == "router"]
        self.link_alive: dict[str, bool] = {l: True for l in self.core_links}
        self.flows: dict[str, FlowRoute] = {}
        for c, s in topo.flow_pairs():
            cands = [p for _, p in k_shortest_paths(self.g, c, s, settings.k_paths, "delay_ms")]
            self.flows[f"{c}>{s}"] = FlowRoute(pair=f"{c}>{s}", src=c, dst=s, candidates=cands, static_path=cands[0])
        self.incidents: deque[Incident] = deque(maxlen=100)
        self._inc_ids = itertools.count(1)
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._reconcile_now = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="routing", daemon=True)
        self.wire_factor = wire_bytes(settings.iperf_payload_bytes) / settings.iperf_payload_bytes
        self.last_tick_ms = 0.0
        self.herd_guard = settings.herd_guard
        self.wtr_s = settings.wtr_s
        self.route_plan: dict | None = None  # intent mode: {"id", "label", "primary", "protection", ...}
        self.link_up_since: dict[str, float] = {l: 0.0 for l in self.core_links}
        self.scenario: str | None = None  # failure scenario the intent-mode controller is serving
        self.on_switch: list[Callable[[str, list[str] | None, list[str], str], None]] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.inst.setup_rules()
        now = time.time()
        for f in self.flows.values():
            self.inst.install_flow(f.pair, f.static_path, None)
            f.path, f.since, f.last_reason = f.static_path, now, "initial"
        self._reconcile()
        self.events.emit("routing.start", f"Routing controller started in {self.mode} mode; {len(self.flows)} managed flows")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def request_reconcile(self) -> None:
        self._reconcile_now.set()

    # ------------------------------------------------------------------ configuration
    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("mode must be 'static', 'adaptive' or 'intent'")
        with self.lock:
            if mode == self.mode:
                return
            if mode == "intent" and not self.route_plan:
                raise ValueError("intent mode needs a plan: create one on the Assure page first")
            self.mode = mode
            label = {"static": "STATIC", "adaptive": "ADAPTIVE", "intent": "INTENT (planned)"}[mode]
            self.events.emit("routing.mode", f"Routing mode set to {label}", mode=mode)
            now = time.time()
            if mode == "static":
                for f in self.flows.values():
                    if f.path != f.static_path:
                        self._switch(f, f.static_path, "static", now)
            elif mode == "intent":
                self._enforce_plan(now)
            else:
                self._rescore(now, force=True)
            self.request_reconcile()

    def set_herd_guard(self, on: bool) -> None:
        with self.lock:
            self.herd_guard = bool(on)
        self.events.emit("routing.herd_guard", f"Herd guard {'on' if on else 'off'}: flows moving in the same tick "
                         f"{'see' if on else 'no longer see'} each other's load", herd_guard=bool(on))

    def apply_plan(self, plan: dict, source: str = "user") -> dict:
        """Load a plan (primary + protection paths) and switch to intent mode."""
        primary = plan.get("primary") or {}
        if set(primary) != set(self.flows):
            raise ValueError("a plan must give a primary path for every managed flow")
        for pair, path in primary.items():
            f = self.flows[pair]
            validate_path(path, f.src, f.dst, self.topo)
        for key, entry in (plan.get("protection") or {}).items():
            for pair, path in entry.items():
                if pair not in self.flows:
                    raise ValueError(f"protection {key}: unknown flow {pair}")
                validate_path(path, self.flows[pair].src, self.flows[pair].dst, self.topo)
        with self.lock:
            prev = self.route_plan
            self.route_plan = {**plan, "applied_t": time.time(), "applied_by": source}
            was = self.mode
            self.mode = "intent"
            now = time.time()
            moved = self._enforce_plan(now, reason="plan")
            self.request_reconcile()
        n_prot = len(plan.get("protection") or {})
        self.events.emit(
            "routing.plan",
            f"Plan {plan.get('id', '?')} applied ({source}): {moved} flow{'s' if moved != 1 else ''} moved, "
            f"{n_prot} failure scenario{'s' if n_prot != 1 else ''} pre-planned" + ("" if was == "intent" else f"; routing {was} → intent"),
            severity="success", plan=plan.get("id"), previous=prev.get("id") if prev else None, source=source,
        )
        return {"moved": moved, "previous": prev}

    def restore(self, plan: dict | None, mode: str, source: str = "rollback") -> None:
        """Put a previous plan/mode back (used by the autopilot's rollback)."""
        with self.lock:
            self.route_plan = plan
            self.mode = mode if (mode != "intent" or plan) else "adaptive"
            now = time.time()
            if self.mode == "intent":
                self._enforce_plan(now, reason=source)
            elif self.mode == "static":
                for f in self.flows.values():
                    if f.path != f.static_path:
                        self._switch(f, f.static_path, source, now)
            else:
                self._rescore(now, force=True)
            self.request_reconcile()

    def clear_plan(self, mode: str = "adaptive") -> None:
        with self.lock:
            self.route_plan = None
            self.scenario = None
            if self.mode != "intent":
                return
            self.mode = mode if mode in ("static", "adaptive") else "adaptive"
            self.events.emit("routing.mode", f"Plan cleared; routing mode set to {self.mode.upper()}", mode=self.mode)
            now = time.time()
            if self.mode == "static":
                for f in self.flows.values():
                    if f.path != f.static_path:
                        self._switch(f, f.static_path, "static", now)
            else:
                self._rescore(now, force=True)
            self.request_reconcile()

    def set_weights(self, latency: float, loss: float, util: float) -> None:
        for v in (latency, loss, util):
            if v < 0 or v > 1000:
                raise ValueError("weights must be within [0, 1000]")
        with self.lock:
            self.weights = Weights(latency, loss, util)
        self.events.emit("routing.weights", f"Path-score weights set to latency={latency:g}, loss={loss:g}, util={util:g}", weights=self.weights.to_dict())

    # ------------------------------------------------------------------ evaluation
    def _link_cache(self, now: float) -> dict[str, dict]:
        return {lid: self.tel.routing_link_metrics(lid, now) for lid in self.core_links}

    def _silence(self, lid: str, now: float) -> float:
        quiet = [q for q in (s.silence(now) for s in self.tel.streams(self.tel.link_streams[lid])) if q is not None]
        return min(quiet) if quiet else math.inf

    def suspect_links(self, now: float) -> set[str]:
        """Links whose probes have been silent for > SUSPECT_SILENCE_S but are not yet declared dead.

        A silently failing link also *looks idle* on the counters (a veth whose peer is down drops
        without counting tx bytes), so without this rule a load-aware controller is attracted
        into the black hole during the detection window - observed live before this was added.
        """
        return {lid for lid in self.core_links if self.link_alive.get(lid, True) and self._silence(lid, now) > SUSPECT_SILENCE_S}

    def evaluate_flow(
        self, f: FlowRoute, now: float, cache: dict[str, dict] | None = None, suspect_policy: str = "avoid_new",
        moved: dict[tuple[str, str], float] | None = None,
    ) -> list[PathEval]:
        """Score every candidate path of a flow.

        moved: herd guard - bit/s already added to (+) or taken off (-) each directed hop by
        flows that switched earlier in the same tick (the counters cannot show them yet).

        suspect_policy:
          "avoid_new" - suspect links make *other* candidates infeasible, but never the current
                        path (suspicion alone must not trigger a fail-over: a +500 ms latency
                        step also causes a short probe silence)
          "avoid_all" - used during a fail-over: steer clear of anything suspicious if possible
          "none"      - pure scores (fallback when everything is suspect)
        """
        cache = cache or self._link_cache(now)
        own = self.offered_mbps(f.src, f.dst) * 1e6 * self.wire_factor
        cur_hops = {(u, v) for u, v, _ in core_hops(self.topo, f.path)} if f.path else set()
        suspect = self.suspect_links(now) if suspect_policy != "none" else set()
        out = []
        for path in f.candidates:
            hops = []
            is_current = path == f.path
            for u, v, lid in core_hops(self.topo, path):
                m = cache[lid]
                cap = max(1e-9, m["cap_mbps"] * 1e6)
                fwd = self.tx_bps(lid, u) + (moved.get((u, v), 0.0) if moved else 0.0)
                rev = self.tx_bps(lid, v) + (moved.get((v, u), 0.0) if moved else 0.0)
                base = max(0.0, fwd - (own if (u, v) in cur_hops else 0.0))
                util = max((base + own) / cap, rev / cap)
                alive = self.link_alive.get(lid, True)
                if lid in suspect and (suspect_policy == "avoid_all" or not is_current):
                    alive = False
                hops.append(HopMetrics(lid, u, v, m["latency_ms"], m["loss"], util, alive))
            out.append(evaluate(path, hops, self.weights))
        if suspect_policy == "avoid_all" and not any(e.feasible for e in out):
            return self.evaluate_flow(f, now, cache, suspect_policy="none", moved=moved)
        return out

    def _note_move(self, moved: dict[tuple[str, str], float] | None, f: FlowRoute, old: list[str] | None, new: list[str]) -> None:
        if moved is None:
            return
        own = self.offered_mbps(f.src, f.dst) * 1e6 * self.wire_factor
        if own <= 0:
            return
        for u, v, _ in core_hops(self.topo, old or []):
            moved[(u, v)] = moved.get((u, v), 0.0) - own
        for u, v, _ in core_hops(self.topo, new):
            moved[(u, v)] = moved.get((u, v), 0.0) + own

    @staticmethod
    def _current(f: FlowRoute, evals: list[PathEval]) -> PathEval | None:
        return next((e for e in evals if e.path == f.path), None)

    def _switch(self, f: FlowRoute, path: list[str], reason: str, now: float, old_eval: PathEval | None = None, new_eval: PathEval | None = None) -> float:
        old = f.path
        elapsed = self.inst.install_flow(f.pair, path, old)
        f.path = path
        f.since = time.time()
        f.changes += 1
        f.last_reason = reason
        detail = ""
        if old_eval and new_eval and old_eval.feasible:
            detail = f"; score {old_eval.score:.1f} → {new_eval.score:.1f}"
        self.events.emit(
            "routing.reroute",
            f"{f.src}→{f.dst} {reason}: {fmt_path(old)} ⇒ {fmt_path(path)} (routes installed in {elapsed * 1000:.0f} ms{detail})",
            severity="success" if reason != "static" else "info",
            pair=f.pair, from_path=old, to_path=path, reason=reason, install_ms=elapsed * 1000,
        )
        for fn in list(self.on_switch):
            try:
                fn(f.pair, old, path, reason)
            except Exception:
                pass
        return elapsed

    def _rescore(self, now: float, force: bool = False) -> None:
        cache = self._link_cache(now)
        moved: dict[tuple[str, str], float] | None = {} if self.herd_guard else None
        desired = self._desired(now)[1] if self.mode == "intent" else {}
        for f in self.flows.values():
            evals = self.evaluate_flow(f, now, cache, moved=moved)
            f.evals = evals
            if self.mode == "intent" and desired.get(f.pair) is not None:
                continue  # the plan decides; _enforce_plan installs it
            if self.mode == "static":
                continue
            cur = self._current(f, evals)
            hold_ok = force or (now - f.since) >= self.hold_down_s
            chosen, reason = decide(cur, evals, self.hysteresis, hold_ok)
            if chosen is None or chosen.path == f.path:
                continue
            old_path = f.path
            elapsed = self._switch(f, chosen.path, reason if self.mode == "adaptive" else f"{reason} (unplanned)", now, cur, chosen)
            self._note_move(moved, f, old_path, chosen.path)
            if reason == "better" and old_path:
                # a voluntary switch is an *incident* only if a fault we injected explains it;
                # otherwise it is ordinary load balancing and only appears in the event log
                old_links = [lid for _, _, lid in core_hops(self.topo, old_path)]
                cause = self.find_cause_any(old_links, now, ("link_latency", "link_loss", "link_bandwidth", "traffic_burst"))
                if cause is None or any(i.cause_id == cause.id and i.pair == f.pair for i in self.incidents):
                    continue
                inc = Incident(
                    id=next(self._inc_ids), kind="degradation", pair=f.pair,
                    link=cause.target if cause.kind != "traffic_burst" else (cur.bottleneck if cur else None),
                    cause=cause.label, cause_id=cause.id, mode=self.mode,
                    t_inject=cause.t, t_detect=now, from_path=old_path, to_path=chosen.path,
                    t_reroute=time.time(), install_ms=elapsed * 1000,
                )
                inc.baseline_rtt = self._baseline_rtt(f.pair, inc.t_inject)
                self.incidents.append(inc)

    # ------------------------------------------------------------------ intent mode
    def _failed_links(self, now: float) -> set[str]:
        """Dead links, plus links that came back less than wait-to-restore seconds ago."""
        return {l for l in self.core_links if not self.link_alive[l] or now - self.link_up_since.get(l, 0.0) < self.wtr_s}

    def _desired(self, now: float) -> tuple[str | None, dict[str, list[str] | None]]:
        if not self.route_plan:
            return None, {}
        return desired_paths(self.route_plan, self._failed_links(now), self.topo)

    def _enforce_plan(self, now: float, reason: str = "plan") -> int:
        """Install the plan's path for every flow whose path differs (make-before-break)."""
        if self.mode != "intent" or not self.route_plan:
            return 0
        key, desired = self._desired(now)
        prev_key, self.scenario = self.scenario, key
        moved = 0
        for f in self.flows.values():
            target = desired.get(f.pair)
            if target is None or target == f.path:
                continue
            if key != prev_key:
                why = "protect" if key else "revert"
            else:
                why = reason
            self._switch(f, target, why, now)
            moved += 1
        if key is None and prev_key is not None:
            self.events.emit("routing.scenario", f"Scenario {prev_key} over: links stable for {self.wtr_s:g} s, flows back on their primary paths",
                             severity="success", scenario=None)
        return moved

    def find_cause_any(self, links: list[str], now: float, kinds: tuple[str, ...]):
        found = [c for c in (self.find_cause(l, now, kinds) for l in links) if c is not None]
        return max(found, key=lambda c: c.t) if found else None

    def _baseline_rtt(self, pair: str, before: float) -> float | None:
        s = self.tel.store.get(f"F:{pair}")
        if not s:
            return None
        vals = s.rtt_samples(before - 5.0, before)
        return sum(vals) / len(vals) if vals else None

    # ------------------------------------------------------------------ failure handling
    def _detect(self, now: float) -> None:
        for lid in self.core_links:
            streams = self.tel.streams(self.tel.link_streams[lid])
            alive = self.tel._liveness(streams, now)
            prev = self.link_alive[lid]
            if alive is False and prev:
                self.link_alive[lid] = False
                self._on_link_dead(lid, now)
            elif alive is True and not prev:
                self.link_alive[lid] = True
                self.link_up_since[lid] = now
                self.events.emit("routing.link_up", f"Link {lid} is answering probes again", severity="success", link=lid)
                self.request_reconcile()

    def _on_link_dead(self, lid: str, now: float) -> None:
        cause = self.find_cause(lid, now, ("link_down", "node_down"))
        t_inj = cause.t if cause else None
        det = f" {(now - t_inj) * 1000:.0f} ms after injection" if t_inj else ""
        self.events.emit(
            "routing.detect",
            f"Link {lid} declared DOWN (no probe echo for {self.s.dead_min_s:g}+ s){det}",
            severity="error", link=lid, detection_ms=(now - t_inj) * 1000 if t_inj else None,
        )
        cache = self._link_cache(now)
        moved: dict[tuple[str, str], float] | None = {} if self.herd_guard else None
        desired = self._desired(now)[1] if self.mode == "intent" else {}
        if self.mode == "intent":
            key = scenario_key(self._failed_links(now), self.topo)
            self.scenario = key
            covered = key in (self.route_plan.get("protection") or {}) if self.route_plan else False
            self.events.emit("routing.scenario", f"Failure scenario {key}: " + (
                "switching to the pre-planned backup paths" if covered else "not covered by the plan, adaptive fallback"),
                severity="success" if covered else "warn", scenario=key, planned=covered)
        for f in self.flows.values():
            if not f.path or lid not in [l for _, _, l in core_hops(self.topo, f.path)]:
                continue
            inc = Incident(
                id=next(self._inc_ids), kind="failure", pair=f.pair, link=lid,
                cause=cause.label if cause else "unexplained probe loss", mode=self.mode,
                t_inject=t_inj, t_detect=now, from_path=f.path, cause_id=cause.id if cause else None,
            )
            planned = desired.get(f.pair)
            if self.mode == "intent" and planned is not None and planned != f.path:
                old_path = f.path
                elapsed = self._switch(f, planned, "protect", now)
                self._note_move(moved, f, old_path, planned)
                inc.t_reroute = time.time()
                inc.install_ms = elapsed * 1000
                inc.to_path = planned
                inc.note = "pre-planned backup path"
            elif self.mode in ("adaptive", "intent"):
                evals = self.evaluate_flow(f, now, cache, suspect_policy="avoid_all", moved=moved)
                f.evals = evals
                chosen, reason = decide(self._current(f, evals), evals, self.hysteresis, True)
                if chosen is not None and chosen.path != f.path:
                    old_path = f.path
                    elapsed = self._switch(f, chosen.path, "failover" if self.mode == "adaptive" else "failover (unplanned)", now)
                    self._note_move(moved, f, old_path, chosen.path)
                    inc.t_reroute = time.time()
                    inc.install_ms = elapsed * 1000
                    inc.to_path = chosen.path
                else:
                    inc.note = "no feasible alternative path"
            else:
                inc.note = "static routing does not react; waiting for repair"
            self.incidents.append(inc)
        if self.mode == "intent":
            # flows the plan moves although they did not cross the dead link (global protection)
            for f in self.flows.values():
                target = desired.get(f.pair)
                if target is not None and target != f.path and lid not in path_link_ids(f.path, self.topo):
                    self._switch(f, target, "protect", now)
        self.request_reconcile()

    def _track_recovery(self, now: float) -> None:
        for inc in list(self.incidents):
            if inc.status != "open":
                continue
            s = self.tel.store.get(f"F:{inc.pair}")
            if s is None:
                continue
            if inc.kind == "failure":
                ref = inc.t_reroute or inc.t_detect
                t = s.first_reply_after(ref)
                if t is not None:
                    self._close(inc, t)
            else:
                # degradation: recovered once the 1 s mean RTT is back near the pre-injection baseline
                vals = s.rtt_samples(now - 1.0, now)
                loss = s.loss_pct(now, 1.0) or 0.0
                if vals and inc.baseline_rtt and now - (inc.t_reroute or now) >= 0.5:
                    if sum(vals) / len(vals) <= inc.baseline_rtt * 1.25 + 1.0 and loss == 0.0:
                        self._close(inc, now)
                elif not inc.baseline_rtt and now - inc.t_detect > 1:
                    self._close(inc, inc.t_reroute or now)
            if inc.status == "open" and now - inc.t_detect > 300:
                inc.status = "unrecovered"

    def _close(self, inc: Incident, t: float) -> None:
        inc.t_recover = t
        inc.status = "recovered"
        d = inc.to_dict()
        parts = [f"{k} {d[k + '_ms']:.0f} ms" for k in ("detection", "reroute", "recovery") if d.get(k + "_ms") is not None]
        self.events.emit(
            "routing.recover",
            f"{inc.pair.replace('>', '→')} recovered ({inc.kind}): " + ", ".join(parts),
            severity="success", incident=d,
        )

    # ------------------------------------------------------------------ loop
    def _reconcile(self) -> None:
        dead = {l for l, a in self.link_alive.items() if not a} if self.mode != "static" else set()
        self.inst.reconcile(self.g, dead, {p: f.path for p, f in self.flows.items()})

    def _loop(self) -> None:
        last_rescore = last_rec = 0.0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                with self.lock:
                    self._detect(t0)
                    if t0 - last_rescore >= self.s.rescore_interval_s:
                        self._rescore(t0)
                        self._enforce_plan(t0)
                        last_rescore = t0
                    if self._reconcile_now.is_set() or t0 - last_rec >= self.s.reconcile_interval_s:
                        self._reconcile_now.clear()
                        self._reconcile()
                        last_rec = t0
                    self._track_recovery(t0)
            except Exception:  # keep the controller alive whatever happens
                log.exception("routing loop error")
            self.last_tick_ms = (time.time() - t0) * 1000
            self._stop.wait(max(0.01, self.s.controller_tick_s - (time.time() - t0)))

    # ------------------------------------------------------------------ state
    def state(self) -> dict:
        with self.lock:
            flows = {}
            for p, f in self.flows.items():
                evals = f.evals
                flows[p] = {
                    "pair": p,
                    "src": f.src,
                    "dst": f.dst,
                    "path": f.path,
                    "static_path": f.static_path,
                    "since": f.since,
                    "changes": f.changes,
                    "last_reason": f.last_reason,
                    "offered_mbps": self.offered_mbps(f.src, f.dst),
                    "candidates": [dict(e.to_dict(), chosen=(e.path == f.path)) for e in evals],
                }
            return {
                "mode": self.mode,
                "weights": self.weights.to_dict(),
                "hysteresis": self.hysteresis,
                "hold_down_s": self.hold_down_s,
                "dead_interval_s": self.s.dead_min_s,
                "probe_interval_s": self.s.probe_interval_s,
                "links_alive": dict(self.link_alive),
                "links_suspect": sorted(self.suspect_links(time.time())),
                "flows": flows,
                "incidents": [i.to_dict() for i in list(self.incidents)[-30:]],
                "tick_ms": round(self.last_tick_ms, 2),
                "herd_guard": self.herd_guard,
                "wtr_s": self.wtr_s,
                "scenario": self.scenario,
                "plan": None if not self.route_plan else {
                    "id": self.route_plan.get("id"), "label": self.route_plan.get("label"),
                    "primary": self.route_plan.get("primary"), "protected": sorted((self.route_plan.get("protection") or {}).keys()),
                    "applied_t": self.route_plan.get("applied_t"), "applied_by": self.route_plan.get("applied_by"),
                },
            }
