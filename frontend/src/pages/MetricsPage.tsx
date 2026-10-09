import { useEffect, useMemo, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../lib/api";
import { flowStyle } from "../lib/colors";
import { ms, pairLabel, pct } from "../lib/format";
import { useStore } from "../lib/store";
import type { HistorySample } from "../lib/types";

const WINDOWS = [
  [60, "1 min"],
  [300, "5 min"],
  [900, "15 min"],
] as const;

const tip = { background: "#18212b", border: "1px solid #3d4c5d", borderRadius: 4, fontSize: 12.5 };
const timeFmt = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour12: false, minute: "2-digit", second: "2-digit" });

export default function MetricsPage() {
  const pairs = useStore((s) => s.pairs);
  const snapT = useStore((s) => s.snap?.t ?? 0);
  const [win, setWin] = useState<number>(300);
  const [hist, setHist] = useState<HistorySample[]>([]);
  const [focus, setFocus] = useState<string>(pairs[0]);

  // poll the 1 Hz history the backend records from the live network
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .get<HistorySample[]>(`/api/metrics/history?seconds=${win}`)
        .then((h) => alive && setHist(h))
        .catch(() => {});
    load();
    const id = setInterval(load, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [win]);

  const rows = useMemo(
    () =>
      hist.map((h) => {
        const r: Record<string, number | null> = { t: h.t };
        for (const p of pairs) {
          const f = h.flows[p];
          r[`${p}|rx`] = f?.rx_mbps ?? null;
          r[`${p}|loss`] = f?.iperf_loss_pct ?? null;
          r[`${p}|ploss`] = f?.probe_loss_pct ?? null;
          r[`${p}|p50`] = f?.rtt_p50 ?? null;
          r[`${p}|p95`] = f?.rtt_p95 ?? null;
          r[`${p}|p99`] = f?.rtt_p99 ?? null;
        }
        return r;
      }),
    [hist, pairs],
  );

  const last = hist[hist.length - 1];
  const activePairs = pairs.filter((p) => hist.some((h) => (h.flows[p]?.offered_mbps ?? 0) > 0));

  return (
    <div className="mx-auto flex max-w-[1500px] flex-col gap-3 p-3">
      <section className="panel flex flex-wrap items-center justify-between gap-3 px-5 py-3">
        <div>
          <h1 className="font-cond text-[22px] font-semibold">Metrics</h1>
          <p className="hint">Sampled once a second from the live network: throughput and loss from iperf3 receivers, latency percentiles from the end-to-end probes (10 s rolling window).</p>
        </div>
        <div className="seg" role="group" aria-label="Time window">
          {WINDOWS.map(([s, l]) => (
            <button key={s} aria-pressed={win === s} onClick={() => setWin(s)}>
              {l}
            </button>
          ))}
        </div>
      </section>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {pairs.map((p) => {
          const f = last?.flows[p];
          const st = flowStyle(pairs, p);
          return (
            <div key={p} className="panel px-4 py-3">
              <div className="flex items-center gap-2 text-[13px] text-ink-2">
                <span className="h-2 w-2 rounded-full" style={{ background: st.hex }} />
                {pairLabel(p)}
              </div>
              <div className="num mt-1 font-cond text-[24px] leading-tight font-semibold">
                {f?.rx_mbps != null ? f.rx_mbps.toFixed(2) : "–"}
                <span className="ml-1 text-[14px] font-medium text-ink-2">Mbit/s</span>
              </div>
              <div className="num text-[12.5px] text-ink-3">
                p50 {ms(f?.rtt_p50, 1)}, p95 {ms(f?.rtt_p95, 1)}, p99 {ms(f?.rtt_p99, 1)}
              </div>
              <div className="num text-[12.5px] text-ink-3">
                data loss {pct(f?.iperf_loss_pct, 2)}, probe loss {pct(f?.probe_loss_pct, 1)}
              </div>
            </div>
          );
        })}
      </div>

      <ChartPanel title="Throughput received (iperf3)" unit="Mbit/s" rows={rows} keys={activePairs.map((p) => ({ key: `${p}|rx`, pair: p }))} pairs={pairs} empty="Start traffic on the Live page to see throughput." />
      <div className="grid gap-3 lg:grid-cols-2">
        <ChartPanel title="Data loss (iperf3)" unit="%" rows={rows} keys={activePairs.map((p) => ({ key: `${p}|loss`, pair: p }))} pairs={pairs} empty="No traffic yet." />
        <ChartPanel title="Probe loss (end-to-end)" unit="%" rows={rows} keys={pairs.map((p) => ({ key: `${p}|ploss`, pair: p }))} pairs={pairs} />
      </div>

      <section className="panel p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="panel-title">Latency percentiles for one flow</h2>
          <div className="flex gap-1" role="tablist" aria-label="Flow">
            {pairs.map((p) => (
              <button key={p} role="tab" aria-selected={focus === p} onClick={() => setFocus(p)} className={`rounded border px-2 py-1 text-[12.5px] ${focus === p ? "border-ink text-ink" : "border-line-strong text-ink-3"}`}>
                {pairLabel(p)}
              </button>
            ))}
          </div>
        </div>
        <div className="mt-2 h-[260px]">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="#2f3c4a" vertical={false} />
              <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={timeFmt} stroke="#848d97" tick={{ fontSize: 12 }} tickLine={false} />
              <YAxis stroke="#848d97" tick={{ fontSize: 12 }} tickLine={false} unit=" ms" width={62} />
              <Tooltip contentStyle={tip} labelFormatter={(t) => timeFmt(Number(t))} formatter={(v, n) => [typeof v === "number" ? `${v.toFixed(2)} ms` : "–", n]} />
              <Legend wrapperStyle={{ fontSize: 12.5 }} />
              {/* one hue per percentile, darkest = median; these are percentiles of the SAME flow, so a sequential ramp */}
              <Line dataKey={`${focus}|p50`} name="p50" stroke="#86b6ef" strokeWidth={2} dot={false} isAnimationActive={false} />
              <Line dataKey={`${focus}|p95`} name="p95" stroke="#3987e5" strokeWidth={2} dot={false} isAnimationActive={false} />
              <Line dataKey={`${focus}|p99`} name="p99" stroke="#1c5cab" strokeWidth={2} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <p className="hint mt-1">
          {hist.length} samples, last {snapT ? timeFmt(snapT) : "–"}. Percentiles are computed over the previous 10 s of 10 Hz probes, so a fault shows up in p99 first.
        </p>
      </section>
    </div>
  );
}

