import { create } from "zustand";
import { api } from "./api";
import type { AddressPlan, NvEvent, Snapshot, Topology } from "./types";

export type Selection = { kind: "node" | "link"; id: string } | null;

interface LinkPoint {
  t: number;
  util: number;
  rtt: number | null;
  loss: number | null;
}

interface State {
  conn: "connecting" | "open" | "closed";
  status: string;
  error: string | null;
  topology: Topology | null;
  plan: AddressPlan | null;
  pairs: string[];
  snap: Snapshot | null;
  lastTick: number;
  events: NvEvent[];
  linkSeries: Record<string, LinkPoint[]>;
  selected: Selection;
  toast: { msg: string; kind: "error" | "ok" } | null;
  select: (s: Selection) => void;
  notify: (msg: string, kind?: "error" | "ok") => void;
}

const MAX_EVENTS = 600;
const MAX_POINTS = 240; // 2 minutes at 2 Hz

export const useStore = create<State>((set) => ({
  conn: "connecting",
  status: "starting",
  error: null,
  topology: null,
  plan: null,
  pairs: [],
  snap: null,
  lastTick: 0,
  events: [],
  linkSeries: {},
  selected: null,
  toast: null,
  select: (s) => set({ selected: s }),
  notify: (msg, kind = "error") => {
    set({ toast: { msg, kind } });
    window.setTimeout(() => set((st) => (st.toast?.msg === msg ? { toast: null } : {})), 5000);
  },
}));

function mergeEvents(prev: NvEvent[], incoming: NvEvent[]): NvEvent[] {
  if (!incoming.length) return prev;
  const last = prev.length ? prev[prev.length - 1].seq : 0;
  const fresh = incoming.filter((e) => e.seq > last);
  if (!fresh.length) return prev;
  const out = prev.concat(fresh);
  return out.length > MAX_EVENTS ? out.slice(out.length - MAX_EVENTS) : out;
}

async function loadTopology(): Promise<void> {
  for (;;) {
    try {
      const t = await api.get<{ topology: Topology; plan: AddressPlan; flow_pairs: string[]; status: string; error: string | null }>("/api/topology");
      useStore.setState({ topology: t.topology, plan: t.plan, pairs: t.flow_pairs, status: t.status, error: t.error });
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 1500));
    }
  }
}

let started = false;

export function connect(): void {
  if (started) return;
  started = true;
  void loadTopology();
  let retry = 500;
  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    useStore.setState({ conn: "connecting" });
    ws.onopen = () => {
      retry = 500;
      useStore.setState({ conn: "open" });
    };
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.type === "hello") {
        useStore.setState((s) => ({ status: msg.status, error: msg.error, events: mergeEvents(s.events, msg.events ?? []) }));
        if (msg.status === "running") void loadTopology();
      } else if (msg.type === "status") {
        useStore.setState((s) => ({ status: msg.status, error: msg.error, events: mergeEvents(s.events, msg.events ?? []) }));
      } else if (msg.type === "tick") {
        const snap: Snapshot = msg.snapshot;
        useStore.setState((s) => {
          const series = { ...s.linkSeries };
          for (const [id, l] of Object.entries(snap.links)) {
            const arr = series[id] ? series[id].slice(-(MAX_POINTS - 1)) : [];
            arr.push({ t: snap.t, util: l.util, rtt: l.rtt_ms, loss: l.loss_pct });
            series[id] = arr;
          }
          return {
            snap,
            status: "running",
            error: null,
            lastTick: Date.now(),
            linkSeries: series,
            events: mergeEvents(s.events, msg.events ?? []),
          };
        });
      }
    };
    ws.onclose = () => {
      useStore.setState({ conn: "closed" });
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 5000);
    };
    // keep NAT/WSL port-forwarding warm
    const ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 15000);
    ws.addEventListener("close", () => clearInterval(ping));
  };
  open();
}

/** Run an API action, surfacing failures as a toast instead of silently failing. */
export async function act<T>(fn: () => Promise<T>, okMsg?: string): Promise<T | undefined> {
  try {
    const r = await fn();
    if (okMsg) useStore.getState().notify(okMsg, "ok");
    return r;
  } catch (e) {
    useStore.getState().notify(e instanceof Error ? e.message : String(e));
    return undefined;
  }
}
