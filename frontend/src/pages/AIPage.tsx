import { useEffect, useMemo, useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import AIInsights, { fmtSignal } from "../components/AIInsights";
import { SetupHelp, Spark } from "../components/Copilot";
import { api } from "../lib/api";
import { useCopilot } from "../lib/copilot";
import { STATUS } from "../lib/colors";
import { ago, clock } from "../lib/format";
import { act, useStore } from "../lib/store";
import type { Anomaly, CopilotStatus, EvalRow, EvalRun, SignalPoint, SignalRow } from "../lib/types";

const KIND_LABEL: Record<SignalRow["kind"], string> = {
  link: "Links: router-to-router probes, and the load on every cable",
  access: "Access segments (host to gateway probes)",
  flow: "End-to-end flows (client to server probes, iperf3 receivers)",
};

export default function AIPage() {
  const [signals, setSignals] = useState<SignalRow[]>([]);
  const [sel, setSel] = useState<string>("link:r2-r5:rtt");
  const [recent, setRecent] = useState<Anomaly[]>([]);
  const setOpen = useCopilot((s) => s.setOpen);

  useEffect(() => {
    let alive = true;
    const load = () => {
      void api.get<SignalRow[]>("/api/ai/signals").then((r) => alive && setSignals(r)).catch(() => {});
      void api.get<{ recent: Anomaly[] }>("/api/ai/insights").then((r) => alive && setRecent(r.recent)).catch(() => {});
    };
    load();
    const id = setInterval(load, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    if (signals.length && !signals.some((s) => s.signal === sel)) setSel(signals[0].signal);
  }, [signals, sel]);

  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-3 p-3">
      <section className="panel p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-3xl">
            <h1 className="font-cond text-[22px] font-semibold">AI operations</h1>
            <p className="mt-1 text-ink-2">
              Three layers work on the live measurements. <strong className="text-ink">Learned baselines</strong> give each of the signals below its own normal,
              and flag sustained deviations that fixed thresholds miss. <strong className="text-ink">Probe-path diagnosis</strong> finds the one link or router
              that explains every bad and every healthy probe at once. The <strong className="text-ink">copilot</strong> answers questions from the same data
              through tools, and only proposes changes for you to apply.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button className="btn btn-primary" onClick={() => setOpen(true)}>
              <Spark size={12} /> Open copilot
            </button>
            <button
              className="btn"
              title="Forget every learned baseline and learn again for 20 s (after a deliberate, permanent change)"
              onClick={() => act(() => api.post("/api/ai/relearn"), "Baselines reset: learning normal behaviour again")}
            >
              Re-learn baselines
            </button>
          </div>
        </div>
      </section>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_420px]">
        <div className="flex min-w-0 flex-col gap-3">
          <SignalChart id={sel} row={signals.find((s) => s.signal === sel)} />
          <SignalTable rows={signals} sel={sel} onSel={setSel} />
        </div>
        <div className="flex min-w-0 flex-col gap-3">
          <AIInsights />
          <CopilotCard />
          <RecentAnomalies items={recent} />
        </div>
      </div>

      <Evaluation />
    </div>
  );
}

