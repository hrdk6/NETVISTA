import { useMemo, useState } from "react";
import { api } from "../../lib/api";
import { routers, type Decision, type Plan } from "../../lib/assure";
import { flowStyle } from "../../lib/colors";
import { ago, pairLabel, pct } from "../../lib/format";
import { act, useStore } from "../../lib/store";
import TopologyView, { type LinkVis, type StrandVis } from "../TopologyView";
import { StatusPill } from "./IntentsPanel";

const STATUS_TONE: Record<string, string> = {
  proposed: "text-ink-2",
  applied: "text-ink",
  verified: "text-[#7fd67f]",
  "rolled back": "text-[#ff9d94]",
  superseded: "text-ink-3",
  withdrawn: "text-ink-3",
  shadow: "text-sim",
  blocked: "text-[#ffd27a]",
  "awaiting approval": "text-[#ffd27a]",
  dismissed: "text-ink-3",
  unchanged: "text-ink-3",
};

function ScoreArrow({ before, after, best }: { before: number | null; after: number | null; best: number | null }) {
  const w = (x: number | null) => `${Math.max(0, Math.min(100, x ?? 0))}%`;
  return (
    <div>
      <div className="flex items-baseline gap-2 font-cond">
        <span className="num text-[22px] font-semibold text-ink-3">{before ?? "–"}%</span>
        <span className="text-ink-3">→</span>
        <span className="num text-[28px] font-semibold text-ink">{after ?? "–"}%</span>
        {best != null && <span className="hint">best possible {best}%</span>}
      </div>
      <div className="relative mt-1.5 h-2 w-full overflow-hidden rounded-full bg-[#1b2531]">
        <div className="absolute inset-y-0 left-0 rounded-full bg-[#3d4c5d]" style={{ width: w(before) }} />
        <div className="absolute inset-y-0 left-0 rounded-full border border-dashed border-sim bg-[#8fb7d940]" style={{ width: w(after) }} />
        {best != null && <div className="absolute inset-y-[-2px] w-[2px] bg-ink" style={{ left: w(best) }} title={`best possible: ${best}%`} />}
      </div>
      <p className="hint mt-1">share of (single failure × intent) cells predicted to hold</p>
    </div>
  );
}

