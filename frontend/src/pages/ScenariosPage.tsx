import { useEffect, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../lib/api";
import { flowStyle } from "../lib/colors";
import { ago, pairLabel } from "../lib/format";
import { act, useStore } from "../lib/store";
import type { HistorySample, NvEvent, ScenarioSummary } from "../lib/types";

interface ScenarioDoc {
  name: string;
  recorded_at: number;
  duration_s: number;
  actions: { t: number; action: string; label?: string }[];
  history: HistorySample[];
  events: NvEvent[];
}

export default function ScenariosPage() {
  const snap = useStore((s) => s.snap)!;
  const pairs = useStore((s) => s.pairs);
  const rec = snap.jobs?.recorder as { recording?: boolean; name?: string; since?: number; actions?: number } | undefined;
  const rep = snap.jobs?.replayer as { running?: boolean; name?: string; index?: number; total?: number; current?: string } | undefined;
  const demo = snap.jobs?.demo as { running?: boolean; steps?: string[]; index?: number; notes?: string[] } | undefined;
  const [list, setList] = useState<ScenarioSummary[]>([]);
  const [name, setName] = useState("my-experiment");
  const [doc, setDoc] = useState<ScenarioDoc | null>(null);

  const load = () => void api.get<ScenarioSummary[]>("/api/scenarios").then(setList).catch(() => {});
  useEffect(load, [rec?.recording, rep?.running]);

  return (
    <div className="mx-auto grid max-w-[1500px] gap-3 p-3 lg:grid-cols-[440px_minmax(0,1fr)]">
      <div className="flex flex-col gap-3">
        <section className="panel p-4">
          <h1 className="panel-title">Demo script</h1>
          <p className="hint mt-1">One button, about 45 seconds, all on the live network. The targets are chosen from whatever path the main flow is using at that moment.</p>
          <ol className="mt-3 space-y-1 text-[13px]">
            {(demo?.steps ?? []).map((s, i) => (
              <li key={s} className={demo?.running && demo.index === i ? "font-semibold text-ink" : "text-ink-2"}>
                <span className="num mr-2 text-ink-3">{i + 1}.</span>
                {s}
              </li>
            ))}
          </ol>
          <div className="mt-3 flex gap-2">
            <button className="btn btn-primary" disabled={demo?.running} onClick={() => act(() => api.post("/api/demo/start"), "Demo started; open the Live page to watch")}>
              Run demo
            </button>
            <button className="btn" disabled={!demo?.running} onClick={() => act(() => api.post("/api/demo/stop"))}>
              Stop
            </button>
          </div>
          {demo?.notes && demo.notes.length > 0 && (
            <ul className="mt-3 space-y-1 border-t border-line pt-2 text-[13px] text-ink-2">
              {demo.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel p-4">
          <h2 className="panel-title">Record a scenario</h2>
          <p className="hint mt-1">Everything you do while recording (faults, reverts, traffic, routing changes) is saved with its timing, together with the measured metrics.</p>
          {rec?.recording ? (
            <div className="mt-3 flex items-center justify-between rounded border border-[#7a3434] bg-[#3a2326] px-3 py-2 text-[13px]">
              <span>
                Recording “{rec.name}”, {rec.actions} actions, started {ago(rec.since, snap.t)}
              </span>
              <button className="btn btn-sm" onClick={() => act(() => api.post("/api/scenarios/record/stop"), "Scenario saved")}>
                Stop and save
              </button>
            </div>
          ) : (
            <div className="mt-3 flex gap-2">
              <input className="field flex-1" value={name} onChange={(e) => setName(e.target.value)} aria-label="Scenario name" />
              <button className="btn" onClick={() => act(() => api.post("/api/scenarios/record/start", { name }), "Recording; go to the Live page and inject faults")}>
                Start recording
              </button>
            </div>
          )}
        </section>

        <section className="panel p-4">
          <h2 className="panel-title">Saved scenarios</h2>
          {rep?.running && (
            <div className="mt-2 flex items-center justify-between rounded border border-line-strong bg-raised px-3 py-2 text-[13px]">
              <span>
                Replaying “{rep.name}”: step {rep.index}/{rep.total}
                {rep.current ? `, ${rep.current}` : ""}
              </span>
              <button className="btn btn-sm" onClick={() => act(() => api.post("/api/replay/stop"))}>
                Stop
              </button>
            </div>
          )}
          <ul className="mt-2 space-y-1.5">
            {list.map((s) => (
              <li key={s.file} className="flex items-center justify-between gap-2 rounded border border-line px-3 py-2 text-[13px]">
                <button className="text-left" onClick={() => api.get<ScenarioDoc>(`/api/scenarios/${encodeURIComponent(s.file)}`).then(setDoc)}>
                  <div className="font-semibold">{s.name}</div>
                  <div className="hint">
                    {s.actions} actions over {s.duration_s.toFixed(0)} s, recorded {ago(s.recorded_at, snap.t)}
                  </div>
                </button>
                <div className="flex gap-1.5">
                  <button className="btn btn-sm" disabled={rep?.running || rec?.recording} onClick={() => act(() => api.post(`/api/scenarios/${encodeURIComponent(s.file)}/replay`), `Replaying ${s.name} on the live network`)}>
                    Replay live
                  </button>
                  <button
                    className="btn btn-sm"
                    onClick={async () => {
                      if (!confirm(`Delete scenario "${s.name}"?`)) return;
                      await act(() => api.del(`/api/scenarios/${encodeURIComponent(s.file)}`));
                      load();
                    }}
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
            {list.length === 0 && <li className="hint">No scenarios yet. Record one above.</li>}
          </ul>
        </section>
      </div>

      <section className="panel p-4">
        {doc ? <Recording doc={doc} pairs={pairs} /> : <p className="hint">Pick a saved scenario to see its timeline and the metrics measured while it was recorded.</p>}
      </section>
    </div>
  );
}

function Recording({ doc, pairs }: { doc: ScenarioDoc; pairs: string[] }) {
  const t0 = doc.recorded_at;
  const rows = doc.history.map((h) => {
    const r: Record<string, number | null> = { t: h.t - t0 };
    for (const p of pairs) r[p] = h.flows[p]?.rtt_p95 ?? null;
    return r;
  });
  return (
    <div>
      <div className="flex items-center justify-between">
        <h2 className="panel-title">{doc.name}</h2>
        <span className="live-tag">Recorded from the live network</span>
      </div>
      <div className="mt-3 h-[260px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="#2f3c4a" vertical={false} />
            <XAxis dataKey="t" type="number" domain={[0, "dataMax"]} tickFormatter={(t) => `${Math.round(Number(t))} s`} stroke="#848d97" tick={{ fontSize: 12 }} tickLine={false} />
            <YAxis stroke="#848d97" tick={{ fontSize: 12 }} tickLine={false} unit=" ms" width={62} />
            <Tooltip contentStyle={{ background: "#18212b", border: "1px solid #3d4c5d", borderRadius: 4, fontSize: 12.5 }} labelFormatter={(t) => `t = ${Number(t).toFixed(0)} s`} formatter={(v, n) => [typeof v === "number" ? `${v.toFixed(2)} ms` : "–", n]} />
            <Legend wrapperStyle={{ fontSize: 12.5 }} />
            {pairs.map((p) => {
              const st = flowStyle(pairs, p);
              return <Line key={p} dataKey={p} name={`${pairLabel(p)} RTT p95`} stroke={st.hex} strokeDasharray={st.dash.join(" ")} strokeWidth={2} dot={false} isAnimationActive={false} />;
            })}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <h3 className="mt-4 font-cond text-[14px] font-semibold">Recorded actions</h3>
      <table className="data mt-1">
        <thead>
          <tr>
            <th className="text-right">At</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {doc.actions.map((a, i) => (
            <tr key={i}>
              <td className="num text-right text-ink-3">{a.t.toFixed(1)} s</td>
              <td>{a.label ?? a.action}</td>
            </tr>
          ))}
          {doc.actions.length === 0 && (
            <tr>
              <td />
              <td className="hint">No actions were recorded.</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
