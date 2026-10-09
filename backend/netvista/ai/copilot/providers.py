"""LLM providers for the copilot: Claude (Anthropic API), Google Gemini and Groq (both through their
OpenAI-compatible endpoints, free tiers work), or a local model through Ollama.

All take the same provider-neutral conversation and return text + tool calls:

    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "input", "extra"?}]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}

("extra" carries provider metadata that must travel with a call, e.g. Gemini's thought signature.)

Selection (first match wins):
    NETVISTA_AI_PROVIDER=anthropic|gemini|groq|ollama|off      explicit choice
    ANTHROPIC_API_KEY set     -> Claude (NETVISTA_AI_MODEL, default claude-opus-5-5)
    GEMINI_API_KEY set        -> Gemini (NETVISTA_GEMINI_MODEL, default gemini-3.8-flash)
    GROQ_API_KEY set          -> Groq (NETVISTA_GROQ_MODEL, default openai/gpt-oss-120b)
    an Ollama server answers  -> its first tool-capable model (or NETVISTA_AI_MODEL)
A backup is used when the primary is rate-limited, down or rejects its key:
    NETVISTA_AI_BACKUP=groq|gemini|none    (default: the other of Gemini/Groq when its key is set)

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
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GROQ_BASE = "https://api.groq.com/openai/v1"
WIN_CURL = "/mnt/c/Windows/System32/curl.exe"


class ProviderError(RuntimeError):
    def __init__(self, message: str, transient: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.transient = transient  # rate limit, outage, network: worth trying the backup
        self.retry_after = retry_after


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    extra: dict[str, Any] | None = None  # provider metadata to send back with the call (Gemini thought signature)


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
    local = False  # runs on this machine (shown in the UI)

    def __init__(self, model: str) -> None:
        self.model = model

    @property
    def compact(self) -> bool:
        """Small context budget: fewer tools, one turn of history, shorter answers."""
        return self.local

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


# ---------------------------------------------------------------------------- Gemini / Groq (OpenAI-compatible)
SKIP_SIGNATURE = "skip_thought_signature_validator"  # Google's documented value for calls the API did not generate


def to_openai(system: str, messages: list[dict], gemini: bool) -> list[dict]:
    """Neutral -> OpenAI chat format.

    Gemini 3 rejects a request (HTTP 400) when a function call of the *current* turn comes back
    without the thought signature it was issued with, so signatures travel in "extra" and are
    returned exactly. A call made by another provider (the backup answered a round) has none:
    the first call of each such step gets Google's documented skip value. Groq gets no extras.
    """
    out: list[dict] = [{"role": "system", "content": system}]
    last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
    for i, m in enumerate(messages):
        role = m["role"]
        if role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
            calls = []
            for k, tc in enumerate(m.get("tool_calls") or []):
                call: dict[str, Any] = {"id": tc["id"], "type": "function",
                                        "function": {"name": tc["name"], "arguments": json.dumps(tc.get("input") or {})}}
                if gemini:
                    extra = tc.get("extra")
                    if extra:
                        call["extra_content"] = extra
                    elif k == 0 and i > last_user:
                        call["extra_content"] = {"google": {"thought_signature": SKIP_SIGNATURE}}
                calls.append(call)
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": "user", "content": m["content"]})
    return out


def _retry_after(headers) -> float | None:
    for h in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        v = headers.get(h)
        if not v:
            continue
        try:
            return float(v)
        except ValueError:
            try:  # Groq writes durations like "2m59.56s" or "7.66s"
                mins, _, secs = v.rstrip("s").rpartition("m")
                return float(mins or 0) * 60 + float(secs or 0)
            except ValueError:
                continue
    return None


class OpenAICompatProvider(Provider):
    """Streaming chat completions with tool calls against an OpenAI-compatible endpoint."""

    def __init__(self, name: str, title: str, base_url: str, api_key: str, model: str, compact: bool = False,
                 extra_body: dict | None = None, max_wait_s: float = 0.0) -> None:
        super().__init__(model)
        self.name = name
        self.title = title
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._compact = compact
        self.extra_body = extra_body or {}
        self.max_wait_s = max_wait_s  # wait this long once for a rate-limit window instead of failing

    @property
    def compact(self) -> bool:
        return self._compact

    @property
    def label(self) -> str:
        return f"{self.title} ({self.model})"

    def check_key(self, timeout: float = 6.0) -> str:
        """'' when the key works and the model exists; otherwise the reason (never raises)."""
        import httpx

        try:
            r = httpx.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"}, timeout=timeout)
        except httpx.HTTPError as e:
            return f"could not reach {self.title} to check the key ({type(e).__name__}); will try anyway"
        if r.status_code in (400, 401, 403):
            return f"{self.title} rejected the API key (HTTP {r.status_code})"
        if r.status_code >= 400:
            return f"{self.title} answered HTTP {r.status_code} to the key check; will try anyway"
        try:
            ids = {str(m.get("id", "")).removeprefix("models/") for m in r.json().get("data", [])}
        except ValueError:
            return ""
        if ids and self.model not in ids:
            near = sorted(i for i in ids if i.split("-")[0] in self.model)[:6]
            return f"{self.title} has no model '{self.model}'" + (f" (e.g. {', '.join(near)})" if near else "")
        return ""

    def _payload(self, system, messages, tools, max_tokens) -> dict:
        p: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai(system, messages, gemini=self.name == "gemini"),
            "stream": True,
            "stream_options": {"include_usage": True},  # token counts in the last chunk (Gemini sends none otherwise)
            "temperature": 0.2,
            "max_tokens": max_tokens,
            **self.extra_body,
        }
        if tools:
            p["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                          for t in tools]
            p["tool_choice"] = "auto"
        return p

    def _stream(self, payload: dict) -> Iterator[dict]:
        """POST and yield the parsed SSE chunks (separate so tests can replace the network)."""
        import httpx

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for attempt in (0, 1):
            try:
                # 60 s of silence (a free tier under load) counts as down: the backup answers instead
                with httpx.stream("POST", f"{self.base_url}/chat/completions", json=payload, headers=headers,
                                  timeout=httpx.Timeout(60.0, connect=10.0)) as r:
                    if r.status_code >= 400:
                        r.read()
                        wait = _retry_after(r.headers)
                        if r.status_code == 429 and attempt == 0 and wait is not None and wait <= self.max_wait_s:
                            time.sleep(wait + 0.5)
                            continue
                        raise self._http_error(r.status_code, r.text, wait)
                    for line in r.iter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            yield json.loads(data)
                        except json.JSONDecodeError:
                            continue
                return
            except httpx.TimeoutException as e:
                raise ProviderError(f"{self.title} did not answer in time", transient=True) from e
            except httpx.HTTPError as e:
                raise ProviderError(f"cannot reach {self.title}: {type(e).__name__}", transient=True) from e

    def _http_error(self, status: int, body: str, wait: float | None) -> ProviderError:
        try:
            err = json.loads(body)
            err = err[0] if isinstance(err, list) else err
            msg = (err.get("error") or {}).get("message") or body
        except (ValueError, AttributeError):
            msg = body
        msg = " ".join(str(msg).split())[:240]
        if status in (401, 403) or (status == 400 and "api key" in msg.lower()):  # Gemini says 400 for a bad key
            return ProviderError(f"{self.title} rejected the API key (HTTP {status}): {msg}", transient=True)
        if status == 429:
            hint = f" (retry in ~{wait:.0f} s)" if wait else ""
            return ProviderError(f"{self.title} rate limit or free-tier quota reached{hint}: {msg}", transient=True, retry_after=wait)
        if status == 413:
            return ProviderError(f"{self.title}: request too large for this model's limits: {msg}", transient=True)
        if status >= 500:
            return ProviderError(f"{self.title} server error {status}: {msg}", transient=True)
        return ProviderError(f"{self.title} error {status}: {msg}")

    def chat(self, system, messages, tools, on_text, max_tokens=2048, should_stop=lambda: False) -> LLMResponse:
        text: list[str] = []
        acc: dict[int, dict] = {}  # tool calls by index, arguments arrive in fragments
        usage: dict[str, int] = {}
        finish = ""
        for chunk in self._stream(self._payload(system, messages, tools, max_tokens)):
            if chunk.get("usage"):
                u = chunk["usage"]
                usage = {"input_tokens": int(u.get("prompt_tokens") or 0), "output_tokens": int(u.get("completion_tokens") or 0)}
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or ch.get("message") or {}
                if d.get("content"):
                    text.append(d["content"])
                    on_text(d["content"])
                for tc in d.get("tool_calls") or []:
                    slot = acc.setdefault(int(tc.get("index", len(acc))), {"id": "", "name": "", "args": "", "extra": None})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] = fn.get("name") or slot["name"]
                    args = fn.get("arguments")
                    if isinstance(args, dict):
                        slot["args"] = json.dumps(args)
                    elif args:
                        slot["args"] += args
                    if tc.get("extra_content"):
                        slot["extra"] = tc["extra_content"]
                finish = ch.get("finish_reason") or finish
            if should_stop():
                break
        calls = []
        for k in sorted(acc):
            s = acc[k]
            try:
                args = json.loads(s["args"]) if s["args"].strip() else {}
            except json.JSONDecodeError:
                args = {}
            if s["name"]:
                calls.append(ToolCall(s["id"] or f"call_{uuid.uuid4().hex[:8]}", s["name"], args if isinstance(args, dict) else {}, s["extra"]))
        return LLMResponse("".join(text), calls, "tool_use" if calls else (finish or "stop"), usage)


class FallbackProvider(Provider):
    """The primary answers; when it is rate-limited, down or rejects its key, the backup answers
    that round, and the primary rests for a cool-down before it is tried again."""

    COOLDOWN_S = 60.0
    AUTH_COOLDOWN_S = 900.0

    def __init__(self, primary: Provider, backup: Provider) -> None:
        super().__init__(primary.model)
        self.primary, self.backup = primary, backup
        self.name = primary.name
        self.resting_until = 0.0
        self.last_used: Provider = primary

    @property
    def compact(self) -> bool:
        return self.primary.compact

    @property
    def label(self) -> str:
        return f"{self.primary.label}, backup {self.backup.label}"

    def chat(self, system, messages, tools, on_text, max_tokens=2048, should_stop=lambda: False) -> LLMResponse:
        if time.time() >= self.resting_until:
            try:
                resp = self.primary.chat(system, messages, tools, on_text, max_tokens, should_stop)
                self.last_used = self.primary
                return resp
            except ProviderError as e:
                if not e.transient:
                    raise
                rest = self.AUTH_COOLDOWN_S if "rejected the API key" in str(e) else max(self.COOLDOWN_S, e.retry_after or 0.0)
                self.resting_until = time.time() + rest
                log.warning("copilot: %s; using the backup for %.0f s", e, rest)
                on_text(f"_({self.primary.label.split(' (')[0]} unavailable: {str(e).split(':')[0]}. Answering with {self.backup.label}.)_\n\n")
        try:
            resp = self.backup.chat(system, messages, tools, on_text, max_tokens, should_stop)
            self.last_used = self.backup
            return resp
        except ProviderError as e:
            raise ProviderError(f"both providers failed. Backup: {e}", transient=e.transient, retry_after=e.retry_after) from e


def make_gemini(key: str, model: str | None = None) -> OpenAICompatProvider:
    # thinking kept low: the copilot's job is tool use and grounded summaries, and a lower budget answers faster
    return OpenAICompatProvider("gemini", "Gemini", GEMINI_BASE, key, model or DEFAULT_GEMINI_MODEL,
                                extra_body={"reasoning_effort": "low"})


def make_groq(key: str, model: str | None = None) -> OpenAICompatProvider:
    # the free tier allows ~8k tokens per minute: compact context, low reasoning, wait once for the window
    extra = {"reasoning_effort": "low"} if "gpt-oss" in (model or DEFAULT_GROQ_MODEL) else {}
    return OpenAICompatProvider("groq", "Groq", GROQ_BASE, key, model or DEFAULT_GROQ_MODEL, compact=True,
                                extra_body=extra, max_wait_s=25.0)


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
    backup: str | None = None  # label of the backup provider, if one is configured

    def to_dict(self) -> dict:
        return {"available": self.available, "provider": self.provider, "model": self.model, "label": self.label,
                "reason": self.reason, "checked_at": self.checked_at, "transport": self.transport, "local": self.local,
                "backup": self.backup}


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
    if want in ("", "gemini", "groq"):
        picked = _select_cloud(want, now)
        if picked is not None:
            return picked
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


def _hard(reason: str) -> bool:
    return "rejected the API key" in reason or "has no model" in reason


def _select_cloud(want: str, now: float) -> tuple[Provider | None, ProviderStatus] | None:
    """Gemini / Groq with an optional backup. None = no cloud key set (fall through to Ollama)."""
    keys = {"gemini": os.environ.get("GEMINI_API_KEY", "").strip(), "groq": os.environ.get("GROQ_API_KEY", "").strip()}
    makers = {"gemini": lambda: make_gemini(keys["gemini"], os.environ.get("NETVISTA_GEMINI_MODEL", "").strip() or None),
              "groq": lambda: make_groq(keys["groq"], os.environ.get("NETVISTA_GROQ_MODEL", "").strip() or None)}
    title = {"gemini": "Gemini", "groq": "Groq"}
    primary = want or next((n for n in ("gemini", "groq") if keys[n]), "")
    if not primary:
        return None
    if not keys[primary]:
        return None, ProviderStatus(False, primary, None, title[primary], f"{primary.upper()}_API_KEY is not set", now)
    other = "groq" if primary == "gemini" else "gemini"
    backup_name = os.environ.get("NETVISTA_AI_BACKUP", "").strip().lower() or (other if keys[other] else "none")
    p = makers[primary]()
    reason = p.check_key()
    b: Provider | None = None
    breason = ""
    if backup_name in ("gemini", "groq") and backup_name != primary:
        if keys[backup_name]:
            b = makers[backup_name]()
            breason = b.check_key()
        else:
            breason = f"{backup_name.upper()}_API_KEY is not set"
    elif backup_name == "ollama":
        t = find_ollama()
        try:
            b = OllamaProvider(t, pick_ollama_model(t, None), int(os.environ.get("NETVISTA_OLLAMA_NUM_CTX", "12288"))) if t else None
            breason = "" if t else "no Ollama server found"
        except Exception as e:
            breason = str(e)
    if b is not None and _hard(breason):
        b = None
    if _hard(reason):
        if b is None:
            return None, ProviderStatus(False, primary, p.model, p.label, reason + (f"; backup: {breason}" if breason else ""), now)
        # the primary's key is bad but the backup works: run on the backup alone and say why
        return b, ProviderStatus(True, b.name, b.model, b.label, f"{reason}; using {b.label} instead", now, "https", b.local)
    notes = [r for r in (reason, f"backup not used: {breason}" if breason else "") if r]
    if b is None:
        return p, ProviderStatus(True, p.name, p.model, p.label, "; ".join(notes), now, "https")
    fb = FallbackProvider(p, b)
    return fb, ProviderStatus(True, p.name, p.model, fb.label, "; ".join(notes), now, "https", False, b.label)


__all__ = [
    "AnthropicProvider", "FallbackProvider", "LLMResponse", "OllamaProvider", "OpenAICompatProvider", "Provider",
    "ProviderError", "ProviderStatus", "ToolCall", "ToolSchema", "make_gemini", "make_groq", "select_provider",
    "to_anthropic", "to_ollama", "to_openai",
]
