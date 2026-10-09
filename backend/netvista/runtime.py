"""Runtime: builds and owns every subsystem, and composes the live snapshot.

Start-up order matters:
  1. Mininet network (namespaces, veths, OVS)       emulation
  2. tc on every interface from the topology JSON   chaos (base state)
  3. counters + probe agents                         telemetry
  4. policy rules + initial routes, controller loop  routing
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Any

from .chaos import ChaosEngine
from .config import Settings
from .emulation.network import EmulatedNetwork
from .emulation.traffic import TrafficManager
from .events import EventBus
from .routing.controller import RoutingController
from .routing.installer import RouteInstaller
from .telemetry import AgentManager, CounterPoller, ProbeStore, Telemetry, plan_probe_targets
from .topology import build_address_plan, load_topology

log = logging.getLogger(__name__)


def sanitize(obj: Any) -> Any:
    """Make a structure JSON-safe for browsers (NaN/inf -> None)."""
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    return obj


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.topo = load_topology(settings.topology_path)
        self.plan = build_address_plan(self.topo)
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventBus(log_path=settings.runs_dir / "events.jsonl")
        self.history: deque[dict] = deque(maxlen=settings.history_len)
        self.started_at: float | None = None
        self.status = "starting"
        self.error: str | None = None
        self._stop = threading.Event()
        self.net: EmulatedNetwork | None = None
        self.extensions: dict[str, Any] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        s, topo, plan = self.s, self.topo, self.plan
        self.events.emit("system.boot", f"Booting topology '{topo.name}' in Mininet ({len(topo.nodes)} nodes, {len(topo.links)} links)")
        t0 = time.time()
        self.net = EmulatedNetwork(topo, plan, switch_impl=s.switch_impl)
        self.net.start()
        self.traffic = TrafficManager(self.net, plan, self.events, s.iperf_payload_bytes, s.iperf_base_port)
        self.chaos = ChaosEngine(topo, self.net, self.traffic, self.events)
        self.chaos.init_network()

        self.store = ProbeStore()
        ns_intfs: dict[int | None, list[str]] = {}
        for n in topo.nodes:
            ns_intfs.setdefault(self.net.pid(n), []).extend(i.name for i in plan.intfs_of(n))
        self.counters = CounterPoller(ns_intfs, s.counter_interval_s, s.tc_stats_interval_s)
        self.counters.start()
        self.telemetry = Telemetry(topo, plan, s, self.store, self.counters, self.chaos.effective_params, self.chaos.admin_up)
        self.agents = AgentManager(self.store, s)
        self.agents.start(self.net.pids, plan_probe_targets(topo, plan))

        self.installer = RouteInstaller(topo, plan, self.net.pids)
        self.controller = RoutingController(
            topo, plan, s, self.telemetry, self.installer, self.events,
            offered_mbps=self.traffic.offered_mbps,
            find_cause=self.chaos.find_cause,
            tx_bps=lambda lid, n: self.counters.get(plan.intf(lid, n).name).tx_bps,
        )
        self.chaos.on_change.append(lambda inj, what: self.controller.request_reconcile())
        self.controller.start()

        self._init_extensions()
        threading.Thread(target=self._history_loop, name="history", daemon=True).start()
        self.started_at = time.time()
        self.status = "running"
        self.events.emit("system.ready", f"Emulated network is up in {time.time() - t0:.1f}s; probes and controller running", severity="success")

    def _init_extensions(self) -> None:
        """Phase 4/5 services (simulator, validation, scenarios, demo) plug in here."""
        try:
            from .services import attach_services
        except ImportError:  # pragma: no cover - services are optional during development
            return
        attach_services(self)

    def stop(self) -> None:
        self._stop.set()
        for name in ("demo", "replayer", "validation"):
            ext = self.extensions.get(name)
            if ext and hasattr(ext, "stop"):
                try:
                    ext.stop()
                except Exception:
                    pass
        sim = self.extensions.get("simulator")
        if sim and hasattr(sim, "shutdown"):
            sim.shutdown()
        for comp in ("controller", "chaos", "counters"):
            obj = getattr(self, comp, None)
            if obj:
                obj.stop()
        if getattr(self, "traffic", None):
            self.traffic.shutdown()
        if getattr(self, "agents", None):
            self.agents.stop()
        if self.net:
            self.net.stop()
        self.status = "stopped"

    # ------------------------------------------------------------------ snapshot
    def flow_view(self, pair: str, rflow: dict, now: float) -> dict:
        src, dst = pair.split(">")
        return {
            "pair": pair,
            "src": src,
            "dst": dst,
            "path": rflow["path"],
            "offered_mbps": rflow["offered_mbps"],
            "probe": self.telemetry.flow_probe_view(pair, now),
            "traffic": self.traffic.pair_stats(src, dst, 3.0, now),
        }

    def snapshot(self) -> dict:
        now = time.time()
        links = {lid: self.telemetry.link_view(lid, now) for lid in self.topo.links}
        nodes = {n: self.telemetry.node_view(n, links) for n in self.topo.nodes}
        routing = self.controller.state()
        flows = {p: self.flow_view(p, rf, now) for p, rf in routing["flows"].items()}
        snap = {
            "t": now,
            "uptime_s": now - (self.started_at or now),
            "links": links,
            "nodes": nodes,
            "flows": flows,
            "routing": routing,
            "chaos": self.chaos.state(),
            "traffic": self.traffic.snapshot(),
            "agents": self.agents.alive(),
        }
        for name, ext in self.extensions.items():
            if hasattr(ext, "status"):
                snap.setdefault("jobs", {})[name] = ext.status()
        return sanitize(snap)

    def history_sample(self, now: float) -> dict:
        flows = {}
        for pair, f in self.controller.flows.items():
            pv = self.telemetry.flow_probe_view(pair, now)
            ts = self.traffic.pair_stats(f.src, f.dst, 1.5, now)
            flows[pair] = {
                "rtt_ms": pv.get("rtt_ms"),
                "rtt_p50": pv.get("rtt_p50"),
                "rtt_p95": pv.get("rtt_p95"),
                "rtt_p99": pv.get("rtt_p99"),
                "probe_loss_pct": pv.get("probe_loss_pct"),
                "rx_mbps": ts["rx_mbps"] if ts else None,
                "iperf_loss_pct": ts["loss_pct"] if ts else None,
                "offered_mbps": self.traffic.offered_mbps(f.src, f.dst),
                "path": "-".join(f.path) if f.path else None,
            }
        links = {}
        for lid in self.topo.links:
            v = self.telemetry.link_view(lid, now)
            links[lid] = {"util": v["util"], "rtt_ms": v["rtt_ms"], "loss_pct": v["loss_pct"], "health": v["health"]}
        return sanitize({"t": now, "flows": flows, "links": links})

    def _history_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.history.append(self.history_sample(time.time()))
            except Exception:
                log.exception("history sample failed")

    # ------------------------------------------------------------------ convenience
    def start_default_traffic(self) -> list[dict]:
        out = []
        for t in self.topo.traffic:
            if self.traffic.offered_mbps(t.src, t.dst) == 0:
                out.append(self.traffic.start_flow(t.src, t.dst, t.rate_mbps).to_dict())
        return out
