import { useEffect, useState } from "react";
import { HealthBadge } from "../components/Inspector";
import { useLiveVis } from "../components/liveVis";
import TopologyView from "../components/TopologyView";
import { api } from "../lib/api";
import { bps, frac, ms, pct } from "../lib/format";
import { act, useStore } from "../lib/store";
import type { Journey } from "../lib/types";

interface Trace {
  raw: string;
  hops: { ttl: number; ip: string | null; node: string | null; rtts_ms: number[] }[];
  t: number;
}

export default function JourneyPage() {
  const topology = useStore((s) => s.topology)!;
  const pairs = useStore((s) => s.pairs);
  const snap = useStore((s) => s.snap)!;
  const vis = useLiveVis(snap);
  const hosts = topology.nodes.filter((n) => n.type === "client" || n.type === "server").map((n) => n.id);
  const [src, setSrc] = useState(topology.nodes.find((n) => n.type === "client")!.id);
  const [dst, setDst] = useState(topology.nodes.find((n) => n.type === "server")!.id);
  const [j, setJ] = useState<Journey | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [tracing, setTracing] = useState(false);

  useEffect(() => {
    let alive = true;
    setTrace(null);
    const load = () =>
      api
        .get<Journey>(`/api/journey?src=${src}&dst=${dst}`)
        .then((r) => {
          if (alive) {
            setJ(r);
            setErr(null);
          }
        })
        .catch((e) => alive && setErr(String(e.message ?? e)));
    load();
    const id = setInterval(load, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [src, dst]);

  const runTrace = async () => {
    setTracing(true);
    const r = await act(() => api.post<Trace>("/api/journey/traceroute", { src, dst }));
    setTracing(false);
    if (r) setTrace(r);
  };

  return (
    <div className="grid min-h-full gap-3 p-3 xl:grid-cols-[minmax(0,1fr)_560px]">
      <div className="flex min-w-0 flex-col gap-3">
        <section className="panel flex flex-wrap items-center gap-3 px-4 py-3">
          <h1 className="panel-title mr-2">Packet journey</h1>
          <label className="flex items-center gap-2 text-[13px]">
            <span className="text-ink-3">From</span>
            <select className="field" value={src} onChange={(e) => setSrc(e.target.value)}>
              {hosts.map((h) => (
                <option key={h} disabled={h === dst}>
                  {h}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-[13px]">
            <span className="text-ink-3">to</span>
            <select className="field" value={dst} onChange={(e) => setDst(e.target.value)}>
              {hosts.map((h) => (
                <option key={h} disabled={h === src}>
                  {h}
                </option>
              ))}
            </select>
          </label>
          <button className="btn" onClick={runTrace} disabled={tracing}>
            {tracing ? "Tracing…" : "Run real traceroute"}
          </button>
          {j && (
            <span className="hint">
              {j.src_ip} → {j.dst_ip}, routed by the {j.route_source}
              {typeof j.table === "number" ? ` ${j.table}` : ""}
            </span>
          )}
        </section>
        <section className="panel h-[460px]">
          <TopologyView
            className="h-full"
            topology={topology}
            pairs={pairs}
            links={vis.links}
            nodes={vis.nodes}
            strands={vis.strands.filter((s) => j && (s.pair === `${j.src}>${j.dst}` || s.pair === `${j.dst}>${j.src}`))}
            highlight={j?.path ?? null}
          />
        </section>
        {trace && (
          <section className="panel p-4">
            <h2 className="panel-title">traceroute from {src} (run inside its network namespace)</h2>
            <table className="data mt-2">
              <thead>
                <tr>
                  <th>TTL</th>
                  <th>Replied from</th>
                  <th>Node</th>
                  <th className="text-right">RTT samples</th>
                </tr>
              </thead>
              <tbody>
                {trace.hops.map((h) => (
                  <tr key={h.ttl}>
                    <td className="num">{h.ttl}</td>
                    <td>{h.ip ?? "*"}</td>
                    <td className="text-ink-2">{h.node ?? "–"}</td>
                    <td className="text-right">{h.rtts_ms.map((r) => r.toFixed(2)).join(", ") || "no reply"} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="hint mt-2">
              Every router decrements TTL and answers with ICMP time-exceeded, so this is the path packets really take, independent of what the controller believes.
              {j && trace.hops.length > 0 && (
                <>
                  {" "}
                  {trace.hops.map((h) => h.node).filter(Boolean).join(" → ") === j.path.filter((n) => topology.nodes.find((x) => x.id === n)?.type !== "switch").slice(1).join(" → ")
                    ? "It matches the installed path."
                    : "It differs from the path shown (a reroute may have happened between the two reads)."}
                </>
              )}
            </p>
          </section>
        )}
      </div>

      <section className="panel min-h-0 overflow-auto p-4">
        <h2 className="panel-title">Hop by hop</h2>
        {err && <p className="mt-2 text-[13px] text-[#ff8a80]">{err}</p>}
        {j && (
          <ol className="mt-3 space-y-3">
            {j.hops.map((h, i) => (
              <li key={h.node} className="rounded border border-line bg-[#1d2733] p-3">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="font-semibold">
                    <span className="num mr-2 text-ink-3">{i + 1}.</span>
                    {h.label} <span className="text-ink-3">({h.node}, {h.type})</span>
                  </span>
                  {h.in && <span className="hint">in on {h.in.intf}{h.in.ip ? ` (${h.in.ip})` : ""}</span>}
                </div>
                {h.kernel_decision && (
                  <div className="mt-1.5 text-[12.5px]">
                    <span className="text-ink-3">Kernel decision (ip route get): </span>
                    <code className="text-ink">{h.kernel_decision}</code>
                  </div>
                )}
                {h.table_routes && h.table_routes.length > 0 && (
                  <div className="mt-1 text-[12.5px]">
                    <span className="text-ink-3">Table {String(j.table)}: </span>
                    <code className="text-ink-2">{h.table_routes.slice(0, 3).join("; ")}</code>
                  </div>
                )}
                {h.out && (
                  <div className="mt-2 border-t border-line pt-2">
                    <div className="flex items-center justify-between text-[13px]">
                      <span>
                        out {h.out.intf} onto <span className="font-semibold">{h.out.link}</span>
                      </span>
                      <HealthBadge h={h.out.health} />
                    </div>
                    <dl className="kv mt-1.5 grid-cols-4">
                      <dt>RTT</dt>
                      <dd>{ms(h.out.rtt_ms, 2)}</dd>
                      <dt>Loss</dt>
                      <dd>{pct(h.out.loss_pct)}</dd>
                      <dt>Load</dt>
                      <dd>{frac(h.out.util, 1)}</dd>
                      <dt>Rate</dt>
                      <dd>{bps(h.out.bps)}</dd>
                      <dt>Shaped</dt>
                      <dd>
                        {h.out.cfg.bw_mbps} Mbit/s, {h.out.cfg.delay_ms} ms
                      </dd>
                      <dt>Drops</dt>
                      <dd>{h.out.drops_ps.toFixed(1)}/s</dd>
                    </dl>
                  </div>
                )}
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
