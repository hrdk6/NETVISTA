import { useMemo, useState } from "react";
import { api } from "../../lib/api";
import {
  INTENT_STATUS,
  KIND_LABEL,
  PROTECT_LABEL,
  fmtCheck,
  type IntentKind,
  type IntentRow,
  type IntentStatus,
  type Priority,
  type Protect,
} from "../../lib/assure";
import { act, useStore } from "../../lib/store";

export function StatusPill({ status, small }: { status: IntentStatus; small?: boolean }) {
  const s = INTENT_STATUS[status] ?? INTENT_STATUS.unknown;
  return (
    <span className={`inline-flex shrink-0 items-center gap-1.5 font-cond font-semibold ${small ? "text-[11.5px]" : "text-[12.5px]"}`} style={{ color: status === "unknown" || status === "disabled" ? "#9aa6b2" : s.color }}>
      <span className="inline-block h-2 w-2 rounded-full" style={{ background: s.color, boxShadow: status === "violated" ? `0 0 6px ${s.color}` : undefined }} />
      {s.label}
    </span>
  );
}

const PRIORITY_STYLE: Record<Priority, string> = {
  critical: "border-[#7a4a4a] text-[#ffb4a8]",
  high: "border-[#6e5a30] text-[#ffd27a]",
  normal: "border-line-strong text-ink-3",
};

/** Last N seconds of one intent's live status, one tick per second, run-length encoded. */
export function Timeline({ points, seconds = 300, now }: { points: [number, string][]; seconds?: number; now: number }) {
  const runs = useMemo(() => {
    const out: { x0: number; x1: number; s: string }[] = [];
    for (const [t, s] of points) {
      const x = Math.max(0, Math.min(1, 1 - (now - t) / seconds));
      const last = out[out.length - 1];
      if (last && last.s === s && x - last.x1 < 2.5 / seconds) last.x1 = x + 1 / seconds;
      else out.push({ x0: x, x1: x + 1 / seconds, s });
    }
    return out;
  }, [points, seconds, now]);
  return (
    <svg className="h-2.5 w-full" viewBox="0 0 300 10" preserveAspectRatio="none" role="img" aria-label={`Status over the last ${seconds / 60} minutes`}>
      <rect x="0" y="3" width="300" height="4" rx="2" fill="#1b2531" />
      {runs.map((r, i) => (
        <rect
          key={i}
          x={r.x0 * 300}
          y={r.s === "violated" ? 0 : 3}
          width={Math.max(1, (r.x1 - r.x0) * 300)}
          height={r.s === "violated" ? 10 : 4}
          fill={(INTENT_STATUS[r.s as IntentStatus] ?? INTENT_STATUS.unknown).color}
          opacity={r.s === "ok" ? 0.55 : 0.95}
        />
      ))}
    </svg>
  );
}

