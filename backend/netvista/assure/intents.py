"""Intents: what the network must guarantee, stated declaratively and checked continuously.

An intent is a typed, validated requirement on flows or links (RFC 9315 calls this
intent-based networking: say *what*, the system works out *how* and keeps checking it):

    reach      the flow must be connected (its probes answer / a path exists)
    latency    the flow's round trip (p50, p95 or p99) <= max_ms
    loss       the flow's loss <= max_pct (iperf3 data loss while it carries traffic, probe loss otherwise)
    bandwidth  at least min_mbps must stay available on the flow's path (headroom for growth)
    avoid      the flow's path must not cross the listed routers or links (policy, compliance)
    waypoint   the flow's path must cross the listed router (e.g. a firewall)
    max_util   the listed links (or every core link, "*") stay at or below max_pct load
    disjoint   two flows share no core link (and, with nodes=true, no transit router)

Every intent also has
    protect    none | link | node | any: it must also hold after any single failure of that
               class, once the routing has reacted (checked by prediction, see resilience.py)
    priority   critical (10) | high (3) | normal (1): weights in the planner's objective

The same check runs on a *live* view (measured every second) and on a *predicted* view
(the fluid model or the packet twin), so "satisfied now" and "would still be satisfied if r2
failed" are answered by one piece of code. Predicted latency checks are risk-aware: with a
prediction interval (conformal.py) a value whose upper bound crosses the limit is "at risk".
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

KINDS = ("reach", "latency", "loss", "bandwidth", "avoid", "waypoint", "max_util", "disjoint")
PROTECT = ("none", "link", "node", "any")
PRIORITY_WEIGHT = {"critical": 10.0, "high": 3.0, "normal": 1.0}
STATS = ("p50", "p95", "p99")


class IntentError(ValueError):
    pass


@dataclass
class Intent:
    id: str
    kind: str
    flows: list[str] = field(default_factory=list)  # pairs "c1>srv1", or ["*"] = every managed flow
    links: list[str] = field(default_factory=list)  # max_util: link ids, or ["*"] = every core link
    params: dict[str, Any] = field(default_factory=dict)
    protect: str = "none"
    priority: str = "normal"
    enabled: bool = True
    label: str = ""
    note: str = ""
    source: str = "user"
    created_t: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def weight(self) -> float:
        return PRIORITY_WEIGHT.get(self.priority, 1.0)

    def covers(self, failure_kind: str) -> bool:
        """Must this intent hold after a failure of this kind ('link' or 'node')?"""
        return self.protect == "any" or self.protect == failure_kind


# ---------------------------------------------------------------------------- validation
def _num(p: dict, key: str, lo: float, hi: float, default: float | None = None) -> float:
    v = p.get(key, default)
    if v is None:
        raise IntentError(f"missing '{key}'")
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise IntentError(f"'{key}' must be a number") from None
    if not lo <= v <= hi:
        raise IntentError(f"'{key}' must be within [{lo:g}, {hi:g}]")
    return v


def _pair_text(p: str) -> str:
    return "every flow" if p == "*" else p.replace(">", " → ")


def describe(it: Intent) -> str:
    flows = ", ".join(_pair_text(p) for p in it.flows) or "–"
    p = it.params
    if it.kind == "reach":
        s = f"{flows} must stay connected"
    elif it.kind == "latency":
        s = f"{flows}: round trip {p['stat']} ≤ {p['max_ms']:g} ms"
    elif it.kind == "loss":
        s = f"{flows}: loss ≤ {p['max_pct']:g}%"
    elif it.kind == "bandwidth":
        s = f"{flows}: ≥ {p['min_mbps']:g} Mbit/s available on the path"
    elif it.kind == "avoid":
        s = f"{flows} must avoid {', '.join(p['elements'])}"
    elif it.kind == "waypoint":
        s = f"{flows} must pass through {p['node']}"
    elif it.kind == "max_util":
        where = "every core link" if it.links == ["*"] else ", ".join(it.links)
        s = f"{where} at or below {p['max_pct']:g}% load"
    else:
        s = f"{' and '.join(_pair_text(x) for x in it.flows)} must not share a {'link or router' if p.get('nodes') else 'link'}"
    if it.protect != "none":
        s += {"link": ", even after any single link failure", "node": ", even after any single router failure",
              "any": ", even after any single link or router failure"}[it.protect]
    return s


def validate_intent(raw: dict[str, Any], topo, pairs: list[str], core_links: list[str]) -> Intent:
    """Normalise and validate an intent spec against the running topology."""
    kind = raw.get("kind")
    if kind not in KINDS:
        raise IntentError(f"kind must be one of {', '.join(KINDS)}")
    params = dict(raw.get("params") or {})
    flows = [str(f) for f in (raw.get("flows") or [])]
    links = [str(l) for l in (raw.get("links") or [])]
    for f in flows:
        if f != "*" and f not in pairs:
            raise IntentError(f"unknown flow {f!r}; flows are {', '.join(pairs)}")
    if kind != "max_util" and not flows:
        raise IntentError("name at least one flow (or * for every flow)")
    protect = raw.get("protect", "none")
    if protect not in PROTECT:
        raise IntentError(f"protect must be one of {', '.join(PROTECT)}")
    priority = raw.get("priority", "normal")
    if priority not in PRIORITY_WEIGHT:
        raise IntentError("priority must be critical, high or normal")

    if kind == "latency":
        stat = params.get("stat", "p95")
        if stat not in STATS:
            raise IntentError("stat must be p50, p95 or p99")
        params = {"stat": stat, "max_ms": _num(params, "max_ms", 0.1, 100000)}
    elif kind == "loss":
        params = {"max_pct": _num(params, "max_pct", 0, 100)}
    elif kind == "bandwidth":
        params = {"min_mbps": _num(params, "min_mbps", 0.01, 100000)}
    elif kind == "avoid":
        els = [str(e) for e in (params.get("elements") or [])]
        if not els:
            raise IntentError("avoid needs at least one router or link")
        for e in els:
            if e not in topo.nodes and e not in topo.links:
                raise IntentError(f"unknown router or link {e!r}")
            if e in topo.nodes and topo.nodes[e].type not in ("router", "switch"):
                raise IntentError(f"{e} is a host; avoid routers, switches or links")
        params = {"elements": els}
    elif kind == "waypoint":
        node = params.get("node")
        if node not in topo.nodes or topo.nodes[node].type != "router":
            raise IntentError("waypoint needs a router id")
        params = {"node": node}
    elif kind == "max_util":
        if not links:
            links = ["*"]
        for l in links:
            if l != "*" and l not in topo.links:
                raise IntentError(f"unknown link {l!r}")
        params = {"max_pct": _num(params, "max_pct", 1, 100)}
        flows = []
    elif kind == "disjoint":
        if len(flows) != 2 or "*" in flows or flows[0] == flows[1]:
            raise IntentError("disjoint needs exactly two different flows")
        params = {"nodes": bool(params.get("nodes", False))}
    else:  # reach
        params = {}
    it = Intent(
        id=str(raw.get("id") or ""), kind=kind, flows=flows, links=links, params=params, protect=protect, priority=priority,
        enabled=bool(raw.get("enabled", True)), note=str(raw.get("note") or "")[:300], source=str(raw.get("source") or "user"),
        created_t=float(raw.get("created_t") or time.time()),
    )
    it.label = describe(it)
    return it


# ---------------------------------------------------------------------------- views
@dataclass
class PairState:
    path: list[str] | None
    alive: bool | None
    rtt: dict[str, float | None]  # p50 / p95 / p99 in ms
    loss_pct: float | None  # data loss (traffic) or probe loss
    loss_source: str  # "data" | "probe"
    offered_mbps: float
    rx_mbps: float | None


@dataclass
class View:
    """A network state, measured or predicted, in the shape every check understands."""

    kind: str  # live | predicted
    pairs: dict[str, PairState]
    link_util: dict[str, float]  # max over both directions, accepted / capacity
    link_up: dict[str, bool]
    hop_rate_bps: dict[tuple[str, str], float]  # directed wire rate
    hop_cap_bps: dict[tuple[str, str], float]
    rtt_margin: float = 0.0  # relative half-width of the latency prediction interval (0 = point estimate)
    wire_factor: float = 1242 / 1200  # wire bytes per payload byte of a data packet


def edge_routers(topo) -> set[str]:
    """Routers that are a LAN gateway (wired to a switch or a host)."""
    return {r for r in topo.routers if any(topo.nodes[n].type != "router" for n in topo.neighbors(r))}


def core_links_of(path: list[str] | None, topo) -> list[str]:
    if not path:
        return []
    out = []
    for u, v in zip(path, path[1:]):
        if topo.nodes[u].type == "router" and topo.nodes[v].type == "router":
            l = topo.link_between(u, v)
            if l:
                out.append(l.id)
    return out


def path_links(path: list[str] | None, topo) -> list[str]:
    if not path:
        return []
    return [l.id for u, v in zip(path, path[1:]) if (l := topo.link_between(u, v)) is not None]


# ---------------------------------------------------------------------------- evaluation
@dataclass
class Check:
    subject: str  # pair, link or "pairA & pairB"
    ok: bool | None  # None = no data
    value: float | str | None
    limit: float | str | None
    unit: str
    detail: str = ""
    at_risk: bool = False  # satisfied by the point estimate, but not by its upper bound
    severity: float = 0.0  # 0 when ok; 1 + relative excess otherwise (guides the planner's search)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("value", "limit"):
            if isinstance(d[k], float):
                d[k] = round(d[k], 3)
        d["severity"] = round(d["severity"], 4)
        return d


@dataclass
class IntentResult:
    intent: str
    status: str  # ok | violated | at_risk | unknown
    checks: list[Check]

    @property
    def severity(self) -> float:
        return sum(c.severity for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "status": self.status, "severity": round(self.severity, 4),
                "checks": [c.to_dict() for c in self.checks]}


def _excess(value: float, limit: float, upper: bool = True) -> float:
    """Severity of a violated numeric limit: 1 + the relative excess, capped."""
    if limit == 0:
        return 1.0 + min(5.0, abs(value))
    rel = (value - limit) / abs(limit) if upper else (limit - value) / abs(limit)
    return 1.0 + min(5.0, max(0.0, rel))


def expand_flows(it: Intent, pairs: list[str]) -> list[str]:
    return list(pairs) if "*" in it.flows else [f for f in it.flows if f in pairs]


def evaluate_intent(it: Intent, view: View, topo, core_links: list[str]) -> IntentResult:
    pairs = list(view.pairs)
    checks: list[Check] = []
    p = it.params
    if it.kind == "max_util":
        targets = core_links if "*" in it.links else [l for l in it.links if l in view.link_util]
        lim = p["max_pct"]
        for lid in targets:
            if not view.link_up.get(lid, True):
                checks.append(Check(lid, True, None, lim, "%", "link is down (carries nothing)"))
                continue
            u = view.link_util.get(lid)
            if u is None:
                checks.append(Check(lid, None, None, lim, "%"))
                continue
            val = 100.0 * u
            ok = val <= lim
            checks.append(Check(lid, ok, val, lim, "%", severity=0.0 if ok else _excess(val, lim)))
    elif it.kind == "disjoint":
        a, b = it.flows
        pa, pb = view.pairs.get(a), view.pairs.get(b)
        if not pa or not pb or not pa.path or not pb.path:
            checks.append(Check(f"{a} & {b}", None, None, None, ""))
        else:
            la, lb = set(core_links_of(pa.path, topo)), set(core_links_of(pb.path, topo))
            shared = sorted(la & lb)
            if p.get("nodes"):
                ra = {n for n in pa.path if topo.nodes[n].type == "router"}
                rb = {n for n in pb.path if topo.nodes[n].type == "router"}
                # edge routers (site gateways) are necessarily shared by flows from the same site
                shared += sorted((ra & rb) - edge_routers(topo))
            ok = not shared
            checks.append(Check(f"{a} & {b}", ok, ", ".join(shared) or "none", "none", "shared",
                                severity=0.0 if ok else 1.0 + 0.2 * len(shared)))
    else:
        for pair in expand_flows(it, pairs):
            st = view.pairs[pair]
            checks.append(_pair_check(it, st, pair, view, topo))
    status = _status(checks)
    return IntentResult(it.id, status, checks)


def _pair_check(it: Intent, st: PairState, pair: str, view: View, topo) -> Check:
    p = it.params
    connected = st.alive is not False and st.path is not None
    if it.kind == "reach":
        if st.alive is None:
            return Check(pair, None, None, None, "")
        return Check(pair, bool(connected), "connected" if connected else "no path", "connected", "",
                     severity=0.0 if connected else 2.0)
    if it.kind in ("avoid", "waypoint"):
        if not st.path:
            return Check(pair, None if st.alive is None else False, "no path", None, "", severity=0.0 if st.alive is None else 2.0)
        crossed = set(st.path) | set(path_links(st.path, topo))
        if it.kind == "avoid":
            hit = [e for e in p["elements"] if e in crossed]
            ok = not hit
            return Check(pair, ok, ", ".join(hit) or "none", "none of " + ", ".join(p["elements"]), "crossed",
                         detail="-".join(n for n in st.path if topo.nodes[n].type == "router"), severity=0.0 if ok else 1.0 + 0.5 * len(hit))
        ok = p["node"] in st.path
        return Check(pair, ok, "yes" if ok else "no", p["node"], "via", detail="-".join(n for n in st.path if topo.nodes[n].type == "router"),
                     severity=0.0 if ok else 1.0)
    if not connected:
        if st.alive is None and st.path is None:
            return Check(pair, None, None, None, "")
        lim = p.get("max_ms") or p.get("max_pct") or p.get("min_mbps")
        return Check(pair, False, "disconnected", lim, "", severity=3.0)
    if it.kind == "latency":
        v = st.rtt.get(p["stat"])
        lim = p["max_ms"]
        if v is None:
            # connected but no RTT: every echo is later than the probe timeout, or no data yet
            if view.kind == "predicted" or (st.loss_pct or 0) >= 99:
                return Check(pair, False, "no echo", lim, "ms", severity=3.0)
            return Check(pair, None, None, lim, "ms")
        ok = v <= lim
        upper = v * (1.0 + view.rtt_margin)
        risk = ok and upper > lim
        return Check(pair, ok, v, lim, "ms", detail=f"RTT {p['stat']}" + (f", upper bound {upper:.2f} ms" if view.rtt_margin else ""),
                     at_risk=risk, severity=0.0 if ok else _excess(v, lim))
    if it.kind == "loss":
        v, lim = st.loss_pct, p["max_pct"]
        if v is None:
            return Check(pair, None, None, lim, "%")
        ok = v <= lim + 1e-9
        return Check(pair, ok, v, lim, "%", detail=f"{st.loss_source} loss", severity=0.0 if ok else 1.0 + min(5.0, (v - lim) / max(lim, 1.0)))
    if it.kind == "bandwidth":
        lim = p["min_mbps"]
        avail = available_mbps(st, view, topo)
        if avail is None:
            return Check(pair, None, None, lim, "Mbit/s")
        ok = avail >= lim
        return Check(pair, ok, avail, lim, "Mbit/s", detail="spare capacity at the path's bottleneck",
                     severity=0.0 if ok else _excess(avail, lim, upper=False))
    return Check(pair, None, None, None, "")


def available_mbps(st: PairState, view: View, topo) -> float | None:
    """Spare capacity at the bottleneck of the flow's path, not counting the flow itself."""
    if not st.path:
        return None
    own = st.offered_mbps * 1e6 * view.wire_factor
    best = None
    for u, v in zip(st.path, st.path[1:]):
        cap = view.hop_cap_bps.get((u, v))
        if cap is None:
            continue
        rate = view.hop_rate_bps.get((u, v), 0.0)
        spare = cap - max(0.0, rate - own)
        best = spare if best is None else min(best, spare)
    return None if best is None else max(0.0, best) / 1e6


