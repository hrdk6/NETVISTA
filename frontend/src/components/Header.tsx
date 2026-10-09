import { api } from "../lib/api";
import { useCopilot } from "../lib/copilot";
import { act, useStore } from "../lib/store";
import { Spark } from "./Copilot";

export type Page = "live" | "simulate" | "validation" | "ai" | "metrics" | "journey" | "scenarios";

const NAV: { id: Page; label: string }[] = [
  { id: "live", label: "Live network" },
  { id: "simulate", label: "What-if twin" },
  { id: "validation", label: "Validation" },
  { id: "ai", label: "AI ops" },
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
    <header className="flex h-14 shrink-0 items-center gap-4 border-b border-line bg-panel px-3 sm:px-5 lg:gap-6">
      <div className="flex shrink-0 items-baseline gap-2.5">
        <span className="font-cond text-[19px] font-semibold tracking-[0.06em]">NETVISTA</span>
        <span className="hint hidden xl:inline">{topology?.name ?? "network digital twin"}</span>
      </div>
      <nav className="flex h-full min-w-0 flex-1 items-stretch gap-1 overflow-x-auto [scrollbar-width:none] [mask-image:linear-gradient(to_right,black_88%,transparent)] 2xl:[mask-image:none]" aria-label="Pages">
        {NAV.map((n) => (
          <a
            key={n.id}
            href={`#${n.id}`}
            aria-current={page === n.id ? "page" : undefined}
            className={`flex shrink-0 items-center border-b-2 px-2.5 text-[13.5px] font-medium whitespace-nowrap lg:px-3 ${
              page === n.id ? "border-ink text-ink" : "border-transparent text-ink-3 hover:text-ink-2"
            }`}
          >
            {n.label}
          </a>
        ))}
      </nav>
      <div className="ml-auto flex shrink-0 items-center gap-3">
        {rec?.recording && (
          <span className="inline-flex items-center gap-1.5 text-[12.5px] text-[#ff8a80]">
            <span className="h-2 w-2 rounded-full bg-[#d03b3b]" /> Recording “{rec.name}”
          </span>
        )}
        <Conn conn={conn} agentsDown={agentsDown} />
        <CopilotButton />
        {mode && (
          <span className="hidden text-[12.5px] whitespace-nowrap text-ink-3 lg:inline">
            Routing <span className="font-semibold text-ink">{mode === "adaptive" ? "adaptive" : "static"}</span>
          </span>
        )}
        <button
          className="btn btn-primary hidden sm:inline-flex"
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
    <span className="inline-flex items-center gap-1.5 text-[12.5px] whitespace-nowrap text-ink-2" title={`${text}: WebSocket to the backend that drives the Mininet emulation`}>
      <span className={`h-2 w-2 rounded-full ${ok ? "bg-[#0ca30c]" : conn === "connecting" ? "bg-[#fab219]" : "bg-[#d03b3b]"}`} />
      <span className="hidden lg:inline">{text}</span>
    </span>
  );
}

function CopilotButton() {
  const open = useCopilot((s) => s.open);
  const setOpen = useCopilot((s) => s.setOpen);
  const streaming = useCopilot((s) => s.streaming);
  const ai = useStore((s) => s.snap?.ai);
  const causes = ai?.diagnosis.causes.length ?? 0;
  return (
    <button
      className="btn relative"
      aria-pressed={open}
      onClick={() => setOpen(!open)}
      title={`Ask the copilot about the live network (Ctrl+K)${ai?.copilot.available ? `: ${ai.copilot.label}` : ""}`}
    >
      <Spark size={13} className={streaming ? "animate-pulse" : ""} />
      <span className="hidden md:inline">Copilot</span>
      <kbd className="hidden rounded border border-line-strong px-1 text-[10.5px] font-normal text-ink-3 2xl:inline">Ctrl K</kbd>
      {causes > 0 && (
        <span className="absolute -top-1 -right-1 h-2.5 w-2.5 rounded-full border-2 border-panel bg-[#fab219]" title="The AI layer has a diagnosis" />
      )}
    </button>
  );
}
