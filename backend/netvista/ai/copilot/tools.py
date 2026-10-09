"""The copilot's tools: what the language model may read, simulate and propose.

Three kinds, and the split is the safety model:
    read       live measurements and state (probes, counters, controller, event log, AI insights)
    simulate   the SimPy twin; results are labelled SIMULATION
    propose    a change the USER may apply from a card in the UI. The model can never change
               the network itself: a proposal is validated, stored and shown, nothing more.

Results are compact JSON with rounded numbers (they go back into the model's context).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from ...chaos import ChaosError, change_label, validate_change
from ...journey import packet_journey
from .providers import ToolSchema


def r(x: Any, nd: int = 2) -> Any:
    if isinstance(x, float):
        return round(x, nd)
    return x


def compact(x: Any) -> Any:
    """Drop nulls and empty containers: every token costs time on a local model."""
    if isinstance(x, dict):
        out = {k: compact(v) for k, v in x.items()}
        return {k: v for k, v in out.items() if v is not None and v != [] and v != {}}
    if isinstance(x, list):
        return [compact(v) for v in x]
    return x


def mbit(bps: float | None) -> float | None:
    return None if bps is None else round(bps / 1e6, 3)


def core_path(topo, path: list[str] | None) -> str | None:
    if not path:
        return None
    return "-".join(n for n in path if topo.nodes[n].type == "router")


@dataclass
class Tool:
    name: str
    kind: str  # read | simulate | propose
    description: str
    parameters: dict[str, Any]
    fn: Callable[[dict[str, Any]], Any]
    local: bool = True  # offered to small local models too
    label: Callable[[dict[str, Any]], str] = lambda a: ""

    def schema(self) -> ToolSchema:
        return ToolSchema(self.name, self.description, self.parameters)


def _obj(props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props or {}, "required": required or []}


CHANGE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["link_latency", "link_loss", "link_bandwidth", "link_down", "node_down", "traffic_burst"]},
        "target": {"type": "string", "description": "link id (e.g. r2-r5) for link_*; router/switch id for node_down; 'src>dst' for traffic_burst"},
        "params": {
            "type": "object",
            "description": "link_latency: {add_ms, jitter_ms?}; link_loss: {loss_pct}; link_bandwidth: {bw_mbps}; "
                           "traffic_burst: {rate_mbps, duration_s}; link_down/node_down: {}",
        },
    },
    "required": ["kind", "target"],
}


class Toolbox:
    def __init__(self, rt, ai, proposals) -> None:
        self.rt = rt
        self.ai = ai
        self.proposals = proposals  # ProposalStore
        self.conversation_id: str | None = None
        self.tools: dict[str, Tool] = {t.name: t for t in self._build()}

    def schemas(self, local: bool) -> list[ToolSchema]:
        return [t.schema() for t in self.tools.values() if t.local or not local]

    def run(self, name: str, args: dict[str, Any]) -> tuple[bool, Any]:
        tool = self.tools.get(name)
        if tool is None:
            return False, {"error": f"unknown tool {name!r}"}
        try:
            return True, compact(tool.fn(args or {}))
        except (ChaosError, ValueError, KeyError, LookupError) as e:
            return False, {"error": str(e).strip("'\"")}

    # ------------------------------------------------------------------ helpers
    def _link_row(self, lid: str, v: dict) -> dict:
        d = max((v["ab"], v["ba"]), key=lambda x: x["bps"])
        base = self.rt.topo.links[lid]
        drops = v["ab"]["drops_ps"] + v["ba"]["drops_ps"]
        row = {
            "link": lid, "health": v["health"], "rtt_ms": r(v["rtt_ms"]), "design_rtt_ms": r(v["expected_rtt_ms"]),
            "probe_loss_pct": r(v["loss_pct"], 1), "load_pct": r(v["util"] * 100, 1), "load_mbps": mbit(d["bps"]),
            "capacity_mbps": v["cfg"]["bw_mbps"],
        }
        if v["cfg"]["bw_mbps"] != base.bw_mbps:
            row["design_capacity_mbps"] = base.bw_mbps
        if drops >= 0.05:
            row["drops_per_s"] = r(drops, 1)
        return row

    def _flow_row(self, pair: str, f: dict) -> dict:
        p, t = f["probe"], f["traffic"]
        rf = self.rt.controller.flows[pair]
        return {
            "flow": pair, "path": core_path(self.rt.topo, f["path"]), "probe_alive": p.get("alive"),
            "rtt_ms": r(p.get("rtt_ms")), "rtt_p50_ms": r(p.get("rtt_p50")), "rtt_p95_ms": r(p.get("rtt_p95")),
            "probe_loss_pct": r(p.get("probe_loss_pct"), 1), "offered_mbps": f["offered_mbps"],
            "received_mbps": r(t["rx_mbps"], 3) if t else None, "data_loss_pct": r(t["loss_pct"], 2) if t else None,
            "path_changes": rf.changes, "last_route_reason": rf.last_reason,
        }

    def _signals(self, prefix: str) -> list[dict]:
        rows = []
        for s in self.ai.detector.signal_table():
            if s["signal"].startswith(prefix):
                rows.append({k: s[k] for k in ("metric", "state", "value", "normal_mean", "normal_upper", "z") if s.get(k) is not None or k == "state"})
        return rows

    def _age(self, t: float | None) -> float | None:
        return None if t is None else round(time.time() - t, 1)

    # ------------------------------------------------------------------ read tools
    def network_status(self, _: dict) -> dict:
        rt = self.rt
        snap = rt.snapshot()
        topo = rt.topo
        ins = self.ai.snapshot_view()
        return {
            "measured_at": time.strftime("%H:%M:%S", time.localtime(snap["t"])),
            "routing": {"mode": snap["routing"]["mode"], "weights": snap["routing"]["weights"]},
            "nodes": {n: v["health"] for n, v in snap["nodes"].items()},
            "links": [self._link_row(lid, v) for lid, v in snap["links"].items()],
            "flows": [self._flow_row(p, f) for p, f in snap["flows"].items()],
            "traffic": [
                {"flow": f["pair"], "kind": f["kind"], "rate_mbps": f["rate_mbps"], "age_s": self._age(f["started_at"])}
                for f in snap["traffic"] if f["state"] == "running"
            ],
            "active_faults_injected_by_chaos_lab": [
                {"id": i["id"], "label": i["label"], "age_s": self._age(i["t"])} for i in snap["chaos"]["active"]
            ],
            "ai": {
                "active_anomalies": [a["label"] for a in ins["anomalies"]],
                "diagnosis": [c["title"] + f" ({c['confidence']})" for c in ins["diagnosis"]["causes"]],
            },
            "note": f"{len(topo.nodes)} nodes; rtt_ms = mean probe RTT over the last 2 s; probe_loss_pct over 5 s; load = tx / shaped capacity",
        }

    def link_details(self, a: dict) -> dict:
        lid = a.get("link_id") or a.get("link") or ""
        rt = self.rt
        if lid not in rt.topo.links:
            # accept "r5-r2" for "r2-r5"
            parts = lid.split("-")
            if len(parts) == 2 and f"{parts[1]}-{parts[0]}" in rt.topo.links:
                lid = f"{parts[1]}-{parts[0]}"
            else:
                raise KeyError(f"unknown link {lid!r}; links: {', '.join(rt.topo.links)}")
        now = time.time()
        v = rt.telemetry.link_view(lid, now)
        hist = [h["links"][lid] for h in list(rt.history) if h["t"] >= now - 120 and lid in h["links"]]

        def stats(key: str, scale: float = 1.0) -> dict | None:
            xs = [h[key] * scale for h in hist if h.get(key) is not None]
            if not xs:
                return None
            return {"min": r(min(xs)), "mean": r(sum(xs) / len(xs)), "max": r(max(xs))}

        flows = [p for p, f in rt.controller.flows.items() if f.path and any(
            rt.topo.link_between(u, w) and rt.topo.link_between(u, w).id == lid for u, w in zip(f.path, f.path[1:]))]
        return {
            **self._link_row(lid, v),
            "probe_span": v["probe_span"], "alive": v["alive"], "admin_up": v["admin_up"],
            "rtt_p50_ms": r(v["rtt_p50"]), "rtt_p95_ms": r(v["rtt_p95"]), "rtt_p99_ms": r(v["rtt_p99"]),
            "directions": {
                f"{v['a']}->{v['b']}": {"mbps": mbit(v["ab"]["bps"]), "pps": r(v["ab"]["pps"], 0), "load_pct": r(v["ab"]["util"] * 100, 1),
                                        "drops_per_s": v["ab"]["drops_ps"], "queue_pkts": v["ab"]["backlog_pkts"]},
                f"{v['b']}->{v['a']}": {"mbps": mbit(v["ba"]["bps"]), "pps": r(v["ba"]["pps"], 0), "load_pct": r(v["ba"]["util"] * 100, 1),
                                        "drops_per_s": v["ba"]["drops_ps"], "queue_pkts": v["ba"]["backlog_pkts"]},
            },
            "last_120s": {"rtt_ms": stats("rtt_ms"), "load_pct": stats("util", 100.0), "probe_loss_pct": stats("loss_pct")},
            "learned_normal": self._signals(f"link:{lid}:"),
            "flows_using_it": flows,
        }

    def node_details(self, a: dict) -> dict:
        n = a.get("node_id") or a.get("node") or ""
        rt = self.rt
        if n not in rt.topo.nodes:
            raise KeyError(f"unknown node {n!r}; nodes: {', '.join(rt.topo.nodes)}")
        now = time.time()
        links = {l.id: rt.telemetry.link_view(l.id, now) for l in rt.topo.links_of(n)}
        v = rt.telemetry.node_view(n, links)
        spec = rt.topo.nodes[n]
        return {
            "node": n, "type": spec.type, "label": spec.label, "health": v["health"],
            "rx_mbps": mbit(v["rx_bps"]), "tx_mbps": mbit(v["tx_bps"]), "drops_per_s": v["drops_ps"],
            "links": {lid: lv["health"] for lid, lv in links.items()},
            "interfaces": [{"name": i["name"], "ip": i["ip"], "link": i["link"], "rx_mbps": mbit(i["rx_bps"]), "tx_mbps": mbit(i["tx_bps"]),
                            "tx_pps": r(i["tx_pps"], 0)} for i in v["interfaces"]],
            "flows_through_it": [p for p, f in rt.controller.flows.items() if f.path and n in f.path],
            "ip": rt.plan.host_ip.get(n),
        }

    def flow_details(self, a: dict) -> dict:
        pair = (a.get("flow") or a.get("pair") or "").replace("→", ">").replace(" ", "").replace("->", ">")
        rt = self.rt
        if pair not in rt.controller.flows:
            raise KeyError(f"unknown flow {pair!r}; flows: {', '.join(rt.controller.flows)}")
        st = rt.controller.state()["flows"][pair]
        snap_flow = rt.flow_view(pair, st, time.time())
        return {
            **self._flow_row(pair, snap_flow),
            "full_path": "-".join(st["path"]) if st["path"] else None,
            "static_shortest_path": core_path(rt.topo, st["static_path"]),
            "on_path_since_s": self._age(st["since"]) if st["since"] else None,
            "candidates_scored": [
                {"path": core_path(rt.topo, c["path"]), "chosen": c.get("chosen"), "feasible": c["feasible"], "score": r(c["score"], 3),
                 "latency_ms": r(c["latency_ms"]), "loss_pct": r(c["loss_pct"], 2), "load_pct": r(c["util_pct"], 1),
                 "bottleneck": c["bottleneck"], "dead_links": c["dead_links"]}
                for c in st["candidates"]
            ],
            "score_formula": "score = w_latency*latency_ms + w_loss*loss_pct + w_util*projected_load_pct (lower is better)",
            "weights": rt.controller.weights.to_dict(),
            "learned_normal": self._signals(f"flow:{pair}:"),
        }

    def insights(self, _: dict) -> dict:
        ins = self.ai.insights()
        d = ins["diagnosis"]
        return {
            "method": "anomalies = learned per-signal baselines (EWMA mean/var, z>=4 sustained); "
                      "diagnosis = probe-path tomography over measured probes only (never reads the chaos lab)",
            "detector": {k: ins["detector"][k] for k in ("signals", "learning", "normal", "anomalous")},
            "active_anomalies": [
                {k: x[k] for k in ("label", "unit", "value", "normal_mean", "normal_upper", "z", "severity")} | {"for_s": self._age(x["t_start"])}
                for x in ins["anomalies"]
            ],
            "recently_cleared": [
                {"label": x["label"], "peak_value": x["peak_value"], "normal_mean": x["normal_mean"],
                 "lasted_s": r(x["t_end"] - x["t_start"], 0), "ended_s_ago": self._age(x["t_end"])}
                for x in ins["recent"][:8]
            ],
            "diagnosis": {
                "status": d["status"],
                "causes": [
                    {k: c[k] for k in ("title", "type", "element", "confidence", "evidence", "contradicted_by", "alternatives",
                                       "affected_flows")}
                    | {"for_s": self._age(c["since"]),
                       "rerouted_flows": [{"flow": x["pair"], "from": core_path(self.rt.topo, x["from"]), "to": core_path(self.rt.topo, x["to"])}
                                          for x in c["rerouted_flows"]]}
                    for c in d["causes"]
                ],
                "side_effects": d["consequences"],
                "unexplained": d["unexplained"],
            },
        }

    def incidents(self, a: dict) -> list[dict]:
        limit = int(a.get("limit") or 10)
        out = []
        for i in list(self.rt.controller.incidents)[-limit:][::-1]:
            d = i.to_dict()
            out.append({
                "id": d["id"], "kind": d["kind"], "flow": d["pair"], "link": d["link"], "cause": d["cause"], "status": d["status"],
                "routing_mode": d["mode"], "from_path": core_path(self.rt.topo, d["from_path"]), "to_path": core_path(self.rt.topo, d["to_path"]),
                "detection_ms": d["detection_ms"], "reroute_ms": d["reroute_ms"], "route_install_ms": d["install_ms"],
                "recovery_ms": d["recovery_ms"], "detected_s_ago": self._age(d["t_detect"]), "note": d["note"] or None,
            })
        return out

    def events(self, a: dict) -> list[dict]:
        limit = min(int(a.get("limit") or 40), 120)
        needle = (a.get("contains") or "").lower()
        evs = self.rt.events.recent(600)
        if needle:
            evs = [e for e in evs if needle in e.message.lower() or needle in e.kind]
        return [{"s_ago": self._age(e.t), "kind": e.kind, "severity": e.severity, "message": e.message} for e in evs[-limit:]]

    def history(self, a: dict) -> dict:
        target = (a.get("target") or "").replace("→", ">").replace(" ", "")
        secs = max(20, min(int(a.get("seconds") or 120), 900))
        rt = self.rt
        now = time.time()
        hist = [h for h in list(rt.history) if h["t"] >= now - secs]
        bucket = 10 if secs <= 300 else 30
        if target in rt.topo.links:
            rows = [(h["t"], h["links"][target]) for h in hist if target in h["links"]]
            fields = {"rtt_ms": 1.0, "util": 100.0, "loss_pct": 1.0}
            names = {"rtt_ms": "rtt_ms", "util": "load_pct", "loss_pct": "probe_loss_pct"}
        elif target in rt.controller.flows:
            rows = [(h["t"], h["flows"][target]) for h in hist if target in h["flows"]]
            fields = {"rtt_ms": 1.0, "rx_mbps": 1.0, "probe_loss_pct": 1.0, "iperf_loss_pct": 1.0}
            names = {"rtt_ms": "rtt_ms", "rx_mbps": "received_mbps", "probe_loss_pct": "probe_loss_pct", "iperf_loss_pct": "data_loss_pct"}
        else:
            raise KeyError(f"target must be a link id or a flow like c1>srv1, got {target!r}")
        out = []
        for start in range(int(now - secs), int(now), bucket):
            chunk = [v for t, v in rows if start <= t < start + bucket]
            if not chunk:
                continue
            row: dict[str, Any] = {"from_s_ago": round(now - start)}
            for k, sc in fields.items():
                xs = [c[k] * sc for c in chunk if c.get(k) is not None]
                if xs:
                    row[names[k]] = {"mean": r(sum(xs) / len(xs)), "max": r(max(xs))}
            if "path" in chunk[-1] and chunk[-1]["path"]:
                row["path"] = core_path(rt.topo, chunk[-1]["path"].split("-"))
            out.append(row)
        return {"target": target, "bucket_s": bucket, "samples": out}

    def journey(self, a: dict) -> dict:
        j = packet_journey(self.rt, a.get("src", ""), a.get("dst", ""))
        hops = []
        for h in j["hops"]:
            row = {"node": h["node"], "type": h["type"]}
            if h.get("kernel_decision"):
                row["kernel_route_get"] = h["kernel_decision"]
            if h.get("out"):
                o = h["out"]
                row["out"] = {"link": o["link"], "intf": o["intf"], "health": o["health"], "rtt_ms": r(o["rtt_ms"]),
                              "load_pct": r(o["util"] * 100, 1), "probe_loss_pct": o["loss_pct"]}
            hops.append(row)
        return {"src": j["src"], "src_ip": j["src_ip"], "dst": j["dst"], "dst_ip": j["dst_ip"],
                "path": "-".join(j["path"]), "routed_by": j["route_source"], "table": j["table"], "hops": hops}

    def twin_accuracy(self, _: dict) -> dict:
        v = self.rt.extensions.get("validation")
        runs = v.list_runs()[:12] if v else []
        rows = []
        for run in runs:
            s = run["summary"]
            rows.append({"scenario": run["label"], "mode": run["mode"], "rtt_p50_error_pct": r(s.get("latency_p50_mape")),
                         "rtt_p95_error_pct": r(s.get("latency_p95_mape")), "throughput_error_pct": r(s.get("throughput_mape")),
                         "loss_error_pp": r(s.get("loss_mae_pp")), "path_match_pct": r(s.get("path_match_pct"), 0),
                         "ran_s_ago": self._age(run["t"])})
        ev = self.ai.evaluation.list_runs()[:1]
        return {"twin_validation_runs": rows or "no validation runs yet (Validation page -> Run validation suite)",
                "ai_detector_evaluation": ev[0]["summary"] if ev else "not run yet (AI page -> Run evaluation)"}

    # ------------------------------------------------------------------ simulate
    def what_if(self, a: dict) -> dict:
        sim = self.rt.extensions.get("simulator")
        if sim is None:
            raise ValueError("the simulator is not available")
        changes = a.get("changes") or []
        if isinstance(changes, dict):
            changes = [changes]
        clean = []
        for c in changes:
            t, p = validate_change(self.rt.topo, c.get("kind", ""), c.get("target"), c.get("params") or {})
            clean.append({"kind": c["kind"], "target": t, "params": p})
        mode = a.get("routing_mode") or None
        pred = sim.predict(clean, duration_s=8.0, routing_mode=mode, include_baseline=True)
        topo = self.rt.topo

        def view(x: dict | None) -> dict | None:
            if not x:
                return None
            return {"path": core_path(topo, x.get("path")), "rtt_p50_ms": r(x.get("rtt_p50")), "rtt_p95_ms": r(x.get("rtt_p95")),
                    "probe_loss_pct": r(x.get("probe_loss_pct"), 2), "received_mbps": r(x.get("rx_mbps"), 3),
                    "data_loss_pct": r(x.get("loss_pct"), 2)}

        flows = {}
        for pair, p in pred["result"]["pairs"].items():
            flows[pair] = {
                "measured_now": view(pred["live"]["pairs"].get(pair)),
                "twin_without_change": view((pred["baseline"] or {}).get("pairs", {}).get(pair)),
                "twin_with_change": view(p),
                "controller_would_reroute": p["rerouted"],
                "offered_mbps": p["offered_mbps"],
            }
        links = {lid: {"load_pct": r(l["util"] * 100, 1), "up": l["up"]} for lid, l in pred["result"]["links"].items()
                 if l["util"] >= 0.5 or not l["up"]}
        return {
            "label": "SIMULATION: digital-twin prediction, not a measurement",
            "scenario": pred["label"], "routing_mode": pred["mode"], "prediction_id": pred["id"], "twin_wall_s": r(pred["wall_s"], 1),
            "flows": flows, "busy_or_down_links_in_twin": links,
        }

    # ------------------------------------------------------------------ propose
    def propose_change(self, a: dict) -> dict:
        t, p = validate_change(self.rt.topo, a.get("kind", ""), a.get("target"), a.get("params") or {})
        spec = {"kind": a["kind"], "target": t, "params": p}
        return self.proposals.add(self.conversation_id, "chaos_inject", spec, change_label(a["kind"], t, p), a.get("reason"))

    def propose_revert(self, a: dict) -> dict:
        fid = (a.get("fault_id") or "all").strip()
        active = {i.id: i for i in self.rt.chaos.active()}
        if fid != "all" and fid not in active:
            raise KeyError(f"no active fault {fid!r}; active: {', '.join(active) or 'none'}")
        label = "Revert all active faults" if fid == "all" else f"Revert: {active[fid].label}"
        return self.proposals.add(self.conversation_id, "chaos_revert", {"fault_id": fid}, label, a.get("reason"))

    def propose_routing(self, a: dict) -> dict:
        spec: dict[str, Any] = {}
        parts = []
        if a.get("mode"):
            if a["mode"] not in ("static", "adaptive"):
                raise ValueError("mode must be static or adaptive")
            spec["mode"] = a["mode"]
            parts.append(f"routing policy {a['mode']}")
        w = a.get("weights")
        if w:
            cur = self.rt.controller.weights.to_dict()
            spec["weights"] = {k: float(w.get(k, cur[k])) for k in ("latency", "loss", "util")}
            for k, val in spec["weights"].items():
                if not 0 <= val <= 1000:
                    raise ValueError(f"weight {k} must be within [0, 1000]")
            parts.append("score weights " + ", ".join(f"{k} {v:g}" for k, v in spec["weights"].items()))
        if not spec:
            raise ValueError("give mode and/or weights")
        return self.proposals.add(self.conversation_id, "routing", spec, "Set " + "; ".join(parts), a.get("reason"))

    def propose_traffic(self, a: dict) -> dict:
        action = a.get("action")
        if action not in ("start", "stop"):
            raise ValueError("action must be start or stop")
        label = "Start the default iperf3 traffic profile" if action == "start" else "Stop all background iperf3 traffic"
        return self.proposals.add(self.conversation_id, "traffic", {"action": action}, label, a.get("reason"))

    # ------------------------------------------------------------------ registry
    def _build(self) -> list[Tool]:
        reason = {"type": "string", "description": "one sentence: why you propose it"}
        return [
            Tool("get_network_status", "read",
                 "Live overview: health of every node and link, per-link RTT/loss/load, per-flow path, RTT, loss and throughput, "
                 "running traffic, faults injected by the chaos lab, and the AI diagnosis headline. Call this first.",
                 _obj(), self.network_status, label=lambda a: "Read the live network state"),
            Tool("get_link_details", "read",
                 "Everything measured on one link: RTT percentiles, per-direction rate, packets/s, drops and queue, the last 120 s "
                 "(min/mean/max), its learned normal baseline, and which flows use it.",
                 _obj({"link_id": {"type": "string", "description": "e.g. r2-r5"}}, ["link_id"]), self.link_details,
                 label=lambda a: f"Read link {a.get('link_id', '')}"),
            Tool("get_node_details", "read", "One router, switch or host: health, per-interface rates and IPs, flows through it.",
                 _obj({"node_id": {"type": "string", "description": "e.g. r2"}}, ["node_id"]), self.node_details, local=False,
                 label=lambda a: f"Read node {a.get('node_id', '')}"),
            Tool("get_flow_details", "read",
                 "One client->server flow: its path, why it is on it, every candidate path with the controller's score breakdown, "
                 "probe RTT/loss, iperf3 throughput/loss and its learned baseline.",
                 _obj({"flow": {"type": "string", "description": "e.g. c1>srv1"}}, ["flow"]), self.flow_details,
                 label=lambda a: f"Read flow {a.get('flow', '')}"),
            Tool("get_ai_insights", "read",
                 "The AIOps layer: active anomalies (value vs learned normal), recently cleared ones, and the root-cause diagnosis "
                 "with its evidence, confidence, alternatives and side effects.",
                 _obj(), self.insights, label=lambda a: "Read AI anomalies and diagnosis"),
            Tool("get_incidents", "read",
                 "Failure/degradation incidents handled by the routing controller with measured detection, reroute and recovery times.",
                 _obj({"limit": {"type": "integer", "description": "default 10"}}), self.incidents,
                 label=lambda a: "Read routing incidents"),
            Tool("get_events", "read", "Recent event log lines (chaos, routing, traffic, AI, twin), newest last.",
                 _obj({"limit": {"type": "integer", "description": "default 40"},
                       "contains": {"type": "string", "description": "optional filter text, e.g. r2-r5 or reroute"}}),
                 self.events, label=lambda a: "Read the event log" + (f" ({a['contains']})" if a.get("contains") else "")),
            Tool("get_history", "read", "Time series of one link or flow over the last N seconds in 10 s buckets (mean/max).",
                 _obj({"target": {"type": "string", "description": "link id (r2-r5) or flow (c1>srv1)"},
                       "seconds": {"type": "integer", "description": "20-900, default 120"}}, ["target"]),
                 self.history, local=False, label=lambda a: f"Read history of {a.get('target', '')}"),
            Tool("trace_path", "read",
                 "The path packets from src to dst take right now, with each router's live kernel routing decision (ip route get).",
                 _obj({"src": {"type": "string"}, "dst": {"type": "string"}}, ["src", "dst"]), self.journey, local=False,
                 label=lambda a: f"Trace {a.get('src', '')} → {a.get('dst', '')}"),
            Tool("get_twin_accuracy", "read",
                 "How accurate the digital twin and the AI detector are: latest validation errors and the AI evaluation summary.",
                 _obj(), self.twin_accuracy, local=False, label=lambda a: "Read twin and AI accuracy"),
            Tool("run_what_if", "simulate",
                 "Ask the SimPy digital twin what WOULD happen if changes were applied (the live network is untouched; ~5-15 s). "
                 "Returns measured_now, twin_without_change and twin_with_change per flow. This is a SIMULATION.",
                 _obj({"changes": {"type": "array", "items": CHANGE_SCHEMA},
                       "routing_mode": {"type": "string", "enum": ["static", "adaptive"], "description": "default: current mode"}},
                      ["changes"]),
                 self.what_if, label=lambda a: "Ran the digital twin (simulation)"),
            Tool("propose_change", "propose",
                 "Propose injecting a fault in the chaos lab. NOT executed: the user sees a card and decides.",
                 {**CHANGE_SCHEMA, "properties": {**CHANGE_SCHEMA["properties"], "reason": reason}}, self.propose_change,
                 label=lambda a: "Proposed a change"),
            Tool("propose_revert", "propose", "Propose reverting one active fault (by id) or all of them. NOT executed.",
                 _obj({"fault_id": {"type": "string", "description": "fault id from active faults, or 'all'"}, "reason": reason}),
                 self.propose_revert, label=lambda a: "Proposed a revert"),
            Tool("propose_routing", "propose", "Propose a routing policy (static/adaptive) and/or score weights. NOT executed.",
                 _obj({"mode": {"type": "string", "enum": ["static", "adaptive"]},
                       "weights": {"type": "object", "properties": {"latency": {"type": "number"}, "loss": {"type": "number"},
                                                                    "util": {"type": "number"}}},
                       "reason": reason}),
                 self.propose_routing, local=False, label=lambda a: "Proposed a routing change"),
            Tool("propose_traffic", "propose", "Propose starting or stopping the default iperf3 traffic. NOT executed.",
                 _obj({"action": {"type": "string", "enum": ["start", "stop"]}, "reason": reason}, ["action"]),
                 self.propose_traffic, local=False, label=lambda a: "Proposed a traffic change"),
        ]


def dumps(x: Any) -> str:
    return json.dumps(x, separators=(",", ":"), default=str)
