import { useMemo, useState } from "react";
import { clock } from "../lib/format";
import { useStore } from "../lib/store";

const FILTERS = [
  { id: "all", label: "All" },
  { id: "chaos", label: "Faults" },
  { id: "routing", label: "Routing" },
  { id: "traffic", label: "Traffic" },
  { id: "sim", label: "Twin" },
  { id: "validation", label: "Validation" },
  { id: "demo", label: "Demo" },
] as const;

const SEV: Record<string, string> = { info: "#848d97", warn: "#fab219", error: "#d03b3b", success: "#0ca30c" };

export default function EventLog({ className = "" }: { className?: string }) {
  const events = useStore((s) => s.events);
  const [f, setF] = useState<string>("all");
  const shown = useMemo(() => {
    const list = f === "all" ? events : events.filter((e) => e.kind.startsWith(f) || (f === "demo" && e.kind.startsWith("scenario")));
    return list.slice(-250).reverse();
  }, [events, f]);
  return (
    <section className={`panel flex min-h-0 flex-col p-4 ${className}`}>
      <div className="flex items-center justify-between gap-3">
        <h2 className="panel-title">Event log</h2>
        <div className="flex flex-wrap gap-1" role="group" aria-label="Filter events">
          {FILTERS.map((x) => (
            <button
              key={x.id}
              aria-pressed={f === x.id}
              onClick={() => setF(x.id)}
              className={`rounded px-2 py-0.5 text-[12px] ${f === x.id ? "bg-ink text-base font-semibold" : "text-ink-3 hover:text-ink-2"}`}
            >
              {x.label}
            </button>
          ))}
        </div>
      </div>
      <ol className="mt-2 min-h-0 flex-1 overflow-auto text-[13px]" aria-live="polite">
        {shown.map((e) => (
          <li key={e.seq} className="grid grid-cols-[78px_10px_1fr] items-baseline gap-2 border-b border-[#263240] py-1 last:border-0">
            <span className="num text-ink-3">{clock(e.t)}</span>
            <span aria-hidden="true" className="h-2 w-2 translate-y-[-1px] rounded-full" style={{ background: SEV[e.severity] ?? SEV.info }} />
            <span className="text-ink-2">
              {e.message}
              {e.data?.source === "validation" || e.data?.source === "demo" || e.data?.source === "replay" ? (
                <span className="ml-1.5 text-[11.5px] text-ink-3">({String(e.data.source)})</span>
              ) : null}
            </span>
          </li>
        ))}
        {shown.length === 0 && <li className="hint py-2">Nothing yet.</li>}
      </ol>
    </section>
  );
}
