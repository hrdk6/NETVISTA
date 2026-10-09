#!/usr/bin/env python3
"""Export the evidence behind DESIGN.md's numbers from runs/ (git-ignored) to docs/results/ (committed).

    python3 scripts/export_results.py [--topology netvista-default]

Writes, for the latest complete runs of one topology:
    docs/results/benchmark.json       per-strategy summary + per-failure rows (timelines dropped)
    docs/results/validation.json      the latest validation suite: both engines' errors per scenario
    docs/results/drills.json          drill outcomes
    docs/results/RESULTS.md           the same as Markdown tables
Stdlib only, so it runs with any Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
OUT = ROOT / "docs" / "results"
LABEL = {"static": "Static (Dijkstra)", "adaptive-raw": "Adaptive, no herd guard", "adaptive": "Adaptive + herd guard",
         "intent": "Intent plan (Assure)"}


def jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def f(x, d=2, u=""):
    return "–" if x is None else f"{x:.{d}f}{u}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topology", default="netvista-default")
    a = ap.parse_args()
    topo = a.topology
    OUT.mkdir(parents=True, exist_ok=True)
    md = [f"# Results ({topo})", "", "Exported by `scripts/export_results.py` from `runs/`. Every number was measured on the live emulation.", ""]

    # ---- benchmark
    benches = [b for b in jsonl(RUNS / "benchmarks.jsonl") if b.get("topology", "netvista-default") == topo and len(b.get("strategies", [])) >= 2]
    if benches:
        b = max(benches, key=lambda r: (len(r["results"]), r["t"]))
        slim = {k: b[k] for k in ("id", "t", "duration_s", "strategies", "scenarios", "rates", "summary")}
        slim["intents"] = [{k: i[k] for k in ("id", "label", "priority", "protect")} for i in b.get("intents", [])]
        slim["results"] = [{k: v for k, v in r.items() if k != "timeline"} for r in b["results"]]
        (OUT / "benchmark.json").write_text(json.dumps(slim, indent=1), encoding="utf-8")
        md += ["## Live benchmark", "",
               f"{len(b['scenarios'])} failures × {len(b['strategies'])} strategies, {b['duration_s'] / 60:.0f} min; traffic "
               + ", ".join(f"{p.replace('>', '→')} {r:g}" for p, r in b["rates"].items()) + " Mbit/s.", "",
               "| Strategy | Weighted intent-s violated | of which new | Mean time to compliance | Never compliant | Route changes | Delivered | Prediction right |",
               "|---|---|---|---|---|---|---|---|"]
        for s in b["strategies"]:
            v = b["summary"].get(s)
            if not v:
                continue
            md.append(f"| {LABEL.get(s, s)} | {v['violation_s']:g} | {v['new_violation_s']:g} | {f(v['mean_time_to_compliance_s'], 1, ' s')} | "
                      f"{v['never_compliant']} | {v['route_changes']} | {v['delivered_pct']:.2f} % | {f(v['prediction_accuracy_pct'], 0, ' %')} |")
        md += ["", "Per failure (weighted intent-seconds violated in the 20 s after the failure):", "",
               "| Failure | " + " | ".join(LABEL.get(s, s) for s in b["strategies"]) + " |",
               "|---|" + "---|" * len(b["strategies"])]
        for sc in b["scenarios"]:
            cells = []
            for s in b["strategies"]:
                r = next((x for x in b["results"] if x["scenario"] == sc and x["strategy"] == s), None)
                cells.append("–" if r is None else f"{r['violation_s']:g}" + ("" if r["prediction_match"] in (None, True) else " *"))
            label = next((x["label"] for x in b["results"] if x["scenario"] == sc), sc)
            md.append(f"| {label} | " + " | ".join(cells) + " |")
        md += ["", "\\* the measured set of broken intents differed from the failure analysis' prediction.", ""]
        full = sorted((r for r in benches if len(r["results"]) == len(b["results"]) and r["strategies"] == b["strategies"]), key=lambda r: r["t"])
        if len(full) > 1:  # every complete run, so a better-looking run cannot quietly replace a worse one
            md += ["All complete runs (weighted intent-s violated, route changes in brackets):", "",
                   "| Run | " + " | ".join(LABEL.get(s, s) for s in b["strategies"]) + " |", "|---|" + "---|" * len(b["strategies"])]
            for r in full:
                when = __import__("time").strftime("%Y-%m-%d %H:%M", __import__("time").localtime(r["t"]))
                md.append(f"| {when}{' (latest)' if r is b else ''} | " + " | ".join(
                    f"{r['summary'][s]['violation_s']:g} ({r['summary'][s]['route_changes']})" for s in b["strategies"]) + " |")
            md.append("")

    # ---- validation (the latest 11-scenario suite)
    runs = jsonl(RUNS / "validation_runs.jsonl")
    suite = [r for r in runs if r.get("summary_fluid")][-11:]
    if suite:
        (OUT / "validation.json").write_text(json.dumps([{k: r.get(k) for k in ("id", "t", "label", "mode", "summary", "summary_fluid", "sim_wall_s", "fluid_wall_s")}
                                                         for r in suite], indent=1), encoding="utf-8")
        md += ["## Twin validation (both engines against the live network)", "",
               "| Scenario | Routing | RTT p50 packet / fluid | RTT p95 packet / fluid | Bottleneck total packet / fluid | Loss (per flow) | Paths |",
               "|---|---|---|---|---|---|---|"]
        for r in suite:
            s, sf = r["summary"], r["summary_fluid"]
            md.append(f"| {r['label']} | {r['mode']} | {f(s.get('latency_p50_mape'))} / {f(sf.get('latency_p50_mape'))} % | "
                      f"{f(s.get('latency_p95_mape'))} / {f(sf.get('latency_p95_mape'))} % | {f(s.get('aggregate_throughput_err'))} / "
                      f"{f(sf.get('aggregate_throughput_err'))} % | {f(s.get('loss_mae_pp'))} pp | {f(s.get('path_match_pct'), 0)} % |")
        md.append("")

    # ---- drills
    drills = [d for d in jsonl(RUNS / "drills.jsonl") if d.get("topology", "netvista-default") == topo]
    if drills:
        (OUT / "drills.json").write_text(json.dumps([{k: d.get(k) for k in ("id", "t", "scenario", "mode", "plan", "intents", "intent_accuracy",
                                                                           "paths_match_fluid", "paths_match_packet", "summary_fluid", "summary_packet")}
                                                     for d in drills], indent=1), encoding="utf-8")
        md += ["## Drills", "", "| Failure | Routing | Intent outcomes as predicted | Paths as predicted | RTT p50 err fluid / packet |", "|---|---|---|---|---|"]
        for d in drills:
            md.append(f"| {d['scenario']['label']} | {d['mode']}{' (' + d['plan'] + ')' if d.get('plan') else ''} | {f(d.get('intent_accuracy'), 0, ' %')} | "
                      f"{'yes' if d.get('paths_match_fluid') else 'no'} | {f((d.get('summary_fluid') or {}).get('latency_p50_mape'))} / "
                      f"{f((d.get('summary_packet') or {}).get('latency_p50_mape'))} % |")
        md.append("")
    (OUT / "RESULTS.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
