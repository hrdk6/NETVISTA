"""Chaos lab: inject faults into the REAL emulated network and revert them.

Every injection maps to a concrete Linux action:
    link_latency    tc netem delay   (base + add_ms, optional jitter)   on both link ends
    link_loss       tc netem loss %                                       on both link ends
    link_bandwidth  tc htb rate                                           on both link ends
    link_down       ip link set <both ends> down
    node_down       ip link set <every interface of the node> down        (router "crash")
    traffic_burst   an extra iperf3 UDP flow for N seconds

Link parameters are composed as base (from the topology JSON) + at most one active override
per field, so reverting one fault never disturbs another (e.g. latency and loss on one link).
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..emulation.network import EmulatedNetwork
from ..emulation.tc import LinkParams
from ..emulation.traffic import TrafficManager
from ..events import EventBus
from ..topology import Topology

KINDS = ("link_latency", "link_loss", "link_bandwidth", "link_down", "node_down", "traffic_burst")
FIELD_OF = {"link_latency": "delay", "link_loss": "loss_pct", "link_bandwidth": "bw_mbps"}


class ChaosError(ValueError):
    pass


@dataclass
class Injection:
    id: str
    kind: str
    target: str
    params: dict[str, Any]
    t: float
    label: str
    state: str = "active"  # active | reverted | superseded | expired
    t_end: float | None = None
    flow_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "target": self.target, "params": self.params,
            "t": self.t, "label": self.label, "state": self.state, "t_end": self.t_end,
        }


def validate_change(topo: Topology, kind: str, target: str | None, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Normalise + validate a change spec (shared by chaos, simulator what-ifs and scenarios)."""
    p = dict(params or {})

    def num(key: str, lo: float, hi: float, default: float | None = None) -> float:
        v = p.get(key, default)
        if v is None:
            raise ChaosError(f"{kind}: missing '{key}'")
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ChaosError(f"{kind}: '{key}' must be a number") from None
        if not lo <= v <= hi:
            raise ChaosError(f"{kind}: '{key}' must be within [{lo}, {hi}]")
        return v

    if kind not in KINDS:
        raise ChaosError(f"unknown change kind {kind!r}")
    if kind.startswith("link_"):
        if target not in topo.links:
            raise ChaosError(f"unknown link {target!r}")
        if kind == "link_latency":
            p = {"add_ms": num("add_ms", 0, 500), "jitter_ms": num("jitter_ms", 0, 200, 0.0)}
        elif kind == "link_loss":
            p = {"loss_pct": num("loss_pct", 0, 100)}
        elif kind == "link_bandwidth":
            p = {"bw_mbps": num("bw_mbps", 0.5, 1000)}
        else:
            p = {}
        return target, p
    if kind == "node_down":
        if target not in topo.nodes or topo.nodes[target].type not in ("router", "switch"):
            raise ChaosError("node_down target must be a router or switch")
        return target, {}
    # traffic_burst
    src, dst = p.get("src"), p.get("dst")
    if (not src or not dst) and target and ">" in target:
        src, dst = target.split(">", 1)
    if src not in topo.hosts or dst not in topo.hosts or src == dst:
        raise ChaosError("traffic_burst needs src and dst hosts")
    return f"{src}>{dst}", {
        "src": src, "dst": dst,
        "rate_mbps": num("rate_mbps", 0.1, 500),
        "duration_s": num("duration_s", 1, 600, 15.0),
    }


def change_label(kind: str, target: str, p: dict[str, Any]) -> str:
    if kind == "link_latency":
        j = f" ±{p['jitter_ms']:g} ms jitter" if p.get("jitter_ms") else ""
        return f"Latency +{p['add_ms']:g} ms{j} on {target}"
    if kind == "link_loss":
        return f"Packet loss {p['loss_pct']:g}% on {target}"
    if kind == "link_bandwidth":
        return f"Bandwidth cap {p['bw_mbps']:g} Mbit/s on {target}"
    if kind == "link_down":
        return f"Link {target} DOWN"
    if kind == "node_down":
        return f"Node {target} DOWN"
    return f"Traffic burst {p['src']}→{p['dst']} {p['rate_mbps']:g} Mbit/s for {p['duration_s']:g}s"