function ChartPanel({
  title,
  unit,
  rows,
  keys,
  pairs,
  empty,
}: {
  title: string;
  unit: string;
  rows: Record<string, number | null>[];
  keys: { key: string; pair: string }[];
  pairs: string[];
  empty?: string;
}) {
  return (
    <section className="panel p-4">
      <h2 className="panel-title">{title}</h2>
      {keys.length === 0 ? (
        <p className="hint mt-2">{empty}</p>
      ) : (
        <div className="mt-2 h-[220px]">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="#2f3c4a" vertical={false} />
              <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={timeFmt} stroke="#848d97" tick={{ fontSize: 12 }} tickLine={false} />
              <YAxis stroke="#848d97" tick={{ fontSize: 12 }} tickLine={false} unit={unit === "%" ? "%" : ""} width={48} />
              <Tooltip contentStyle={tip} labelFormatter={(t) => timeFmt(Number(t))} formatter={(v, n) => [typeof v === "number" ? `${v.toFixed(2)} ${unit}` : "–", n]} />
              <Legend wrapperStyle={{ fontSize: 12.5 }} />
              {keys.map(({ key, pair }) => {
                const st = flowStyle(pairs, pair);
                return (
                  <Line
                    key={key}
                    dataKey={key}
                    name={pairLabel(pair)}
                    stroke={st.hex}
                    strokeDasharray={st.dash.join(" ")}
                    strokeWidth={2}
                    dot={false}
                    isAnimationActive={false}
                  />
                );
              })}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}

