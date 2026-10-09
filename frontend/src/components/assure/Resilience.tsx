import { useMemo, useState } from "react";
import { api } from "../../lib/api";
import { routers, type Criticality, type IntentRow, type Resilience, type ResilienceRow } from "../../lib/assure";
import { ago, pairLabel } from "../../lib/format";
import { act, useStore } from "../../lib/store";
import { useLiveVis } from "../liveVis";
import TopologyView, { type RiskVis } from "../TopologyView";

/** Criticality from the failure analysis -> what the map draws in "risk" mode. */
export function riskVis(crit: Record<string, Criticality> | null | undefined, rows?: ResilienceRow[]): Record<string, RiskVis> {
  const out: Record<string, RiskVis> = {};
  if (!crit) return out;
  for (const [el, c] of Object.entries(crit)) {
    const row = rows?.find((r) => r.scenario.id === c.scenario);
    out[el] = { norm: c.norm, spof: c.unreachable.length > 0, breaks: c.violated, avoidable: row?.avoidable ?? [] };
  }
  return out;
}

export function FragilityMap({ res, className }: { res: Resilience | null; className?: string }) {
  const snap = useStore((s) => s.snap)!;
  const topology = useStore((s) => s.topology)!;
  const pairs = useStore((s) => s.pairs);
  const live = useLiveVis(snap);
  const risk = useMemo(() => riskVis(res?.criticality, res?.scenarios), [res]);
  return (
    <section className={`panel flex flex-col overflow-hidden ${className ?? ""}`}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-dashed border-sim/40 px-4 py-2.5">
        <div>
          <h2 className="panel-title">Fragility map</h2>
          <p className="hint">Each link and router, coloured by how much breaks if it fails (predicted with the routing that is live now)</p>
        </div>
        <span className="sim-tag">Prediction{res?.t ? `, ${ago(res.t, snap.t)}` : ""}</span>
      </div>
      <TopologyView
        className="min-h-[320px] flex-1"
        topology={topology}
        pairs={pairs}
        links={live.links}
        nodes={live.nodes}
        strands={live.strands}
        colorMode="risk"
        risk={risk}
        packets={false}
      />
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-line px-4 py-2 text-[12px] text-ink-3">
        <span className="inline-flex items-center gap-2">
          <span className="h-1.5 w-14 rounded-full" style={{ background: "linear-gradient(90deg,#3b4a59,#ff7a59)" }} />
          harmless → breaks the most intents if it fails
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="22" height="10" aria-hidden="true">
            <line x1="1" y1="5" x2="21" y2="5" stroke="#ff7a59" strokeWidth="5" strokeDasharray="2 3" opacity="0.7" />
          </svg>
          single point of failure: no routing can save the flows behind it
        </span>
        <span>dashed: a prediction, not a measurement</span>
      </div>
    </section>
  );
}

type Cell = "held" | "avoidable" | "unavoidable" | "spof" | "risk" | "na";

const CELL: Record<Cell, { bg: string; fg: string; text: string; title: string }> = {
  held: { bg: "rgba(12,163,12,0.16)", fg: "#7fd67f", text: "✓", title: "predicted to hold" },
  risk: { bg: "rgba(250,178,25,0.14)", fg: "#ffd27a", text: "~", title: "predicted to hold, but within the prediction margin of its limit" },
  avoidable: { bg: "rgba(208,59,59,0.32)", fg: "#ffb4a8", text: "✗", title: "predicted to break, and a better routing would have kept it" },
  unavoidable: { bg: "rgba(250,178,25,0.22)", fg: "#ffd27a", text: "✗", title: "predicted to break whatever the routing does (physics: no path left that meets it)" },
  spof: { bg: "repeating-linear-gradient(135deg,rgba(208,59,59,0.22) 0 4px,rgba(0,0,0,0) 4px 8px)", fg: "#ff9d94", text: "✗", title: "the failure cuts the flow off: a single point of failure of the topology" },
  na: { bg: "transparent", fg: "#4b5866", text: "·", title: "this intent is not required to survive this kind of failure" },
};