class ChaosEngine:
    def __init__(self, topo: Topology, net: EmulatedNetwork, traffic: TrafficManager, events: EventBus) -> None:
        self.topo = topo
        self.net = net
        self.traffic = traffic
        self.events = events
        self.base = {lid: LinkParams(**l.params()) for lid, l in topo.links.items()}
        self.overrides: dict[str, dict[str, tuple[str, Any]]] = {lid: {} for lid in topo.links}
        self.link_down: dict[str, str] = {}
        self.node_down: dict[str, str] = {}
        self.injections: dict[str, Injection] = {}
        self._ids = itertools.count(1)
        self.lock = threading.RLock()
        self._admin: dict[str, bool] = {lid: True for lid in topo.links}
        self._intf_admin: dict[tuple[str, str], bool] = {(l.id, n): True for l in topo.links.values() for n in (l.a, l.b)}
        self.on_change: list[Callable[[Injection, str], None]] = []
        self._stop = threading.Event()
        threading.Thread(target=self._expire_loop, name="chaos-expire", daemon=True).start()

    # ------------------------------------------------------------------ state
    def init_network(self) -> None:
        for lid in self.topo.links:
            self.net.apply_link_params(lid, self.base[lid])

    def effective_params(self, lid: str) -> LinkParams:
        b = self.base[lid]
        o = self.overrides[lid]
        delay, jitter = b.delay_ms, b.jitter_ms
        if "delay" in o:
            add = o["delay"][1]
            delay, jitter = b.delay_ms + add["add_ms"], add["jitter_ms"] or b.jitter_ms
        return LinkParams(
            bw_mbps=o["bw_mbps"][1] if "bw_mbps" in o else b.bw_mbps,
            delay_ms=delay,
            jitter_ms=jitter,
            loss_pct=o["loss_pct"][1] if "loss_pct" in o else b.loss_pct,
            queue_pkts=b.queue_pkts,
        )

    def admin_up(self, lid: str) -> bool:
        return self._admin[lid]

    def _sync_admin(self) -> None:
        """Bring every interface to the state implied by active link_down/node_down faults.

        link_down: both ends go down (cable pulled). node_down: only the crashed node's own
        interfaces go down - its neighbours merely lose carrier and keep forwarding into the
        void until the controller notices, exactly like a real router dying.
        """
        for lid, link in self.topo.links.items():
            changed_up = False
            for n in (link.a, link.b):
                want = lid not in self.link_down and n not in self.node_down
                if self._intf_admin[(lid, n)] != want:
                    self.net.set_intf_admin(n, [self.net.plan.intf(lid, n).name], want)
                    self._intf_admin[(lid, n)] = want
                    changed_up |= want
            if changed_up:
                self.net.apply_link_params(lid, self.effective_params(lid))
            self._admin[lid] = all(self._intf_admin[(lid, n)] for n in (link.a, link.b))

    # ------------------------------------------------------------------ actions
    def inject(self, kind: str, target: str | None, params: dict[str, Any] | None = None, source: str = "user") -> Injection:
        target, p = validate_change(self.topo, kind, target, params or {})
        with self.lock:
            inj = Injection(id=f"i{next(self._ids)}", kind=kind, target=target, params=p, t=time.time(), label=change_label(kind, target, p))
            if kind == "link_down" and target in self.link_down:
                raise ChaosError(f"link {target} is already down")
            if kind == "node_down" and target in self.node_down:
                raise ChaosError(f"node {target} is already down")
            try:
                if kind in FIELD_OF:
                    fld = FIELD_OF[kind]
                    prev = self.overrides[target].get(fld)
                    self.overrides[target][fld] = (inj.id, p if fld == "delay" else next(iter(p.values())))
                    try:
                        self.net.apply_link_params(target, self.effective_params(target))
                    except Exception:
                        if prev:
                            self.overrides[target][fld] = prev
                        else:
                            del self.overrides[target][fld]
                        raise
                    if prev:
                        old = self.injections[prev[0]]
                        old.state, old.t_end = "superseded", inj.t
                elif kind == "link_down":
                    self.link_down[target] = inj.id
                    self._sync_admin()
                elif kind == "node_down":
                    self.node_down[target] = inj.id
                    self._sync_admin()
                else:
                    flow = self.traffic.start_flow(p["src"], p["dst"], p["rate_mbps"], kind="burst", duration_s=p["duration_s"])
                    inj.flow_id = flow.id
            except ChaosError:
                raise
            except Exception as e:
                raise ChaosError(f"could not apply '{inj.label}': {e}") from e
            self.injections[inj.id] = inj
        self.events.emit("chaos.inject", inj.label, severity="warn", injection=inj.to_dict(), source=source, t=inj.t)
        for fn in self.on_change:
            fn(inj, "inject")
        return inj

    def revert(self, inj_id: str, source: str = "user") -> Injection:
        with self.lock:
            inj = self.injections.get(inj_id)
            if inj is None:
                raise ChaosError(f"unknown injection {inj_id}")
            if inj.state != "active":
                return inj
            # bookkeeping first, then the network: even if a command fails the fault is no
            # longer "wanted", and the next sync / reconcile will retry the real state
            try:
                if inj.kind in FIELD_OF:
                    fld = FIELD_OF[inj.kind]
                    cur = self.overrides[inj.target].get(fld)
                    if cur and cur[0] == inj.id:
                        del self.overrides[inj.target][fld]
                        self.net.apply_link_params(inj.target, self.effective_params(inj.target))
                elif inj.kind == "link_down":
                    self.link_down.pop(inj.target, None)
                    self._sync_admin()
                elif inj.kind == "node_down":
                    self.node_down.pop(inj.target, None)
                    self._sync_admin()
                elif inj.flow_id:
                    self.traffic.stop_flow(inj.flow_id)
            except Exception as e:
                self.events.emit("chaos.error", f"Revert of '{inj.label}' hit an error: {e}", severity="error")
            finally:
                inj.state, inj.t_end = "reverted", time.time()
        self.events.emit("chaos.revert", f"Reverted: {inj.label}", severity="info", injection=inj.to_dict(), source=source)
        for fn in self.on_change:
            fn(inj, "revert")
        return inj

    def revert_all(self, source: str = "user") -> int:
        n = 0
        for inj in list(self.injections.values()):
            if inj.state == "active":
                self.revert(inj.id, source=source)
                n += 1
        return n

    def _expire_loop(self) -> None:
        while not self._stop.wait(0.5):
            for inj in list(self.injections.values()):
                if inj.state == "active" and inj.kind == "traffic_burst" and inj.flow_id:
                    f = self.traffic.flows.get(inj.flow_id)
                    if f and f.state not in ("starting", "running"):
                        inj.state, inj.t_end = "expired", time.time()
                        self.events.emit("chaos.expire", f"Finished: {inj.label}", injection=inj.to_dict())

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ queries
    def active(self) -> list[Injection]:
        return [i for i in self.injections.values() if i.state == "active"]

    def find_cause(self, link_id: str, t: float, kinds: tuple[str, ...]) -> Injection | None:
        """Most recent injection (still active, or ended after t-1s) of `kinds` that affects link_id."""
        link = self.topo.links[link_id]
        best = None
        for inj in self.injections.values():
            if inj.kind not in kinds or inj.t > t:
                continue
            if inj.state != "active" and (inj.t_end or 0) < t - 1.0:
                continue
            hit = (inj.kind.startswith("link_") and inj.target == link_id) or (
                inj.kind == "node_down" and inj.target in (link.a, link.b)
            )
            if inj.kind == "traffic_burst":
                hit = True  # bursts affect whatever links their flow crosses
            if hit and (best is None or inj.t > best.t):
                best = inj
        return best

    def state(self) -> dict:
        with self.lock:
            return {
                "active": [i.to_dict() for i in self.active()],
                "history": [i.to_dict() for i in list(self.injections.values())[-50:]],
                "links": {lid: {"params": self.effective_params(lid).to_dict(), "admin_up": self._admin[lid]} for lid in self.topo.links},
                "nodes_down": list(self.node_down),
            }
