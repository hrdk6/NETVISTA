import { useState } from "react";
import { useStore } from "../lib/store";
import type { Change } from "../lib/types";

export function changeLabel(c: Change): string {
  const p = c.params ?? {};
  switch (c.kind) {
    case "link_latency":
      return `+${p.add_ms} ms latency on ${c.target}${p.jitter_ms ? ` (±${p.jitter_ms} ms)` : ""}`;
    case "link_loss":
      return `${p.loss_pct}% loss on ${c.target}`;
    case "link_bandwidth":
      return `Cap ${c.target} at ${p.bw_mbps} Mbit/s`;
    case "link_down":
      return `Link ${c.target} down`;
    case "node_down":
      return `Router ${c.target} down`;
    case "traffic_burst":
      return `Burst ${String(c.target).replace(">", " → ")} at ${p.rate_mbps} Mbit/s`;
    default:
      return c.kind;
  }
}

const KINDS = [
  ["link_latency", "Add latency"],
  ["link_loss", "Packet loss"],
  ["link_bandwidth", "Bandwidth cap"],
  ["link_down", "Link down"],
  ["node_down", "Router down"],
  ["traffic_burst", "Traffic burst"],
] as const;

export default function ChangeBuilder({ changes, onChange }: { changes: Change[]; onChange: (c: Change[]) => void }) {
  const topology = useStore((s) => s.topology)!;
  const pairs = useStore((s) => s.pairs);
  const links = topology.links.map((l) => l.id);
  const routers = topology.nodes.filter((n) => n.type === "router" || n.type === "switch").map((n) => n.id);
  const [kind, setKind] = useState<string>("link_latency");
  const [target, setTarget] = useState<string>("r2-r5");
  const [v1, setV1] = useState(40);

  const targets = kind === "node_down" ? routers : kind === "traffic_burst" ? pairs : links;
  const tgt = targets.includes(target) ? target : targets[0];

  const add = () => {
    let c: Change;
    if (kind === "link_latency") c = { kind, target: tgt, params: { add_ms: v1, jitter_ms: 0 } };
    else if (kind === "link_loss") c = { kind, target: tgt, params: { loss_pct: v1 } };
    else if (kind === "link_bandwidth") c = { kind, target: tgt, params: { bw_mbps: v1 } };
    else if (kind === "traffic_burst") c = { kind, target: tgt, params: { rate_mbps: v1, duration_s: 60 } };
    else c = { kind, target: tgt };
    onChange([...changes.filter((x) => !(x.kind === c.kind && x.target === c.target)), c]);
  };

  const unit = kind === "link_latency" ? "ms" : kind === "link_loss" ? "%" : kind === "link_bandwidth" || kind === "traffic_burst" ? "Mbit/s" : null;

  return (
    <div>
      <ul className="space-y-1.5">
        {changes.map((c, i) => (
          <li key={i} className="flex items-center justify-between gap-2 rounded border border-dashed border-sim/60 bg-[#1e2c3b] px-3 py-1.5 text-[13px]">
            <span>{changeLabel(c)}</span>
            <button className="btn btn-sm" onClick={() => onChange(changes.filter((_, j) => j !== i))} aria-label={`Remove ${changeLabel(c)}`}>
              Remove
            </button>
          </li>
        ))}
        {changes.length === 0 && <li className="hint">No changes: the twin predicts the network exactly as it is now.</li>}
      </ul>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-[13px]">
        <select className="field" value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Change type">
          {KINDS.map(([k, l]) => (
            <option key={k} value={k}>
              {l}
            </option>
          ))}
        </select>
        <select className="field" value={tgt} onChange={(e) => setTarget(e.target.value)} aria-label="Target">
          {targets.map((t) => (
            <option key={t} value={t}>
              {t.replace(">", " → ")}
            </option>
          ))}
        </select>
        {unit && (
          <>
            <input className="field w-20" type="number" min={0} value={v1} onChange={(e) => setV1(+e.target.value)} aria-label={`Value in ${unit}`} />
            <span className="text-ink-3">{unit}</span>
          </>
        )}
        <button className="btn btn-sm" onClick={add}>
          Add change
        </button>
      </div>
    </div>
  );
}
