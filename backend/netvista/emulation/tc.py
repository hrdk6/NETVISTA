"""tc (traffic control) configuration for one interface.

Each emulated link direction is shaped on the *egress* interface of the sender:

    root  htb 1:              (rate limiter: the link bandwidth)
     └─ class 1:1 htb rate B  ceil B
         └─ qdisc 10: netem delay D [jitter J normal] loss L% limit Q

So a packet a->b first waits D in netem (propagation), then is released by HTB at rate B
(serialisation + queueing), and netem's `limit` is the shared buffer. The simulator models
exactly this pipeline. All later changes use `replace`, so parameters change in place
without tearing down the qdisc tree (no traffic gap when injecting latency).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class LinkParams:
    bw_mbps: float
    delay_ms: float
    jitter_ms: float = 0.0
    loss_pct: float = 0.0
    queue_pkts: int = 1000

    def with_(self, **kw) -> "LinkParams":
        return replace(self, **kw)

    def to_dict(self) -> dict:
        return asdict(self)


def _fmt(x: float) -> str:
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def tc_lines(dev: str, p: LinkParams, create_root: bool = True) -> list[str]:
    """tc batch lines for one interface.

    The HTB root qdisc does not support in-place `change`, so it is only created once
    (create_root=True); later updates rewrite just the HTB class (rate) and the netem child
    (delay/jitter/loss), both of which support change without dropping queued packets.
    """
    netem = f"qdisc replace dev {dev} parent 1:1 handle 10: netem limit {int(p.queue_pkts)} delay {_fmt(p.delay_ms)}ms"
    if p.jitter_ms > 0:
        netem += f" {_fmt(p.jitter_ms)}ms distribution normal"
    netem += f" loss {_fmt(p.loss_pct)}%"
    lines = [
        f"class replace dev {dev} parent 1: classid 1:1 htb rate {_fmt(p.bw_mbps)}mbit ceil {_fmt(p.bw_mbps)}mbit quantum 1514",
        netem,
    ]
    if create_root:
        lines.insert(0, f"qdisc replace dev {dev} root handle 1: htb default 1")
    return lines


def parse_qdisc_stats(json_text: str) -> dict[str, dict[str, int]]:
    """Parse `tc -s -j qdisc show` into {dev: {drops, backlog_pkts, backlog_bytes, overlimits}}.

    HTB accounts its children's drops (netem random loss and buffer overflow) in the root
    qdisc, so the root entry alone is the per-interface total; summing would double count.
    """
    try:
        entries = json.loads(json_text or "[]")
    except json.JSONDecodeError:
        return {}
    out: dict[str, dict[str, int]] = {}
    for e in entries:
        dev = e.get("dev")
        if not dev:
            continue
        is_root = e.get("root") is True or e.get("parent") in (None, "root")
        stats = {
            "drops": int(e.get("drops", 0)),
            "backlog_bytes": int(e.get("backlog", 0)),
            "backlog_pkts": int(e.get("qlen", 0)),
            "overlimits": int(e.get("overlimits", 0)),
        }
        if is_root and e.get("kind") == "htb":
            out[dev] = stats
        elif dev not in out:
            out.setdefault(dev, stats)
    return out