// ------------------------------------------------------------------ signal chart
function SignalChart({ id, row }: { id: string; row?: SignalRow }) {
  const [pts, setPts] = useState<SignalPoint[]>([]);
  useEffect(() => {
    let alive = true;
    const load = () =>
      void api
        .get<SignalPoint[]>(`/api/ai/signal?id=${encodeURIComponent(id)}&seconds=300`)
        .then((r) => alive && setPts(r))
        .catch(() => {});
    load();
    const t = setInterval(load, 2000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [id]);
  const unit = row?.unit ?? "ms";
  const scale = unit === "ratio" ? 100 : 1;
  const data = useMemo(() => {
    const end = pts.length ? pts[pts.length - 1].t : 0;
    return pts.map((p) => ({
      s: Math.round(p.t - end),
      value: p.value == null ? null : p.value * scale,
      mean: p.normal_mean == null ? null : p.normal_mean * scale,
      upper: p.normal_upper == null ? null : p.normal_upper * scale,
      anomalous: p.anomalous,
    }));
  }, [pts, scale]);
  const suffix = unit === "ms" ? " ms" : "%";
  return (
    <section className="panel p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="panel-title">{row ? row.label : id}: measured vs learned normal</h2>
        <span className="hint">last 5 minutes, 1 sample/s. Shaded: the range it may move in before the detector counts it as unusual</span>
      </div>
      <div className="mt-2 h-[260px]">
        {data.length === 0 ? (
          <p className="hint pt-10 text-center">Collecting samples…</p>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="#2a3644" vertical={false} />
              <XAxis dataKey="s" type="number" allowDecimals={false} domain={["dataMin", 0]} tick={{ fill: "#848d97", fontSize: 11 }} tickFormatter={(v) => `${v} s`} stroke="#3d4c5d" />
              <YAxis tick={{ fill: "#848d97", fontSize: 11 }} stroke="#3d4c5d" width={54} tickFormatter={(v) => `${Number(v).toFixed(unit === "ms" ? 1 : 0)}${suffix}`} />
              <Tooltip
                contentStyle={{ background: "#121a23", border: "1px solid #3d4c5d", fontSize: 12 }}
                labelFormatter={(v) => `${v} s`}
                formatter={(v, name) => [v == null ? "–" : `${Number(v).toFixed(2)}${suffix}`, name]}
              />
              <Area type="stepAfter" dataKey="upper" name="Alarm threshold (learned)" stroke="#5b6b7c" strokeDasharray="3 3" fill="#2b3a4b" fillOpacity={0.55} isAnimationActive={false} connectNulls />
              <Line type="monotone" dataKey="mean" name="Learned normal" stroke="#848d97" strokeDasharray="6 4" dot={false} strokeWidth={1.5} isAnimationActive={false} connectNulls />
              <Line
                type="monotone"
                dataKey="value"
                name="Measured"
                stroke="#e6e4df"
                strokeWidth={2}
                isAnimationActive={false}
                dot={(p: { cx?: number; cy?: number; payload?: { anomalous: boolean }; index?: number }) =>
                  p.payload?.anomalous && p.cx != null && p.cy != null ? (
                    <circle key={p.index} cx={p.cx} cy={p.cy} r={2.6} fill={STATUS.degraded} />
                  ) : (
                    <g key={p.index} />
                  )
                }
              />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-5 gap-y-1 text-[12px] text-ink-3">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-0.5 w-5 bg-ink" /> measured
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-0 w-5 border-t-2 border-dashed border-ink-3" /> learned normal
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-5 bg-[#2b3a4b]" /> allowed range
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: STATUS.degraded }} /> counted as unusual
        </span>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ signals table
function SignalTable({ rows, sel, onSel }: { rows: SignalRow[]; sel: string; onSel: (id: string) => void }) {
  const groups = (["link", "access", "flow"] as const).map((k) => [k, rows.filter((r) => r.kind === k)] as const);
  return (
    <section className="panel px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="panel-title">Watched signals</h2>
        <span className="hint">each learns its own normal (EWMA mean and spread); click one to chart it</span>
      </div>
      <div className="table-scroll mt-2 max-h-[520px] overflow-y-auto">
        <table className="data">
          <thead className="sticky top-0 bg-panel">
            <tr>
              <th>Signal</th>
              <th className="text-right">Now</th>
              <th className="text-right">Learned normal</th>
              <th className="text-right" title="the detector counts the signal as unusual above this value (z = 4)">Unusual above</th>
              <th className="text-right" title="(value − normal) ÷ learned spread">z</th>
              <th>State</th>
            </tr>
          </thead>
          {groups.map(([k, items]) =>
            items.length ? (
              <tbody key={k}>
                <tr>
                  <td colSpan={6} className="!pt-3 font-cond text-[12.5px] font-semibold text-ink-3">
                    {KIND_LABEL[k]}
                  </td>
                </tr>
                {items.map((r) => (
                  <tr
                    key={r.signal}
                    onClick={() => onSel(r.signal)}
                    className={`cursor-pointer ${r.signal === sel ? "bg-raised" : "hover:bg-[#243140]"}`}
                    aria-selected={r.signal === sel}
                  >
                    <td className="whitespace-nowrap">{r.label}</td>
                    <td className="text-right">{fmtSignal(r.value, r.unit)}</td>
                    <td className="text-right text-ink-2">{fmtSignal(r.normal_mean, r.unit)}</td>
                    <td className="text-right text-ink-3">{fmtSignal(r.normal_upper, r.unit)}</td>
                    <td className="text-right text-ink-2">{r.z == null ? "–" : r.z.toFixed(1)}</td>
                    <td className="whitespace-nowrap">
                      <StateDot state={r.state} />
                    </td>
                  </tr>
                ))}
              </tbody>
            ) : null,
          )}
        </table>
      </div>
    </section>
  );
}

function StateDot({ state }: { state: SignalRow["state"] }) {
  const color = state === "anomalous" ? STATUS.degraded : state === "normal" ? STATUS.ok : STATUS.unknown;
  return (
    <span className="inline-flex items-center gap-1.5 text-[12.5px] text-ink-2">
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {state === "anomalous" ? "unusual" : state === "learning" ? "learning" : "normal"}
    </span>
  );
}

// ------------------------------------------------------------------ copilot card
function CopilotCard() {
  const [st, setSt] = useState<CopilotStatus | null>(null);
  const [checking, setChecking] = useState(false);
  useEffect(() => {
    void api.get<{ copilot: CopilotStatus }>("/api/ai/status").then((r) => setSt(r.copilot)).catch(() => {});
  }, []);
  const recheck = async () => {
    setChecking(true);
    const r = await act(() => api.post<CopilotStatus>("/api/ai/copilot/refresh"));
    setChecking(false);
    if (r) setSt(r);
  };
  const count = (k: string) => st?.tools.filter((t) => t.kind === k && (!st.local || t.local)).length ?? 0;
  return (
    <section className="panel p-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="panel-title">Copilot</h2>
        <button className="btn btn-sm" onClick={recheck} disabled={checking}>
          {checking ? "Checking…" : "Check again"}
        </button>
      </div>
      {!st ? (
        <p className="hint mt-1">Loading…</p>
      ) : st.available ? (
        <>
          <dl className="kv mt-2">
            <dt>Model</dt>
            <dd>{st.label}</dd>
            <dt>Connection</dt>
            <dd>
              {st.provider === "anthropic"
                ? "Anthropic API"
                : st.provider === "gemini"
                  ? "Google Gemini API"
                  : st.provider === "groq"
                    ? "Groq API"
                    : st.transport === "win-interop"
                      ? "Ollama on Windows, via WSL interop"
                      : "Ollama over HTTP"}
              {st.backup ? `; if it is rate-limited or down: ${st.backup}` : ""}
            </dd>
            {st.reason && (
              <>
                <dt>Note</dt>
                <dd className="text-[#ffd27a]">{st.reason}</dd>
              </>
            )}
            <dt>Tools</dt>
            <dd>
              {count("read")} read, {count("simulate")} simulate, {count("propose")} propose
            </dd>
          </dl>
          <p className="hint mt-2">
            Read tools see live probes, counters, the controller and the event log. The twin tool is labelled simulation. Propose tools only create a card you
            approve; the model has no tool that changes the network.
            {st.local ? " This is a local model running on the CPU; answers take one to four minutes." : ""}
          </p>
        </>
      ) : (
        <div className="mt-2">
          <p className="mb-3 text-[13px] text-[#ffb3ab]">{st.reason}</p>
          <SetupHelp />
        </div>
      )}
    </section>
  );
}

function RecentAnomalies({ items }: { items: Anomaly[] }) {
  const now = useStore((s) => s.snap?.t);
  return (
    <section className="panel p-4">
      <h2 className="panel-title">Recently cleared</h2>
      {items.length === 0 ? (
        <p className="hint mt-1">Anomalies that came and went are listed here with their peak and duration.</p>
      ) : (
        <ul className="mt-2 divide-y divide-line">
          {items.slice(0, 10).map((a) => (
            <li key={a.id} className="py-1.5 text-[12.5px]">
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-ink-2">{a.label}</span>
                <span className="hint whitespace-nowrap">{ago(a.t_end, now)}</span>
              </div>
              <div className="num text-ink-3">
                peaked at <span className="text-ink-2">{fmtSignal(a.peak_value, a.unit)}</span> against {fmtSignal(a.normal_mean, a.unit)}, lasted{" "}
                {a.t_end ? `${(a.t_end - a.t_start).toFixed(0)} s` : "–"}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ evaluation
function Evaluation() {
  const status = useStore((s) => s.snap?.jobs?.ai) as
    | { running?: boolean; index?: number; total?: number; label?: string; step?: string; step_ends_at?: number }
    | undefined;
  const now = useStore((s) => s.snap?.t ?? Date.now() / 1000);
  const [runs, setRuns] = useState<EvalRun[]>([]);
  useEffect(() => {
    void api.get<EvalRun[]>("/api/ai/evaluations").then(setRuns).catch(() => {});
  }, [status?.running, status?.index]);
  const run = runs[0];
  return (
    <section className="panel p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-3xl">
          <h2 className="font-cond text-[20px] font-semibold">Does the AI catch what fixed thresholds miss?</h2>
          <p className="mt-1 text-ink-2">
            The evaluation injects real faults through the chaos lab, from subtle (+2 ms, 1% loss) to hard (link and router down), with traffic running and static
            routing so each fault stays on the flows&apos; path. For each it times the learned detector against the threshold health rules, and scores the
            diagnosis against the injected fault, which the diagnosis itself never sees. A quiet minute first counts false alarms. About 5 minutes.
          </p>
        </div>
        {status?.running ? (
          <button className="btn btn-danger" onClick={() => act(() => api.post("/api/ai/evaluate/cancel"))}>
            Cancel
          </button>
        ) : (
          <button className="btn btn-primary" onClick={() => act(() => api.post("/api/ai/evaluate"), "AI evaluation started (about 5 minutes)")}>
            Run evaluation
          </button>
        )}
      </div>
      {status?.running && (
        <div className="mt-4 rounded border border-line-strong bg-raised px-4 py-2.5 text-[13.5px]">
          <span className="font-semibold">
            {status.index != null && status.index >= 0 && status.total ? `Fault ${status.index + 1} of ${status.total}: ` : ""}
            {status.label}
          </span>
          <span className="ml-3 text-ink-2">{status.step}</span>
          {status.step_ends_at && status.step_ends_at > now && <span className="num ml-2 text-ink-3">{(status.step_ends_at - now).toFixed(0)} s</span>}
        </div>
      )}
      {!run ? (
        !status?.running && <p className="hint mt-4">No evaluation yet. Run it once the network has been up for a minute.</p>
      ) : (
        <>
          <Comparison run={run} />
          <EvalTable rows={run.scenarios} />
          <p className="hint mt-2">
            Run {clock(run.t)}, {Math.round(run.duration_s / 60)} min.
            {run.quiet && run.quiet.ai_false_alarm_signals.length > 0 ? ` False alarms during the quiet minute: ${run.quiet.ai_false_alarm_signals.join(", ")}.` : ""}
            {runs.length > 1 ? ` ${runs.length - 1} earlier runs are kept in runs/ai_evaluations.jsonl.` : ""}
          </p>
        </>
      )}
    </section>
  );
}

function Comparison({ run }: { run: EvalRun }) {
  const s = run.summary;
  const rows: [string, string, string][] = [
    ["Faults detected", `${s.ai_detected} of ${s.faults}`, `${s.threshold_detected} of ${s.faults}`],
    ["Subtle faults detected", `${s.subtle_ai_detected} of ${s.subtle_faults}`, `${s.subtle_threshold_detected} of ${s.subtle_faults}`],
    ["Mean time to detect", s.ai_mean_detect_s == null ? "–" : `${s.ai_mean_detect_s.toFixed(1)} s`, s.threshold_mean_detect_s == null ? "–" : `${s.threshold_mean_detect_s.toFixed(1)} s`],
    ["False alarms, quiet minute", `${s.false_alarms ?? 0}`, `${s.threshold_false_alarms ?? 0}`],
    ["Colour flips while a fault was on", "n/a (hysteresis)", `${s.threshold_flaps_during_faults}`],
  ];
  return (
    <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
      <table className="data">
        <thead>
          <tr>
            <th />
            <th className="text-right">Learned detector</th>
            <th className="text-right">Threshold rules</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([k, a, b]) => (
            <tr key={k}>
              <td className="text-ink-2">{k}</td>
              <td className="text-right font-semibold">{a}</td>
              <td className="text-right">{b}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="rounded-md border border-dotted border-ink-3 p-4">
        <p className="text-[12.5px] text-ink-3">Root cause named correctly (element and fault type)</p>
        <p className="mt-1 font-cond text-[30px] leading-none font-semibold">
          {s.diagnosis_correct} <span className="text-[18px] text-ink-3">of {s.faults}</span>
        </p>
        <p className="mt-2 text-[12.5px] text-ink-2">
          Right element in {s.diagnosis_location_ok} of {s.faults}. Scored 4 s after the first alarm, from probes and counters only.
        </p>
      </div>
    </div>
  );
}

function EvalTable({ rows }: { rows: EvalRow[] }) {
  const max = Math.max(5, ...rows.flatMap((r) => [r.ai_detect_s ?? 0, r.threshold_detect_s ?? 0]));
  const bar = (v: number | null, strong: boolean) =>
    v == null ? (
      <span className="text-[#ff8a80]">missed</span>
    ) : (
      <span className="inline-flex items-center justify-end gap-2">
        <span className="num">{v.toFixed(1)} s</span>
        <span className="inline-block h-1.5 w-[70px] rounded-full bg-[#263240]">
          <span className={`block h-1.5 rounded-full ${strong ? "bg-ink" : "bg-ink-3"}`} style={{ width: `${Math.max(4, (v / max) * 100)}%` }} />
        </span>
      </span>
    );
  return (
    <div className="table-scroll mt-4">
      <table className="data">
        <thead>
          <tr>
            <th>Injected fault</th>
            <th className="text-right">Learned detector</th>
            <th className="text-right">Threshold rules</th>
            <th>Diagnosis 4 s after the first alarm</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <td className="whitespace-nowrap">
                {r.label}
                {r.ai_first_signal && <div className="hint">first: {r.ai_first_signal}</div>}
              </td>
              <td className="text-right">{bar(r.ai_detect_s, true)}</td>
              <td className="text-right">
                {bar(r.threshold_detect_s, false)}
                {r.threshold_flaps > 2 && <div className="hint">flipped {r.threshold_flaps} times</div>}
              </td>
              <td>
                {r.diagnosis ? (
                  <>
                    <span className={r.diagnosis_correct ? "text-ink" : "text-[#ffb3ab]"}>{r.diagnosis.title}</span>
                    <span className="hint ml-2">
                      {r.diagnosis_correct ? "correct" : r.diagnosis_location_ok ? "right element, wrong type" : "wrong"}, {r.diagnosis.confidence}
                    </span>
                  </>
                ) : (
                  <span className="text-[#ff8a80]">no diagnosis</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
