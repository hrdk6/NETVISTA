"""Thread-safe event log shared by every subsystem (chaos, routing, traffic, demo...).

The WebSocket broadcaster pulls new events by sequence number, so producers never touch asyncio.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Event:
    seq: int
    t: float
    kind: str  # e.g. "chaos.inject", "routing.reroute", "routing.detect"
    message: str
    severity: str = "info"  # info | warn | error | success
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    def __init__(self, maxlen: int = 2000, log_path: Path | None = None) -> None:
        self._events: deque[Event] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._listeners: list[Callable[[Event], None]] = []
        self._log_path = log_path
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._listeners.append(fn)

    def emit(self, kind: str, message: str, severity: str = "info", t: float | None = None, **data: Any) -> Event:
        with self._lock:
            ev = Event(seq=next(self._seq), t=t if t is not None else time.time(), kind=kind, message=message, severity=severity, data=data)
            self._events.append(ev)
        if self._log_path:
            try:
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ev.to_dict(), default=str) + "\n")
            except OSError:
                pass
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # a broken listener must never break the producer
                pass
        return ev

    def since(self, seq: int) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.seq > seq]

    def recent(self, limit: int = 200) -> list[Event]:
        with self._lock:
            return list(self._events)[-limit:]
