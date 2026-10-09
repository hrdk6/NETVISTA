import { api } from "../lib/api";
import { act, useStore } from "../lib/store";

export type Page = "live" | "simulate" | "validation" | "metrics" | "journey" | "scenarios";

const NAV: { id: Page; label: string }[] = [
  { id: "live", label: "Live network" },
  { id: "simulate", label: "What-if twin" },
  { id: "validation", label: "Validation" },
  { id: "metrics", label: "Metrics" },
  { id: "journey", label: "Packet journey" },
  { id: "scenarios", label: "Scenarios & demo" },
];

export default function Header({ page }: { page: Page }) {
  const conn = useStore((s) => s.conn);
  const snap = useStore((s) => s.snap);
  const topology = useStore((s) => s.topology);
  const mode = snap?.routing.mode;
  const demo = snap?.jobs?.demo as { running?: boolean } | undefined;
  const rec = snap?.jobs?.recorder as { recording?: boolean; name?: string } | undefined;
  const agentsDown = snap ? Object.values(snap.agents).filter((a) => !a).length : 0;

  return (
    <header className="flex h-14 shrink-0 items-center gap-6 border-b border-line bg-panel px-5">
      <div className="flex items-baseline gap-2.5">
        <span className="font-cond text-[19px] font-semibold tracking-[0.06em]">NETVISTA</span>
        <span className="hint hidden xl:inline">{topology?.name ?? "network digital twin"}</span>
      </div>
      <nav className="flex h-full items-stretch gap-1" aria-label="Pages">
        {NAV.map((n) => (
          <a
            key={n.id}
            href={`#${n.id}`}
            aria-current={page === n.id ? "page" : undefined}
            className={`flex items-center border-b-2 px-3 text-[13.5px] font-medium ${
              page === n.id ? "border-ink text-ink" : "border-transparent text-ink-3 hover:text-ink-2"
            }`}
          >
            {n.label}
          </a>
        ))}
      </nav>
      <div className="ml-auto flex items-center gap-3">
        {rec?.recording && (
          <span className="inline-flex items-center gap-1.5 text-[12.5px] text-[#ff8a80]">
            <span className="h-2 w-2 rounded-full bg-[#d03b3b]" /> Recording “{rec.name}”
          </span>
        )}
        <Conn conn={conn} agentsDown={agentsDown} />
        {mode && (
          <span className="text-[12.5px] text-ink-3">
            Routing <span className="font-semibold text-ink">{mode === "adaptive" ? "adaptive" : "static"}</span>
          </span>
        )}
        <button
          className="btn btn-primary"
          disabled={!snap || demo?.running}
          onClick={() => {
            location.hash = "#live";
            void act(() => api.post("/api/demo/start"), "Demo started: watch the topology and the event log");
          }}
        >
          {demo?.running ? "Demo running…" : "Run demo"}
        </button>
      </div>
    </header>
  );
}

function Conn({ conn, agentsDown }: { conn: string; agentsDown: number }) {
  const ok = conn === "open" && agentsDown === 0;
  const text = conn !== "open" ? (conn === "connecting" ? "Connecting" : "Backend offline") : agentsDown ? `${agentsDown} probe agents down` : "Emulation live";
  return (
    <span className="inline-flex items-center gap-1.5 text-[12.5px] text-ink-2" title="WebSocket to the backend that drives the Mininet emulation">
      <span className={`h-2 w-2 rounded-full ${ok ? "bg-[#0ca30c]" : conn === "connecting" ? "bg-[#fab219]" : "bg-[#d03b3b]"}`} />
      {text}
    </span>
  );
}