export function PlanPanel({ plans, onChanged }: { plans: Plan[] | null; onChanged: () => void }) {
  const snap = useStore((s) => s.snap)!;
  const topology = useStore((s) => s.topology)!;
  const pairs = useStore((s) => s.pairs);
  const [sel, setSel] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const plan = plans?.find((p) => p.id === sel) ?? plans?.[0] ?? null;
  const routing = snap.routing;
  const live = routing.plan;

  const make = async () => {
    setBusy("plan");
    const r = await act(() => api.post<Plan>("/api/assure/plan"));
    setBusy(null);
    if (r) {
      setSel(r.id);
      onChanged();
    }
  };
  const doIt = async (what: "confirm" | "apply") => {
    if (!plan) return;
    setBusy(what);
    await act(
      () => api.post(`/api/assure/plans/${plan.id}/${what}`),
      what === "apply" ? `Plan ${plan.id} applied: the network is in intent mode; it is verified on live measurements in ~16 s` : undefined,
    );
    setBusy(null);
    onChanged();
  };

  // what the planned routing looks like, drawn as a simulation (dashed provenance)
  const vis = useMemo(() => {
    if (!plan) return null;
    const links: Record<string, LinkVis> = {};
    for (const l of topology.links) {
      const u = plan.predicted.link_util[l.id] ?? 0;
      links[l.id] = { util: u, health: u >= 0.85 ? "degraded" : "ok", admin_up: true };
    }
    const nodes = Object.fromEntries(topology.nodes.map((n) => [n.id, { health: "ok" as const }]));
    const strands: StrandVis[] = Object.entries(plan.primary).map(([p, path]) => ({ pair: p, path, offered_mbps: plan.predicted.pairs[p]?.offered_mbps ?? 0 }));
    return { links, nodes, strands };
  }, [plan, topology]);

  return (
    <section className="panel">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3">
        <div className="max-w-3xl">
          <h2 className="panel-title">Plan routes for the intents</h2>
          <p className="hint">
            The planner tries every combination of the flows' candidate paths against your intents (now, and after each single failure with the best backup paths), then
            picks the plan with the fewest weighted violations and the fewest moves. Backups are part of the plan, checked before they are ever needed.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {plans && plans.length > 1 && (
            <select className="field" value={plan?.id ?? ""} onChange={(e) => setSel(e.target.value)} aria-label="Plan">
              {plans.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.id} · {p.status} · {ago(p.t, snap.t)}
                </option>
              ))}
            </select>
          )}
          <button className="btn btn-primary" onClick={make} disabled={!!busy}>
            {busy === "plan" ? "Searching…" : "Make a plan"}
          </button>
        </div>
      </div>
      {live && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line bg-[#1f2c3a] px-4 py-2 text-[13px]">
          <span>
            <span className="font-semibold">Live routing follows plan {live.id}</span>
            <span className="text-ink-3"> · applied by {live.applied_by} {ago(live.applied_t, snap.t)} · {live.protected.length} failures pre-planned</span>
            {routing.scenario && <span className="ml-2 text-[#ffd27a]">serving failure scenario {routing.scenario}</span>}
          </span>
          <button className="btn btn-sm" onClick={() => act(() => api.post("/api/assure/plan/clear", { mode: "adaptive" }), "Back to the adaptive controller").then(onChanged)}>
            Back to adaptive routing
          </button>
        </div>
      )}
      {!plan ? (
        <p className="px-4 py-6 text-[13px] text-ink-2">No plan yet. Load intents, start some traffic, then press Make a plan: it takes about a second.</p>
      ) : (
        <div className="grid gap-4 p-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
          <div className="flex min-w-0 flex-col gap-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-cond text-[20px] font-semibold">{plan.id}</span>
                  <span className={`font-cond text-[13px] font-semibold ${STATUS_TONE[plan.status] ?? "text-ink-2"}`}>{plan.status}</span>
                  <span className="sim-tag">Predicted</span>
                </div>
                <p className="text-[13px] text-ink-2">{plan.label}</p>
                <p className="hint">
                  {plan.source} · {ago(plan.t, snap.t)} · searched {plan.search.evaluations.toLocaleString()} routings ({plan.search.method}, {plan.search.space.toLocaleString()} primary
                  combinations × {plan.search.scenarios} failures) in {plan.search.wall_s.toFixed(2)} s
                </p>
              </div>
              <div className="flex gap-2">
                <button className="btn" onClick={() => doIt("confirm")} disabled={!!busy} title="Run the packet-level SimPy twin on this plan and compare it with the fluid prediction">
                  {busy === "confirm" ? "Simulating…" : "Cross-check (packet twin)"}
                </button>
                <button className="btn btn-primary" onClick={() => doIt("apply")} disabled={!!busy || plan.status === "applied" || plan.status === "verified"}>
                  {busy === "apply" ? "Applying…" : "Apply plan"}
                </button>
              </div>
            </div>

            <ScoreArrow before={plan.resilience_before.score} after={plan.resilience_after.score} best={plan.resilience_after.score_best ?? plan.resilience_before.score_best} />

            <table className="data">
              <thead>
                <tr>
                  <th>Flow</th>
                  <th>Now</th>
                  <th>Plan</th>
                  <th className="text-right">RTT p95</th>
                  <th className="text-right">Loss</th>
                </tr>
              </thead>
              <tbody>
                {pairs.map((p) => {
                  const pp = plan.predicted.pairs[p];
                  const cur = snap.flows[p]?.path;
                  const moved = plan.moved.includes(p);
                  const st = flowStyle(pairs, p);
                  return (
                    <tr key={p}>
                      <td className="whitespace-nowrap">
                        <span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: st.hex }} />
                        {pairLabel(p)}
                        {pp?.offered_mbps ? <span className="hint ml-1.5">{pp.offered_mbps} Mbit/s</span> : <span className="hint ml-1.5">probes</span>}
                      </td>
                      <td className="text-ink-3">{routers(cur)}</td>
                      <td className={moved ? "font-semibold text-sim" : "text-ink-2"}>{routers(plan.primary[p])}</td>
                      <td className="text-right text-sim">
                        {pp?.rtt_p95 != null ? `${pp.rtt_p95.toFixed(1)} ms` : "–"}
                        {pp?.rtt_p95 != null && plan.rtt_margin > 0 && <span className="hint"> ±{(pp.rtt_p95 * plan.rtt_margin).toFixed(1)}</span>}
                      </td>
                      <td className="text-right text-sim">{pct(pp?.loss_pct, 2)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            <div>
              <p className="font-cond text-[13.5px] font-semibold text-ink-2">Intents with this plan (normal operation)</p>
              <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
                {plan.predicted.intents.map((r) => (
                  <span key={r.intent} className="inline-flex items-center gap-1.5 text-[12.5px]">
                    <span className="text-ink-3">{r.intent}</span>
                    <StatusPill small status={r.status} />
                  </span>
                ))}
              </div>
              <table className="data mt-2">
                <thead>
                  <tr>
                    <th>Objective (lower is better)</th>
                    <th className="text-right">Current routing</th>
                    <th className="text-right">This plan</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td className="text-ink-3">Weighted violation now</td>
                    <td className="text-right">{plan.current.v_now.toFixed(2)}</td>
                    <td className="text-right font-semibold">{plan.objective.v_now.toFixed(2)}</td>
                  </tr>
                  <tr>
                    <td className="text-ink-3">… summed over single failures (best backups)</td>
                    <td className="text-right">{plan.current.v_fail.toFixed(1)}</td>
                    <td className="text-right font-semibold">{plan.objective.v_fail.toFixed(1)}</td>
                  </tr>
                  <tr>
                    <td className="text-ink-3">Path cost + moves (tie-break)</td>
                    <td className="text-right">{plan.current.cost.toFixed(1)}</td>
                    <td className="text-right font-semibold">{plan.objective.cost.toFixed(1)}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            {plan.confirmation && (
              <div className={`rounded border p-3 text-[12.5px] ${plan.confirmation.agree ? "border-[#2f5a3a]" : "border-[#7a5a2a]"}`}>
                <p className="font-semibold">
                  {plan.confirmation.agree ? "Both twins agree" : "The twins disagree"}: packet-level SimPy vs fluid model, RTT within {plan.confirmation.max_rtt_diff_pct.toFixed(2)}%, link load within{" "}
                  {plan.confirmation.max_util_diff_pp.toFixed(2)} pp
                </p>
                <p className="hint">The autopilot's safety gate requires agreement within 5 % / 5 pp before it may apply a plan. Packet twin took {plan.confirmation.wall_s} s.</p>
              </div>
            )}
            {plan.verification && (
              <div className={`rounded border p-3 text-[12.5px] ${plan.verification.verdict === "verified" ? "border-[#2f5a3a] bg-[#1f3328]" : "border-[#7a3434] bg-[#3a2326]"}`}>
                <p className="font-semibold">
                  {plan.verification.verdict === "verified" ? "Verified on the live network" : plan.verification.verdict === "rolled back" ? "Rolled back automatically" : "Not judged"}:{" "}
                  {plan.verification.intent_predictions_correct}/{plan.verification.intent_predictions_judged} intent outcomes as predicted
                </p>
                <p className="mt-1 text-ink-2">
                  {Object.keys(plan.verification.after)
                    .map((i) => `${i}: ${plan.verification!.before[i] ?? "?"} → ${plan.verification!.after[i]} (predicted ${plan.verification!.predicted[i] ?? "?"})`)
                    .join(" · ")}
                </p>
              </div>
            )}
          </div>

          <div className="flex min-w-0 flex-col gap-4">
            {vis && (
              <div className="flex h-[300px] flex-col overflow-hidden rounded border border-dashed border-sim/50 bg-[#1d2a38]">
                <div className="flex items-center justify-between border-b border-dashed border-sim/40 px-3 py-1.5">
                  <span className="sim-tag">Plan {plan.id}: primary paths, predicted load</span>
                </div>
                <TopologyView className="flex-1" topology={topology} pairs={pairs} links={vis.links} nodes={vis.nodes} strands={vis.strands} packets={false} variant="sim" />
              </div>
            )}
            <div>
              <p className="font-cond text-[13.5px] font-semibold text-ink-2">Pre-planned backups ({Object.keys(plan.protection).length} failures)</p>
              <table className="data mt-1">
                <thead>
                  <tr>
                    <th>If…</th>
                    <th>Move</th>
                    <th className="text-right">Then breaks</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(plan.predicted.scenarios).map(([sid, s]) => {
                    const prot = plan.protection[sid];
                    return (
                      <tr key={sid}>
                        <td className="whitespace-nowrap">{s.label}</td>
                        <td className="text-ink-2">
                          {s.unreachable.length > 0 && !prot
                            ? <span className="text-[#ff9d94]">nothing can help: cuts off {s.unreachable.length} flows</span>
                            : prot
                              ? Object.entries(prot).map(([p, path]) => (
                                  <div key={p}>
                                    {pairLabel(p)} → {routers(path)}
                                  </div>
                                ))
                              : <span className="text-ink-3">no flow affected</span>}
                        </td>
                        <td className={`text-right ${s.violated.length ? "text-[#ffd27a]" : "text-[#7fd67f]"}`}>{s.violated.length ? s.violated.join(", ") : "nothing"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ autopilot
const MODE_TEXT: Record<string, string> = {
  off: "Nothing happens on its own. Make and apply plans yourself.",
  shadow: "Plans are made on every change and logged as “would apply”. Nothing changes: the safe way to build trust.",
  approve: "Plans that pass the safety gate wait for your approval here.",
  auto: "Plans that pass the safety gate are applied immediately, then verified; a regression rolls back automatically.",
};

export function AutopilotPanel({ decisions, onChanged }: { decisions: Decision[] | null; onChanged: () => void }) {
  const ap = useStore((s) => s.snap?.assure?.autopilot);
  const snapT = useStore((s) => s.snap?.t);
  const [open, setOpen] = useState<string | null>(null);
  if (!ap) return null;
  const setMode = (m: string) => act(() => api.post("/api/assure/autopilot/mode", { mode: m })).then(onChanged);
  return (
    <section className="panel">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
        <div>
          <h2 className="panel-title">Autopilot</h2>
          <p className="hint max-w-xl">{MODE_TEXT[ap.mode]}</p>
        </div>
        <div className="seg" role="group" aria-label="Autopilot mode">
          {(["off", "shadow", "approve", "auto"] as const).map((m) => (
            <button key={m} aria-pressed={ap.mode === m} onClick={() => setMode(m)}>
              {m === "off" ? "Off" : m === "shadow" ? "Shadow" : m === "approve" ? "Approve" : "Auto"}
            </button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 border-b border-line px-4 py-2 text-[12.5px] text-ink-2">
        <span>
          State <span className="font-semibold text-ink">{ap.state}</span>
        </span>
        <span>
          Deployments verified <span className="num font-semibold text-ink">{ap.deployments - ap.rollbacks}</span>
        </span>
        <span>
          Rolled back <span className="num font-semibold text-ink">{ap.rollbacks}</span>
        </span>
        <span className="text-ink-3">Gate: network free · paths alive · strictly better · no critical regression · peak load ≤ 95% · both twins agree · ≥ 20 s since last change</span>
      </div>
      {ap.pending && (
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-[#2d2a1e] px-4 py-3">
          <div>
            <p className="font-semibold">
              Waiting for approval: plan {ap.pending.plan} — {ap.pending.summary}
            </p>
            {ap.pending.resilience && (
              <p className="text-[12.5px] text-ink-2">
                Resilience {ap.pending.resilience.before}% → {ap.pending.resilience.after}% (predicted). It passed the safety gate.
              </p>
            )}
          </div>
          <div className="flex gap-2">
            <button className="btn btn-primary" onClick={() => act(() => api.post(`/api/assure/autopilot/decisions/${ap.pending!.id}/approve`), "Approved: applying and verifying").then(onChanged)}>
              Approve
            </button>
            <button className="btn" onClick={() => act(() => api.post(`/api/assure/autopilot/decisions/${ap.pending!.id}/dismiss`)).then(onChanged)}>
              Dismiss
            </button>
          </div>
        </div>
      )}
      <ul className="max-h-[420px] divide-y divide-line overflow-auto">
        {(decisions ?? []).length === 0 && <li className="hint px-4 py-3">No decisions yet. Switch to Shadow to see what the autopilot would do.</li>}
        {(decisions ?? []).map((d) => (
          <li key={d.id} className="px-4 py-2 text-[13px]">
            <button className="flex w-full items-baseline justify-between gap-3 text-left" onClick={() => setOpen(open === d.id ? null : d.id)} aria-expanded={open === d.id}>
              <span className="min-w-0">
                <span className={`font-cond font-semibold ${STATUS_TONE[d.status] ?? "text-ink-2"}`}>{d.status}</span>
                <span className="ml-2 text-ink">{d.plan ? `${d.plan}: ` : ""}{d.summary}</span>
                <span className="block text-[12px] text-ink-3">trigger: {d.trigger}{d.resilience ? ` · resilience ${d.resilience.before}% → ${d.resilience.after}%` : ""}</span>
              </span>
              <span className="hint shrink-0">{ago(d.t, snapT)}</span>
            </button>
            {open === d.id && d.gate && (
              <ul className="mt-1.5 space-y-0.5 rounded border border-line bg-[#1b2531] p-2">
                {d.gate.checks.map((c) => (
                  <li key={c.check} className="flex gap-2 text-[12.5px]">
                    <span className={c.ok ? "text-[#7fd67f]" : "text-[#ff9d94]"}>{c.ok ? "✓" : "✗"}</span>
                    <span className="w-28 shrink-0 text-ink-3">{c.check}</span>
                    <span className="text-ink-2">{c.detail}</span>
                  </li>
                ))}
                {d.verification && (
                  <li className="pt-1 text-[12.5px] text-ink-2">
                    Verification: {d.verification.verdict}, {d.verification.intent_predictions_correct}/{d.verification.intent_predictions_judged} intent outcomes as predicted
                  </li>
                )}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
