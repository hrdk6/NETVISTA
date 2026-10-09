import { useState } from "react";
import { api } from "../lib/api";
import { STATUS, STATUS_LABEL } from "../lib/colors";
import { bps, frac, mbps, ms, num, pct } from "../lib/format";
import { ask } from "../lib/copilot";
import { act, useStore } from "../lib/store";
import type { Health, LinkView, NodeView } from "../lib/types";
import { Spark as AISpark } from "./Copilot";
import Spark from "./Spark";

export function HealthBadge({ h }: { h: Health }) {
  const glyph = h === "ok" ? "●" : h === "degraded" ? "▲" : h === "down" ? "✕" : "○";
  return (
    <span className="inline-flex items-center gap-1.5 text-[12.5px] font-medium" style={{ color: STATUS[h] }}>
      <span aria-hidden="true">{glyph}</span>
      <span className="text-ink">{STATUS_LABEL[h]}</span>
    </span>
  );
}

export default function Inspector() {
  const selected = useStore((s) => s.selected);
  const snap = useStore((s) => s.snap);
  if (!snap) return null;
  if (!selected) {
    return (
      <section className="panel p-4">
        <h2 className="panel-title">Inspect</h2>
        <p className="hint mt-1">Click a link or a node on the map to see its live counters and probe results, and to inject faults into it.</p>
      </section>
    );
  }
  if (selected.kind === "link") {
    const l = snap.links[selected.id];
    return l ? <LinkInspector l={l} /> : null;
  }
  const n = snap.nodes[selected.id];
  return n ? <NodeInspector n={n} /> : null;
}

