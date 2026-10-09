"""Calibrate the twin from live measurements (model-based).

The structural model (propagation + token-bucket shaping + queueing + loss) comes straight
from the topology/tc parameters. What it cannot know is the emulator's own overhead: kernel
forwarding in each namespace, veth wake-ups, timer slack and the probe agent's Python
processing. We learn it from live probe RTTs:

  1. run the twin of the CURRENT state (same links, same traffic, same paths) with zero
     overhead, simulating one probe stream for every live probe stream
  2. residual_i = live median RTT_i - simulated median RTT_i      (queueing cancels out)
  3. least-squares fit   residual = E + H * (2 n_i)
        n_i = links on stream i's path (1 for router probes, 2 host->gateway, 6 end-to-end)
        E   = endpoint overhead (agent + socket stack, both ends together)
        H   = per link-traversal overhead
  4. the live jitter around each end-to-end stream's median is kept as an empirical noise
     distribution that the twin adds to every simulated probe RTT

Because step 1 simulates the live load, calibration is valid in the operating regime it was
taken in (validation re-calibrates after its traffic warm-up for exactly this reason).
"""

from __future__ import annotations

import time
from typing import Any

from ..telemetry.health import percentile
from .model import NetworkModel


def fit_overheads(obs: list[tuple[int, float]]) -> tuple[float, float, float]:
    """obs = [(n_links_one_way, residual_ms)] -> (E_ms, H_ms, rms_ms), both clamped >= 0."""
    if not obs:
        return 0.0, 0.0, 0.0
    xs = [2.0 * n for n, _ in obs]
    ys = [r for _, r in obs]
    m = len(obs)
    mx, my = sum(xs) / m, sum(ys) / m
    sxx = sum((x - mx) ** 2 for x in xs)
    h = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 1e-12 else 0.0
    if h < 0:
        h = 0.0
    e = my - h * mx
    if e < 0:  # refit through the origin
        e = 0.0
        sxy = sum(x * y for x, y in zip(xs, ys))
        sx2 = sum(x * x for x in xs)
        h = max(0.0, sxy / sx2) if sx2 > 0 else 0.0
    rms = (sum((y - (e + h * x)) ** 2 for x, y in zip(xs, ys)) / m) ** 0.5
    return e, h, rms


def stream_path(rt, sid: str) -> list[str] | None:
    topo = rt.topo
    kind, ref = sid.split(":", 1)
    if kind == "L":
        l = topo.links[ref]
        return [l.a, l.b]
    if kind == "G":
        link = topo.links_of(ref)[0]
        peer = link.other(ref)
        if topo.nodes[peer].type == "switch":
            return [ref, peer, rt.plan.host_gateway[ref]]
        return [ref, peer]
    if kind == "F":
        f = rt.controller.flows.get(ref)
        return list(f.path) if f and f.path else None
    return None


def collect_live(rt, window_s: float = 15.0) -> list[dict]:
    now = time.time()
    out = []
    for sid, s in list(rt.store.streams.items()):
        t0 = now - window_s
        if sid.startswith("F:"):
            # only samples taken on the flow's current path: a window that spans a reroute mixes
            # two paths' RTTs and the twin is compared with the wrong one (found live: a reroute
            # during calibration gave a 5.7 ms fit RMS and a noise distribution full of queueing outliers)
            f = rt.controller.flows.get(sid[2:])
            if f is None:
                continue
            t0 = max(t0, (f.since or 0.0) + 1.0)
        samples = sorted(s.rtt_samples(t0, now))
        if len(samples) < 20:
            continue
        path = stream_path(rt, sid)
        if not path:
            continue
        out.append({"stream": sid, "path": path, "n_links": len(path) - 1, "samples": samples, "measured_p50": percentile(samples, 50)})
    return out


def calibration_model_cfg(base_cfg: dict, live: list[dict], duration_s: float = 8.0) -> dict:
    flows = []
    for pair, p in base_cfg["pairs"].items():
        for k, fl in enumerate(p.get("flows", [])):
            flows.append({"id": f"{pair}#{k}", "path": p["path"], "rate_mbps": fl["rate_mbps"],
                          "payload_bytes": base_cfg["payload_bytes"], "wire_bytes": base_cfg["wire_bytes"]})
    return {
        "links": base_cfg["links"],
        "flows": flows,
        "probes": [{"id": o["stream"], "path": o["path"], "interval_s": base_cfg["probe_interval_s"]} for o in live],
        "duration_s": duration_s,
        "warmup_s": 3.0,
        "seed": 7,
        "overhead": {},  # zero overhead: we are measuring what the structure alone predicts
        "probe_timeout_s": base_cfg["probe_timeout_s"],
    }


def run_calibration_sim(model_cfg: dict) -> dict[str, Any]:
    """Top-level so it can run in the simulator's worker process."""
    return NetworkModel(model_cfg).run()["probes"]


def finish_calibration(rt, live: list[dict], sim_probes: dict, window_s: float) -> dict[str, Any]:
    obs, noise = [], []
    for o in live:
        sp = sim_probes.get(o["stream"], {}).get("rtt_p50")
        if sp is None:
            continue
        obs.append({"stream": o["stream"], "n_links": o["n_links"], "measured_p50": o["measured_p50"],
                    "structural_p50": sp, "residual": o["measured_p50"] - sp})
        if o["stream"].startswith("F:"):
            noise.extend(x - o["measured_p50"] for x in o["samples"])
    e, h, rms = fit_overheads([(o["n_links"], o["residual"]) for o in obs])
    noise.sort()
    if len(noise) > 400:  # keep an evenly spaced subset of the empirical distribution
        step = len(noise) / 400
        noise = [noise[int(i * step)] for i in range(400)]
    now = time.time()
    utils = [rt.telemetry.link_view(l, now)["util"] for l in rt.topo.links]
    max_util = max(utils) if utils else 0.0
    return {
        "t": now,
        "window_s": window_s,
        "method": "model-based: live median RTT minus zero-overhead twin RTT, least squares E + H*2n",
        "endpoint_ms": round(e, 4),
        "per_hop_ms": round(h, 4),
        "fit_rms_ms": round(rms, 4),
        "n_streams": len(obs),
        "noise_ms": [round(x, 4) for x in noise],
        "noise_p05_ms": round(percentile(noise, 5), 4) if noise else None,
        "noise_p95_ms": round(percentile(noise, 95), 4) if noise else None,
        "conditions": {
            "max_link_util": round(max_util, 4),
            "offered_mbps": sum(f.rate_mbps for f in rt.traffic.running()),
            "active_injections": len(rt.chaos.active()),
            "calm": not rt.chaos.active(),
        },
        "observations": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in o.items()} for o in obs],
    }
