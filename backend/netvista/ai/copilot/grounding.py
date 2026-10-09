"""Grounding check: is every measured value in the copilot's answer traceable to data it read?

The project rule is that every number the UI shows comes from the emulated network (or is
labelled simulation). An LLM can produce plausible numbers that were never measured, so each
answer is checked after it is written:

  1. pull out measurement-like numbers: anything with a unit (ms, s, %, pp, Mbit/s, pps ...)
     or a decimal point. Bare counts ("2 flows"), ids (r2, srv1), IPs and clock times are skipped.
  2. collect every number in the tool results of this conversation (plus the topology facts in
     the system prompt and the user's own messages).
  3. a value is grounded if some source number equals it after unit scaling (x1, x100 for
     fractions shown as %, x1000 / /1000 for s<->ms, /1e6 for bit/s -> Mbit/s) within the
     precision it was written with (or 0.5 %).

What it cannot see: numbers the model derived itself (a difference, a ratio). Those are
reported as "not found in the data", never silently passed.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

UNIT = r"(?:ms|s|sec|seconds?|%|pp|percentage points?|Mbit/s|Mbps|Gbit/s|kbit/s|Kbit/s|bit/s|bps|pps|packets/s|packets per second|x|×)"
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b")
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_CODE = re.compile(r"`[^`]*`")
_LIST_MARK = re.compile(r"(?m)^\s*\d+[.)]\s")
_NUM = re.compile(
    r"(?<![\w.#/:>\-])([+\-−]?\d+(?:\.\d+)?)(?:\s?(" + UNIT + r"))?(?!\w|\.\d)"
)
_ANY_NUM = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?")
SCALES = (1.0, 100.0, 0.01, 1000.0, 0.001, 1e-6)


def extract_claims(text: str) -> list[dict[str, Any]]:
    clean = _CODE.sub(" ", text)
    clean = _IP.sub(" ", clean)
    clean = _CLOCK.sub(" ", clean)
    clean = _LIST_MARK.sub(" ", clean)
    out = []
    for m in _NUM.finditer(clean):
        raw, unit = m.group(1), m.group(2)
        if unit is None and "." not in raw:
            continue  # a bare integer is a count or an ordinal, not a measurement
        val = float(raw.replace("−", "-").lstrip("+"))
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        out.append({"text": m.group(0).strip(), "value": abs(val), "decimals": decimals, "unit": unit})
    return out


def numbers_in(obj: Any, acc: list[float] | None = None) -> list[float]:
    acc = [] if acc is None else acc
    if isinstance(obj, bool) or obj is None:
        return acc
    if isinstance(obj, (int, float)):
        acc.append(abs(float(obj)))
    elif isinstance(obj, str):
        s = obj.strip()
        if s[:1] in "{[":
            try:
                return numbers_in(json.loads(s), acc)
            except (json.JSONDecodeError, ValueError):
                pass
        acc.extend(abs(float(x)) for x in _ANY_NUM.findall(s))
    elif isinstance(obj, dict):
        for v in obj.values():
            numbers_in(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            numbers_in(v, acc)
    return acc


def is_grounded(claim: dict[str, Any], sources: Iterable[float]) -> bool:
    x, d = claim["value"], claim["decimals"]
    tol = max(0.6 * 10 ** (-d), 0.005 * x)
    for v in sources:
        for sc in SCALES:
            if abs(x - v * sc) <= tol:
                return True
    return False


def check(answer: str, sources: list[Any]) -> dict[str, Any]:
    claims = extract_claims(answer)
    pool: list[float] = []
    for s in sources:
        numbers_in(s, pool)
    # de-duplicate (rounded) to keep the check fast on long conversations
    pool = sorted({round(v, 6) for v in pool})
    grounded, ungrounded = [], []
    for c in claims:
        (grounded if is_grounded(c, pool) else ungrounded).append(c["text"])
    return {
        "checked": len(claims),
        "grounded": len(grounded),
        "ungrounded": ungrounded,
        "sources": len(pool),
    }