function cellOf(row: ResilienceRow, iid: string): Cell {
  const r = row.intents.find((x) => x.intent === iid);
  if (!r) return "na";
  if (row.unprotectable.includes(iid)) return "spof";
  if (row.violated.includes(iid)) return row.avoidable.includes(iid) || !row.best ? "avoidable" : "unavoidable";
  if (r.status === "at_risk") return "risk";
  return "held";
}

export function ResilienceMatrix({ res, intents, onRefresh }: { res: Resilience | null; intents: IntentRow[] | null; onRefresh: () => void }) {
  const snapT = useStore((s) => s.snap?.t);
  const drillRunning = useStore((s) => (s.snap?.jobs?.assure as { drill?: { running?: boolean } } | undefined)?.drill?.running);
  const faults = useStore((s) => s.snap?.chaos.active.length ?? 0);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const ids = (intents ?? []).filter((i) => i.enabled).map((i) => i.id);
  const byId = Object.fromEntries((intents ?? []).map((i) => [i.id, i]));
  const run = async () => {
    setBusy(true);
    await act(() => api.post("/api/assure/resilience", { double: false }));
    setBusy(false);
    onRefresh();
  };
  const drill = (sid: string) =>
    act(() => api.post("/api/assure/drill", { scenario: sid }), "Drill started: the failure is being injected into the live network; results appear under Evidence");
  return (
    <section className="panel">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3">
        <div className="max-w-2xl">
          <h2 className="panel-title">If one element fails…</h2>
          <p className="hint">
            For every core link and router: how the {res?.mode === "intent" ? "plan's pre-planned backups" : `${res?.mode ?? "current"} routing`} would react, and which
            intents would still hold. Predicted with the fluid twin in {res ? `${(res.wall_s * 1000).toFixed(0)} ms` : "–"}, including an exhaustive search for the best
            routing after each failure (the bound).
          </p>
        </div>
        <div className="flex items-center gap-4">
          {res && (
            <div className="text-right">
              <div className="num font-cond text-[26px] leading-none font-semibold">{res.score ?? "–"}%</div>
              <div className="hint">hold{res.score_best != null && <> · best possible {res.score_best}%</>}</div>
            </div>
          )}
          <button className="btn" onClick={run} disabled={busy}>
            {busy ? "Analysing…" : "Analyse now"}
          </button>
        </div>
      </div>
      {!res || !res.t ? (
        <p className="hint px-4 py-4">The first analysis runs a few seconds after start-up, and again whenever traffic, links, routing or intents change.</p>
      ) : ids.length === 0 ? (
        <p className="hint px-4 py-4">Add intents to see which of them survive each failure.</p>
      ) : (
        <div className="table-scroll">
          <table className="data">
            <thead>
              <tr>
                <th>Failure</th>
                {ids.map((i) => (
                  <th key={i} className="text-center" title={byId[i]?.label}>
                    {i}
                  </th>
                ))}
                <th className="text-right" title="flows the routing moves">Moved</th>
                <th className="text-right" title="highest predicted link load after the failure">Peak load</th>
                <th className="text-right" title="traffic that cannot be delivered">Lost</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {res.scenarios.map((row) => (
                <MatrixRow
                  key={row.scenario.id}
                  row={row}
                  ids={ids}
                  open={open === row.scenario.id}
                  onToggle={() => setOpen(open === row.scenario.id ? null : row.scenario.id)}
                  onDrill={() => drill(row.scenario.id)}
                  drillDisabled={!!drillRunning || faults > 0}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line px-4 py-2 text-[12px] text-ink-3">
        {(["held", "risk", "avoidable", "unavoidable", "spof", "na"] as Cell[]).map((c) => (
          <span key={c} className="inline-flex items-center gap-1.5">
            <span className="inline-flex h-4 w-4 items-center justify-center rounded-sm text-[11px] font-bold" style={{ background: CELL[c].bg, color: CELL[c].fg }}>
              {CELL[c].text}
            </span>
            {CELL[c].title}
          </span>
        ))}
        {res?.t && <span className="ml-auto">analysed {ago(res.t, snapT)}</span>}
      </div>
    </section>
  );
}

function MatrixRow({ row, ids, open, onToggle, onDrill, drillDisabled }: { row: ResilienceRow; ids: string[]; open: boolean; onToggle: () => void; onDrill: () => void; drillDisabled: boolean }) {
  return (
    <>
      <tr className={open ? "bg-[#243142]" : ""}>
        <td className="whitespace-nowrap">
          <button className="text-left hover:underline" onClick={onToggle} aria-expanded={open}>
            {row.scenario.label}
          </button>
          {row.unreachable.length > 0 && <span className="ml-2 text-[11.5px] text-[#ff9d94]">cuts off {row.unreachable.length}</span>}
        </td>
        {ids.map((i) => {
          const c = cellOf(row, i);
          return (
            <td key={i} className="text-center">
              <span className="inline-flex h-5 w-7 items-center justify-center rounded-sm text-[12px] font-bold" style={{ background: CELL[c].bg, color: CELL[c].fg }} title={`${i}: ${CELL[c].title}`}>
                {CELL[c].text}
              </span>
            </td>
          );
        })}
        <td className="text-right">{row.moved.length || "–"}</td>
        <td className={`text-right ${row.max_util > 0.85 ? "text-[#ffd27a]" : ""}`}>{(row.max_util * 100).toFixed(0)}%</td>
        <td className="text-right">{row.lost_mbps > 0.05 ? `${row.lost_mbps.toFixed(1)} Mbit/s` : "–"}</td>
        <td className="text-right">
          <button className="btn btn-sm" onClick={onDrill} disabled={drillDisabled} title={drillDisabled ? "Another drill is running or faults are active" : "Fail it on the live network and check this prediction"}>
            Drill live
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={ids.length + 5} className="!bg-[#1b2531]">
            <div className="grid gap-3 py-1 text-[12.5px] md:grid-cols-2">
              <div>
                <p className="font-cond font-semibold text-ink-2">Paths after the failure ({row.planned ? "pre-planned backups" : row.rounds ? `${row.rounds} controller rounds` : "no reaction"})</p>
                <ul className="mt-1 space-y-0.5">
                  {Object.entries(row.paths).map(([p, path]) => (
                    <li key={p} className="flex justify-between gap-3">
                      <span className="text-ink-3">{pairLabel(p)}</span>
                      <span className={row.moved.includes(p) ? "text-ink" : "text-ink-2"}>
                        {row.unreachable.includes(p) ? "cut off" : routers(path)}
                        {row.rtt[p] != null && <span className="num ml-2 text-ink-3">{row.rtt[p]!.toFixed(1)} ms p95</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
              <div>
                {row.best && (
                  <>
                    <p className="font-cond font-semibold text-ink-2">Best routing for this failure ({row.best.method}, {row.best.combinations} combinations)</p>
                    <p className="mt-1 text-ink-2">
                      {row.best.violated.length === 0 ? "keeps every intent" : `still breaks ${row.best.violated.join(", ")}`}
                      {row.avoidable.length > 0 && <span className="text-[#ffb4a8]"> — the current routing also breaks {row.avoidable.join(", ")}, which it could have kept</span>}
                    </p>
                  </>
                )}
                {row.transient && row.transient.violated.length > 0 && (
                  <p className="mt-1 text-[#ffd27a]">
                    Right after the fail-over (before the controller re-balances): {row.transient.violated.join(", ")} broken, peak load {(row.transient.max_util * 100).toFixed(0)}%.
                  </p>
                )}
                {row.intents
                  .filter((r) => r.status === "violated")
                  .map((r) => {
                    const w = r.checks.filter((c) => c.ok === false).sort((a, b) => b.severity - a.severity)[0];
                    return (
                      <p key={r.intent} className="mt-1 text-ink-3">
                        {r.intent}: {w ? `${w.subject.replace(">", " → ")} ${typeof w.value === "number" ? w.value.toFixed(1) : w.value} ${w.unit} (limit ${w.limit})` : "violated"}
                      </p>
                    );
                  })}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
