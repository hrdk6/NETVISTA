import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { flowStyle } from "../lib/colors";
import { ago, corePath, ms, num, pairLabel, pct } from "../lib/format";
import { ask } from "../lib/copilot";
import { act, useStore } from "../lib/store";
import type { Incident } from "../lib/types";

const REASON: Record<string, string> = {
  initial: "initial shortest path",
  failover: "failed over after a link died",
  better: "moved to a better-scoring path",
  static: "static Dijkstra path",
  keep: "kept",
  plan: "pinned by the intent plan",
  protect: "moved to its pre-planned backup path",
  revert: "back on its planned primary path",
  rollback: "restored by an automatic rollback",
  "failover (unplanned)": "failed over (failure not covered by the plan)",
};

export function PathSelection() {
  const snap = useStore((s) => s.snap)!;
  const pairs = useStore((s) => s.pairs);
  const r = snap.routing;
  const [open, setOpen] = useState<string>(pairs[0]);
  const f = r.flows[open];
  return (
    <section className="panel p-4">
      <div className="flex items-baseline justify-between">
        <h2 className="panel-title">Path selection</h2>
        <span className="hint">score = {r.weights.latency}·latency + {r.weights.loss}·loss% + {r.weights.util}·load%</span>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5" role="tablist" aria-label="Flow">
        {pairs.map((p) => {
          const st = flowStyle(pairs, p);
          return (
            <button
              key={p}
              role="tab"
              aria-selected={open === p}
              onClick={() => setOpen(p)}
              className={`inline-flex items-center gap-1.5 rounded border px-2 py-1 text-[12.5px] ${open === p ? "border-ink text-ink" : "border-line-strong text-ink-3"}`}
            >
              <span className="h-2 w-2 rounded-full" style={{ background: st.hex }} />
              {pairLabel(p)}
            </button>
          );
        })}
      </div>
      {f && (
        <>
          <p className="mt-3 text-[13px] text-ink-2">
            Using <span className="text-ink">{corePath(f.path)}</span>, {REASON[f.last_reason] ?? f.last_reason} {ago(f.since, snap.t)}.
            {f.offered_mbps > 0 ? ` Carrying ${f.offered_mbps.toFixed(1)} Mbit/s of iperf3 traffic.` : " Probe traffic only."}
          </p>
          <table className="data mt-2">
            <thead>
              <tr>
                <th>Candidate (routers)</th>
                <th className="text-right">Latency</th>
                <th className="text-right">Loss</th>
                <th className="text-right">Load</th>
                <th className="text-right">Score</th>
              </tr>
            </thead>
            <tbody>
              {f.candidates.map((c) => {
                const dead = c.dead_links.length > 0;
                return (
                  <tr key={c.path.join("-")} className={c.chosen ? "bg-[#2a3a4c]" : ""}>
                    <td>
                      <span className={c.chosen ? "font-semibold text-ink" : dead ? "text-ink-3 line-through" : "text-ink-2"}>{corePath(c.path)}</span>
                      {c.chosen && <span className="ml-2 text-[12px] text-ink">in use</span>}
                      {dead && <div className="hint">unusable: {c.dead_links.join(", ")}</div>}
                    </td>
                    <td className="text-right">{ms(c.latency_ms, 1)}</td>
                    <td className="text-right">{pct(c.loss_pct, 1)}</td>
                    <td className="text-right" title={c.bottleneck ? `bottleneck ${c.bottleneck}` : undefined}>
                      {pct(c.util_pct, 0)}
                    </td>
                    <td className="text-right font-semibold">{c.score == null ? "∞" : num(c.score, 1)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="hint mt-2">
            {r.mode === "adaptive"
              ? `Re-scored every second from live probes and counters. A flow moves only if another path is ${(r.hysteresis * 100).toFixed(0)}% better and it has stayed ${r.hold_down_s}s; a dead link forces an immediate fail-over.`
              : "Static mode: scores are shown for comparison but flows stay on their Dijkstra shortest path whatever happens."}
            {r.links_suspect.length > 0 && ` Suspect (silent) links avoided: ${r.links_suspect.join(", ")}.`}
          </p>
        </>
      )}
    </section>
  );
}

export function RoutingPolicy() {
  const r = useStore((s) => s.snap!.routing);
  const [w, setW] = useState(r.weights);
  useEffect(() => setW(r.weights), [r.weights.latency, r.weights.loss, r.weights.util]); // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = w.latency !== r.weights.latency || w.loss !== r.weights.loss || w.util !== r.weights.util;
  return (
    <section className="panel p-4">
      <div className="flex items-center justify-between">
        <h2 className="panel-title">Routing policy</h2>
        <div className="seg" role="group" aria-label="Routing mode">
          {(["static", "adaptive", "intent"] as const).map((m) => (
            <button
              key={m}
              aria-pressed={r.mode === m}
              disabled={m === "intent" && !r.plan}
              title={m === "intent" ? (r.plan ? `Follow plan ${r.plan.id} and its pre-planned backups` : "Make and apply a plan on the Assure page first") : undefined}
              onClick={() => act(() => api.post("/api/routing/mode", { mode: m }))}
            >
              {m === "static" ? "Static" : m === "adaptive" ? "Adaptive" : "Intent plan"}
            </button>
          ))}
        </div>
      </div>
      {r.mode === "intent" && r.plan && (
        <p className="mt-2 text-[13px] text-ink-2">
          Following plan <span className="font-semibold text-ink">{r.plan.id}</span>: pinned primary paths and pre-planned backups for {r.plan.protected.length} failures
          {r.scenario ? <span className="text-[#ffd27a]">; serving failure scenario {r.scenario}</span> : ""}. Restored links are trusted again after {r.wtr_s ?? 5} s
          (wait-to-restore). <a className="underline" href="#assure?tab=plan">Plan details</a>
        </p>
      )}
      <label className="mt-3 flex items-start gap-2 text-[13px]">
        <input type="checkbox" className="mt-0.5" checked={!!r.herd_guard} onChange={(e) => act(() => api.post("/api/routing/herd_guard", { on: e.target.checked }))} />
        <span>
          <span className="text-ink">Herd guard</span>
          <span className="hint block">
            Flows that move in the same tick see each other's load. Without it they all pick the same "empty" path and overload it (the delay-routing oscillation ARPANET
            hit in 1979).
          </span>
        </span>
      </label>
      <div className="mt-3 grid grid-cols-3 gap-3 text-[13px]">
        {(
          [
            ["latency", "Latency weight", "per ms"],
            ["loss", "Loss weight", "per % lost"],
            ["util", "Load weight", "per % busy"],
          ] as const
        ).map(([k, label, unit]) => (
          <label key={k} className="flex flex-col gap-1">
            <span className="text-ink-3">{label}</span>
            <input className="field" type="number" step={0.1} min={0} max={1000} value={w[k]} onChange={(e) => setW({ ...w, [k]: +e.target.value })} />
            <span className="hint">{unit}</span>
          </label>
        ))}
      </div>
      <button className="btn mt-3" disabled={!dirty} onClick={() => act(() => api.post("/api/routing/weights", w), "Weights applied; the next re-score uses them")}>
        Apply weights
      </button>
    </section>
  );
}

export function Incidents({ limit = 8 }: { limit?: number }) {
  const r = useStore((s) => s.snap!.routing);
  const items: Incident[] = [...r.incidents].reverse().slice(0, limit);
  return (
    <section className="panel flex min-h-0 flex-1 flex-col p-4">
      <div className="flex items-baseline justify-between">
        <h2 className="panel-title">Failure handling</h2>
        <span className="hint">
          probes every {r.probe_interval_s * 1000} ms, link declared dead after {r.dead_interval_s} s of silence
        </span>
      </div>
      {items.length === 0 ? (
        <p className="hint mt-2">No incidents yet. Take a link down or crash a router: detection, reroute and recovery times appear here.</p>
      ) : (
        <div className="mt-2 min-h-0 overflow-auto">
          <table className="data">
            <thead>
              <tr>
                <th>Flow</th>
                <th>Cause</th>
                <th className="text-right" title="failure injected → link declared dead">Detection</th>
                <th className="text-right" title="declared dead → new routes installed">Reroute</th>
                <th className="text-right" title="failure injected → first end-to-end echo on the new path">Recovery</th>
                <th>Path change</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.id}>
                  <td className="whitespace-nowrap">{pairLabel(i.pair)}</td>
                  <td className="text-ink-2">
                    {i.cause}
                    <div className="hint">
                      {i.kind}, {i.mode}
                      {i.status !== "recovered" ? `, ${i.status}` : ""}
                      {i.note ? `: ${i.note}` : ""}
                    </div>
                  </td>
                  <td className="text-right">{i.kind === "failure" ? ms(i.detection_ms, 0) : ms(i.detection_ms, 0)}</td>
                  <td className="text-right">{ms(i.reroute_ms, 0)}</td>
                  <td className="text-right font-semibold">{ms(i.recovery_ms, 0)}</td>
                  <td className="text-ink-2">
                    {corePath(i.from_path)}
                    {i.to_path && <div className="text-ink">⇒ {corePath(i.to_path)}</div>}
                  </td>
                  <td className="text-right">
                    <button
                      className="btn btn-sm"
                      title="Ask the copilot to write a post-mortem from the measured timings, events and diagnosis"
                      onClick={() => ask(`Write a post-mortem of incident ${i.id} (${pairLabel(i.pair)}, ${i.cause ?? i.kind}).`)}
                    >
                      Post-mortem
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
