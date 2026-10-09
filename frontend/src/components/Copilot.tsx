import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { type ChatMessage, type Part, type ProposalPart, type ToolPart, useCopilot } from "../lib/copilot";
import { act, useStore } from "../lib/store";
import type { Proposal } from "../lib/types";
import Markdown from "./Markdown";

// The copilot drawer. Everything the model says is grounded in tool calls the user can open,
// every change it suggests is a card the user has to apply, and each answer carries the result
// of the grounding check (measured values traced back to the data the model read).

export function Spark({ size = 14, className = "" }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true" className={className}>
      <path d="M8 0.5 9.7 6.3 15.5 8 9.7 9.7 8 15.5 6.3 9.7 0.5 8 6.3 6.3Z" fill="currentColor" />
      <path d="M13.2 1.2 13.8 2.9 15.5 3.5 13.8 4.1 13.2 5.8 12.6 4.1 10.9 3.5 12.6 2.9Z" fill="currentColor" opacity="0.6" />
    </svg>
  );
}

const SUGGESTIONS = [
  "What is happening on the network right now?",
  "Is anything unusual compared to normal behaviour?",
  "Why is c1 → srv1 on its current path?",
  "What would happen if router r2 crashed?",
  "Write a post-mortem of the latest incident",
];

export default function CopilotDrawer() {
  const open = useCopilot((s) => s.open);
  const setOpen = useCopilot((s) => s.setOpen);
  const messages = useCopilot((s) => s.messages);
  const streaming = useCopilot((s) => s.streaming);
  const newChat = useCopilot((s) => s.newChat);
  const brief = useStore((s) => s.snap?.ai?.copilot);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(!useCopilot.getState().open);
      } else if (e.key === "Escape" && useCopilot.getState().open) {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setOpen]);

  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [messages, open]);

  if (!open) return null;
  return (
    <aside
      className="fixed top-14 right-0 bottom-0 z-40 flex w-full flex-col border-l border-line-strong bg-panel shadow-[-12px_0_32px_rgba(0,0,0,0.35)] sm:w-[460px]"
      aria-label="Copilot"
    >
      <div className="flex items-center gap-2.5 border-b border-line px-4 py-2.5">
        <Spark size={16} className="text-ink" />
        <div className="min-w-0 flex-1">
          <h2 className="panel-title leading-tight">Copilot</h2>
          <p className="hint truncate">{brief?.available ? brief.label : brief ? "No language model configured" : "Connecting…"}</p>
        </div>
        {messages.length > 0 && (
          <button className="btn btn-sm" onClick={newChat} disabled={streaming}>
            New chat
          </button>
        )}
        <button className="btn btn-sm" onClick={() => setOpen(false)} aria-label="Close copilot" title="Close (Esc)">
          Close
        </button>
      </div>

      <div
        ref={scroller}
        className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
        }}
      >
        {messages.length === 0 ? <Intro /> : messages.map((m) => (m.role === "user" ? <UserBubble key={m.id} m={m} /> : <Answer key={m.id} m={m} />))}
      </div>

      <Composer />
    </aside>
  );
}

