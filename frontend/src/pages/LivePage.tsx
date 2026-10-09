import { useState } from "react";
import AIInsights from "../components/AIInsights";
import EventLog from "../components/EventLog";
import ActiveFaults from "../components/Faults";
import Inspector from "../components/Inspector";
import { useLiveVis } from "../components/liveVis";
import { Incidents, PathSelection, RoutingPolicy } from "../components/RoutingPanels";
import TopologyView, { FlowLegend, MapLegend } from "../components/TopologyView";
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
  const [focus, setFocus] = useState<string | null>(null);
  const vis = useLiveVis(snap);
  const background = snap.traffic.filter((f) => f.kind === "background" && f.state === "running");

  return (
    <div className="flex flex-col gap-3 p-3">
      {/* first screen: the map is the hero, the controls sit beside it */}
      <div className="grid gap-3 xl:h-[calc(100vh-80px)] xl:min-h-[640px] xl:grid-cols-[minmax(0,1fr)_400px]">
      <section className="panel relative flex h-[72vh] min-h-[520px] flex-col overflow-hidden xl:h-auto xl:min-h-0">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line px-4 py-2.5">
          <div className="mr-1">
            <h1 className="panel-title">Live emulated network</h1>
            <MapSummary />
          </div>
          <div className="ml-auto flex items-center gap-2">
            <div className="seg" role="group" aria-label="Cable colouring">
              <button aria-pressed={colorMode === "load"} onClick={() => setColorMode("load")} title="Cable brightness shows tx counters divided by shaped capacity">
                Load
              </button>
              <button aria-pressed={colorMode === "latency"} onClick={() => setColorMode("latency")} title="Cable glow shows how much slower than designed the probes measure">
                Latency
              </button>
            </div>
            {background.length ? (
              <button className="btn btn-sm" onClick={() => act(() => api.post("/api/traffic/stop", {}))}>
                Stop traffic
              </button>
            ) : (
              <button className="btn btn-sm btn-primary" onClick={() => act(() => api.post("/api/traffic/start", {}), "iperf3 flows started")}>
                Start traffic
              </button>
            )}
          </div>
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
          focusPair={focus}
          cause={vis.cause}
        />
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5 border-t border-line px-3 py-2">
          <FlowLegend pairs={pairs} strands={vis.strands} focus={focus} onFocus={setFocus} />
          <MapLegend />
        </div>
      </section>

      <aside className="flex min-h-0 flex-col gap-3 xl:overflow-auto xl:pr-1">
        <DemoProgress />
        <AIInsights />
        <Inspector />
        <ActiveFaults />
        <PathSelection />
        <RoutingPolicy />
      </aside>
      </div>

      {/* below the fold: per-flow numbers, failure timings, the event log */}
      <div className="grid gap-3 lg:grid-cols-2 2xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-3 lg:col-span-2 2xl:col-span-1">
          <FlowTable />
        </div>
        <div className="flex h-[380px] min-h-0 min-w-0 flex-col">
          <Incidents />
        </div>
        <EventLog className="h-[380px]" />
      </div>
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

function MapSummary() {
  const snap = useStore((s) => s.snap)!;
  const nodes = Object.values(snap.nodes);
  const healthy = nodes.filter((n) => n.health === "ok").length;
  const flows = Object.values(snap.flows).filter((f) => f.offered_mbps > 0);
  const offered = flows.reduce((s, f) => s + f.offered_mbps, 0);
  const faults = snap.chaos.active.length;
  return (
    <p className="text-[12.5px] text-ink-3">
      <span className={healthy === nodes.length ? "text-ink-2" : "text-[#fab219]"}>
        {healthy} of {nodes.length} devices healthy
      </span>
      <span aria-hidden="true" className="mx-2 text-line-strong">|</span>
      {flows.length ? `${flows.length} flows carrying ${offered.toFixed(1)} Mbit/s` : "no iperf3 traffic yet"}
      <span aria-hidden="true" className="mx-2 text-line-strong">|</span>
      <span className={faults ? "text-[#fab219]" : ""}>{faults ? `${faults} active ${faults === 1 ? "fault" : "faults"}` : "no faults injected"}</span>
    </p>
  );
}
