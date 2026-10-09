import { fmtCheck } from "../../lib/assure";
import { useStore } from "../../lib/store";
import { StatusPill } from "./IntentsPanel";

/** Compact intent status for the live page's side column. */
export function IntentsCard() {
  const a = useStore((s) => s.snap?.assure);
  if (!a) return null;
  const res = a.resilience;
  return (
    <section className="panel p-4">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="panel-title">Intents</h2>
        <a href="#assure" className="text-[12.5px] text-ink-2 hover:text-ink">
          Open Assure →
        </a>
      </div>
      {a.intents.length === 0 ? (
        <p className="hint mt-1">
          No intents yet. On the <a className="underline" href="#assure">Assure</a> page, say what the network must guarantee (latency, loss, headroom, policy, survive
          failures) and it is checked here every second.
        </p>
      ) : (
        <ul className="mt-2 space-y-1.5">
          {a.intents.map((i) => (
            <li key={i.id} className="text-[12.5px]">
              <div className="flex items-start gap-2">
                <span className="num w-5 shrink-0 font-cond font-semibold text-ink-3">{i.id}</span>
                <span className="min-w-0 flex-1 leading-snug text-ink-2">{i.label}</span>
                <StatusPill small status={i.status} />
              </div>
              {i.worst && (
                <p className={`ml-7 ${i.status === "violated" ? "text-[#ff9d94]" : "text-[#ffd27a]"}`}>
                  {i.worst.subject.replace(">", " → ")}: {fmtCheck(i.worst)}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      {res && a.intents.length > 0 && (
        <p className="mt-2 border-t border-dashed border-sim/40 pt-2 text-[12.5px] text-ink-2">
          <span className="sim-tag mr-1.5 !px-1.5 !py-0 !text-[11px]">predicted</span>
          {res.score}% of failure × intent cells survive a single failure
          {res.score_best != null && res.score_best > (res.score ?? 0) + 0.05 && <span className="text-[#ffd27a]"> (a plan could reach {res.score_best}%)</span>}.
          {res.worst[0] && <span className="text-ink-3"> Worst: {res.worst[0].label.toLowerCase()}.</span>}
        </p>
      )}
    </section>
  );
}
