import { useEffect, useMemo, useState } from "react";
import ChangeBuilder, { changeLabel } from "../components/ChangeBuilder";
import { useLiveVis } from "../components/liveVis";
import TopologyView, { FlowLegend, type LinkVis, type StrandVis } from "../components/TopologyView";
import { api } from "../lib/api";
import { flowStyle } from "../lib/colors";
import { ago, corePath, mbps, ms, pairLabel, pct } from "../lib/format";
import { act, useStore } from "../lib/store";
import type { Calibration, Change, Health, Prediction, SimResult } from "../lib/types";

function simVis(res: SimResult) {
  const links: Record<string, LinkVis> = {};
  const nodes: Record<string, { health: Health }> = {};
  const strands: StrandVis[] = [];
  for (const [id, l] of Object.entries(res.links)) {
    const health: Health = !l.up ? "down" : l.loss_pct >= 1 || l.util >= 0.85 ? "degraded" : "ok";
    links[id] = { util: l.util, health, rtt: l.latency_ms * 2, expected: l.cfg.delay_ms * 2, admin_up: l.up };
  }
  for (const [p, f] of Object.entries(res.pairs)) strands.push({ pair: p, path: f.path, offered_mbps: f.offered_mbps });
  return { links, nodes, strands };
}

export default function SimulatePage() {
  const topology = useStore((s) => s.topology)!;
  const pairs = useStore((s) => s.pairs);
  const snap = useStore((s) => s.snap)!;
  const live = useLiveVis(snap);
  const [changes, setChanges] = useState<Change[]>([{ kind: "link_latency", target: "r2-r5", params: { add_ms: 40, jitter_ms: 0 } }]);
  const [mode, setMode] = useState<"current" | "static" | "adaptive">("current");
  const [duration, setDuration] = useState(10);
  const [busy, setBusy] = useState(false);
  const [pred, setPred] = useState<Prediction | null>(null);
  const [history, setHistory] = useState<Prediction[]>([]);
  const [cal, setCal] = useState<Calibration | null>(null);

  const refresh = () => {
    void api.get<Calibration>("/api/sim/calibration").then(setCal).catch(() => {});
    void api.get<Prediction[]>("/api/sim/predictions").then((h) => {
      setHistory(h);
      setPred((p) => p ?? h[0] ?? null);
    }).catch(() => {});
  };
  useEffect(refresh, []);

  // node health in the twin view: a node is down when every one of its links is down
  const sim = useMemo(() => {
    if (!pred) return null;
    const v = simVis(pred.result);
    for (const n of topology.nodes) {
      const ls = topology.links.filter((l) => l.a === n.id || l.b === n.id).map((l) => v.links[l.id]?.health);
      v.nodes[n.id] = { health: ls.length && ls.every((h) => h === "down") ? "down" : ls.some((h) => h === "down" || h === "degraded") ? "degraded" : "ok" };
    }
    return v;
  }, [pred, topology]);

  const run = async () => {
    setBusy(true);
    const r = await act(() =>
      api.post<Prediction>("/api/sim/predict", {
        changes,
        duration_s: duration,
        routing_mode: mode === "current" ? null : mode,
        include_baseline: true,
      }),
    );
    setBusy(false);
    if (r) {
      setPred(r);
      refresh();
    }
  };

  const validate = async () => {
    const r = await act(() => api.post("/api/validation/run", { changes, measure_s: 15 }), "Validation started: the same change is now being applied to the live network");
    if (r) location.hash = "#validation";
  };

  return (
    <div className="grid min-h-full grid-cols-[380px_minmax(0,1fr)] gap-3 p-3">
      <aside className="flex flex-col gap-3">
        <section className="panel p-4">
          <div className="flex items-center justify-between">
            <h1 className="panel-title">What-if scenario</h1>
            <span className="sim-tag">Simulation only</span>
          </div>
          <p className="hint mt-1">Changes here are applied to the SimPy twin, never to the live network, until you choose to validate them.</p>
          <div className="mt-3">
            <ChangeBuilder changes={changes} onChange={setChanges} />
          </div>
          <div className="mt-4 grid grid-cols-2 gap-3 text-[13px]">
            <label className="flex flex-col gap-1">
              <span className="text-ink-3">Routing in the twin</span>
              <select className="field" value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
                <option value="current">Same as live ({snap.routing.mode})</option>
                <option value="static">Static (Dijkstra)</option>
                <option value="adaptive">Adaptive (scored)</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-ink-3">Simulated time</span>
              <select className="field" value={duration} onChange={(e) => setDuration(+e.target.value)}>
                {[6, 10, 14, 20].map((d) => (
                  <option key={d} value={d}>
                    {d} s
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="mt-4 flex gap-2">
            <button className="btn btn-primary flex-1 justify-center" disabled={busy} onClick={run}>
              {busy ? "Simulating…" : "Predict"}
            </button>
            <button className="btn flex-1 justify-center" disabled={busy || !changes.length || snap.chaos.active.length > 0} onClick={validate} title="Predict, apply the same change live, measure, revert and compare">
              Validate live
            </button>
          </div>
          {snap.chaos.active.length > 0 && <p className="hint mt-2">Revert the active live faults first to run a validation from a clean state.</p>}
        </section>

        <CalibrationPanel cal={cal} onCalibrated={setCal} />

        <section className="panel p-4">
          <h2 className="panel-title">Earlier predictions</h2>
          <ul className="mt-2 space-y-1 text-[13px]">
            {history.slice(0, 10).map((h) => (
              <li key={h.id}>
                <button className={`w-full rounded px-2 py-1 text-left hover:bg-raised ${pred?.id === h.id ? "bg-raised text-ink" : "text-ink-2"}`} onClick={() => setPred(h)}>
                  {h.label}
                  <span className="hint ml-2">
                    {h.mode}, {ago(h.t, snap.t)}
                  </span>
                </button>
              </li>
            ))}
            {history.length === 0 && <li className="hint">None yet.</li>}
          </ul>
        </section>
      </aside>

      <div className="flex min-w-0 flex-col gap-3">
        <div className="grid grid-cols-2 gap-3">
          <section className="panel flex h-[400px] flex-col">
            <div className="flex items-center justify-between border-b border-line px-4 py-2">
              <span className="live-tag">Live, measured now</span>
              <span className="hint">real Mininet counters and probes</span>
            </div>
            <TopologyView className="flex-1" topology={topology} pairs={pairs} links={live.links} nodes={live.nodes} strands={live.strands} packets={false} />
          </section>
          <section className="panel flex h-[400px] flex-col border-dashed border-sim/50 bg-[#1d2a38]">
            <div className="flex items-center justify-between border-b border-dashed border-sim/40 px-4 py-2">
              <span className="sim-tag">Simulation: {pred ? pred.label : "no prediction yet"}</span>
              {pred && <span className="hint">{pred.mode} routing, {pred.duration_s} s simulated in {pred.wall_s.toFixed(1)} s</span>}
            </div>
            {sim ? (
              <TopologyView className="flex-1" topology={topology} pairs={pairs} links={sim.links} nodes={sim.nodes} strands={sim.strands} packets={false} variant="sim" />
            ) : (
              <div className="hint flex flex-1 items-center justify-center p-6 text-center">Build a scenario on the left and press Predict to see the twin's forecast here.</div>
            )}
          </section>
        </div>
        <div className="flex justify-between px-1">
          <FlowLegend pairs={pairs} strands={live.strands} />
          <span className="hint">Link brightness and width: utilisation. Dashed red: down. Amber ring: degraded.</span>
        </div>
        {pred && <Comparison pred={pred} pairs={pairs} />}
      </div>
    </div>
  );
}

function CalibrationPanel({ cal, onCalibrated }: { cal: Calibration | null; onCalibrated: (c: Calibration) => void }) {
  const snapT = useStore((s) => s.snap?.t);
  const [busy, setBusy] = useState(false);
  return (
    <section className="panel p-4">
      <div className="flex items-center justify-between">
        <h2 className="panel-title">Twin calibration</h2>
        <button
          className="btn btn-sm"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            const r = await act(() => api.post<Calibration>("/api/sim/calibrate"), "Twin re-calibrated from the last 15 s of live probes");
            setBusy(false);
            if (r) onCalibrated(r);
          }}
        >
          {busy ? "Calibrating…" : "Calibrate now"}
        </button>
      </div>
      {cal?.t ? (
        <>
          <dl className="kv mt-2">
            <dt>Calibrated</dt>
            <dd>
              {ago(cal.t, snapT)}
              {cal.conditions?.offered_mbps ? `, under ${cal.conditions.offered_mbps} Mbit/s` : ", idle network"}
            </dd>
            <dt>Endpoint overhead E</dt>
            <dd>{ms(cal.endpoint_ms, 3)}</dd>
            <dt>Per-hop overhead H</dt>
            <dd>{ms(cal.per_hop_ms, 3)}</dd>
            <dt>Fit error (RMS)</dt>
            <dd>{ms(cal.fit_rms_ms, 3)}</dd>
            <dt>Jitter p5 … p95</dt>
            <dd>
              {cal.noise_p05_ms?.toFixed(2)} … {cal.noise_p95_ms?.toFixed(2)} ms
            </dd>
          </dl>
          <p className="hint mt-2">
            Fitted from {cal.n_streams} live probe streams: measured RTT minus the twin's zero-overhead RTT = E + H × 2 × links.
          </p>
        </>
      ) : (
        <p className="hint mt-1">Not calibrated yet. It happens automatically 15 s after start-up, or press Calibrate now.</p>
      )}
    </section>
  );
}

function Comparison({ pred, pairs }: { pred: Prediction; pairs: string[] }) {
  const rows: { key: keyof Prediction["result"]["pairs"][string]; label: string; fmt: (v: number | null) => string }[] = [
    { key: "rtt_p50", label: "RTT p50", fmt: (v) => ms(v, 2) },
    { key: "rtt_p95", label: "RTT p95", fmt: (v) => ms(v, 2) },
    { key: "rtt_p99", label: "RTT p99", fmt: (v) => ms(v, 2) },
    { key: "probe_loss_pct", label: "Probe loss", fmt: (v) => pct(v, 1) },
    { key: "rx_mbps", label: "Throughput", fmt: (v) => mbps(v) },
    { key: "loss_pct", label: "Data loss", fmt: (v) => pct(v, 2) },
  ];
  return (
    <section className="panel p-4">
      <div className="flex items-baseline justify-between">
        <h2 className="panel-title">Live now vs twin forecast</h2>
        <span className="hint">“Twin, unchanged” checks the twin against the live network; “Twin, what-if” is the forecast for your changes.</span>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-x-6 gap-y-4 xl:grid-cols-2">
        {pairs.map((p) => {
          const lv = pred.live.pairs[p];
          const base = pred.baseline?.pairs[p];
          const wi = pred.result.pairs[p];
          const st = flowStyle(pairs, p);
          return (
            <div key={p}>
              <h3 className="flex items-center gap-2 text-[13.5px] font-semibold">
                <span className="h-2 w-2 rounded-full" style={{ background: st.hex }} />
                {pairLabel(p)}
                {wi.rerouted && <span className="text-[12px] font-normal text-sim">twin re-routes ({wi.reason})</span>}
              </h3>
              <table className="data mt-1">
                <thead>
                  <tr>
                    <th />
                    <th className="text-right">Live now</th>
                    <th className="text-right">Twin, unchanged</th>
                    <th className="text-right">Twin, what-if</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    if ((r.key === "rx_mbps" || r.key === "loss_pct") && !wi.offered_mbps && !lv?.offered_mbps) return null;
                    const lval = lv ? (lv[r.key as keyof typeof lv] as number | null) : null;
                    return (
                      <tr key={r.key}>
                        <td className="text-ink-3">{r.label}</td>
                        <td className="text-right">{r.fmt(lval)}</td>
                        <td className="text-right text-ink-2">{base ? r.fmt(base[r.key] as number | null) : "–"}</td>
                        <td className="text-right font-semibold text-sim">{r.fmt(wi[r.key] as number | null)}</td>
                      </tr>
                    );
                  })}
                  <tr>
                    <td className="text-ink-3">Path</td>
                    <td className="text-right">{corePath(lv?.path)}</td>
                    <td className="text-right text-ink-2">{corePath(base?.path)}</td>
                    <td className="text-right text-sim">{corePath(wi.path)}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          );
        })}
      </div>
      {pred.changes.length > 0 && <p className="hint mt-3">Scenario: {pred.changes.map(changeLabel).join("; ")}</p>}
    </section>
  );
}