export function IntentsPanel({ rows, onChanged, compact }: { rows: IntentRow[] | null; onChanged: () => void; compact?: boolean }) {
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const t = useStore((s) => s.snap?.t ?? Date.now() / 1000);
  const loadPreset = async (name: string) => {
    if (!name) return;
    const ok = await act(() => api.post("/api/assure/intents/preset", { name, replace: true }), `Loaded the "${name}" intent set`);
    if (ok) onChanged();
  };
  return (
    <section className="panel flex min-h-0 flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-3">
        <div>
          <h2 className="panel-title">Intents</h2>
          <p className="hint">What the network must guarantee, checked every second against live measurements</p>
        </div>
        <div className="flex items-center gap-2">
          <select className="field" defaultValue="" onChange={(e) => void loadPreset(e.target.value)} aria-label="Load a preset intent set" title="Replace the intents with a preset derived from this topology's design numbers">
            <option value="" disabled>
              Load preset…
            </option>
            <option value="gold-bronze">Gold / bronze SLOs</option>
            <option value="basic">Basic availability</option>
            <option value="policy">SLOs + policy</option>
          </select>
          <button className="btn btn-sm btn-primary" onClick={() => setAdding((a) => !a)} aria-expanded={adding}>
            {adding ? "Close" : "Add intent"}
          </button>
        </div>
      </div>
      {adding && <IntentEditor onDone={() => { setAdding(false); onChanged(); }} />}
      <ul className={`divide-y divide-line ${compact ? "" : "overflow-auto"}`}>
        {rows === null && <li className="hint px-4 py-3">Loading…</li>}
        {rows?.length === 0 && (
          <li className="px-4 py-5 text-[13px] text-ink-2">
            No intents yet. Load a preset above (it reads this topology's design delays and capacities), or add one: for example
            "c1 → srv1 round trip p95 ≤ 30 ms, even after any single failure".
          </li>
        )}
        {rows?.map((r) => {
          const worst = r.checks.filter((c) => c.ok === false).sort((a, b) => b.severity - a.severity)[0] ?? r.checks.find((c) => c.at_risk) ?? null;
          const res = r.resilience;
          const comp = r.compliance["5m"].compliance_pct;
          return (
            <li key={r.id} className={`px-4 py-2.5 ${r.enabled ? "" : "opacity-55"}`}>
              <div className="flex items-start gap-3">
                <span className="num mt-0.5 w-6 shrink-0 font-cond text-[13px] font-semibold text-ink-3">{r.id}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <StatusPill status={r.enabled ? r.status : "disabled"} />
                    <span className={`rounded border px-1.5 text-[11px] leading-[17px] ${PRIORITY_STYLE[r.priority]}`}>{r.priority}</span>
                    <span className="text-[11.5px] text-ink-3">{KIND_LABEL[r.kind]}</span>
                  </div>
                  <button className="mt-0.5 block text-left text-[13px] leading-snug text-ink hover:underline" onClick={() => setOpen(open === r.id ? null : r.id)} aria-expanded={open === r.id}>
                    {r.label}
                  </button>
                  {worst && (
                    <p className={`text-[12px] ${worst.ok === false ? "text-[#ff9d94]" : "text-[#ffd27a]"}`}>
                      {worst.subject.replace(">", " → ")}: {fmtCheck(worst)}
                      {worst.at_risk && worst.ok !== false ? " (inside the limit, but not with its prediction margin)" : ""}
                    </p>
                  )}
                  <div className="mt-1.5 flex items-center gap-3">
                    <div className="min-w-0 flex-1">
                      <Timeline points={r.timeline} now={t} />
                    </div>
                    <span className="num w-[86px] shrink-0 text-right text-[11.5px] text-ink-3" title="Share of the last 5 minutes in which the intent was met (live)">
                      {comp == null ? "–" : `${comp.toFixed(comp >= 99.95 ? 0 : 1)}% met, 5 min`}
                    </span>
                  </div>
                  {res && r.protect !== "none" && (
                    <p className="mt-1 text-[12px] text-ink-2">
                      <span className="sim-tag mr-1.5 !px-1.5 !py-0 !text-[11px]">predicted</span>
                      holds in {res.held} of {res.scenarios} single failures
                      {res.avoidable.length > 0 && <span className="text-[#ff9d94]">; {res.avoidable.length} avoidable by better routing</span>}
                      {res.unavoidable.length > 0 && <span className="text-[#ffd27a]">; {res.unavoidable.length} no routing can avoid</span>}
                      {res.unprotectable.length > 0 && <span className="text-ink-3">; {res.unprotectable.length} cut the flow off</span>}
                    </p>
                  )}
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <button
                    className="btn btn-sm"
                    title={r.enabled ? "Stop checking this intent (it is ignored by the planner too)" : "Check this intent again"}
                    onClick={async () => {
                      await act(() => api.put(`/api/assure/intents/${r.id}`, { enabled: !r.enabled }));
                      onChanged();
                    }}
                  >
                    {r.enabled ? "Pause" : "Resume"}
                  </button>
                  <button
                    className="btn btn-sm btn-danger"
                    aria-label={`Remove intent ${r.id}`}
                    onClick={async () => {
                      await act(() => api.del(`/api/assure/intents/${r.id}`));
                      onChanged();
                    }}
                  >
                    Remove
                  </button>
                </div>
              </div>
              {open === r.id && <IntentDetail row={r} />}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function IntentDetail({ row }: { row: IntentRow }) {
  return (
    <div className="mt-2 ml-9 rounded border border-line bg-[#1b2531] p-3">
      <dl className="kv">
        <dt>Must hold</dt>
        <dd className="!text-left">{PROTECT_LABEL[row.protect]}</dd>
        <dt>Last 1 / 5 / 15 min</dt>
        <dd className="!text-left">
          {(["1m", "5m", "15m"] as const)
            .map((w) => {
              const c = row.compliance[w];
              return c.compliance_pct == null ? "–" : `${c.compliance_pct.toFixed(1)}% (${c.violated_s} s violated)`;
            })
            .join(" · ")}
        </dd>
        {row.note && (
          <>
            <dt>Note</dt>
            <dd className="!text-left">{row.note}</dd>
          </>
        )}
      </dl>
      {row.checks.length > 0 && (
        <table className="data mt-2">
          <thead>
            <tr>
              <th>Checked</th>
              <th>Status</th>
              <th className="text-right">Measured</th>
            </tr>
          </thead>
          <tbody>
            {row.checks.map((c) => (
              <tr key={c.subject}>
                <td>{c.subject.replace(">", " → ")}</td>
                <td>
                  <StatusPill small status={c.ok === false ? "violated" : c.at_risk ? "at_risk" : c.ok ? "ok" : "unknown"} />
                </td>
                <td className="text-right">{fmtCheck(c)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {row.resilience && row.resilience.broken.length > 0 && (
        <p className="mt-2 text-[12px] text-ink-2">
          Predicted to break after: {row.resilience.broken.map((s) => s.replace("link:", "link ").replace("node:", "router ")).join(", ")}
        </p>
      )}
    </div>
  );
}

const KINDS: IntentKind[] = ["latency", "loss", "reach", "max_util", "bandwidth", "avoid", "waypoint", "disjoint"];

function IntentEditor({ onDone }: { onDone: () => void }) {
  const pairs = useStore((s) => s.pairs);
  const topology = useStore((s) => s.topology)!;
  const routersList = topology.nodes.filter((n) => n.type === "router").map((n) => n.id);
  const coreLinks = topology.links.filter((l) => routersList.includes(l.a) && routersList.includes(l.b)).map((l) => l.id);
  const [kind, setKind] = useState<IntentKind>("latency");
  const [flows, setFlows] = useState<string[]>([pairs[0]]);
  const [links, setLinks] = useState<string[]>(["*"]);
  const [stat, setStat] = useState("p95");
  const [maxMs, setMaxMs] = useState(30);
  const [maxPct, setMaxPct] = useState(1);
  const [util, setUtil] = useState(85);
  const [minMbps, setMinMbps] = useState(10);
  const [elements, setElements] = useState<string[]>([]);
  const [node, setNode] = useState(routersList[1] ?? routersList[0]);
  const [nodes, setNodes] = useState(false);
  const [protect, setProtect] = useState<Protect>("any");
  const [priority, setPriority] = useState<Priority>("high");
  const [note, setNote] = useState("");

  const toggle = (arr: string[], v: string, set: (a: string[]) => void) => set(arr.includes(v) ? arr.filter((x) => x !== v) : [...arr.filter((x) => x !== "*"), v]);
  const params = (): Record<string, unknown> => {
    switch (kind) {
      case "latency":
        return { stat, max_ms: maxMs };
      case "loss":
        return { max_pct: maxPct };
      case "max_util":
        return { max_pct: util };
      case "bandwidth":
        return { min_mbps: minMbps };
      case "avoid":
        return { elements };
      case "waypoint":
        return { node };
      case "disjoint":
        return { nodes };
      default:
        return {};
    }
  };
  const submit = async () => {
    const body = { kind, flows: kind === "max_util" ? [] : flows, links: kind === "max_util" ? links : [], params: params(), protect, priority, note };
    const ok = await act(() => api.post("/api/assure/intents", body), "Intent added: it is checked live from now on");
    if (ok) onDone();
  };
  const Chip = ({ on, label, onClick }: { on: boolean; label: string; onClick: () => void }) => (
    <button type="button" aria-pressed={on} onClick={onClick} className={`rounded-full border px-2.5 py-0.5 text-[12.5px] ${on ? "border-ink bg-raised text-ink" : "border-line-strong text-ink-3 hover:text-ink-2"}`}>
      {label}
    </button>
  );
  return (
    <div className="border-b border-line bg-[#1d2733] px-4 py-3 text-[13px]">
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-ink-3">Kind</span>
          <select className="field" value={kind} onChange={(e) => setKind(e.target.value as IntentKind)}>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {KIND_LABEL[k]}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-ink-3">Priority</span>
          <select className="field" value={priority} onChange={(e) => setPriority(e.target.value as Priority)}>
            <option value="critical">Critical (weight 10)</option>
            <option value="high">High (weight 3)</option>
            <option value="normal">Normal (weight 1)</option>
          </select>
        </label>
      </div>
      {kind !== "max_util" ? (
        <div className="mt-3">
          <span className="text-ink-3">{kind === "disjoint" ? "Two flows" : "Flows"}</span>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {kind !== "disjoint" && <Chip on={flows.includes("*")} label="every flow" onClick={() => setFlows(flows.includes("*") ? [pairs[0]] : ["*"])} />}
            {pairs.map((p) => (
              <Chip key={p} on={flows.includes(p)} label={p.replace(">", " → ")} onClick={() => toggle(flows, p, setFlows)} />
            ))}
          </div>
        </div>
      ) : (
        <div className="mt-3">
          <span className="text-ink-3">Links</span>
          <div className="mt-1 flex flex-wrap gap-1.5">
            <Chip on={links.includes("*")} label="every core link" onClick={() => setLinks(links.includes("*") ? [coreLinks[0]] : ["*"])} />
            {coreLinks.map((l) => (
              <Chip key={l} on={links.includes(l)} label={l} onClick={() => toggle(links, l, setLinks)} />
            ))}
          </div>
        </div>
      )}
      <div className="mt-3 grid grid-cols-2 gap-3">
        {kind === "latency" && (
          <>
            <label className="flex flex-col gap-1">
              <span className="text-ink-3">Round-trip percentile</span>
              <select className="field" value={stat} onChange={(e) => setStat(e.target.value)}>
                <option value="p50">p50 (median)</option>
                <option value="p95">p95</option>
                <option value="p99">p99</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-ink-3">At most (ms)</span>
              <input className="field" type="number" min={1} step={1} value={maxMs} onChange={(e) => setMaxMs(+e.target.value)} />
            </label>
          </>
        )}
        {kind === "loss" && (
          <label className="flex flex-col gap-1">
            <span className="text-ink-3">Loss at most (%)</span>
            <input className="field" type="number" min={0} step={0.1} value={maxPct} onChange={(e) => setMaxPct(+e.target.value)} />
          </label>
        )}
        {kind === "max_util" && (
          <label className="flex flex-col gap-1">
            <span className="text-ink-3">Load at most (%)</span>
            <input className="field" type="number" min={1} max={100} value={util} onChange={(e) => setUtil(+e.target.value)} />
          </label>
        )}
        {kind === "bandwidth" && (
          <label className="flex flex-col gap-1">
            <span className="text-ink-3">Spare capacity at least (Mbit/s)</span>
            <input className="field" type="number" min={0.1} step={1} value={minMbps} onChange={(e) => setMinMbps(+e.target.value)} />
          </label>
        )}
        {kind === "waypoint" && (
          <label className="flex flex-col gap-1">
            <span className="text-ink-3">Must pass through</span>
            <select className="field" value={node} onChange={(e) => setNode(e.target.value)}>
              {routersList.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
          </label>
        )}
        {kind === "disjoint" && (
          <label className="col-span-2 flex items-center gap-2">
            <input type="checkbox" checked={nodes} onChange={(e) => setNodes(e.target.checked)} />
            <span className="text-ink-2">also no shared transit router (site gateways excepted)</span>
          </label>
        )}
      </div>
      {kind === "avoid" && (
        <div className="mt-3">
          <span className="text-ink-3">Must avoid</span>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {[...routersList, ...coreLinks].map((e) => (
              <Chip key={e} on={elements.includes(e)} label={e} onClick={() => toggle(elements, e, setElements)} />
            ))}
          </div>
        </div>
      )}
      <div className="mt-3 grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-ink-3">Must hold</span>
          <select className="field" value={protect} onChange={(e) => setProtect(e.target.value as Protect)}>
            <option value="none">Normal operation only</option>
            <option value="link">+ any single link failure</option>
            <option value="node">+ any single router failure</option>
            <option value="any">+ any single link or router failure</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-ink-3">Note (optional)</span>
          <input className="field" value={note} maxLength={300} onChange={(e) => setNote(e.target.value)} placeholder="why it matters" />
        </label>
      </div>
      <div className="mt-3 flex justify-end">
        <button className="btn btn-primary" onClick={submit} disabled={kind !== "max_util" && flows.length === 0}>
          Add intent
        </button>
      </div>
    </div>
  );
}
