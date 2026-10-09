import { create } from "zustand";
import { api } from "./api";
import { useStore } from "./store";
import type { Proposal } from "./types";

// The copilot drawer's state. The conversation itself (with every tool result) lives on the
// backend; this keeps what the drawer shows, streamed as server-sent events from /api/ai/chat.

export type ToolPart = {
  kind: "tool";
  id: string;
  name: string;
  label: string;
  toolKind: "read" | "simulate" | "propose";
  input: unknown;
  ok?: boolean;
  ms?: number;
  result?: string;
  truncated?: boolean;
};
export type ProposalPart = { kind: "proposal"; proposal: Proposal };
export type Part = { kind: "text"; text: string } | ToolPart | ProposalPart;

export interface Grounding {
  checked: number;
  grounded: number;
  ungrounded: string[];
  sources: number;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  context?: string;
  parts: Part[];
  grounding?: Grounding;
  error?: string;
  done?: boolean;
  ms?: number;
  rounds?: number;
  usage?: { input_tokens: number; output_tokens: number };
  model?: string;
  notice?: string;
  t: number;
}

interface CopilotState {
  open: boolean;
  conversationId: string | null;
  messages: ChatMessage[];
  streaming: boolean;
  draft: string;
  setOpen: (open: boolean) => void;
  setDraft: (d: string) => void;
  send: (text: string, context?: string) => Promise<void>;
  stop: () => void;
  newChat: () => void;
  setProposal: (p: Proposal) => void;
}

const KEY = "netvista.copilot.v1";

function load(): { conversationId: string | null; messages: ChatMessage[] } {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const j = JSON.parse(raw);
      const messages = (j.messages ?? []).map((m: ChatMessage) => ({ ...m, done: true }));
      return { conversationId: j.conversationId ?? null, messages };
    }
  } catch {
    /* storage unavailable: start empty */
  }
  return { conversationId: null, messages: [] };
}

function save(s: { conversationId: string | null; messages: ChatMessage[] }) {
  try {
    localStorage.setItem(KEY, JSON.stringify({ conversationId: s.conversationId, messages: s.messages.slice(-40) }));
  } catch {
    /* storage full or blocked: the drawer still works for this session */
  }
}

let abort: AbortController | null = null;
const uid = () => Math.random().toString(36).slice(2, 10);

/** What the user is looking at, so "this link" means something to the copilot. */
export function dashboardContext(): string {
  const page = (location.hash.replace("#", "") || "live").split("?")[0];
  const names: Record<string, string> = {
    live: "Live network", simulate: "What-if twin", validation: "Validation", metrics: "Metrics",
    journey: "Packet journey", scenarios: "Scenarios & demo", ai: "AI operations",
  };
  const sel = useStore.getState().selected;
  return `the user is on the ${names[page] ?? page} page` + (sel ? `, with ${sel.kind} ${sel.id} selected` : "");
}

