"""LLM providers for the copilot: Claude (Anthropic API) or a local model through Ollama.

Both take the same provider-neutral conversation and return text + tool calls:

    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "input"}]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}

Selection (first match wins):
    NETVISTA_AI_PROVIDER=anthropic|ollama|off      explicit choice
    ANTHROPIC_API_KEY set                          -> Claude (NETVISTA_AI_MODEL, default claude-opus-5-5)
    an Ollama server answers                       -> its first tool-capable model (or NETVISTA_AI_MODEL)

Ollama on Windows + backend in WSL2 (NAT networking): Windows' 127.0.0.1 is not reachable from
WSL, so when the plain HTTP probe fails the requests are sent through Windows' own curl.exe via
WSL interop. That needs no firewall rule and no change to Ollama's settings.
"""

from __future__ import annotations

import json
import logging
import os

import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
WIN_CURL = "/mnt/c/Windows/System32/curl.exe"


class ProviderError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]


class Provider:
    name = "none"
    local = False

    def __init__(self, model: str) -> None:
        self.model = model

    @property
    def label(self) -> str:
        return self.model

    def chat(self, system: str, messages: list[dict], tools: list[ToolSchema], on_text: Callable[[str], None],
             max_tokens: int = 2048, should_stop: Callable[[], bool] = lambda: False) -> LLMResponse:
        raise NotImplementedError


# ---------------------------------------------------------------------------- Claude
def to_anthropic(messages: list[dict]) -> list[dict]:
    """Neutral -> Anthropic Messages API (tool results become tool_result blocks in a user turn)."""
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append({"type": "text", "text": m["content"]})
            else:
                out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
            if not blocks:
                blocks.append({"type": "text", "text": "(no answer)"})
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if m.get("is_error"):
                block["is_error"] = True
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(model)
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - depends on the venv
            raise ProviderError("the 'anthropic' package is not installed: /opt/netvista/venv/bin/pip install anthropic") from e
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=120.0)

    @property
    def label(self) -> str:
        return f"Claude ({self.model})"

    def chat(self, system, messages, tools, on_text, max_tokens=2048, should_stop=lambda: False) -> LLMResponse:
        a = self._anthropic
        try:
            with self.client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=to_anthropic(messages),
                tools=[{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools],
                cache_control={"type": "ephemeral"},  # system prompt + tools are identical every round
            ) as stream:
                for ev in stream:
                    if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                        on_text(ev.delta.text)
                    if should_stop():
                        break
                final = stream.get_final_message()
        except a.AuthenticationError as e:
            raise ProviderError("Claude rejected the API key (check ANTHROPIC_API_KEY in .env)") from e
        except a.RateLimitError as e:
            raise ProviderError("Claude rate limit reached; try again in a moment") from e
        except a.APIConnectionError as e:
            raise ProviderError(f"cannot reach the Anthropic API: {e}") from e
        except a.APIStatusError as e:
            raise ProviderError(f"Anthropic API error {e.status_code}: {getattr(e, 'message', e)}") from e
        text = "".join(b.text for b in final.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in final.content if b.type == "tool_use"]
        u = final.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0}
        return LLMResponse(text, calls, final.stop_reason or "", usage)


