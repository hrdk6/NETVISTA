"""The copilot agent loop: model -> tool calls -> tool results -> model ... -> answer.

One question runs at a time (a single-user dashboard, and a local model can only do one).
Everything is streamed to the browser as events:

    start        conversation id, provider, model
    text         a piece of the answer as it is written
    tool_call    the model asked for a tool (name, input, human label)
    tool_result  what the tool returned (the browser can show the raw JSON: provenance)
    proposal     a change the model proposes; only the user can apply it
    grounding    how many measured values in the answer were found in the data it read
    done | error
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .grounding import check
from .prompt import system_prompt
from .providers import Provider, ProviderError, ProviderStatus, select_provider
from .tools import Toolbox, dumps

log = logging.getLogger(__name__)

MAX_ROUNDS = 8
MAX_CONVERSATIONS = 30
RESULT_PREVIEW = 8000


@dataclass
class Proposal:
    id: str
    conversation_id: str | None
    action: str  # chaos_inject | chaos_revert | routing | traffic
    spec: dict[str, Any]
    label: str
    reason: str | None
    t: float
    status: str = "pending"  # pending | applied | dismissed | failed
    result: Any = None
    t_decided: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "conversation_id": self.conversation_id, "action": self.action, "spec": self.spec,
                "label": self.label, "reason": self.reason, "t": self.t, "status": self.status, "result": self.result,
                "t_decided": self.t_decided}


class ProposalStore:
    def __init__(self) -> None:
        self.items: OrderedDict[str, Proposal] = OrderedDict()
        self._ids = itertools.count(1)
        self.lock = threading.Lock()

    def add(self, conv_id: str | None, action: str, spec: dict, label: str, reason: str | None) -> dict:
        with self.lock:
            p = Proposal(f"P{next(self._ids)}", conv_id, action, spec, label, reason, time.time())
            self.items[p.id] = p
            while len(self.items) > 200:
                self.items.popitem(last=False)
        return {"proposal_id": p.id, "label": label, "status": "shown to the user as a card; NOT applied unless the user clicks Apply"}

    def get(self, pid: str) -> Proposal:
        p = self.items.get(pid)
        if p is None:
            raise KeyError(f"unknown proposal {pid}")
        return p


@dataclass
class Conversation:
    id: str
    created: float
    messages: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    title: str = ""
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})


def context_window(messages: list[dict], keep_turns: int, max_messages: int = 48) -> list[dict]:
    """Messages to send: the last `max_messages`, with tool results older than the last
    `keep_turns` user turns replaced by a stub (structure kept so tool calls still pair up)."""
    msgs = messages[-max_messages:]
    while msgs and not (msgs[0]["role"] == "user"):
        msgs = msgs[1:]
    user_idx = [i for i, m in enumerate(msgs) if m["role"] == "user"]
    cutoff = user_idx[-keep_turns] if len(user_idx) >= keep_turns else 0
    out = []
    for i, m in enumerate(msgs):
        if m["role"] == "tool" and i < cutoff:
            m = dict(m, content="(older tool result omitted to save context; call the tool again if you need it)")
        out.append(m)
    return out


class Copilot:
    def __init__(self, rt, ai) -> None:
        self.rt = rt
        self.ai = ai
        self.proposals = ProposalStore()
        self.toolbox = Toolbox(rt, ai, self.proposals)
        self.convs: OrderedDict[str, Conversation] = OrderedDict()
        self.provider: Provider | None = None
        self.pstatus = ProviderStatus(False, "none", None, "Checking…", "detecting a language model", time.time())
        self._busy = threading.Lock()
        self._cancel: threading.Event | None = None
        self._detect_lock = threading.Lock()
        threading.Thread(target=self.refresh, name="copilot-detect", daemon=True).start()

    # ------------------------------------------------------------------ provider
    def refresh(self) -> dict:
        with self._detect_lock:
            try:
                p, st = select_provider()
            except Exception as e:  # never let detection kill the service
                log.exception("provider detection failed")
                p, st = None, ProviderStatus(False, "none", None, "Not configured", str(e), time.time())
            self.provider, self.pstatus = p, st
            if p:
                log.info("copilot provider: %s", st.label)
            return self.status()

    def status_brief(self) -> dict:
        s = self.pstatus
        return {"available": s.available, "provider": s.provider, "label": s.label, "local": s.local, "busy": self._busy.locked()}

    def status(self) -> dict:
        return {**self.pstatus.to_dict(), "busy": self._busy.locked(), "conversations": len(self.convs),
                "tools": [{"name": t.name, "kind": t.kind, "local": t.local} for t in self.toolbox.tools.values()]}

    # ------------------------------------------------------------------ conversations
    def _conv(self, cid: str | None) -> tuple[Conversation, bool]:
        if cid and cid in self.convs:
            self.convs.move_to_end(cid)
            return self.convs[cid], False
        c = Conversation(uuid.uuid4().hex[:12], time.time())
        self.convs[c.id] = c
        while len(self.convs) > MAX_CONVERSATIONS:
            self.convs.popitem(last=False)
        return c, bool(cid)

    def reset(self, cid: str) -> None:
        self.convs.pop(cid, None)

    def cancel(self) -> bool:
        """Stop the running answer after the current model call (the Stop button)."""
        c = self._cancel
        if c is not None and self._busy.locked():
            c.set()
            return True
        return False

    # ------------------------------------------------------------------ chat
    def chat(self, cid: str | None, text: str, context: str | None = None) -> Iterator[dict]:
        provider = self.provider
        if provider is None:
            yield {"type": "error", "message": f"The copilot has no language model: {self.pstatus.reason}. See the AI page for setup."}
            return
        if not self._busy.acquire(blocking=False):
            yield {"type": "error", "message": "The copilot is still answering another question; wait for it to finish."}
            return
        q: queue.Queue = queue.Queue()
        cancel = threading.Event()
        self._cancel = cancel

        def worker() -> None:
            try:
                self._run(provider, cid, text, context, q.put, cancel)
            except ProviderError as e:
                q.put({"type": "error", "message": str(e)})
            except Exception as e:  # pragma: no cover - surfaced to the UI
                log.exception("copilot failed")
                q.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                self._busy.release()
                q.put(None)

        threading.Thread(target=worker, name="copilot", daemon=True).start()
        try:
            while True:
                ev = q.get()
                if ev is None:
                    break
                yield ev
        finally:
            cancel.set()

    def _run(self, provider: Provider, cid: str | None, text: str, context: str | None,
             emit: Callable[[dict], None], cancel: threading.Event) -> None:
        t0 = time.time()
        conv, renewed = self._conv(cid)
        emit({"type": "start", "conversation_id": conv.id, "renewed": renewed, "provider": provider.name, "label": provider.label})
        if not conv.title:
            conv.title = text[:80]
        prefix = list(conv.notes)
        conv.notes.clear()
        if context:
            prefix.append(f"[Dashboard context: {context}]")
        conv.messages.append({"role": "user", "content": ("\n".join(prefix) + "\n\n" + text) if prefix else text})
        self.toolbox.conversation_id = conv.id
        system = system_prompt(self.rt)
        tools = self.toolbox.schemas(local=provider.local)
        usage = {"input_tokens": 0, "output_tokens": 0}
        answer_parts: list[str] = []
        rounds = 0
        while rounds < MAX_ROUNDS and not cancel.is_set():
            rounds += 1
            resp = provider.chat(
                system, context_window(conv.messages, keep_turns=1 if provider.local else 3), tools,
                on_text=lambda d: emit({"type": "text", "delta": d}),
                max_tokens=1200 if provider.local else 3000, should_stop=cancel.is_set,
            )
            for k in usage:
                usage[k] += int(resp.usage.get(k, 0) or 0)
            calls = [{"id": tc.id, "name": tc.name, "input": tc.input} for tc in resp.tool_calls]
            conv.messages.append({"role": "assistant", "content": resp.text, "tool_calls": calls})
            if resp.text.strip():
                answer_parts.append(resp.text)
            if not calls:
                break
            if resp.text.strip():
                emit({"type": "text", "delta": "\n\n"})
            for tc in calls:
                tool = self.toolbox.tools.get(tc["name"])
                label = tool.label(tc["input"]) if tool else tc["name"]
                emit({"type": "tool_call", "id": tc["id"], "name": tc["name"], "kind": tool.kind if tool else "read",
                      "label": label, "input": tc["input"]})
                t1 = time.time()
                ok, result = self.toolbox.run(tc["name"], tc["input"])
                content = dumps(result)
                emit({"type": "tool_result", "id": tc["id"], "ok": ok, "ms": round((time.time() - t1) * 1000),
                      "result": content[:RESULT_PREVIEW], "truncated": len(content) > RESULT_PREVIEW})
                if ok and tool and tool.kind == "propose":
                    emit({"type": "proposal", **self.proposals.get(result["proposal_id"]).to_dict()})
                conv.messages.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"], "content": content, "is_error": not ok})
        else:
            if rounds >= MAX_ROUNDS:
                emit({"type": "text", "delta": "\n\n(Stopped after the maximum number of tool rounds.)"})
        for k in usage:
            conv.usage[k] += usage[k]
        sources: list[Any] = [system] + [m["content"] for m in conv.messages if m["role"] in ("user", "tool")]
        sources += [p.label for p in self.proposals.items.values() if p.conversation_id == conv.id]
        g = check("\n\n".join(answer_parts), sources)
        emit({"type": "grounding", **g})
        emit({"type": "done", "usage": usage, "ms": round((time.time() - t0) * 1000), "rounds": rounds})

    # ------------------------------------------------------------------ proposals
    def apply_proposal(self, pid: str) -> dict:
        p = self.proposals.get(pid)
        if p.status != "pending":
            raise ValueError(f"proposal {pid} is already {p.status}")
        rt = self.rt
        try:
            if p.action == "chaos_inject":
                inj = rt.chaos.inject(p.spec["kind"], p.spec["target"], p.spec["params"], source="ai-copilot")
                result: Any = {"fault_id": inj.id, "label": inj.label}
            elif p.action == "chaos_revert":
                if p.spec["fault_id"] == "all":
                    result = {"reverted": rt.chaos.revert_all(source="ai-copilot")}
                else:
                    result = {"reverted": rt.chaos.revert(p.spec["fault_id"], source="ai-copilot").id}
            elif p.action == "routing":
                if "mode" in p.spec:
                    rt.controller.set_mode(p.spec["mode"])
                if "weights" in p.spec:
                    w = p.spec["weights"]
                    rt.controller.set_weights(w["latency"], w["loss"], w["util"])
                result = {"mode": rt.controller.mode, "weights": rt.controller.weights.to_dict()}
            elif p.action == "traffic":
                if p.spec["action"] == "start":
                    result = {"started": [f["pair"] for f in rt.start_default_traffic()]}
                else:
                    rt.traffic.stop_all("background")
                    result = {"stopped": True}
            else:
                raise ValueError(f"unknown action {p.action}")
        except Exception as e:
            p.status, p.result, p.t_decided = "failed", {"error": str(e)}, time.time()
            raise
        p.status, p.result, p.t_decided = "applied", result, time.time()
        rt.events.emit("ai.action", f"Applied copilot proposal (approved by the user): {p.label}", severity="info", proposal=p.id)
        conv = self.convs.get(p.conversation_id or "")
        if conv:
            conv.notes.append(f"[The user applied proposal {p.id} \"{p.label}\" {time.strftime('%H:%M:%S')}; result: {dumps(result)}]")
        return p.to_dict()

    def dismiss_proposal(self, pid: str) -> dict:
        p = self.proposals.get(pid)
        if p.status == "pending":
            p.status, p.t_decided = "dismissed", time.time()
            conv = self.convs.get(p.conversation_id or "")
            if conv:
                conv.notes.append(f"[The user dismissed proposal {p.id} \"{p.label}\"]")
        return p.to_dict()

    def predict_proposal(self, pid: str) -> dict:
        p = self.proposals.get(pid)
        if p.action != "chaos_inject":
            raise ValueError("only fault proposals can be simulated")
        return self.toolbox.what_if({"changes": [p.spec]})