function LinkInspector({ l }: { l: LinkView }) {
  const series = useStore((s) => s.linkSeries[l.id] ?? []);
  const changed = l.cfg.delay_ms !== l.base.delay_ms || l.cfg.loss_pct !== l.base.loss_pct || l.cfg.bw_mbps !== l.base.bw_mbps;
  return (
    <section className="panel p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="panel-title">Link {l.id}</h2>
          <p className="hint">Probe: {l.probe_span}</p>
        </div>
        <HealthBadge h={l.health} />
      </div>
      <AskAbout prompt={`How is link ${l.id} doing compared with its normal behaviour, and which flows depend on it?`} />
      <div className="mt-3 grid grid-cols-2 gap-x-5 gap-y-2">
        <Spark label="Utilisation" data={series.map((p) => ({ t: p.t, v: p.util * 100 }))} format={(v) => `${v.toFixed(1)}%`} domainMax={100} />
        <Spark label="Round-trip time" data={series.map((p) => ({ t: p.t, v: p.rtt }))} format={(v) => `${v.toFixed(1)} ms`} />
      </div>
      <dl className="kv mt-3">
        <dt>RTT now (2 s mean)</dt>
        <dd>{ms(l.rtt_ms, 2)}</dd>
        <dt>RTT p50 · p95 · p99 (10 s)</dt>
        <dd>
          {num(l.rtt_p50, 2)} · {num(l.rtt_p95, 2)} · {num(l.rtt_p99, 2)} ms
        </dd>
        <dt>Designed RTT</dt>
        <dd>{ms(l.expected_rtt_ms, 1)}</dd>
        <dt>Probe loss (5 s)</dt>
        <dd>{pct(l.loss_pct)}</dd>
      </dl>
      <table className="data mt-3">
        <thead>
          <tr>
            <th>Direction</th>
            <th className="text-right">Rate</th>
            <th className="text-right">Packets/s</th>
            <th className="text-right">Load</th>
            <th className="text-right">Drops/s</th>
            <th className="text-right">Queue</th>
          </tr>
        </thead>
        <tbody>
          {(
            [
              [`${l.a} → ${l.b}`, l.ab],
              [`${l.b} → ${l.a}`, l.ba],
            ] as const
          ).map(([name, d]) => (
            <tr key={name}>
              <td className="text-ink-2">{name}</td>
              <td className="text-right whitespace-nowrap">{bps(d.bps)}</td>
              <td className="text-right">{d.pps.toFixed(0)}</td>
              <td className="text-right">{frac(d.util, 1)}</td>
              <td className="text-right">{d.drops_ps.toFixed(1)}</td>
              <td className="text-right">{d.backlog_pkts}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <dl className="kv mt-3">
        <dt>Shaping (tc)</dt>
        <dd>
          {mbps(l.cfg.bw_mbps, 0)}, {ms(l.cfg.delay_ms, 1)} one way
          {l.cfg.jitter_ms ? ` ±${l.cfg.jitter_ms} ms` : ""}, {l.cfg.loss_pct}% loss
        </dd>
        {changed && (
          <>
            <dt>Designed</dt>
            <dd className="text-ink-3">
              {mbps(l.base.bw_mbps, 0)}, {ms(l.base.delay_ms, 1)}, {l.base.loss_pct}% loss
            </dd>
          </>
        )}
        <dt>Admin state</dt>
        <dd>{l.admin_up ? "up" : "down"}</dd>
      </dl>
      <LinkChaos id={l.id} />
    </section>
  );
}

function LinkChaos({ id }: { id: string }) {
  const [add, setAdd] = useState(60);
  const [jit, setJit] = useState(0);
  const [loss, setLoss] = useState(5);
  const [bw, setBw] = useState(10);
  const inject = (kind: string, params: Record<string, number> = {}) =>
    act(() => api.post("/api/chaos/inject", { kind, target: id, params }));
  return (
    <div className="mt-4 border-t border-line pt-3">
      <h3 className="font-cond text-[14px] font-semibold">Inject into {id}</h3>
      <div className="mt-2 grid grid-cols-[1fr_auto] items-center gap-x-2 gap-y-2 text-[13px]">
        <label className="flex items-center gap-2">
          <span className="w-20 text-ink-3">Latency</span>
          <input className="field w-16" type="number" min={0} max={500} value={add} onChange={(e) => setAdd(+e.target.value)} aria-label="Added latency in ms" />
          <span className="text-ink-3">ms ±</span>
          <input className="field w-14" type="number" min={0} max={200} value={jit} onChange={(e) => setJit(+e.target.value)} aria-label="Jitter in ms" />
        </label>
        <button className="btn btn-sm" onClick={() => inject("link_latency", { add_ms: add, jitter_ms: jit })}>
          Add latency
        </button>
        <label className="flex items-center gap-2">
          <span className="w-20 text-ink-3">Loss</span>
          <input className="field w-16" type="number" min={0} max={100} step={0.5} value={loss} onChange={(e) => setLoss(+e.target.value)} aria-label="Loss percent" />
          <span className="text-ink-3">%</span>
        </label>
        <button className="btn btn-sm" onClick={() => inject("link_loss", { loss_pct: loss })}>
          Drop packets
        </button>
        <label className="flex items-center gap-2">
          <span className="w-20 text-ink-3">Bandwidth</span>
          <input className="field w-16" type="number" min={0.5} max={1000} value={bw} onChange={(e) => setBw(+e.target.value)} aria-label="Bandwidth cap in Mbit/s" />
          <span className="text-ink-3">Mbit/s</span>
        </label>
        <button className="btn btn-sm" onClick={() => inject("link_bandwidth", { bw_mbps: bw })}>
          Cap bandwidth
        </button>
      </div>
      <button className="btn btn-danger mt-3 w-full justify-center" onClick={() => inject("link_down")}>
        Take link down
      </button>
    </div>
  );
}

function NodeInspector({ n }: { n: NodeView }) {
  const topology = useStore((s) => s.topology)!;
  const plan = useStore((s) => s.plan);
  const spec = topology.nodes.find((x) => x.id === n.id);
  return (
    <section className="panel p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="panel-title">{spec?.label ?? n.id}</h2>
          <p className="hint">
            {n.type} {n.id}
            {plan?.hosts[n.id] ? `, ${plan.hosts[n.id].ip} via ${plan.hosts[n.id].gateway}` : ""}
          </p>
        </div>
        <HealthBadge h={n.health} />
      </div>
      <AskAbout
        prompt={
          n.type === "router"
            ? `What does ${n.id} carry right now, and what would happen if it crashed?`
            : `How is ${n.id} doing, and which paths does its traffic take?`
        }
      />
      <dl className="kv mt-3">
        <dt>Receiving</dt>
        <dd>{bps(n.rx_bps)}</dd>
        <dt>Sending</dt>
        <dd>{bps(n.tx_bps)}</dd>
        <dt>Queue drops</dt>
        <dd>{n.drops_ps.toFixed(1)}/s</dd>
      </dl>
      <table className="data mt-3">
        <thead>
          <tr>
            <th>Interface</th>
            <th>Address</th>
            <th className="text-right">In</th>
            <th className="text-right">Out</th>
          </tr>
        </thead>
        <tbody>
          {n.interfaces.map((i) => (
            <tr key={i.name}>
              <td>
                <div>{i.name}</div>
                <div className="hint">{i.link}</div>
              </td>
              <td className="text-ink-2">{i.ip ?? "L2 port"}</td>
              <td className="text-right">{bps(i.rx_bps)}</td>
              <td className="text-right">{bps(i.tx_bps)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {(n.type === "router" || n.type === "switch") && (
        <button
          className="btn btn-danger mt-4 w-full justify-center"
          onClick={() => act(() => api.post("/api/chaos/inject", { kind: "node_down", target: n.id }))}
        >
          Crash {n.id} (all interfaces down)
        </button>
      )}
      {n.type === "client" && <BurstForm src={n.id} />}
    </section>
  );
}

function BurstForm({ src }: { src: string }) {
  const topology = useStore((s) => s.topology)!;
  const servers = topology.nodes.filter((x) => x.type === "server").map((x) => x.id);
  const [dst, setDst] = useState(servers[0]);
  const [rate, setRate] = useState(20);
  const [dur, setDur] = useState(20);
  return (
    <div className="mt-4 border-t border-line pt-3">
      <h3 className="font-cond text-[14px] font-semibold">Traffic burst from {src}</h3>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[13px]">
        <select className="field" value={dst} onChange={(e) => setDst(e.target.value)} aria-label="Destination server">
          {servers.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <input className="field w-16" type="number" min={0.1} max={500} value={rate} onChange={(e) => setRate(+e.target.value)} aria-label="Rate in Mbit/s" />
        <span className="text-ink-3">Mbit/s for</span>
        <input className="field w-14" type="number" min={1} max={600} value={dur} onChange={(e) => setDur(+e.target.value)} aria-label="Duration in seconds" />
        <span className="text-ink-3">s</span>
      </div>
      <button
        className="btn mt-2 w-full justify-center"
        onClick={() => act(() => api.post("/api/chaos/inject", { kind: "traffic_burst", target: `${src}>${dst}`, params: { rate_mbps: rate, duration_s: dur } }))}
      >
        Start burst
      </button>
    </div>
  );
}

function AskAbout({ prompt }: { prompt: string }) {
  return (
    <button className="btn btn-sm mt-2.5" onClick={() => ask(prompt)} title={prompt}>
      <AISpark size={11} /> Ask the copilot
    </button>
  );
}
