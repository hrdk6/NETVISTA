"""Root-cause analysis by probe-path tomography.

Every probe stream crosses a known set of network elements (links and routers/switches):
    L:r2-r5     {link:r2-r5, node:r2, node:r5}
    G:c1        {link:c1-sw1, node:sw1, link:sw1-r1, node:r1}
    F:c1>srv1   every element on the flow's current path
An element that is broken makes every stream through it look bad, and leaves every stream
that avoids it alone. So (Boolean network tomography):

  1. observations   each stream is bad / good / unknown for each symptom class
                      dead     no echo for the dead interval              (probe liveness)
                      latency  RTT anomaly from the learned baseline       (detector)
                      loss     probe-loss anomaly                          (detector)
                      congestion  a link at >= 85 % of its shaped rate     (counters)
  2. candidates     every element some bad observation crosses
                      explains     = bad observations through the element
                      contradicts  = clearly-good observations of the same class through it
  3. greedy cover   repeatedly pick the element with the most newly explained observations
                    minus its contradictions, until everything bad is explained. One crashed
                    router (explains 4 dead streams) beats four separate link failures; a
                    single dead link beats its routers (their other links still echo).
  4. classify       dead -> link/node down; a loaded link -> congestion (traffic surge when a
                    burst flow crosses it); else loss -> packet loss; else latency -> added delay.

Inputs are measurements only. The chaos lab's fault list is never read, so the evaluation
suite can score the diagnosis against it honestly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CONGESTED_UTIL = 0.85
GOOD_Z = 1.0  # a stream counts as clearly normal (able to contradict) below this z
# Loss is random, latency is not. A window of ~100 probes with zero losses is still 13 % likely
# at 2 % loss, so a loss-free stream only contradicts a loss hypothesis once the loss seen on
# the bad streams is high enough that zero losses would be implausible: (1 - 0.05)^100 < 1 %.
LOSS_CONTRADICTS_FROM_PCT = 5.0


@dataclass
class SignalState:
    state: str = "learning"  # learning | normal | anomalous
    value: float | None = None
    mean: float | None = None
    z: float | None = None
    since: float | None = None


@dataclass
class StreamObs:
    sid: str
    label: str
    elements: frozenset[str]
    alive: bool | None
    silent_s: float | None = None
    rtt: SignalState = field(default_factory=SignalState)
    loss: SignalState = field(default_factory=SignalState)
    flow: str | None = None
    rerouted: bool = False  # flow stream now on a different path than the one its baseline learned
    # elements the probes in the loss window crossed (old + new path right after a reroute);
    # liveness is about now, so it always uses `elements`
    window_elements: frozenset[str] | None = None


@dataclass
class LinkLoad:
    util: float
    cap_mbps: float
    load_mbps: float
    drops_ps: float = 0.0


@dataclass
class FlowInfo:
    pair: str
    path: list[str] | None
    elements: frozenset[str]
    baseline_path: list[str] | None
    baseline_elements: frozenset[str]
    offered_mbps: float = 0.0


@dataclass
class Obs:
    id: str
    kind: str  # dead | latency | loss | congestion
    elements: frozenset[str]
    bad: bool
    evidence: str
    direct_of: str | None  # element whose own probe this is (L:/G: streams), for confidence
    since: float | None = None
    flow: str | None = None
    value: float | None = None


def _fmt(v: float | None, nd: int = 2) -> str:
    return "n/a" if v is None else f"{v:.{nd}f}"


def observations(streams: list[StreamObs], loads: dict[str, LinkLoad], now: float) -> tuple[list[Obs], list[str]]:
    """Turn measurements into bad/good observations, plus 'consequence' notes (reroute side effects)."""
    obs: list[Obs] = []
    moved: list[str] = []
    for s in streams:
        direct = None
        if s.sid.startswith("L:"):
            direct = f"link:{s.sid[2:]}"
        if s.alive is not None:
            obs.append(Obs(
                f"dead:{s.sid}", "dead", s.elements, bad=not s.alive,
                evidence=(f"{s.label}: no echo for {_fmt(s.silent_s, 1)} s" if not s.alive else f"{s.label}: echoing"),
                direct_of=direct, since=(now - s.silent_s) if (not s.alive and s.silent_s is not None) else None, flow=s.flow,
            ))
        if s.alive is False:
            continue  # a dead path says nothing about its latency or loss
        for kind, st, unit in (("latency", s.rtt, "ms"), ("loss", s.loss, "%")):
            if st.state == "anomalous":
                if kind == "latency" and s.rerouted:
                    moved.append(f"{(s.flow or s.sid).replace('>', ' → ')} {_fmt(st.value, 1)} ms (normally {_fmt(st.mean, 1)} ms)")
                    continue
                what = "data loss" if s.sid.startswith("D:") else "probe loss"
                ev = (f"{s.label}: {_fmt(st.value)} ms, normally {_fmt(st.mean)} ms" if kind == "latency"
                      else f"{s.label}: {_fmt(st.value, 1)}% {what}, normally {_fmt(st.mean, 1)}%")
                obs.append(Obs(f"{kind}:{s.sid}", kind, s.window_elements or s.elements, True, ev, direct, st.since, s.flow, st.value))
            elif st.state == "normal" and st.z is not None and st.z < GOOD_Z and not (kind == "latency" and s.rerouted):
                obs.append(Obs(f"{kind}:{s.sid}", kind, s.elements, False, f"{s.label}: normal", direct, None, s.flow))
    for lid, ld in loads.items():
        if ld.util >= CONGESTED_UTIL:
            obs.append(Obs(
                f"congestion:{lid}", "congestion", frozenset({f"link:{lid}"}), True,
                f"{lid}: carrying {_fmt(ld.load_mbps, 1)} Mbit/s, {ld.util * 100:.0f}% of its {ld.cap_mbps:g} Mbit/s"
                + (f", dropping {ld.drops_ps:.0f} packets/s" if ld.drops_ps >= 1 else ""),
                f"link:{lid}",
            ))
    consequences = []
    if moved:
        consequences.append(
            ("Rerouted flow " if len(moved) == 1 else "Rerouted flows ") + ", ".join(moved)
            + (" runs" if len(moved) == 1 else " run") + " slower than normal on the longer path: an expected side effect, not a separate fault."
        )
    return obs, consequences


def _label(element: str) -> str:
    kind, name = element.split(":", 1)
    return name


def diagnose(
    streams: list[StreamObs],
    loads: dict[str, LinkLoad],
    flows: dict[str, FlowInfo],
    bursts: list[dict[str, Any]],
    node_types: dict[str, str],
    now: float,
) -> dict[str, Any]:
    obs, consequences = observations(streams, loads, now)
    bad = [o for o in obs if o.bad]
    good = [o for o in obs if not o.bad]
    if not bad:
        return {"t": now, "status": "normal", "causes": [], "consequences": consequences, "unexplained": [],
                "observed": {"bad": 0, "good": len(good)}}

    candidates = sorted({e for o in bad for e in o.elements})
    explains = {e: {o.id for o in bad if e in o.elements} for e in candidates}

    def contradictions(e: str, kinds: set[str]) -> tuple[list[Obs], int]:
        """(strong contradictions, weak ones). Weak = loss-free streams through e while the loss
        seen is too low for "no loss in this window" to be real evidence; they only break ties."""
        weak = 0
        ld = loads.get(e[5:]) if e.startswith("link:") else None
        if ld is not None and ld.util >= CONGESTED_UTIL:
            kinds = kinds - {"dead"}  # a full queue drops some probes, not all: echoes don't clear it
        if "loss" in kinds:
            seen = max((o.value or 0.0 for o in bad if o.kind == "loss" and e in o.elements), default=0.0)
            if seen < LOSS_CONTRADICTS_FROM_PCT:
                kinds = kinds - {"loss"}
                weak = sum(1 for o in good if e in o.elements and o.kind == "loss")
        return [o for o in good if e in o.elements and o.kind in kinds], weak

    by_id = {o.id: o for o in bad}
    uncovered = set(by_id)
    chosen: list[tuple[str, set[str], list[Obs], list[str]]] = []
    while uncovered:
        best = None
        scored = {}
        for e in candidates:
            new = explains[e] & uncovered
            if not new:
                continue
            kinds = {by_id[i].kind for i in explains[e]}
            con, weak = contradictions(e, kinds - {"congestion"})
            score = len(new) - len(con)
            direct = any(by_id[i].direct_of == e for i in new)
            key = (score, -len(con), -weak, len(explains[e]), direct, e.startswith("link:"))
            scored[e] = (new, len(con), weak)
            if best is None or key > best[0]:
                best = (key, e, new, con)
        if best is None or best[0][0] <= 0:
            break
        _, e, new, con = best
        # equally good alternatives: same newly explained set, same strong and weak contradictions
        alts = [c for c, (n2, c2, w2) in scored.items() if c != e and n2 == new and c2 == len(con) and w2 == scored[e][2]]
        chosen.append((e, explains[e], con, alts))
        uncovered -= explains[e]

    causes = []
    for e, ids, con, alts in chosen:
        exp = [by_id[i] for i in sorted(ids)]
        kinds = {o.kind for o in exp}
        kind, name = e.split(":", 1)
        ntype = node_types.get(name, "node") if kind == "node" else "link"
        surge = None
        if kind == "link":
            ld = loads.get(name)
            # a link whose counters show it sending at capacity is up, whatever the probes say:
            # silent probes on it were lost in its full queue (bufferbloat), not to a cut
            congested = "congestion" in kinds or bool(ld and ld.util >= CONGESTED_UTIL)
            if "dead" in kinds and not congested:
                ctype, title = "link_down", f"Link {name} is down"
            elif congested:
                ctype, title = "congestion", f"Link {name} is congested"
                for b in bursts:
                    fl = flows.get(b["pair"])
                    if fl and e in fl.elements:
                        surge = b
                        ctype, title = "traffic_surge", f"Traffic surge from {b['pair'].replace('>', ' → ')} is congesting {name}"
                        break
            elif "loss" in kinds:
                ctype, title = "packet_loss", f"Link {name} is dropping packets"
            else:
                ctype, title = "latency", f"Link {name} has extra latency"
        else:
            if "dead" in kinds:
                ctype, title = "node_down", f"{ntype.capitalize()} {name} is down"
            else:
                ctype, title = "node_degraded", f"{ntype.capitalize()} {name} is degraded"
        direct = any(o.direct_of == e for o in exp)
        if con:
            confidence = "low"
        elif alts:
            confidence = "medium"  # the probes cannot tell these elements apart
        elif len(exp) >= 2 or direct:
            confidence = "high"
        else:
            confidence = "medium"
        affected = sorted(p for p, f in flows.items() if e in f.elements or e in f.baseline_elements)
        rerouted = [
            {"pair": p, "from": f.baseline_path, "to": f.path}
            for p, f in flows.items()
            if e in f.baseline_elements and e not in f.elements and f.path
        ]
        since = [o.since for o in exp if o.since is not None]
        causes.append({
            "id": f"{ctype}:{name}",
            "type": ctype,
            "element": name,
            "element_kind": "link" if kind == "link" else ntype,
            "title": title,
            "confidence": confidence,
            "since": min(since) if since else None,
            "evidence": [o.evidence for o in exp],
            "explains": len(exp),
            "contradicted_by": [o.evidence for o in con],
            "alternatives": [_label(a) for a in alts],
            "affected_flows": affected,
            "rerouted_flows": rerouted,
            "surge": surge,
        })
    unexplained = [by_id[i].evidence for i in sorted(uncovered)]
    # most trustworthy first: the map pins the first cause
    rank = {"high": 0, "medium": 1, "low": 2}
    causes.sort(key=lambda c: (rank[c["confidence"]], -c["explains"]))
    return {
        "t": now,
        "status": "degraded",
        "causes": causes,
        "consequences": consequences,
        "unexplained": unexplained,
        "observed": {"bad": len(bad), "good": len(good)},
    }
