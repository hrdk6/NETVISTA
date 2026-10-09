import { useState } from "react";
import EventLog from "../components/EventLog";
import ActiveFaults from "../components/Faults";
import Inspector from "../components/Inspector";
import { useLiveVis } from "../components/liveVis";
import { Incidents, PathSelection, RoutingPolicy } from "../components/RoutingPanels";
import TopologyView, { FlowLegend } from "../components/TopologyView";
import { api } from "../lib/api";
import { flowStyle } from "../lib/colors";
import { corePath, mbps, ms, pairLabel, pct } from "../lib/format";
import { act, useStore } from "../lib/store";

export default function LivePage() {
  const snap = useStore((s) => s.snap)!;
  const topology = useStore((s) => s.topology)!;
  const pairs = useStore((s) => s.pairs);
  const selected = useStore((s) => s.selected);
  const select = useStore((s) => s.select);
  const [colorMode, setColorMode] = useState<"load" | "latency">("load");
  const vis = useLiveVis(snap);
  const background = snap.traffic.filter((f) => f.kind === "background" && f.state === "running");

  return (
    <div className="grid h-full min-h-[820px] grid-cols-[minmax(0,1fr)_400px] grid-rows-[minmax(0,1fr)_340px] gap-3 p-3">
      <section className="panel relative flex min-h-0 flex-col">
        <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2.5">
          <h1 className="panel-title mr-2">Live emulated network</h1>
          {background.length ? (
            <button className="btn btn-sm" onClick={() => act(() => api.post("/api/traffic/stop", {}))}>
              Stop traffic
            </button>
          ) : (
            <button className="btn btn-sm btn-primary" onClick={() => act(() => api.post("/api/traffic/start", {}), "iperf3 flows started")}>
              Start traffic
            </button>
          )}
          <div className="seg" role="group" aria-label="Link colouring">
            <button aria-pressed={colorMode === "load"} onClick={() => setColorMode("load")}>
              Load
            </button>
            <button aria-pressed={colorMode === "latency"} onClick={() => setColorMode("latency")}>
              Latency
            </button>
          </div>
          <span className="hint">
            {colorMode === "load" ? "Brighter, wider links carry more traffic (tx counters ÷ shaped capacity)." : "Brighter links are slower than designed (probe RTT vs designed RTT)."}
          </span>
        </div>
        <TopologyView
          className="min-h-0 flex-1"
          topology={topology}
          pairs={pairs}
          links={vis.links}
          nodes={vis.nodes}
          strands={vis.strands}
          colorMode={colorMode}
          selected={selected}
          onSelect={select}
        />
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-2">
          <FlowLegend pairs={pairs} strands={vis.strands} />
          <StatusLegend />
        </div>
      </section>

      <aside className="row-span-2 flex min-h-0 flex-col gap-3 overflow-auto pr-1">
        <DemoProgress />
        <Inspector />
        <ActiveFaults />
        <PathSelection />
        <RoutingPolicy />
      </aside>

      <div className="grid min-h-0 grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] gap-3">
        <div className="flex min-h-0 flex-col gap-3">
          <FlowTable />
          <Incidents />
        </div>
        <EventLog />
      </div>
    </div>
  );
}

function StatusLegend() {
  const items = [
    ["#0ca30c", "●", "healthy"],
    ["#fab219", "▲", "degraded"],
    ["#d03b3b", "✕", "down (dashed link)"],
  ] as const;
  return (
    <div className="flex items-center gap-3 text-[12.5px] text-ink-3">
      {items.map(([c, g, t]) => (
        <span key={t} className="inline-flex items-center gap-1">
          <span style={{ color: c }} aria-hidden="true">
            {g}
          </span>
          {t}
        </span>
      ))}
    </div>
  );
}

function FlowTable() {
  const snap = useStore((s) => s.snap)!;
  const pairs = useStore((s) => s.pairs);
  return (
    <section className="panel px-4 py-2">
      <table className="data">
        <thead>
          <tr>
            <th>Flow</th>
            <th>Path (routers)</th>
            <th className="text-right">RTT p50</th>
            <th className="text-right">p95</th>
            <th className="text-right">Probe loss</th>
            <th className="text-right">Received</th>
            <th className="text-right">Data loss</th>
          </tr>
        </thead>
        <tbody>
          {pairs.map((p) => {
            const f = snap.flows[p];
            const st = flowStyle(pairs, p);
            return (
              <tr key={p}>
                <td className="whitespace-nowrap">
                  <span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: st.hex }} />
                  {pairLabel(p)}
                </td>
                <td className="text-ink-2">{f.probe.alive === false ? <span className="text-[#ff8a80]">no echo: {corePath(f.path)}</span> : corePath(f.path)}</td>
                <td className="text-right">{ms(f.probe.rtt_p50, 1)}</td>
                <td className="text-right">{ms(f.probe.rtt_p95, 1)}</td>
                <td className="text-right">{pct(f.probe.probe_loss_pct, 1)}</td>
                <td className="text-right">{f.traffic ? mbps(f.traffic.rx_mbps) : <span className="text-ink-3">no traffic</span>}</td>
                <td className="text-right">{f.traffic ? pct(f.traffic.loss_pct, 2) : "–"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function DemoProgress() {
  const demo = useStore((s) => s.snap?.jobs?.demo) as
    | { running?: boolean; steps?: string[]; index?: number; notes?: string[]; step_ends_at?: number }
    | undefined;
  if (!demo || (!demo.running && !demo.notes?.length)) return null;
  return (
    <section className="panel border-ink-3 p-4">
      <div className="flex items-center justify-between">
        <h2 className="panel-title">{demo.running ? "Demo in progress" : "Demo finished"}</h2>
        {demo.running && (
          <button className="btn btn-sm" onClick={() => act(() => api.post("/api/demo/stop"))}>
            Stop
          </button>
        )}
      </div>
      <ol className="mt-2 space-y-1 text-[13px]">
        {(demo.steps ?? []).map((s, i) => (
          <li key={s} className={i === demo.index && demo.running ? "text-ink" : i < (demo.index ?? 0) || !demo.running ? "text-ink-3" : "text-ink-3/60"}>
            <span className="num mr-2">{i + 1}.</span>
            {s}
            {i === demo.index && demo.running ? " …" : ""}
          </li>
        ))}
      </ol>
      {demo.notes && demo.notes.length > 0 && <p className="mt-2 border-t border-line pt-2 text-[13px] text-ink-2">{demo.notes[demo.notes.length - 1]}</p>}
    </section>
  );
}
