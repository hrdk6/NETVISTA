import { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { changeLabel } from "../components/ChangeBuilder";
import { api } from "../lib/api";
import { clock, num, pairLabel } from "../lib/format";
import { act, useStore } from "../lib/store";
import type { ValidationRow, ValidationRun } from "../lib/types";

const METRIC_LABEL: Record<string, string> = {
  rtt_p50: "RTT p50",
  rtt_p95: "RTT p95",
  rtt_p99: "RTT p99",
  probe_loss_pct: "Probe loss",
  rx_mbps: "Throughput",
  loss_pct: "Data loss",
  path: "Path",
};

function mean(xs: (number | null | undefined)[]) {
  const v = xs.filter((x): x is number => x != null && Number.isFinite(x));
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

export default function ValidationPage() {
  const status = useStore((s) => s.snap?.jobs?.validation) as
    | { running?: boolean; index?: number; total?: number; label?: string; step?: string; step_ends_at?: number; mode?: string }
    | undefined;
  const now = useStore((s) => s.snap?.t ?? Date.now() / 1000);
  const [runs, setRuns] = useState<ValidationRun[]>([]);
  const [open, setOpen] = useState<string | null>(null);

  const load = () => void api.get<ValidationRun[]>("/api/validation/runs").then(setRuns).catch(() => {});
  useEffect(load, []);
  useEffect(load, [status?.running, status?.index]);

  const agg = useMemo(
    () => ({
      p50: mean(runs.map((r) => r.summary.latency_p50_mape)),
      p95: mean(runs.map((r) => r.summary.latency_p95_mape)),
      thr: mean(runs.map((r) => r.summary.throughput_mape)),
      aggThr: mean(runs.map((r) => r.summary.aggregate_throughput_err)),
      loss: mean(runs.map((r) => r.summary.loss_mae_pp)),
      path: mean(runs.map((r) => r.summary.path_match_pct)),
    }),
    [runs],
  );

  const chart = useMemo(
    () =>
      [...runs].reverse().map((r, i) => ({
        name: `${i + 1}`,
        label: `${r.label} (${r.mode})`,
        p50: r.summary.latency_p50_mape,
        p95: r.summary.latency_p95_mape,
        thr: r.summary.throughput_mape,
        agg: r.summary.aggregate_throughput_err ?? null,
      })),
    [runs],
  );

  return (
    <div className="mx-auto flex max-w-[1500px] flex-col gap-3 p-3">
      <section className="panel p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-3xl">
            <h1 className="font-cond text-[22px] font-semibold">Does the twin predict the real network?</h1>
            <p className="mt-1 text-ink-2">
              Each run freezes a twin prediction first, then applies exactly the same change to the live Mininet network, waits for it to settle,
              measures probes and iperf3 for a window, reverts, and compares. Latency and throughput errors are relative,{" "}
              <span className="whitespace-nowrap">|predicted − measured| ÷ measured</span>; loss errors are absolute percentage points, because a
              relative error is meaningless when the true loss is near zero.
            </p>
          </div>
          <div className="flex gap-2">
            {status?.running ? (
              <button className="btn btn-danger" onClick={() => act(() => api.post("/api/validation/cancel"))}>
                Cancel
              </button>
            ) : (
              <button className="btn btn-primary" onClick={() => act(() => api.post("/api/validation/suite"), "Validation suite started (about 7 minutes)")}>
                Run validation suite
              </button>
            )}
          </div>
        </div>
        {status?.running && (
          <div className="mt-4 rounded border border-line-strong bg-raised px-4 py-2.5 text-[13.5px]">
            <span className="font-semibold">
              {status.total && status.total > 1 ? `Scenario ${(status.index ?? 0) + 1} of ${status.total}: ` : ""}
              {status.label}
            </span>
            {status.mode && <span className="text-ink-3"> ({status.mode} routing)</span>}
            <span className="ml-3 text-ink-2">{status.step}</span>
            {status.step_ends_at && status.step_ends_at > now && <span className="num ml-2 text-ink-3">{(status.step_ends_at - now).toFixed(0)} s</span>}
          </div>
        )}
      </section>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 2xl:grid-cols-6">
        <Stat label="Latency error, RTT p50" value={agg.p50} unit="%" />
        <Stat label="Latency error, RTT p95" value={agg.p95} unit="%" />
        <Stat label="Throughput error, per flow" value={agg.thr} unit="%" />
        <Stat label="Throughput error, bottleneck total" value={agg.aggThr} unit="%" />
        <Stat label="Loss error, per flow" value={agg.loss} unit=" pp" digits={2} />
        <Stat label="Paths predicted correctly" value={agg.path} unit="%" digits={0} />
      </div>

      {runs.length > 0 && (
        <div className="grid gap-3 lg:grid-cols-2">
          <ErrorChart
            title="Latency prediction error per run"
            data={chart}
            series={[
              { key: "p50", name: "RTT p50 error", color: "#3987e5" },
              { key: "p95", name: "RTT p95 error", color: "#199e70" },
            ]}
          />
          <ErrorChart
            title="Throughput prediction error per run"
            data={chart}
            series={[
              { key: "thr", name: "Per-flow throughput error", color: "#b05ec4" },
              { key: "agg", name: "Bottleneck total throughput error", color: "#a86b3c" },
            ]}
          />
        </div>
      )}

      <section className="panel p-4">
        <h2 className="panel-title">Runs</h2>
        {runs.length === 0 ? (
          <p className="hint mt-2">
            No validation runs yet. Run the suite above, or build a scenario on the What-if page and choose Validate live.
          </p>
        ) : (
          <div className="table-scroll">
          <table className="data mt-2">
            <thead>
              <tr>
                <th>#</th>
                <th>Scenario</th>
                <th>Routing</th>
                <th className="text-right">RTT p50 err</th>
                <th className="text-right">RTT p95 err</th>
                <th className="text-right">Throughput err</th>
                <th className="text-right">Loss err</th>
                <th className="text-right">Paths</th>
                <th>Measured at</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {runs.map((r, idx) => (
                <RunRows key={r.id} r={r} n={runs.length - idx} open={open === r.id} toggle={() => setOpen(open === r.id ? null : r.id)} />
              ))}
            </tbody>
          </table>
          </div>
        )}
      </section>
    </div>
  );
}

function Stat({ label, value, unit, digits = 1 }: { label: string; value: number | null; unit: string; digits?: number }) {
  return (
    <div className="panel px-4 py-3">
      <div className="text-[12.5px] text-ink-3">{label}</div>
      <div className="num mt-0.5 font-cond text-[28px] leading-tight font-semibold">
        {value == null ? "–" : value.toFixed(digits)}
        <span className="text-[16px] font-medium text-ink-2">{value == null ? "" : unit}</span>
      </div>
      <div className="hint">mean over all runs</div>
    </div>
  );
}

function fmtVal(r: ValidationRow, v: number | string | null) {
  if (v == null) return "–";
  if (typeof v === "string") return v.split("-").filter((n) => /^r\d/.test(n)).join(" · ") || v;
  if (r.unit === "ms") return `${v.toFixed(2)} ms`;
  if (r.unit === "%") return `${v.toFixed(2)}%`;
  if (r.unit === "Mbit/s") return `${v.toFixed(2)}`;
  return String(v);
}

function fmtErr(r: ValidationRow) {
  if (r.error == null) return r.predicted == null && r.measured == null ? "both none" : "n/a";
  if (r.kind === "match") return r.error === 0 ? "match" : "differs";
  if (r.kind === "absolute") return `${r.error.toFixed(2)} pp`;
  return `${r.error.toFixed(2)}%`;
}

function RunRows({ r, n, open, toggle }: { r: ValidationRun; n: number; open: boolean; toggle: () => void }) {
  const s = r.summary;
  const pairs = [...new Set(r.rows.map((x) => x.pair))];
  return (
    <>
      <tr>
        <td className="num text-ink-3">{n}</td>
        <td>
          {r.label}
          <div className="hint">{r.changes.length ? r.changes.map(changeLabel).join("; ") : "no change"}</div>
        </td>
        <td className="text-ink-2">{r.mode}</td>
        <td className="text-right">{s.latency_p50_mape == null ? "–" : `${num(s.latency_p50_mape, 2)}%`}</td>
        <td className="text-right">{s.latency_p95_mape == null ? "–" : `${num(s.latency_p95_mape, 2)}%`}</td>
        <td className="text-right">{s.throughput_mape == null ? "–" : `${num(s.throughput_mape, 2)}%`}</td>
        <td className="text-right">{s.loss_mae_pp == null ? "–" : `${num(s.loss_mae_pp, 2)} pp`}</td>
        <td className="text-right">{s.path_match_pct == null ? "–" : `${num(s.path_match_pct, 0)}%`}</td>
        <td className="num text-ink-3">{clock(r.measure_window[0])}</td>
        <td>
          <button className="btn btn-sm" onClick={toggle} aria-expanded={open}>
            {open ? "Hide" : "Details"}
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td />
          <td colSpan={9}>
            <div className="grid gap-x-6 gap-y-3 py-2 lg:grid-cols-2">
              {pairs.map((p) => (
                <table key={p} className="data">
                  <thead>
                    <tr>
                      <th>{p === "all" ? "All traffic flows, total" : pairLabel(p)}</th>
                      <th className="text-right">Twin predicted</th>
                      <th className="text-right">Live measured</th>
                      <th className="text-right">Error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.rows
                      .filter((x) => x.pair === p)
                      .map((x) => (
                        <tr key={x.metric}>
                          <td className="text-ink-3">{METRIC_LABEL[x.metric] ?? x.metric}</td>
                          <td className="text-right text-sim">{fmtVal(x, x.predicted)}</td>
                          <td className="text-right">{fmtVal(x, x.measured)}</td>
                          <td className="text-right font-semibold">{fmtErr(x)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              ))}
            </div>
            <p className="hint pb-2">
              Settled {r.settle_s} s, measured {(r.measure_window[1] - r.measure_window[0]).toFixed(0)} s live; twin simulated {r.sim_duration_s} s in{" "}
              {r.sim_wall_s.toFixed(1)} s of compute.
            </p>
          </td>
        </tr>
      )}
    </>
  );
}

function ErrorChart({ title, data, series }: { title: string; data: Record<string, unknown>[]; series: { key: string; name: string; color: string }[] }) {
  return (
    <section className="panel p-4">
      <h2 className="panel-title">{title}</h2>
      <div className="mt-2 h-[220px]">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 6, right: 12, bottom: 0, left: 0 }} barGap={2}>
            <CartesianGrid stroke="#2f3c4a" vertical={false} />
            <XAxis dataKey="name" stroke="#848d97" tick={{ fontSize: 12 }} tickLine={false} />
            <YAxis stroke="#848d97" tick={{ fontSize: 12 }} tickLine={false} unit="%" width={48} />
            <Tooltip
              cursor={{ fill: "#2a3a4c" }}
              contentStyle={{ background: "#18212b", border: "1px solid #3d4c5d", borderRadius: 4, fontSize: 12.5 }}
              labelFormatter={(_, p) => (p?.[0] ? String(p[0].payload.label) : "")}
              formatter={(v, n) => [typeof v === "number" ? `${v.toFixed(2)}%` : "–", n]}
            />
            <Legend wrapperStyle={{ fontSize: 12.5, color: "#b4b9bf" }} />
            {series.map((s) => (
              <Bar key={s.key} dataKey={s.key} name={s.name} fill={s.color} radius={[4, 4, 0, 0]} maxBarSize={18} isAnimationActive={false} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="hint mt-1">Bars are numbered in run order (see the table); hover for the scenario. Lower is better.</p>
    </section>
  );
}
