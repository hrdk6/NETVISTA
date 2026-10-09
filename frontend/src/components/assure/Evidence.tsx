import { Fragment, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../../lib/api";
import { STRATEGY_LABEL, routers, type BenchRun, type DrillRun, type LedgerEntry, type UncertaintyClass } from "../../lib/assure";
import { ago, pairLabel } from "../../lib/format";
import { act, useStore } from "../../lib/store";

function fmtPct(x: number | null | undefined, d = 1) {
  return x == null ? "–" : `${x.toFixed(d)}%`;
}

// ------------------------------------------------------------------ deployments ledger
export function Ledger({ entries }: { entries: LedgerEntry[] | null }) {
  const snapT = useStore((s) => s.snap?.t);
  const judged = (entries ?? []).filter((e) => e.verdict !== "not judged");
  const correct = judged.reduce((s, e) => s + e.intent_predictions_correct, 0);
  const total = judged.reduce((s, e) => s + e.intent_predictions_judged, 0);
  return (
    <section className="panel">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line px-4 py-3">
        <div>
          <h2 className="panel-title">Deployments: predicted vs measured</h2>
          <p className="hint">Every plan applied (by hand or by the autopilot) is measured for 10 s after a 6 s settle and compared with what the twin predicted.</p>
        </div>
        {total > 0 && (
          <span className="text-[13px] text-ink-2">
            <span className="num font-cond text-[20px] font-semibold text-ink">{correct}/{total}</span> intent outcomes as predicted
          </span>
        )}
      </div>
      <div className="table-scroll">
        <table className="data">
          <thead>
            <tr>
              <th>Plan</th>
              <th>Applied by</th>
              <th>Verdict</th>
              <th>Intents before → after (predicted)</th>
              <th className="text-right">When</th>
            </tr>
          </thead>
          <tbody>
            {(entries ?? []).length === 0 && (
              <tr>
                <td colSpan={5} className="hint">
                  No deployments yet.
                </td>
              </tr>
            )}
            {(entries ?? []).map((e) => (
              <tr key={e.id}>
                <td className="font-semibold">{e.plan}</td>
                <td className="text-ink-2">{e.source}</td>
                <td className={e.verdict === "verified" ? "text-[#7fd67f]" : e.verdict === "rolled back" ? "text-[#ff9d94]" : "text-ink-3"}>
                  {e.verdict}
                  {e.verdict !== "not judged" && <span className="hint"> · {e.intent_predictions_correct}/{e.intent_predictions_judged} as predicted</span>}
                  {e.regressions?.length ? <div className="text-[12px]">broke {e.regressions.join(", ")}</div> : null}
                </td>
                <td className="text-[12.5px] text-ink-2">
                  {Object.keys(e.after ?? {}).map((i) => (
                    <span key={i} className="mr-3 inline-block whitespace-nowrap">
                      {i} {short(e.before?.[i])}→{short(e.after[i])} <span className="text-ink-3">({short(e.predicted?.[i])})</span>
                    </span>
                  ))}
                  {e.reason && <span className="text-ink-3">{e.reason}</span>}
                </td>
                <td className="text-right text-ink-3">{ago(e.t, snapT)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function short(s: string | undefined) {
  if (!s) return "?";
  return s === "violated" ? "✗" : s === "ok" || s === "held" ? "✓" : s === "at_risk" ? "~" : "?";
}

// ------------------------------------------------------------------ drills
export function Drills({ runs, scenarios, onChanged }: { runs: DrillRun[] | null; scenarios: { id: string; label: string }[]; onChanged: () => void }) {
  const snapT = useStore((s) => s.snap?.t);
  const job = useStore((s) => (s.snap?.jobs?.assure as { drill?: { running?: boolean; label?: string; step?: string; step_ends_at?: number } } | undefined)?.drill);
  const faults = useStore((s) => s.snap?.chaos.active.length ?? 0);
  const [sid, setSid] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const start = async () => {
    const s = sid || scenarios[0]?.id;
    if (!s) return;
    await act(() => api.post("/api/assure/drill", { scenario: s }), "Drill started");
    onChanged();
  };
  return (
    <section className="panel">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
        <div className="max-w-2xl">
          <h2 className="panel-title">Resilience drills</h2>
          <p className="hint">Fail one element on the live network and check the failure analysis: paths, every intent's outcome, and the error of both twins.</p>
        </div>
        <div className="flex items-center gap-2">
          <select className="field" value={sid} onChange={(e) => setSid(e.target.value)} aria-label="Failure to drill">
            {scenarios.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
          {job?.running ? (
            <button className="btn" onClick={() => act(() => api.post("/api/assure/drill/cancel"))}>
              Stop drill
            </button>
          ) : (
            <button className="btn btn-primary" onClick={start} disabled={faults > 0} title={faults ? "Revert the active faults first" : undefined}>
              Run drill
            </button>
          )}
        </div>
      </div>
      {job?.running && (
        <p className="border-b border-line bg-[#2d2a1e] px-4 py-2 text-[13px]">
          Drilling “{job.label}”: {job.step}
          {job.step_ends_at && snapT ? ` (${Math.max(0, job.step_ends_at - snapT).toFixed(0)} s)` : ""}
        </p>
      )}
      <div className="table-scroll">
        <table className="data">
          <thead>
            <tr>
              <th>Failure</th>
              <th>Routing</th>
              <th className="text-right">Intent outcomes</th>
              <th className="text-right">Paths</th>
              <th className="text-right" title="RTT p50 error of the fluid model / packet twin">RTT err fluid / packet</th>
              <th className="text-right" title="intent-seconds violated from the moment of failure">Transient</th>
              <th className="text-right">When</th>
            </tr>
          </thead>
          <tbody>
            {(runs ?? []).length === 0 && (
              <tr>
                <td colSpan={7} className="hint">
                  No drills yet. Pick a failure and run one, or use “Drill live” in the failure matrix.
                </td>
              </tr>
            )}
            {(runs ?? []).map((d) => {
              const judged = d.intents.filter((c) => c.match !== null);
              const ok = judged.filter((c) => c.match).length;
              const transient = Object.values(d.transient.violation_s).reduce((s, x) => s + x, 0);
              return (
                <Fragment key={d.id}>
                  <tr className="cursor-pointer hover:bg-[#243142]" onClick={() => setOpen(open === d.id ? null : d.id)}>
                    <td className="whitespace-nowrap">{d.scenario.label}</td>
                    <td className="text-ink-2">
                      {d.mode}
                      {d.plan ? ` (${d.plan})` : ""}
                    </td>
                    <td className={`text-right ${ok === judged.length ? "text-[#7fd67f]" : "text-[#ffd27a]"}`}>
                      {ok}/{judged.length} as predicted
                    </td>
                    <td className={`text-right ${d.paths_match_fluid ? "text-[#7fd67f]" : "text-[#ff9d94]"}`}>{d.paths_match_fluid ? "as predicted" : "different"}</td>
                    <td className="text-right">
                      {fmtPct(d.summary_fluid?.latency_p50_mape, 2)} / {fmtPct(d.summary_packet?.latency_p50_mape, 2)}
                    </td>
                    <td className="text-right text-ink-2">
                      {transient} s{d.transient.settled_after_s != null ? `, clean after ${d.transient.settled_after_s.toFixed(0)} s` : ""}
                    </td>
                    <td className="text-right text-ink-3">{ago(d.t, snapT)}</td>
                  </tr>
                  {open === d.id && (
                    <tr>
                      <td colSpan={7} className="!bg-[#1b2531]">
                        <div className="grid gap-3 py-1 text-[12.5px] md:grid-cols-2">
                          <div>
                            <p className="font-cond font-semibold text-ink-2">Intents (predicted → measured)</p>
                            {d.intents.map((c) => (
                              <p key={c.intent} className={c.match === false ? "text-[#ff9d94]" : "text-ink-2"}>
                                {c.intent}: predicted {c.predicted}, measured {c.measured} {c.match === false ? "✗" : c.match ? "✓" : ""}
                              </p>
                            ))}
                          </div>
                          <div>
                            <p className="font-cond font-semibold text-ink-2">Paths (predicted → measured)</p>
                            {Object.entries(d.measured.paths).map(([p, path]) => (
                              <p key={p} className="text-ink-2">
                                {pairLabel(p)}: {routers(d.predicted.paths_fluid[p])} → {routers(path)}
                              </p>
                            ))}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ uncertainty
const CLS_LABEL: Record<string, string> = { rtt: "Round-trip time", throughput: "Throughput", loss: "Data loss" };

export function Uncertainty({ pool }: { pool: Record<string, UncertaintyClass> | null }) {
  const rows = Object.values(pool ?? {}).sort((a, b) => (a.engine + a.cls).localeCompare(b.engine + b.cls));
  return (
    <section className="panel">
      <div className="border-b border-line px-4 py-3">
        <h2 className="panel-title">How much to trust a prediction</h2>
        <p className="hint max-w-3xl">
          Every time a prediction met reality (validation runs, drills, deployments) its error joined this pool. The 90 % interval is the error that 90 % of past predictions
          stayed within (split-conformal). Scenarios differ, so instead of assuming the guarantee, each new result is first tested against the interval made without it:
          that is the measured coverage. The planner uses the RTT interval as its safety margin.
        </p>
      </div>
      <table className="data">
        <thead>
          <tr>
            <th>Engine</th>
            <th>Metric</th>
            <th className="text-right">Residuals</th>
            <th className="text-right">90 % interval</th>
            <th className="text-right">Bias</th>
            <th className="text-right">Measured coverage</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={6} className="hint">
                No residuals yet: run the validation suite, a drill, or apply a plan.
              </td>
            </tr>
          )}
          {rows.map((r) => {
            const rel = r.unit === "relative";
            const hw = r.half_width == null ? "–" : rel ? `± ${(r.half_width * 100).toFixed(2)}%` : `± ${r.half_width.toFixed(2)} pp`;
            const bias = r.bias == null ? "–" : rel ? `${r.bias >= 0 ? "+" : ""}${(r.bias * 100).toFixed(2)}%` : `${r.bias >= 0 ? "+" : ""}${r.bias.toFixed(2)} pp`;
            return (
              <tr key={`${r.engine}:${r.cls}`}>
                <td>{r.engine === "fluid" ? "Fluid model" : "Packet twin"}</td>
                <td className="text-ink-2">{CLS_LABEL[r.cls] ?? r.cls}</td>
                <td className="text-right">{r.n}</td>
                <td className="text-right">
                  {hw}
                  {r.half_width_source !== r.engine && r.half_width_source !== "none" && <span className="hint"> (from {r.half_width_source})</span>}
                </td>
                <td className="text-right text-ink-2" title="mean signed error: positive = the twin predicts too high">
                  {bias}
                </td>
                <td className="text-right">
                  {r.coverage == null ? "–" : `${(r.coverage * 100).toFixed(0)}%`}
                  <span className="hint"> of {r.coverage_tested}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

// ------------------------------------------------------------------ benchmark
const STRATS = ["static", "adaptive-raw", "adaptive", "intent"];

export function Benchmark({ runs, onChanged }: { runs: BenchRun[] | null; onChanged: () => void }) {
  const snapT = useStore((s) => s.snap?.t);
  const job = useStore(
    (s) =>
      (s.snap?.jobs?.assure as { benchmark?: { running?: boolean; index?: number; total?: number; strategy?: string; scenario?: string; step?: string; eta_s?: number; started?: number } } | undefined)
        ?.benchmark,
  );
  const [strats, setStrats] = useState<string[]>(STRATS);
  const [sel, setSel] = useState<string | null>(null);
  const run = runs?.find((r) => r.id === sel) ?? runs?.[0] ?? null;
  const start = async () => {
    await act(() => api.post("/api/assure/benchmark", { strategies: strats }), "Benchmark started: it injects every failure under each strategy (about 30 minutes)");
    onChanged();
  };
  return (
    <section className="panel">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3">
        <div className="max-w-3xl">
          <h2 className="panel-title">Live benchmark: which routing keeps the intents?</h2>
          <p className="hint">
            The same failures (every core link and transit router) are injected into the real emulated network under each routing strategy, with the same traffic and
            the same intents. Every intent is checked every second; the score is weighted intent-seconds violated in the 20 s after each failure.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {STRATS.map((s) => (
            <label key={s} className="inline-flex items-center gap-1.5 text-[12.5px] text-ink-2">
              <input type="checkbox" checked={strats.includes(s)} onChange={() => setStrats(strats.includes(s) ? strats.filter((x) => x !== s) : [...strats, s])} />
              {STRATEGY_LABEL[s]}
            </label>
          ))}
          {job?.running ? (
            <button className="btn" onClick={() => act(() => api.post("/api/assure/benchmark/cancel"))}>
              Stop
            </button>
          ) : (
            <button className="btn btn-primary" onClick={start} disabled={!strats.length}>
              Run benchmark
            </button>
          )}
        </div>
      </div>
      {job?.running && (
        <div className="border-b border-line bg-[#2d2a1e] px-4 py-2 text-[13px]">
          <div className="flex justify-between gap-3">
            <span>
              {job.index ?? 0}/{job.total} · {STRATEGY_LABEL[job.strategy ?? ""] ?? job.strategy} · {job.step}
            </span>
            {job.started && job.eta_s && snapT && <span className="text-ink-3">about {Math.max(0, (job.started + job.eta_s - snapT) / 60).toFixed(0)} min left</span>}
          </div>
          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-[#1b2531]">
            <div className="h-full rounded-full bg-ink-2" style={{ width: `${(100 * (job.index ?? 0)) / Math.max(1, job.total ?? 1)}%` }} />
          </div>
        </div>
      )}
      {!run ? (
        <p className="px-4 py-5 text-[13px] text-ink-2">No benchmark yet. It needs intents (load the Gold / bronze preset) and takes about half an hour.</p>
      ) : (
        <BenchResult run={run} runs={runs ?? []} onSel={setSel} />
      )}
    </section>
  );
}

function BenchResult({ run, runs, onSel }: { run: BenchRun; runs: BenchRun[]; onSel: (id: string) => void }) {
  const snapT = useStore((s) => s.snap?.t);
  const strategies = run.strategies.filter((s) => run.summary[s]);
  const data = strategies.map((s) => ({ s, label: STRATEGY_LABEL[s] ?? s, v: run.summary[s].violation_s, ...run.summary[s] }));
  const scen = run.scenarios;
  const cell = (sc: string, st: string) => run.results.find((r) => r.scenario === sc && r.strategy === st);
  const maxV = Math.max(1, ...run.results.map((r) => r.violation_s));
  return (
    <div className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[13px] text-ink-2">
          Run {ago(run.t, snapT)} · {(run.duration_s / 60).toFixed(0)} min · {scen.length} failures × {strategies.length} strategies · traffic{" "}
          {Object.entries(run.rates).map(([p, r]) => `${pairLabel(p)} ${r}`).join(", ")} Mbit/s
        </p>
        {runs.length > 1 && (
          <select className="field" value={run.id} onChange={(e) => onSel(e.target.value)} aria-label="Benchmark run">
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                {new Date(r.t * 1000).toLocaleString()}
              </option>
            ))}
          </select>
        )}
      </div>
      <div className="mt-3 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        <div>
          <p className="font-cond text-[13.5px] font-semibold text-ink-2">Weighted intent-seconds violated, all failures (lower is better)</p>
          <div className="h-[200px]">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data} layout="vertical" margin={{ top: 4, right: 56, bottom: 4, left: 8 }} barCategoryGap={10}>
                <CartesianGrid horizontal={false} stroke="#263240" />
                <XAxis type="number" tick={{ fill: "#848d97", fontSize: 12 }} axisLine={{ stroke: "#2f3c4a" }} tickLine={false} />
                <YAxis type="category" dataKey="label" width={168} tick={{ fill: "#b4b9bf", fontSize: 12.5 }} axisLine={false} tickLine={false} />
                <Tooltip
                  cursor={{ fill: "rgba(255,255,255,0.04)" }}
                  contentStyle={{ background: "#121a23", border: "1px solid #3d4c5d", borderRadius: 6, fontSize: 12.5 }}
                  labelStyle={{ color: "#e6e4df", fontWeight: 600 }}
                  formatter={(v) => [`${v} weighted intent-seconds`, "violated"]}
                />
                <Bar dataKey="v" radius={[0, 4, 4, 0]} barSize={18}>
                  {data.map((d) => (
                    <Cell key={d.s} fill={d.s === "intent" ? "#e6e4df" : "#5d7186"} />
                  ))}
                  <LabelList dataKey="v" position="right" fill="#b4b9bf" fontSize={12.5} />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
        <table className="data self-start">
          <thead>
            <tr>
              <th>Strategy</th>
              <th className="text-right" title="weighted intent-seconds violated after the failures">Violated</th>
              <th className="text-right" title="of which: intents that held before the failure">New</th>
              <th className="text-right" title="mean seconds until every intent that held before holds again">Recovers in</th>
              <th className="text-right">Route changes</th>
              <th className="text-right">Delivered</th>
              <th className="text-right" title="the failure analysis' prediction of which intents break, made just before each failure">Predicted right</th>
            </tr>
          </thead>
          <tbody>
            {data.map((d) => (
              <tr key={d.s} className={d.s === "intent" ? "bg-[#243142]" : ""}>
                <td className="whitespace-nowrap">{d.label}</td>
                <td className="text-right font-semibold">{d.violation_s}</td>
                <td className="text-right">{d.new_violation_s}</td>
                <td className="text-right">
                  {d.mean_time_to_compliance_s == null ? "–" : `${d.mean_time_to_compliance_s.toFixed(1)} s`}
                  {d.never_compliant > 0 && <span className="hint"> ({d.never_compliant}× never)</span>}
                </td>
                <td className="text-right">{d.route_changes}</td>
                <td className="text-right">{d.delivered_pct.toFixed(2)}%</td>
                <td className="text-right">{fmtPct(d.prediction_accuracy_pct, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-4 font-cond text-[13.5px] font-semibold text-ink-2">Per failure: weighted intent-seconds violated</p>
      <div className="table-scroll">
        <table className="data mt-1">
          <thead>
            <tr>
              <th>Failure</th>
              {strategies.map((s) => (
                <th key={s} className="text-right">
                  {STRATEGY_LABEL[s]}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {scen.map((sc) => (
              <tr key={sc}>
                <td className="whitespace-nowrap text-ink-2">{run.results.find((r) => r.scenario === sc)?.label ?? sc}</td>
                {strategies.map((st) => {
                  const r = cell(sc, st);
                  const k = r ? r.violation_s / maxV : 0;
                  return (
                    <td key={st} className="text-right" style={{ background: r && r.violation_s > 0 ? `rgba(255,122,89,${0.08 + 0.42 * k})` : undefined }}>
                      {r ? (
                        <span title={`${r.label}: ${r.violation_s} violated (new ${r.new_violation_s}); ${r.route_changes} route changes; lost ${r.lost_mbit} Mbit; measured broken ${r.measured_broken.join(", ") || "none"}; predicted ${r.predicted_broken?.join(", ") || "none"}`}>
                          {r.violation_s}
                          {r.prediction_match === false && <span className="text-[#ffd27a]"> *</span>}
                        </span>
                      ) : (
                        "–"
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint mt-2">* the measured outcome differed from the failure analysis' prediction for that strategy. Hover a cell for details.</p>
    </div>
  );
}
