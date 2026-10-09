"""AIOps service: runs the detector and root-cause analysis once a second and owns the copilot.

Plugged into the runtime as extension "ai" (see services.py). Its compact view is part of
every WebSocket tick (snapshot["ai"]), so the map, the insights panel and the copilot all see
the same, current diagnosis.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .anomaly import Z_OFF, Anomaly, AnomalyDetector
from .rca import FlowInfo, LinkLoad, SignalState, StreamObs, diagnose
from .signals import build_specs, element_link, gateway_path, path_elements, sample

log = logging.getLogger(__name__)
BASELINE_PATH_STABLE_S = 30.0


class AIService:
    def __init__(self, rt) -> None:
        self.rt = rt
        topo = rt.topo
        self.specs = build_specs(topo)
        self.detector = AnomalyDetector(self.specs, on_raise=self._on_raise, on_clear=self._on_clear)
        self.diagnosis: dict[str, Any] = {"t": None, "status": "learning", "causes": [], "consequences": [], "unexplained": [], "observed": {}}
        self.baseline_path: dict[str, list[str] | None] = {}
        self._last_cause_ids: tuple[str, ...] = ()
        self._stop = threading.Event()
        self.lock = threading.RLock()
        self.tick_ms = 0.0
        # static parts of the probe geometry
        self._link_elems = {
            lid: frozenset({element_link(lid), f"node:{topo.links[lid].a}", f"node:{topo.links[lid].b}"}) for lid in topo.links
        }
        self._gw_elems = {h: path_elements(topo, gateway_path(topo, h, rt.plan.host_gateway[h])) for h in topo.hosts}
        from .copilot import Copilot
        from .evaluation import AIEvaluationService

        self.copilot = Copilot(rt, self)
        self.evaluation = AIEvaluationService(rt, self)
        threading.Thread(target=self._loop, name="ai", daemon=True).start()

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            t0 = time.time()
            try:
                self.step(t0)
            except Exception:
                log.exception("AI step failed")
            self.tick_ms = (time.time() - t0) * 1000

    def step(self, now: float) -> None:
        values = sample(self.rt, self.specs, now)
        self.detector.update(now, values)
        diag = diagnose(*self._rca_inputs(now))
        with self.lock:
            self.diagnosis = diag
        ids = tuple(c["id"] for c in diag["causes"])
        if ids != self._last_cause_ids:
            for c in diag["causes"]:
                if c["id"] not in self._last_cause_ids:
                    self.rt.events.emit(
                        "ai.diagnosis", f"AI diagnosis: {c['title']} ({c['confidence']} confidence, explains {c['explains']} "
                        f"observation{'s' if c['explains'] != 1 else ''})", severity="warn", cause=c["id"],
                    )
            if not ids and self._last_cause_ids:
                self.rt.events.emit("ai.diagnosis", "AI diagnosis: every probe path measures normal again", severity="success")
            self._last_cause_ids = ids

    def _sig(self, sid: str) -> SignalState:
        s = self.detector.state_of(sid)
        if s is None:
            return SignalState()
        return SignalState(
            state=s.state, value=s.last_value, mean=s.mean if s.n else None, z=s.last_z,
            since=s.anomaly.t_start if s.anomaly else None,
        )

    def _rca_inputs(self, now: float):
        rt = self.rt
        topo, ctrl, tel = rt.topo, rt.controller, rt.telemetry
        dead_min, dead_mult = rt.s.dead_min_s, rt.s.dead_mult

        def liveness(sid: str) -> tuple[bool | None, float | None]:
            st = rt.store.get(sid)
            if st is None:
                return None, None
            d = st.is_dead(now, dead_min, dead_mult)
            silent = st.silence(now)
            return (None if d is None else not d), silent

        flows: dict[str, FlowInfo] = {}
        for pair, f in list(ctrl.flows.items()):
            path = list(f.path) if f.path else None
            # the path the flow's RTT baseline was learned on. A new path is adopted only after the
            # flow has stayed on it for BASELINE_PATH_STABLE_S with a clearly normal RTT: the
            # controller often reroutes (~1.3 s) before the detector fires, and the 2 s RTT window
            # can look normal for a tick right after the switch, so a z test alone would race
            rtt_state = self.detector.state_of(f"flow:{pair}:rtt")
            clearly_normal = (rtt_state is not None and rtt_state.state == "normal"
                              and rtt_state.last_z is not None and rtt_state.last_z < Z_OFF)
            stable = now - (f.since or 0.0) >= BASELINE_PATH_STABLE_S
            learning = rtt_state is not None and rtt_state.state == "learning"
            if path and (pair not in self.baseline_path or learning or (stable and clearly_normal)):
                self.baseline_path[pair] = path
            base = self.baseline_path.get(pair)
            flows[pair] = FlowInfo(pair, path, path_elements(topo, path), base, path_elements(topo, base), rt.traffic.offered_mbps(f.src, f.dst))

        streams: list[StreamObs] = []
        for lid in topo.links:
            if topo.nodes[topo.links[lid].a].type == "router" and topo.nodes[topo.links[lid].b].type == "router":
                alive, silent = liveness(f"L:{lid}")
                streams.append(StreamObs(f"L:{lid}", f"{lid} probes", self._link_elems[lid], alive, silent,
                                         self._sig(f"link:{lid}:rtt"), self._sig(f"link:{lid}:loss")))
        for h in topo.hosts:
            alive, silent = liveness(f"G:{h}")
            streams.append(StreamObs(f"G:{h}", f"{h} → gateway probes", self._gw_elems[h], alive, silent,
                                     self._sig(f"access:{h}:rtt"), self._sig(f"access:{h}:loss")))
        for pair, fi in flows.items():
            alive, silent = liveness(f"F:{pair}")
            rerouted = bool(fi.path and fi.baseline_path and fi.path != fi.baseline_path)
            streams.append(StreamObs(
                f"F:{pair}", f"{pair.replace('>', ' → ')} end-to-end probes", fi.elements, alive, silent,
                self._sig(f"flow:{pair}:rtt"), self._sig(f"flow:{pair}:loss"), flow=pair, rerouted=rerouted,
                # a rerouted flow's loss window still holds probes sent down the old path
                window_elements=(fi.elements | fi.baseline_elements) if rerouted else None,
            ))

        # iperf3 data loss, measured at the receiver: same tomography, over the flow's path
        for pair, fi in flows.items():
            if fi.offered_mbps > 0:
                rerouted = bool(fi.path and fi.baseline_path and fi.path != fi.baseline_path)
                streams.append(StreamObs(
                    f"D:{pair}", f"{pair.replace('>', ' → ')} iperf3 data", fi.elements, None, None,
                    SignalState(), self._sig(f"flow:{pair}:data_loss"), flow=pair, rerouted=rerouted,
                    window_elements=(fi.elements | fi.baseline_elements) if rerouted else None,
                ))

        loads: dict[str, LinkLoad] = {}
        for lid, link in topo.links.items():
            cap = tel.link_params(lid).bw_mbps
            ab, ba = tel.direction(lid, link.a, cap), tel.direction(lid, link.b, cap)
            loads[lid] = LinkLoad(max(ab["util"], ba["util"]), cap, max(ab["bps"], ba["bps"]) / 1e6, ab["drops_ps"] + ba["drops_ps"])

        bursts = [{"pair": f.pair, "rate_mbps": f.rate_mbps} for f in rt.traffic.running() if f.kind == "burst"]
        node_types = {n: topo.nodes[n].type for n in topo.nodes}
        return streams, loads, flows, bursts, node_types, now

    # ------------------------------------------------------------------ events
    def _on_raise(self, a: Anomaly) -> None:
        unit = {"ms": " ms", "%": "%", "ratio": ""}[a.unit]
        val = f"{a.value * 100:.0f}% load" if a.unit == "ratio" else f"{a.value:.2f}{unit}"
        norm = f"{a.baseline_mean * 100:.0f}%" if a.unit == "ratio" else f"{a.baseline_mean:.2f}{unit}"
        self.rt.events.emit("ai.anomaly", f"Unusual {a.label}: {val}, normally {norm}", severity="warn", anomaly=a.id, signal=a.signal)

    def _on_clear(self, a: Anomaly) -> None:
        self.rt.events.emit("ai.anomaly_clear", f"{a.label.capitalize()} back to normal after {a.t_end - a.t_start:.0f} s", anomaly=a.id, signal=a.signal)

    # ------------------------------------------------------------------ views
    def insights(self) -> dict:
        with self.lock:
            diag = self.diagnosis
        return {
            "t": time.time(),
            "anomalies": [a.to_dict() for a in self.detector.active()],
            "recent": [a.to_dict() for a in reversed(self.detector.recent(30))],
            "diagnosis": diag,
            "detector": self.detector.summary(),
        }

    def snapshot_view(self) -> dict:
        with self.lock:
            diag = self.diagnosis
        return {
            "anomalies": [a.to_dict() for a in self.detector.active()],
            "diagnosis": diag,
            "detector": self.detector.summary(),
            "copilot": self.copilot.status_brief(),
        }

    def status(self) -> dict:
        return self.evaluation.status()

    def relearn(self) -> dict:
        self.detector.relearn()
        self.baseline_path.clear()
        self.rt.events.emit("ai.relearn", "AI baselines reset: learning the network's normal behaviour again (20 s)")
        return self.detector.summary()

    def stop(self) -> None:
        self._stop.set()
        self.evaluation.stop()