# ---------------------------------------------------------------------------- Ollama
def to_ollama(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                msg["tool_calls"] = [{"function": {"name": tc["name"], "arguments": tc["input"]}} for tc in m["tool_calls"]]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"], "tool_name": m["name"]})
        else:
            out.append({"role": "user", "content": m["content"]})
    return out


def _in_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


class OllamaTransport:
    """POST JSON and stream NDJSON lines back, over HTTP or (WSL -> Windows) via curl.exe."""

    def __init__(self, base_url: str, mode: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.mode = mode  # http | win-interop

    def get_json(self, path: str, timeout: float = 3.0) -> Any:
        url = self.base_url + path
        if self.mode == "http":
            import httpx

            r = httpx.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()
        res = subprocess.run([WIN_CURL, "-s", "-f", "-m", str(int(timeout) or 1), url], capture_output=True, timeout=timeout + 5)
        if res.returncode != 0:
            raise ProviderError(f"Ollama at {url} did not answer (curl.exe exit {res.returncode})")
        return json.loads(res.stdout)

    def post_lines(self, path: str, payload: dict, timeout: float = 600.0) -> Iterator[str]:
        url = self.base_url + path
        body = json.dumps(payload).encode()
        if self.mode == "http":
            import httpx

            with httpx.stream("POST", url, content=body, headers={"Content-Type": "application/json"},
                              timeout=httpx.Timeout(timeout, connect=5.0)) as r:
                if r.status_code >= 400:
                    r.read()
                    raise ProviderError(f"Ollama error {r.status_code}: {r.text[:300]}")
                yield from r.iter_lines()
            return
        proc = subprocess.Popen(
            [WIN_CURL, "-s", "-N", "-m", str(int(timeout)), "-X", "POST", "-H", "Content-Type: application/json",
             "--data-binary", "@-", url],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            assert proc.stdin and proc.stdout
            proc.stdin.write(body)
            proc.stdin.close()
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    yield line
            proc.wait(timeout=10)
            if proc.returncode not in (0, None):
                err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                raise ProviderError(f"Ollama request failed (curl.exe exit {proc.returncode}) {err[:200]}")
        finally:
            if proc.poll() is None:
                proc.kill()


def find_ollama() -> OllamaTransport | None:
    urls = [os.environ.get("NETVISTA_OLLAMA_URL", "").strip() or "http://127.0.0.1:11434"]
    if _in_wsl():
        try:  # the Windows host as seen from a WSL2 NAT network (works if Ollama listens on 0.0.0.0)
            for line in Path("/proc/net/route").read_text().splitlines()[1:]:
                f = line.split()
                if f[1] == "00000000":
                    gw = ".".join(str(int(f[2][i:i + 2], 16)) for i in (6, 4, 2, 0))
                    urls.append(f"http://{gw}:11434")
                    break
        except (OSError, IndexError, ValueError):
            pass
    for url in urls:
        t = OllamaTransport(url, "http")
        try:
            t.get_json("/api/version", timeout=1.5)
            return t
        except Exception:
            continue
    if _in_wsl() and Path(WIN_CURL).exists() and urls[0].startswith(("http://127.0.0.1", "http://localhost")):
        t = OllamaTransport(urls[0], "win-interop")
        try:
            t.get_json("/api/version", timeout=3)
            return t
        except Exception:
            return None
    return None


def pick_ollama_model(t: OllamaTransport, wanted: str | None) -> str:
    tags = t.get_json("/api/tags", timeout=5).get("models", [])
    names = [m["name"] for m in tags]
    if wanted:
        if wanted not in names and f"{wanted}:latest" not in names:
            raise ProviderError(f"Ollama has no model '{wanted}' (installed: {', '.join(names) or 'none'})")
        return wanted
    for m in tags:
        caps = m.get("capabilities")
        if caps is None:  # older Ollama: /api/tags has no capabilities, /api/show does
            try:
                caps = json.loads("".join(t.post_lines("/api/show", {"model": m["name"]}, timeout=10))).get("capabilities")
            except Exception:
                caps = None
        if caps and "tools" in caps:
            return m["name"]
    raise ProviderError(
        "no tool-capable Ollama model found (installed: " + (", ".join(names) or "none") + "); e.g. `ollama pull qwen3:8b`"
    )


class OllamaProvider(Provider):
    name = "ollama"
    local = True

    def __init__(self, transport: OllamaTransport, model: str, num_ctx: int = 12288) -> None:
        super().__init__(model)
        self.t = transport
        self.num_ctx = num_ctx

    @property
    def label(self) -> str:
        return f"{self.model} (local, Ollama)"

    def chat(self, system, messages, tools, on_text, max_tokens=2048, should_stop=lambda: False) -> LLMResponse:
        payload = {
            "model": self.model,
            "messages": to_ollama(system, messages),
            "tools": [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools],
            "stream": True,
            "think": False,
            "keep_alive": "30m",
            "options": {"num_ctx": self.num_ctx, "temperature": 0.2, "num_predict": max_tokens},
        }
        text, calls, usage, stop = [], [], {}, ""
        for line in self.t.post_lines("/api/chat", payload):
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in chunk:
                raise ProviderError(f"Ollama: {chunk['error']}")
            msg = chunk.get("message") or {}
            if msg.get("content"):
                text.append(msg["content"])
                on_text(msg["content"])
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                calls.append(ToolCall(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", fn.get("name", ""), args))
            if chunk.get("done"):
                stop = chunk.get("done_reason", "stop")
                usage = {"input_tokens": chunk.get("prompt_eval_count", 0), "output_tokens": chunk.get("eval_count", 0)}
            if should_stop():
                break
        return LLMResponse("".join(text), calls, "tool_use" if calls else stop, usage)


# ---------------------------------------------------------------------------- selection
@dataclass
class ProviderStatus:
    available: bool
    provider: str  # anthropic | ollama | none
    model: str | None
    label: str
    reason: str = ""
    checked_at: float = 0.0
    transport: str | None = None
    local: bool = False

    def to_dict(self) -> dict:
        return {"available": self.available, "provider": self.provider, "model": self.model, "label": self.label,
                "reason": self.reason, "checked_at": self.checked_at, "transport": self.transport, "local": self.local}


def select_provider() -> tuple[Provider | None, ProviderStatus]:
    want = os.environ.get("NETVISTA_AI_PROVIDER", "").strip().lower()
    model = os.environ.get("NETVISTA_AI_MODEL", "").strip() or None
    now = time.time()
    if want == "off":
        return None, ProviderStatus(False, "none", None, "Copilot off", "NETVISTA_AI_PROVIDER=off", now)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if want in ("", "anthropic") and key:
        try:
            p = AnthropicProvider(key, model or DEFAULT_CLAUDE_MODEL)
            return p, ProviderStatus(True, "anthropic", p.model, p.label, "", now, "https")
        except ProviderError as e:
            return None, ProviderStatus(False, "anthropic", model, "Claude", str(e), now)
    if want == "anthropic":
        return None, ProviderStatus(False, "anthropic", model, "Claude", "ANTHROPIC_API_KEY is not set", now)
    t = find_ollama()
    if t is None:
        reason = "no ANTHROPIC_API_KEY and no Ollama server found"
        return None, ProviderStatus(False, "none", None, "Not configured", reason, now)
    try:
        m = pick_ollama_model(t, model)
    except Exception as e:
        return None, ProviderStatus(False, "ollama", model, "Ollama", str(e), now, t.mode, True)
    num_ctx = int(os.environ.get("NETVISTA_OLLAMA_NUM_CTX", "12288"))
    p = OllamaProvider(t, m, num_ctx)
    return p, ProviderStatus(True, "ollama", m, p.label, "", now, t.mode, True)


__all__ = [
    "AnthropicProvider", "LLMResponse", "OllamaProvider", "Provider", "ProviderError", "ProviderStatus",
    "ToolCall", "ToolSchema", "select_provider", "to_anthropic", "to_ollama",
]
