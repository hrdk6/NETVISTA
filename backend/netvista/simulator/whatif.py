"""What-if prediction: run the SimPy twin for a (possibly modified) network state.

In adaptive mode the twin also predicts what the routing controller would do: it runs a
short pilot simulation, feeds the predicted link latency/loss/utilisation into the SAME
scoring + hysteresis code the live controller uses (routing/scoring.py), re-routes, and
repeats until the paths are stable (max 3 rounds). Then the final simulation is run.

run_prediction() is a pure function of a JSON-able config, so it can run in a separate
process (the live backend keeps its probe/controller threads responsive meanwhile).
"""

from __future__ import annotations

import time
from typing import Any

from ..routing.scoring import HopMetrics, Weights, decide, evaluate
from .model import PROBE_WIRE_BYTES, NetworkModel


def _core_hops(path: list[str], routers: set[str]) -> list[tuple[str, str]]:
    return [(u, v) for u, v in zip(path, path[1:]) if u in routers and v in routers]


def _link_id(cfg: dict, u: str, v: str) -> str:
    for lid, l in cfg["links"].items():
        if {l["a"], l["b"]} == {u, v}:
            return lid
    raise KeyError(f"no link {u}-{v}")


def simulate(cfg: dict[str, Any], paths: dict[str, list[str]], duration: float, seed: int) -> dict[str, Any]:
    flows, probes = [], []
    for pair, p in cfg["pairs"].items():
        for k, fl in enumerate(p.get("flows", [])):
            flows.append({
                "id": f"{pair}#{k}", "path": paths[pair], "rate_mbps": fl["rate_mbps"],
                "start_s": fl.get("start_s", 0.0), "stop_s": min(fl.get("stop_s") or duration, duration),
                "payload_bytes": cfg.get("payload_bytes", 1200), "wire_bytes": cfg.get("wire_bytes", 1242),
            })
        probes.append({"id": pair, "path": paths[pair], "interval_s": cfg.get("probe_interval_s", 0.1)})
    model = NetworkModel({
        "links": cfg["links"],
        "flows": flows,
        "probes": probes,
        "duration_s": duration,
        "warmup_s": min(cfg.get("warmup_s", 3.0), duration / 2),
        "seed": seed,
        "overhead": cfg.get("overhead", {}),
        "probe_timeout_s": cfg.get("probe_timeout_s", 2.0),
    })
    return model.run()


def decide_paths(cfg: dict, paths: dict[str, list[str]], pilot: dict) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """Apply the live controller's decision rule to simulated link metrics."""
    routers = set(cfg["routers"])
    w = Weights(**cfg["weights"])
    hyst = cfg.get("hysteresis", 0.15)
    wire_factor = cfg.get("wire_bytes", 1242) / cfg.get("payload_bytes", 1200)
    new_paths, decisions = dict(paths), {}
    for pair, p in cfg["pairs"].items():
        own = p.get("offered_mbps", 0.0) * 1e6 * wire_factor
        cur_hops = set(_core_hops(paths[pair], routers))
        evals = []
        for cand in p["candidates"]:
            hops = []
            for u, v in _core_hops(cand, routers):
                lid = _link_id(cfg, u, v)
                L = cfg["links"][lid]
                d = pilot["links"][lid][f"{u}>{v}"]
                rev = pilot["links"][lid][f"{v}>{u}"]
                cap = L["bw_mbps"] * 1e6
                lat = d["probe_latency_ms"]
                if lat is None:
                    lat = L["delay_ms"] + d["queue_ms"] + PROBE_WIRE_BYTES * 8 / cap * 1000
                base = max(0.0, d["rate_bps"] - (own if (u, v) in cur_hops else 0.0))
                util = max((base + own) / cap, rev["rate_bps"] / cap)
                hops.append(HopMetrics(lid, u, v, lat, d["loss_frac"], util, bool(L.get("up", True))))
            evals.append(evaluate(cand, hops, w))
        cur = next((e for e in evals if e.path == paths[pair]), None)
        chosen, reason = decide(cur, evals, hyst, True)
        if chosen is not None:
            new_paths[pair] = chosen.path
        decisions[pair] = {
            "reason": reason,
            "chosen": chosen.path if chosen else None,
            "candidates": [e.to_dict() for e in evals],
        }
    return new_paths, decisions


def run_prediction(cfg: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    start_paths = {p: v["path"] for p, v in cfg["pairs"].items()}
    paths = dict(start_paths)
    rounds: list[dict] = []
    if cfg.get("mode") == "static":
        paths = {p: v["static_path"] for p, v in cfg["pairs"].items()}
    else:
        for i in range(3):
            pilot = simulate(cfg, paths, min(cfg["duration_s"], 6.0), cfg.get("seed", 1) + 101 + i)
            new_paths, decisions = decide_paths(cfg, paths, pilot)
            rounds.append(decisions)
            if new_paths == paths:
                break
            paths = new_paths
    res = simulate(cfg, paths, cfg["duration_s"], cfg.get("seed", 1))

    pairs_out = {}
    for pair, p in cfg["pairs"].items():
        fl = [v for k, v in res["flows"].items() if k.split("#")[0] == pair]
        sent = sum(v["sent"] for v in fl)
        recv = sum(v["received"] for v in fl)
        pr = res["probes"].get(pair, {})
        reason = None
        for r in rounds:
            if r.get(pair, {}).get("reason") not in (None, "keep"):
                reason = r[pair]["reason"]
        pairs_out[pair] = {
            "path": paths[pair],
            "start_path": start_paths[pair],
            "rerouted": paths[pair] != start_paths[pair],
            "reason": reason,
            "offered_mbps": p.get("offered_mbps", 0.0),
            "rx_mbps": sum(v["rx_mbps"] for v in fl) if fl else None,
            "loss_pct": 100.0 * (sent - recv) / sent if sent else None,
            "owd_p50_ms": fl[0]["owd_p50_ms"] if fl else None,
            "rtt_mean": pr.get("rtt_mean"),
            "rtt_p50": pr.get("rtt_p50"),
            "rtt_p95": pr.get("rtt_p95"),
            "rtt_p99": pr.get("rtt_p99"),
            "probe_loss_pct": pr.get("loss_pct"),
        }
    links_out = {}
    for lid, dirs in res["links"].items():
        L = cfg["links"][lid]
        links_out[lid] = {
            "up": bool(L.get("up", True)),
            "util": max(d["util"] for d in dirs.values()),
            "latency_ms": L["delay_ms"] + max(d["queue_ms"] for d in dirs.values()),
            "loss_pct": 100.0 * max(d["loss_frac"] for d in dirs.values()),
            "dirs": dirs,
            "cfg": {
                "bw_mbps": L["bw_mbps"], "delay_ms": L["delay_ms"], "jitter_ms": L.get("jitter_ms", 0.0),
                "loss_pct": L.get("loss_pct", 0.0), "queue_pkts": L.get("queue_pkts", 1000),
            },
        }
    return {
        "mode": cfg.get("mode"),
        "duration_s": cfg["duration_s"],
        "warmup_s": cfg.get("warmup_s"),
        "pairs": pairs_out,
        "links": links_out,
        "routing_rounds": rounds,
        "wall_s": time.perf_counter() - t0,
    }