def _status(checks: list[Check]) -> str:
    if any(c.ok is False for c in checks):
        return "violated"
    if not checks or all(c.ok is None for c in checks):
        return "unknown"
    if any(c.at_risk for c in checks):
        return "at_risk"
    return "ok"


def evaluate_all(intents: list[Intent], view: View, topo, core_links: list[str], failure_kind: str | None = None) -> list[IntentResult]:
    """Check every enabled intent; with failure_kind, only those that must hold under that failure."""
    out = []
    for it in intents:
        if not it.enabled:
            continue
        if failure_kind is not None and not it.covers(failure_kind):
            continue
        out.append(evaluate_intent(it, view, topo, core_links))
    return out


def weighted_violation(results: list[IntentResult], intents: dict[str, Intent], risk_weight: float = 0.25) -> float:
    total = 0.0
    for r in results:
        w = intents[r.intent].weight
        total += w * r.severity
        if risk_weight:
            total += w * risk_weight * sum(1 for c in r.checks if c.at_risk)
    return total


# ---------------------------------------------------------------------------- store + compliance
class IntentStore:
    """The intent set (persisted to runs/intents.json) and its live compliance history."""

    HISTORY_S = 3600

    def __init__(self, path, topo, pairs: list[str], core_links: list[str]) -> None:
        self.path = path
        self.topo = topo
        self.pairs = pairs
        self.core_links = core_links
        self.items: dict[str, Intent] = {}
        self._ids = itertools.count(1)
        self.lock = threading.RLock()
        self.version = 0
        # per intent: deque of (t, status) at 1 Hz, and debounced alert state
        self.history: dict[str, deque] = {}
        self.alert: dict[str, dict[str, Any]] = {}
        self.latest: dict[str, IntentResult] = {}
        self._load()

    # ---------------------------------------------------------------- persistence
    def _load(self) -> None:
        import json

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("topology") != self.topo.name:
            return  # intents name flows and links of one topology
        for raw in data.get("intents", []):
            try:
                it = validate_intent(raw, self.topo, self.pairs, self.core_links)
            except IntentError:
                continue
            self.items[it.id] = it
        nums = [int(i[1:]) for i in self.items if i[1:].isdigit()]
        self._ids = itertools.count(max(nums, default=0) + 1)
        self.version += 1

    def _save(self) -> None:
        import json

        try:
            self.path.write_text(json.dumps({"topology": self.topo.name, "intents": [i.to_dict() for i in self.items.values()]}, indent=1))
        except OSError:
            pass

    # ---------------------------------------------------------------- CRUD
    def list(self) -> list[Intent]:
        with self.lock:
            return list(self.items.values())

    def add(self, raw: dict[str, Any], source: str = "user") -> Intent:
        with self.lock:
            raw = dict(raw)
            raw["id"] = f"I{next(self._ids)}"
            raw["source"] = source
            it = validate_intent(raw, self.topo, self.pairs, self.core_links)
            self.items[it.id] = it
            self.version += 1
            self._save()
            return it

    def update(self, iid: str, raw: dict[str, Any]) -> Intent:
        with self.lock:
            old = self.items.get(iid)
            if old is None:
                raise KeyError(f"unknown intent {iid}")
            merged = {**old.to_dict(), **raw, "id": iid, "created_t": old.created_t, "source": old.source}
            it = validate_intent(merged, self.topo, self.pairs, self.core_links)
            self.items[iid] = it
            self.history.pop(iid, None)
            self.alert.pop(iid, None)
            self.version += 1
            self._save()
            return it

    def remove(self, iid: str) -> None:
        with self.lock:
            if self.items.pop(iid, None) is None:
                raise KeyError(f"unknown intent {iid}")
            self.history.pop(iid, None)
            self.alert.pop(iid, None)
            self.latest.pop(iid, None)
            self.version += 1
            self._save()

    def replace_all(self, raws: list[dict[str, Any]], source: str = "preset") -> list[Intent]:
        with self.lock:
            fresh = []
            for raw in raws:
                raw = dict(raw)
                raw["id"] = "tmp"
                fresh.append(validate_intent(raw, self.topo, self.pairs, self.core_links))
            self.items.clear()
            self.history.clear()
            self.alert.clear()
            self.latest.clear()
            self._ids = itertools.count(1)
            for it in fresh:
                it.id = f"I{next(self._ids)}"
                it.source = source
                self.items[it.id] = it
            self.version += 1
            self._save()
            return list(self.items.values())

    # ---------------------------------------------------------------- compliance
    def record(self, now: float, results: list[IntentResult], emit) -> None:
        """Store this second's live results; raise/clear debounced alerts (2 s on, 3 s off)."""
        with self.lock:
            for r in results:
                it = self.items.get(r.intent)
                if it is None:
                    continue
                self.latest[r.intent] = r
                h = self.history.setdefault(r.intent, deque(maxlen=self.HISTORY_S))
                h.append((now, r.status))
                a = self.alert.setdefault(r.intent, {"violated": False, "since": None, "bad": 0, "good": 0})
                if r.status == "violated":
                    a["bad"] += 1
                    a["good"] = 0
                    if not a["violated"] and a["bad"] >= 2:
                        a["violated"], a["since"] = True, now - 1
                        worst = max((c for c in r.checks if c.ok is False), key=lambda c: c.severity, default=None)
                        what = ""
                        if worst is not None:
                            what = f": {worst.subject.replace('>', ' → ')} {_fmt(worst.value, worst.unit)}"
                            if worst.limit is not None and worst.unit not in ("", "crossed", "via", "shared"):
                                what += f" vs {_fmt(worst.limit, worst.unit)}"
                        emit("intent.violated", f"Intent {it.id} violated ({it.label}){what}", "error", intent=it.id)
                elif r.status in ("ok", "at_risk"):
                    a["good"] += 1
                    a["bad"] = 0
                    if a["violated"] and a["good"] >= 3:
                        dur = now - (a["since"] or now)
                        a["violated"], a["since"] = False, None
                        emit("intent.restored", f"Intent {it.id} satisfied again after {dur:.0f} s ({it.label})", "success", intent=it.id)

    def compliance(self, iid: str, now: float, window_s: float) -> dict[str, Any]:
        h = [s for t, s in list(self.history.get(iid, ())) if t >= now - window_s]
        known = [s for s in h if s != "unknown"]
        bad = sum(1 for s in known if s == "violated")
        return {
            "window_s": window_s,
            "samples": len(known),
            "violated_s": bad,
            "compliance_pct": None if not known else round(100.0 * (1 - bad / len(known)), 2),
        }

    def timeline(self, iid: str, now: float, window_s: float = 300) -> list[tuple[float, str]]:
        return [(round(t, 1), s) for t, s in list(self.history.get(iid, ())) if t >= now - window_s]


def _fmt(v: Any, unit: str) -> str:
    if isinstance(v, float):
        if unit == "ms":
            return f"{v:.1f} ms"
        if unit == "%":
            return f"{v:.1f}%"
        if unit == "Mbit/s":
            return f"{v:.1f} Mbit/s"
        return f"{v:.2f}"
    return str(v)
