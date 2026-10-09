import { useEffect, useState } from "react";
import { Spark } from "../components/Copilot";
import { Benchmark, Drills, Ledger, Uncertainty } from "../components/assure/Evidence";
import { IntentsPanel } from "../components/assure/IntentsPanel";
import { AutopilotPanel, PlanPanel } from "../components/assure/Plan";
import { FragilityMap, ResilienceMatrix } from "../components/assure/Resilience";
import { api } from "../lib/api";
import type { BenchRun, Decision, DrillRun, IntentRow, LedgerEntry, Plan, Resilience, UncertaintyClass } from "../lib/assure";
import { ask } from "../lib/copilot";
import { usePoll } from "../lib/poll";
import { act, useStore } from "../lib/store";

type Tab = "assure" | "plan" | "evidence";

function tabFromHash(): Tab {
  const q = location.hash.split("?")[1] ?? "";
  const t = new URLSearchParams(q).get("tab");
  return t === "plan" || t === "evidence" ? t : "assure";
}

export default function AssurePage() {
  const [tab, setTabState] = useState<Tab>(tabFromHash);
  const setTab = (t: Tab) => {
    setTabState(t);
    history.replaceState(null, "", `#assure${t === "assure" ? "" : `?tab=${t}`}`);
  };
  const assure = useStore((s) => s.snap?.assure);
  const resId = assure?.resilience?.id;
  const intents = usePoll<IntentRow[]>("/api/assure/intents", 2000);
  const res = usePoll<Resilience>("/api/assure/resilience", 0, resId);
  const plans = usePoll<Plan[]>(tab === "plan" ? "/api/assure/plans" : null, 4000);
  const autopilot = usePoll<{ decisions: Decision[]; ledger: LedgerEntry[] }>(tab !== "assure" ? "/api/assure/autopilot" : null, 3000);
  const drills = usePoll<DrillRun[]>(tab === "evidence" ? "/api/assure/drills" : null, 4000);
  const pool = usePoll<Record<string, UncertaintyClass>>(tab === "evidence" ? "/api/assure/uncertainty" : null, 8000);
  const bench = usePoll<BenchRun[]>(tab === "evidence" ? "/api/assure/benchmarks" : null, 10000);
  const scen = usePoll<{ scenarios: { id: string; label: string }[] }>("/api/assure/scenarios", 0);

  useEffect(() => {
    const on = () => setTabState(tabFromHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  const refreshAll = () => {
    intents.refresh();
    res.refresh();
    plans.refresh();
    autopilot.refresh();
  };

  return (
    <div className="mx-auto flex max-w-[1720px] flex-col gap-3 p-3">
      <Hero onChanged={refreshAll} />
      <DemoPanel />
      <div className="flex items-center gap-1 border-b border-line" role="tablist" aria-label="Assure views">
        {(
          [
            ["assure", "Intents & resilience"],
            ["plan", "Plan & autopilot"],
            ["evidence", "Evidence"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={`-mb-px border-b-2 px-3 py-2 text-[13.5px] font-medium ${tab === id ? "border-ink text-ink" : "border-transparent text-ink-3 hover:text-ink-2"}`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "assure" && (
        <div className="grid gap-3 2xl:grid-cols-[480px_minmax(0,1fr)] xl:grid-cols-[420px_minmax(0,1fr)]">
          <div className="flex min-w-0 flex-col gap-3">
            <IntentsPanel rows={intents.data} onChanged={refreshAll} />
          </div>
          <div className="flex min-w-0 flex-col gap-3">
            <FragilityMap res={res.data && res.data.t ? res.data : null} className="h-[440px]" />
            <ResilienceMatrix res={res.data && res.data.t ? res.data : null} intents={intents.data} onRefresh={res.refresh} />
          </div>
        </div>
      )}
      {tab === "plan" && (
        <div className="flex flex-col gap-3">
          <PlanPanel plans={plans.data} onChanged={refreshAll} />
          <AutopilotPanel decisions={autopilot.data?.decisions ?? null} onChanged={refreshAll} />
        </div>
      )}
      {tab === "evidence" && (
        <div className="flex flex-col gap-3">
          <Benchmark runs={bench.data} onChanged={bench.refresh} />
          <div className="grid gap-3 xl:grid-cols-2">
            <Drills runs={drills.data} scenarios={scen.data?.scenarios ?? []} onChanged={drills.refresh} />
            <Uncertainty pool={pool.data} />
          </div>
          <Ledger entries={autopilot.data?.ledger ?? null} />
        </div>
      )}
    </div>
  );
}

type DemoStatus = { running?: boolean; steps?: string[]; index?: number; notes?: string[]; step_ends_at?: number };

function DemoPanel() {
  const demo = useStore((s) => (s.snap?.jobs?.assure as { demo?: DemoStatus } | undefined)?.demo);
  const now = useStore((s) => s.snap?.t ?? 0);
  if (!demo || (!demo.running && !demo.notes?.length)) return null;
  return (
    <section className="panel p-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="panel-title">{demo.running ? "Assure demo in progress" : "Assure demo finished"}</h2>
        {demo.running && (
          <span className="flex items-center gap-3">
            {demo.step_ends_at && demo.step_ends_at > now && <span className="hint num">{(demo.step_ends_at - now).toFixed(0)} s</span>}
            <button className="btn btn-sm" onClick={() => act(() => api.post("/api/assure/demo/stop"))}>
              Stop
            </button>
          </span>
        )}
      </div>
      <div className="mt-2 grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)]">
        <ol className="space-y-1 text-[13px]">
          {(demo.steps ?? []).map((st, i) => (
            <li key={st} className={i === demo.index && demo.running ? "font-semibold text-ink" : i < (demo.index ?? 0) || !demo.running ? "text-ink-3" : "text-ink-3/60"}>
              <span className="num mr-2">{i + 1}.</span>
              {st}
              {i === demo.index && demo.running ? " …" : ""}
            </li>
          ))}
        </ol>
        <ul className="space-y-1.5 border-l border-line pl-4 text-[13px] text-ink-2">
          {(demo.notes ?? []).map((n, i) => (
            <li key={i} className={i === (demo.notes?.length ?? 0) - 1 ? "text-ink" : ""}>
              {n}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function Tile({ label, children, sub, tone }: { label: string; children: React.ReactNode; sub?: React.ReactNode; tone?: "live" | "sim" }) {
  return (
    <div className={`min-w-0 rounded-md border px-4 py-3 ${tone === "sim" ? "border-dashed border-sim/50 bg-[#1d2a38]" : "border-line bg-raised/40"}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="font-cond text-[12.5px] font-semibold tracking-wide text-ink-3 uppercase">{label}</span>
        {tone === "sim" ? <span className="text-[11px] text-sim">predicted</span> : <span className="text-[11px] text-ink-3">live</span>}
      </div>
      <div className="mt-1">{children}</div>
      {sub && <div className="mt-0.5 text-[12px] text-ink-3">{sub}</div>}
    </div>
  );
}

function Hero({ onChanged }: { onChanged: () => void }) {
  const snap = useStore((s) => s.snap)!;
  const a = snap.assure;
  const counts = a?.counts ?? {};
  const total = (a?.intents ?? []).filter((i) => i.status !== "disabled").length;
  const met = (counts.ok ?? 0) + (counts.at_risk ?? 0);
  const res = a?.resilience;
  const r = snap.routing;
  const ap = a?.autopilot;
  const startStress = async () => {
    const prof = await api.get<Record<string, number>>("/api/assure/stress_profile");
    await act(() => api.post("/api/traffic/stop", {}));
    await act(
      () => api.post("/api/traffic/start", { flows: Object.entries(prof).map(([p, rate]) => ({ src: p.split(">")[0], dst: p.split(">")[1], rate_mbps: rate })) }),
      "Stress traffic started on every flow",
    );
    onChanged();
  };
  return (
    <section className="panel p-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-3xl">
          <h1 className="font-cond text-[22px] font-semibold">Assure: intent-based assurance</h1>
          <p className="mt-1 text-ink-2">
            Say what the network must guarantee. NETVISTA checks it every second on the live network, predicts which guarantees would survive each possible failure,
            plans routes and backups that keep them, applies a plan only through a safety gate, and then proves on live measurements whether the prediction was right.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            className="btn btn-primary"
            onClick={() => act(() => api.post("/api/assure/demo/start"), "Assure demo started: about two minutes, narrated below")}
            title="Two minutes on the live network: intents, stress, plan, verification, a protected failure, recovery"
          >
            Run Assure demo
          </button>
          <button className="btn" onClick={startStress} title="Every managed flow carries iperf3 traffic (~40% of the source site's uplinks): the load at which routing choices matter">
            Start stress traffic
          </button>
          <button className="btn" onClick={() => ask("Summarise the intents: which hold now, which would break after a single failure, whether those breaks are avoidable, and what you would change.")}>
            <Spark size={12} /> Ask the copilot
          </button>
        </div>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Tile label="Intents met now" sub={total ? `${counts.violated ?? 0} violated · ${counts.at_risk ?? 0} at risk · ${counts.unknown ?? 0} no data` : "no intents yet"}>
          <span className="num font-cond text-[28px] leading-none font-semibold">
            {met}
            <span className="text-[18px] text-ink-3">/{total}</span>
          </span>
        </Tile>
        <Tile label="Survive a single failure" tone="sim" sub={res ? (res.score_best != null ? `best any routing could do: ${res.score_best}%` : `${res.mode} routing`) : "analysing…"}>
          <span className="num font-cond text-[28px] leading-none font-semibold">{res?.score ?? "–"}%</span>
          <span className="ml-2 text-[12.5px] text-ink-3">of failure × intent cells hold</span>
        </Tile>
        <Tile label="Routing" sub={r.plan ? `${r.plan.protected.length} failures pre-planned${r.scenario ? ` · serving ${r.scenario}` : ""}` : r.mode === "adaptive" ? `herd guard ${r.herd_guard ? "on" : "off"}` : "never reacts"}>
          <span className="font-cond text-[22px] leading-none font-semibold">{r.mode === "intent" ? `Plan ${r.plan?.id ?? ""}` : r.mode === "adaptive" ? "Adaptive" : "Static"}</span>
        </Tile>
        <Tile label="Autopilot" sub={ap?.last ? `last: ${ap.last.status}${ap.last.summary ? ` – ${ap.last.summary}` : ""}` : "no decisions yet"}>
          <div className="flex items-center justify-between gap-2">
            <span className="font-cond text-[22px] leading-none font-semibold capitalize">{ap?.mode ?? "off"}</span>
            <div className="seg" role="group" aria-label="Autopilot mode">
              {(["off", "shadow", "approve", "auto"] as const).map((m) => (
                <button key={m} aria-pressed={ap?.mode === m} onClick={() => act(() => api.post("/api/assure/autopilot/mode", { mode: m })).then(onChanged)} className="!px-2 capitalize">
                  {m}
                </button>
              ))}
            </div>
          </div>
        </Tile>
      </div>
    </section>
  );
}