function Intro() {
  const brief = useStore((s) => s.snap?.ai?.copilot);
  const send = useCopilot((s) => s.send);
  if (brief && !brief.available) return <SetupHelp />;
  return (
    <div className="space-y-4">
      <div className="space-y-2 text-[13.5px] text-ink-2">
        <p>Ask about the live network in plain words. The copilot reads the same probes, counters and controller state as this dashboard, and can ask the digital twin what a change would do.</p>
        <p>It never changes the network on its own: anything it suggests appears as a card that you apply yourself.</p>
        {brief?.local && <p className="hint">Running on a local model on this PC&apos;s CPU, so answers take a minute or two.</p>}
      </div>
      <div className="flex flex-col items-start gap-1.5">
        {SUGGESTIONS.map((s) => (
          <button key={s} className="rounded-md border border-line px-3 py-1.5 text-left text-[13px] text-ink-2 hover:border-line-strong hover:text-ink" onClick={() => send(s)}>
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

export function SetupHelp() {
  return (
    <div className="space-y-3 text-[13.5px] text-ink-2">
      <p className="text-ink">The copilot needs a language model. Pick one (keys go in the repo-root <code className="text-ink">.env</code>, then restart NETVISTA):</p>
      <div className="rounded-md border border-line p-3">
        <p className="font-semibold text-ink">Free: Google Gemini, with Groq as backup</p>
        <p className="mt-1">
          <code className="text-ink">GEMINI_API_KEY=…</code> (aistudio.google.com/apikey) and optionally <code className="text-ink">GROQ_API_KEY=…</code>{" "}
          (console.groq.com/keys). When Gemini is rate-limited or down, Groq answers.
        </p>
      </div>
      <div className="rounded-md border border-line p-3">
        <p className="font-semibold text-ink">Claude</p>
        <p className="mt-1">
          <code className="text-ink">ANTHROPIC_API_KEY=…</code> (console.anthropic.com).
        </p>
      </div>
      <div className="rounded-md border border-line p-3">
        <p className="font-semibold text-ink">A local model with Ollama</p>
        <p className="mt-1">
          Install Ollama, run <code className="text-ink">ollama pull qwen3:8b</code> (any model with tool support), then restart NETVISTA. It is found automatically.
        </p>
      </div>
      <p className="hint">Anomaly detection and root-cause analysis work without a language model.</p>
    </div>
  );
}

function UserBubble({ m }: { m: ChatMessage }) {
  return (
    <div className="flex flex-col items-end">
      <div className="max-w-[88%] rounded-lg rounded-br-sm bg-raised px-3 py-2 text-[13.5px] whitespace-pre-wrap text-ink">{m.text}</div>
    </div>
  );
}

function Answer({ m }: { m: ChatMessage }) {
  const flagged = m.grounding?.ungrounded ?? [];
  const running = !m.done;
  const tools = m.parts.filter((p): p is ToolPart => p.kind === "tool");
  const pending = tools.find((t) => t.ok === undefined);
  const local = useStore((s) => s.snap?.ai?.copilot.local);
  const idle = local ? "The local model is reading the question and its tools (a minute or two on CPU)…" : "Thinking…";
  return (
    <div className="space-y-2.5">
      {m.notice && <p className="hint">{m.notice}</p>}
      {m.parts.map((p, i) => (
        <PartView key={i} p={p} flagged={flagged} />
      ))}
      {running && (
        <p className="flex items-center gap-2 text-[12.5px] text-ink-3" role="status">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ink-2" />
          {pending ? `${pending.label}…` : m.parts.length ? (m.parts[m.parts.length - 1].kind === "text" ? "Writing…" : local ? "Reading the results (local model)…" : "Thinking…") : idle}
        </p>
      )}
      {m.error && <p className="text-[13px] text-[#ff8a80]">{m.error}</p>}
      {m.done && !m.error && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line pt-2 text-[11.5px] text-ink-3">
          <span className="ai-tag">
            <Spark size={10} /> AI answer
          </span>
          {m.grounding && <GroundingBadge g={m.grounding} />}
          <span className="ml-auto whitespace-nowrap">
            {m.model ? `${m.model}, ` : ""}
            {m.ms != null ? `${(m.ms / 1000).toFixed(1)} s` : ""}
          </span>
        </div>
      )}
    </div>
  );
}

function GroundingBadge({ g }: { g: NonNullable<ChatMessage["grounding"]> }) {
  if (g.checked === 0) return <span>No measured values quoted</span>;
  if (!g.ungrounded.length)
    return (
      <span className="text-ink-2" title="Every number with a unit was found in the tool results this conversation read">
        All {g.checked} measured values traced to live data
      </span>
    );
  return (
    <span className="text-ink-2" title={`Not found in the data it read: ${g.ungrounded.join(", ")}. Dotted underline in the answer.`}>
      {g.grounded} of {g.checked} values traced to data; {g.ungrounded.length} not found (dotted)
    </span>
  );
}

function PartView({ p, flagged }: { p: Part; flagged: string[] }) {
  if (p.kind === "text") return <Markdown text={p.text} flagged={flagged} />;
  if (p.kind === "tool") return <ToolRow t={p} />;
  return <ProposalCard p={p} />;
}

function ToolRow({ t }: { t: ToolPart }) {
  const [open, setOpen] = useState(false);
  const state = t.ok === undefined ? "running" : t.ok ? "ok" : "error";
  let pretty = t.result ?? "";
  if (open && t.result) {
    try {
      pretty = JSON.stringify(JSON.parse(t.result), null, 1);
    } catch {
      /* truncated JSON: show raw */
    }
  }
  return (
    <div className="rounded-md border border-line bg-[#1b2531] text-[12.5px]">
      <button className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left" onClick={() => t.result && setOpen(!open)} aria-expanded={open}>
        <span
          className={`h-1.5 w-1.5 shrink-0 rounded-full ${state === "running" ? "animate-pulse bg-ink-2" : state === "ok" ? "bg-[#0ca30c]" : "bg-[#d03b3b]"}`}
          aria-hidden="true"
        />
        <span className="min-w-0 flex-1 truncate text-ink-2">
          {t.label}
          {t.toolKind === "simulate" && <span className="sim-tag ml-2 !py-0 !text-[11px]">Simulation</span>}
        </span>
        {t.ms != null && <span className="num shrink-0 text-ink-3">{t.ms < 1000 ? `${t.ms} ms` : `${(t.ms / 1000).toFixed(1)} s`}</span>}
        {t.result && <span className="shrink-0 text-ink-3">{open ? "Hide data" : "Data"}</span>}
      </button>
      {open && (
        <pre className="max-h-64 overflow-auto border-t border-line px-2.5 py-2 text-[11.5px] leading-snug whitespace-pre-wrap text-ink-3">
          {pretty}
          {t.truncated ? "\n… (truncated)" : ""}
        </pre>
      )}
    </div>
  );
}

interface WhatIf {
  label: string;
  scenario: string;
  flows: Record<string, { measured_now: Row | null; twin_with_change: Row | null; controller_would_reroute: boolean; offered_mbps: number }>;
}
type Row = { path: string | null; rtt_p50_ms: number | null; probe_loss_pct: number | null };

function ProposalCard({ p }: { p: ProposalPart }) {
  const setProposal = useCopilot((s) => s.setProposal);
  const notify = useStore((s) => s.notify);
  const [busy, setBusy] = useState<string | null>(null);
  const [pred, setPred] = useState<WhatIf | null>(null);
  const pr = p.proposal;
  const decide = async (what: "apply" | "dismiss") => {
    setBusy(what);
    const r = await act(() => api.post<Proposal>(`/api/ai/proposals/${pr.id}/${what}`));
    setBusy(null);
    if (r) {
      setProposal(r);
      if (what === "apply") notify(`Applied: ${r.label}`, "ok");
    }
  };
  const predict = async () => {
    setBusy("predict");
    const r = await act(() => api.post<WhatIf>(`/api/ai/proposals/${pr.id}/predict`));
    setBusy(null);
    if (r) setPred(r);
  };
  return (
    <div className="rounded-md border border-dashed border-ink-3 p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[11.5px] text-ink-3">Proposed change</p>
          <p className="font-semibold text-ink">{pr.label}</p>
          {pr.reason && <p className="mt-0.5 text-[12.5px] text-ink-2">{pr.reason}</p>}
        </div>
        {pr.status !== "pending" && (
          <span className={`text-[12px] whitespace-nowrap ${pr.status === "applied" ? "text-[#7cd992]" : pr.status === "failed" ? "text-[#ff8a80]" : "text-ink-3"}`}>
            {pr.status === "applied" ? "Applied" : pr.status === "failed" ? "Failed" : "Dismissed"}
          </span>
        )}
      </div>
      {pr.status === "pending" && (
        <div className="mt-2.5 flex flex-wrap gap-2">
          <button className="btn btn-sm btn-primary" onClick={() => decide("apply")} disabled={!!busy}>
            {busy === "apply" ? "Applying…" : "Apply to the live network"}
          </button>
          {pr.action === "chaos_inject" && !pred && (
            <button className="btn btn-sm" onClick={predict} disabled={!!busy}>
              {busy === "predict" ? "Asking the twin…" : "Predict impact first"}
            </button>
          )}
          <button className="btn btn-sm" onClick={() => decide("dismiss")} disabled={!!busy}>
            Dismiss
          </button>
        </div>
      )}
      {pred && (
        <div className="mt-2.5 rounded border border-dashed border-sim/60 p-2">
          <span className="sim-tag !text-[11px]">Simulation</span>
          <table className="data mt-1.5 !text-[12px]">
            <thead>
              <tr>
                <th>Flow</th>
                <th className="text-right">RTT now</th>
                <th className="text-right">Twin predicts</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(pred.flows).map(([pair, f]) => (
                <tr key={pair}>
                  <td className="whitespace-nowrap">{pair.replace(">", " → ")}</td>
                  <td className="text-right">{f.measured_now?.rtt_p50_ms != null ? `${f.measured_now.rtt_p50_ms.toFixed(1)} ms` : "–"}</td>
                  <td className="text-right">
                    {f.twin_with_change?.rtt_p50_ms != null ? `${f.twin_with_change.rtt_p50_ms.toFixed(1)} ms` : "no echo"}
                    {f.controller_would_reroute ? `, rerouted via ${f.twin_with_change?.path ?? "?"}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Composer() {
  const draft = useCopilot((s) => s.draft);
  const setDraft = useCopilot((s) => s.setDraft);
  const send = useCopilot((s) => s.send);
  const stop = useCopilot((s) => s.stop);
  const streaming = useCopilot((s) => s.streaming);
  const available = useStore((s) => s.snap?.ai?.copilot.available);
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, []);
  const submit = () => {
    if (draft.trim() && !streaming) void send(draft.trim());
  };
  return (
    <div className="border-t border-line p-3">
      <div className="flex items-end gap-2">
        <textarea
          ref={ref}
          rows={2}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          disabled={available === false}
          placeholder={available === false ? "Configure a language model to chat" : "Ask about the network…"}
          aria-label="Message the copilot"
          className="field min-h-[52px] flex-1 resize-none py-1.5 leading-snug"
        />
        {streaming ? (
          <button className="btn" onClick={stop}>
            Stop
          </button>
        ) : (
          <button className="btn btn-primary" onClick={submit} disabled={!draft.trim() || available === false}>
            Send
          </button>
        )}
      </div>
      <p className="hint mt-1.5">Reads live data. Nothing changes until you press Apply on a card.</p>
    </div>
  );
}
