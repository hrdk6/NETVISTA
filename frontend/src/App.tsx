import { useEffect, useState } from "react";
import CopilotDrawer from "./components/Copilot";
import Header, { type Page } from "./components/Header";
import { useStore } from "./lib/store";
import AIPage from "./pages/AIPage";
import AssurePage from "./pages/AssurePage";
import DesignerPage from "./pages/DesignerPage";
import JourneyPage from "./pages/JourneyPage";
import LivePage from "./pages/LivePage";
import MetricsPage from "./pages/MetricsPage";
import ScenariosPage from "./pages/ScenariosPage";
import SimulatePage from "./pages/SimulatePage";
import ValidationPage from "./pages/ValidationPage";

const PAGES: Page[] = ["live", "assure", "design", "simulate", "validation", "ai", "metrics", "journey", "scenarios"];

function pageFromHash(): Page {
  const h = location.hash.replace("#", "").split("?")[0] as Page;
  return PAGES.includes(h) ? h : "live";
}

export default function App() {
  const [page, setPage] = useState<Page>(pageFromHash);
  const { status, error, topology, toast, snap } = useStore();

  useEffect(() => {
    const on = () => setPage(pageFromHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <Header page={page} />
      <main className="min-h-0 flex-1 overflow-auto">
        {status === "error" ? (
          <StartupError error={error} />
        ) : !topology || !snap || status === "restarting" || status === "starting" ? (
          <Booting status={status} />
        ) : (
          <>
            {page === "live" && <LivePage />}
            {page === "assure" && <AssurePage />}
            {page === "design" && <DesignerPage />}
            {page === "simulate" && <SimulatePage />}
            {page === "validation" && <ValidationPage />}
            {page === "ai" && <AIPage />}
            {page === "metrics" && <MetricsPage />}
            {page === "journey" && <JourneyPage />}
            {page === "scenarios" && <ScenariosPage />}
          </>
        )}
      </main>
      {topology && snap && <CopilotDrawer />}
      {toast && (
        <div
          role="status"
          className={`fixed right-4 bottom-4 z-50 max-w-md rounded-md border px-4 py-3 text-[13px] ${
            toast.kind === "error" ? "border-[#7a3434] bg-[#3a2326] text-ink" : "border-[#2f5a3a] bg-[#1f3328] text-ink"
          }`}
        >
          {toast.msg}
        </div>
      )}
    </div>
  );
}

function Booting({ status }: { status: string }) {
  const events = useStore((s) => s.events);
  const conn = useStore((s) => s.conn);
  return (
    <div className="mx-auto mt-24 max-w-xl px-6">
      <h1 className="font-cond text-2xl font-semibold">{status === "restarting" ? "Deploying the new topology" : "Starting the emulated network"}</h1>
      <p className="mt-2 text-ink-2">
        {conn !== "open"
          ? "Waiting for the NETVISTA backend on this machine. Start it with scripts/run.ps1 (Windows) or sudo scripts/run.sh (Linux)."
          : status === "restarting"
            ? "The running network is being torn down (probe agents, iperf3, Mininet namespaces and Open vSwitch bridges), then the new design boots as real Linux routers. This takes 10 to 30 seconds."
            : `Backend is ${status}. Mininet is creating namespaces, links and queues; this takes a few seconds.`}
      </p>
      <ul className="mt-6 space-y-1 text-[13px] text-ink-3">
        {events.slice(-6).map((e) => (
          <li key={e.seq}>{e.message}</li>
        ))}
      </ul>
    </div>
  );
}

function StartupError({ error }: { error: string | null }) {
  return (
    <div className="mx-auto mt-24 max-w-2xl px-6">
      <h1 className="font-cond text-2xl font-semibold text-[#ff8a80]">The emulated network did not start</h1>
      <pre className="panel mt-4 overflow-auto p-4 text-[13px] whitespace-pre-wrap text-ink-2">{error}</pre>
      <p className="mt-4 text-ink-2">
        NETVISTA needs root inside Linux/WSL for Mininet. Run <code className="text-ink">scripts/run.ps1</code> on Windows, or{" "}
        <code className="text-ink">sudo bash scripts/run.sh</code> on Linux. If Open vSwitch is the problem, try{" "}
        <code className="text-ink">NETVISTA_SWITCH=linuxbridge</code>.
      </p>
    </div>
  );
}