export const useCopilot = create<CopilotState>((set, get) => {
  const initial = load();
  const patchLast = (fn: (m: ChatMessage) => ChatMessage) =>
    set((s) => {
      const msgs = s.messages.slice();
      const i = msgs.length - 1;
      if (i >= 0 && msgs[i].role === "assistant") msgs[i] = fn(msgs[i]);
      return { messages: msgs };
    });

  const handle = (ev: Record<string, unknown>) => {
    switch (ev.type) {
      case "start":
        set({ conversationId: ev.conversation_id as string });
        patchLast((m) => ({
          ...m,
          model: ev.label as string,
          notice: ev.renewed ? "The backend restarted, so this is a fresh conversation (earlier answers are kept above)." : undefined,
        }));
        break;
      case "text":
        patchLast((m) => {
          const parts = m.parts.slice();
          const last = parts[parts.length - 1];
          if (last && last.kind === "text") parts[parts.length - 1] = { kind: "text", text: last.text + (ev.delta as string) };
          else if ((ev.delta as string).trim()) parts.push({ kind: "text", text: ev.delta as string });
          return { ...m, parts };
        });
        break;
      case "tool_call":
        patchLast((m) => ({
          ...m,
          parts: [...m.parts, { kind: "tool", id: ev.id as string, name: ev.name as string, label: ev.label as string, toolKind: ev.kind as ToolPart["toolKind"], input: ev.input }],
        }));
        break;
      case "tool_result":
        patchLast((m) => ({
          ...m,
          parts: m.parts.map((p) =>
            p.kind === "tool" && p.id === ev.id ? { ...p, ok: ev.ok as boolean, ms: ev.ms as number, result: ev.result as string, truncated: ev.truncated as boolean } : p,
          ),
        }));
        break;
      case "proposal": {
        const { type: _t, ...proposal } = ev;
        patchLast((m) => ({ ...m, parts: [...m.parts, { kind: "proposal", proposal: proposal as unknown as Proposal }] }));
        break;
      }
      case "grounding":
        patchLast((m) => ({ ...m, grounding: ev as unknown as Grounding }));
        break;
      case "done":
        patchLast((m) => ({ ...m, done: true, ms: ev.ms as number, rounds: ev.rounds as number, usage: ev.usage as ChatMessage["usage"] }));
        break;
      case "error":
        patchLast((m) => ({ ...m, done: true, error: ev.message as string }));
        break;
    }
  };

  return {
    open: false,
    conversationId: initial.conversationId,
    messages: initial.messages,
    streaming: false,
    draft: "",
    setOpen: (open) => set({ open }),
    setDraft: (draft) => set({ draft }),
    setProposal: (p) => {
      set((s) => ({
        messages: s.messages.map((m) => ({
          ...m,
          parts: m.parts.map((x) => (x.kind === "proposal" && x.proposal.id === p.id ? { ...x, proposal: p } : x)),
        })),
      }));
      save(get());
    },
    newChat: () => {
      const id = get().conversationId;
      if (id) void api.del(`/api/ai/conversations/${id}`).catch(() => {});
      abort?.abort();
      set({ conversationId: null, messages: [], streaming: false });
      save(get());
    },
    stop: () => {
      void api.post("/api/ai/chat/cancel").catch(() => {});
      abort?.abort();
    },
    send: async (text, context) => {
      if (get().streaming || !text.trim()) return;
      const ctx = context ?? dashboardContext();
      set((s) => ({
        open: true,
        streaming: true,
        draft: "",
        messages: [
          ...s.messages,
          { id: uid(), role: "user", text, context: ctx, parts: [], t: Date.now() / 1000, done: true },
          { id: uid(), role: "assistant", text: "", parts: [], t: Date.now() / 1000 },
        ],
      }));
      abort = new AbortController();
      try {
        const res = await fetch("/api/ai/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text, conversation_id: get().conversationId, context: ctx }),
          signal: abort.signal,
        });
        if (!res.ok || !res.body) {
          let detail = res.statusText;
          try {
            detail = (await res.json()).detail ?? detail;
          } catch {
            /* not JSON */
          }
          throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i: number;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const chunk = buf.slice(0, i);
            buf = buf.slice(i + 2);
            for (const line of chunk.split("\n")) {
              if (line.startsWith("data: ")) {
                try {
                  handle(JSON.parse(line.slice(6)));
                } catch {
                  /* malformed line: skip */
                }
              }
            }
          }
        }
        patchLast((m) => (m.done ? m : { ...m, done: true }));
      } catch (e) {
        const aborted = e instanceof DOMException && e.name === "AbortError";
        patchLast((m) => ({ ...m, done: true, error: aborted ? "Stopped." : e instanceof Error ? e.message : String(e) }));
      } finally {
        abort = null;
        set({ streaming: false });
        save(get());
      }
    },
  };
});

/** Open the copilot and ask something (used by the "Ask" buttons around the dashboard). */
export function ask(prompt: string, context?: string) {
  const c = useCopilot.getState();
  c.setOpen(true);
  if (c.streaming) {
    c.setDraft(prompt);
    return;
  }
  void c.send(prompt, context);
}
