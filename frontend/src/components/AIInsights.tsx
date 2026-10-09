import { ask } from "../lib/copilot";
import { ago, pairLabel } from "../lib/format";
import { useStore } from "../lib/store";
import type { Anomaly, Cause } from "../lib/types";
import { Spark } from "./Copilot";

// The AIOps verdict beside the map: what the learned baselines flag, and which single element
// best explains it. Computed on the backend from probes and counters only (never the chaos lab).

export function fmtSignal(v: number | null | undefined, unit: Anomaly["unit"]): string {
  if (v == null || !Number.isFinite(v)) return "–";
  if (unit === "ratio") return `${(v * 100).toFixed(0)}%`;
  if (unit === "%") return `${v.toFixed(1)}%`;
  return `${v.toFixed(v >= 100 ? 0 : 2)} ms`;
}

const CONF: Record<Cause["confidence"], string> = { high: "high confidence", medium: "medium confidence", low: "low confidence" };

export function causeElement(c: Cause): { kind: "node" | "link"; id: string } {
  return { kind: c.element_kind === "link" ? "link" : "node", id: c.element };
}

export default function AIInsights() {
  const ai = useStore((s) => s.snap?.ai);
  const now = useStore((s) => s.snap?.t);
  const select = useStore((s) => s.select);
  if (!ai) return null;
  const { diagnosis: d, anomalies, detector } = ai;
  const learning = detector.normal + detector.anomalous === 0 || (detector.warmup_progress != null && detector.warmup_progress < 1);
  return (
    <section className="panel p-4" aria-live="polite">
      <div className="flex items-center gap-2">
        <Spark size={13} className="text-ink-2" />
        <h2 className="panel-title">AI insights</h2>
        <span className="hint ml-auto">
          {learning ? `learning normal behaviour${detector.warmup_progress != null ? ` (${Math.round(detector.warmup_progress * 100)}%)` : ""}` : `watching ${detector.signals} signals`}
        </span>
      </div>

      {d.causes.length === 0 && anomalies.length === 0 && (
        <p className="hint mt-1.5">
          {learning
            ? "Each link, access segment and flow is learning its own normal round trip, loss and load from live probes."
            : "Every probe path matches its learned normal. A fault shows up here with the element that best explains it."}
        </p>
      )}

      {d.causes.map((c) => (
        <div key={c.id} className="mt-3 rounded-md border border-dotted border-ink-3 p-3">
          <div className="flex items-start justify-between gap-2">
            <div>
              <p className="text-[11.5px] text-ink-3">Most likely cause{c.since ? `, since ${ago(c.since, now)}` : ""}</p>
              <p className="font-semibold text-ink">{c.title}</p>
            </div>
            <span className={`shrink-0 text-[11.5px] whitespace-nowrap ${c.confidence === "high" ? "text-ink-2" : "text-ink-3"}`}>{CONF[c.confidence]}</span>
          </div>
          <ul className="mt-1.5 space-y-0.5 text-[12.5px] text-ink-2">
            {c.evidence.slice(0, 4).map((e) => (
              <li key={e}>{e}</li>
            ))}
            {c.evidence.length > 4 && <li className="text-ink-3">and {c.evidence.length - 4} more observations</li>}
          </ul>
          {c.alternatives.length > 0 && <p className="hint mt-1">The probes cannot tell it apart from: {c.alternatives.join(", ")}</p>}
          {c.contradicted_by.length > 0 && <p className="hint mt-1">Weakened by: {c.contradicted_by.slice(0, 2).join("; ")}</p>}
          {c.rerouted_flows.length > 0 && (
            <p className="mt-1 text-[12.5px] text-ink-2">
              Controller moved {c.rerouted_flows.map((r) => pairLabel(r.pair)).join(", ")} off it.
            </p>
          )}
          {c.affected_flows.length > 0 && c.rerouted_flows.length === 0 && (
            <p className="hint mt-1">Flows over it: {c.affected_flows.map(pairLabel).join(", ")}</p>
          )}
          <div className="mt-2.5 flex flex-wrap gap-2">
            <button className="btn btn-sm" onClick={() => select(causeElement(c))}>
              Show on map
            </button>
            <button
              className="btn btn-sm"
              onClick={() =>
                ask(`Explain the current AI diagnosis "${c.title}": what is the evidence, what is affected, did the controller react, and what should I do?`)
              }
            >
              <Spark size={11} /> Explain
            </button>
          </div>
        </div>
      ))}

      {anomalies.length > 0 && (
        <div className="mt-3">
          <p className="text-[11.5px] text-ink-3">Unusual compared with its learned normal</p>
          <ul className="mt-1 divide-y divide-line">
            {anomalies.slice(0, 5).map((a) => (
              <li key={a.id} className="flex items-baseline justify-between gap-3 py-1 text-[12.5px]">
                <span className="min-w-0 truncate text-ink-2">{a.label}</span>
                <span className="num shrink-0 text-right">
                  <span className="text-ink">{fmtSignal(a.value, a.unit)}</span>
                  <span className="text-ink-3"> vs {fmtSignal(a.normal_mean, a.unit)}</span>
                </span>
              </li>
            ))}
          </ul>
          {anomalies.length > 5 && <p className="hint">and {anomalies.length - 5} more on the AI ops page</p>}
        </div>
      )}
      {d.consequences.length > 0 && (
        <ul className="mt-2 space-y-0.5">
          {d.consequences.map((c) => (
            <li key={c} className="hint">
              {c}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
