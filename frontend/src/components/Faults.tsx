import { api } from "../lib/api";
import { ago } from "../lib/format";
import { act, useStore } from "../lib/store";

export default function ActiveFaults() {
  const snap = useStore((s) => s.snap)!;
  const active = snap.chaos.active;
  return (
    <section className="panel p-4">
      <div className="flex items-center justify-between">
        <h2 className="panel-title">Active faults</h2>
        <button className="btn btn-sm" disabled={!active.length} onClick={() => act(() => api.post("/api/chaos/revert_all"))}>
          Revert all
        </button>
      </div>
      {active.length === 0 ? (
        <p className="hint mt-1">The network is running as designed. Faults you inject appear here and can be undone one by one.</p>
      ) : (
        <ul className="mt-2 space-y-1.5">
          {active.map((i) => (
            <li key={i.id} className="flex items-center justify-between gap-2 rounded border border-[#5a4520] bg-[#2c2617] px-3 py-1.5 text-[13px]">
              <span>
                {i.label}
                <span className="hint ml-2">{ago(i.t, snap.t)}</span>
              </span>
              <button className="btn btn-sm" onClick={() => act(() => api.post(`/api/chaos/revert/${i.id}`))}>
                Revert
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
