"""Real traffic: iperf3 UDP constant-bit-rate flows between Mininet hosts.

UDP CBR is used on purpose: its offered load is known exactly, so the simulator can be
given the same demand (TCP would adapt its rate and make predictions untestable).
Per-second receiver reports (throughput, loss, jitter) are parsed from the iperf3 server.
"""

from __future__ import annotations

import itertools
import logging
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from ..events import EventBus
from ..topology import AddressPlan
from . import nsexec
from .network import EmulatedNetwork

log = logging.getLogger(__name__)

# [  5]   1.00-2.00   sec   732 KBytes  6.00 Mbits/sec  0.022 ms  0/625 (0%)
_IPERF_RE = re.compile(
    r"\[\s*\d+\]\s+([\d.]+)-([\d.]+)\s+sec\s+[\d.]+\s+\w?Bytes\s+([\d.]+)\s+(\w?)bits/sec\s+"
    r"([\d.]+)\s+ms\s+(\d+)/(\d+)\s+\(([^)%]*)%\)"
)
_UNIT = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}


def parse_iperf_interval(line: str) -> dict | None:
    m = _IPERF_RE.search(line)
    if not m:
        return None
    t0, t1 = float(m.group(1)), float(m.group(2))
    if t1 - t0 > 1.5 or "receiver" in line or "sender" in line:
        return None  # end-of-test summary, not a 1 s interval
    return {
        "rx_mbps": float(m.group(3)) * _UNIT.get(m.group(4), 1.0),
        "jitter_ms": float(m.group(5)),
        "lost": int(m.group(6)),
        "total": int(m.group(7)),
    }


@dataclass
class TrafficFlow:
    id: str
    src: str
    dst: str
    rate_mbps: float
    kind: str  # background | burst
    port: int
    started_at: float
    duration_s: float | None
    state: str = "starting"  # starting | running | finished | stopped | failed
    ended_at: float | None = None
    server: subprocess.Popen | None = None
    client: subprocess.Popen | None = None
    samples: deque = field(default_factory=lambda: deque(maxlen=900))  # (t, rx_mbps, lost, total, jitter)

    @property
    def pair(self) -> str:
        return f"{self.src}>{self.dst}"

    def latest(self) -> dict | None:
        if not self.samples:
            return None
        t, rx, lost, total, jit = self.samples[-1]
        return {"t": t, "rx_mbps": rx, "loss_pct": 100.0 * lost / total if total else 0.0, "jitter_ms": jit}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "src": self.src,
            "dst": self.dst,
            "pair": self.pair,
            "rate_mbps": self.rate_mbps,
            "kind": self.kind,
            "port": self.port,
            "state": self.state,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": self.duration_s,
            "latest": self.latest(),
        }


class TrafficManager:
    def __init__(self, net: EmulatedNetwork, plan: AddressPlan, events: EventBus, payload_bytes: int = 1200, base_port: int = 5201) -> None:
        self.net = net
        self.plan = plan
        self.events = events
        self.payload = payload_bytes
        self.base_port = base_port
        self.flows: dict[str, TrafficFlow] = {}
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watcher = threading.Thread(target=self._watch, name="traffic-watch", daemon=True)
        self._watcher.start()

    # ------------------------------------------------------------------ control
    def _free_port(self, dst: str) -> int:
        used = {f.port for f in self.flows.values() if f.dst == dst and f.state in ("starting", "running")}
        for p in range(self.base_port, self.base_port + 200):
            if p not in used:
                return p
        raise RuntimeError("no free iperf3 port")

    def start_flow(self, src: str, dst: str, rate_mbps: float, kind: str = "background", duration_s: float | None = None) -> TrafficFlow:
        if src not in self.plan.host_ip or dst not in self.plan.host_ip:
            raise ValueError("src and dst must be hosts")
        with self._lock:
            port = self._free_port(dst)
            flow = TrafficFlow(
                id=f"t{next(self._ids)}", src=src, dst=dst, rate_mbps=float(rate_mbps), kind=kind,
                port=port, started_at=time.time(), duration_s=duration_s,
            )
            self.flows[flow.id] = flow
        flow.server = nsexec.popen(
            self.net.pid(dst),
            ["iperf3", "-s", "-1", "-p", str(port), "-i", "1", "-f", "m", "--forceflush"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, bufsize=1,
        )
        threading.Thread(target=self._read_server, args=(flow,), name=f"iperf-{flow.id}", daemon=True).start()
        time.sleep(0.3)  # give the server time to bind before the client connects
        dur = int(duration_s) if duration_s else 86400
        flow.client = nsexec.popen(
            self.net.pid(src),
            ["iperf3", "-c", self.plan.host_ip[dst], "-u", "-b", f"{rate_mbps}M", "-l", str(self.payload),
             "-t", str(dur), "-p", str(port), "-i", "0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        flow.state = "running"
        self.events.emit(
            "traffic.start",
            f"{kind} traffic {src} → {dst} at {rate_mbps:g} Mbit/s" + (f" for {duration_s:g}s" if duration_s else ""),
            flow=flow.id, src=src, dst=dst, rate_mbps=rate_mbps, traffic_kind=kind,
        )
        return flow

    def stop_flow(self, flow_id: str, reason: str = "stopped") -> None:
        with self._lock:
            flow = self.flows.get(flow_id)
        if not flow or flow.state not in ("starting", "running"):
            return
        self._kill(flow)
        flow.state = reason
        flow.ended_at = time.time()
        self.events.emit("traffic.stop", f"{flow.kind} traffic {flow.src} → {flow.dst} {reason}", flow=flow.id)

    def stop_all(self, kind: str | None = None) -> None:
        for f in list(self.flows.values()):
            if kind is None or f.kind == kind:
                self.stop_flow(f.id)

    def shutdown(self) -> None:
        self._stop.set()
        for f in list(self.flows.values()):
            self._kill(f)

    @staticmethod
    def _kill(flow: TrafficFlow) -> None:
        for p in (flow.client, flow.server):
            if p and p.poll() is None:
                p.terminate()
        for p in (flow.client, flow.server):
            if p:
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()

    # ------------------------------------------------------------------ monitoring
    def _read_server(self, flow: TrafficFlow) -> None:
        assert flow.server and flow.server.stdout
        for line in flow.server.stdout:
            s = parse_iperf_interval(line)
            if s:
                flow.samples.append((time.time(), s["rx_mbps"], s["lost"], s["total"], s["jitter_ms"]))

    def _watch(self) -> None:
        while not self._stop.wait(0.5):
            for f in list(self.flows.values()):
                if f.state == "running" and f.client and f.client.poll() is not None:
                    ok = f.client.returncode == 0
                    self._kill(f)
                    f.state = "finished" if ok else "failed"
                    f.ended_at = time.time()
                    self.events.emit(
                        "traffic.end",
                        f"{f.kind} traffic {f.src} → {f.dst} " + ("finished" if ok else f"exited with code {f.client.returncode}"),
                        severity="info" if ok else "warn",
                        flow=f.id,
                    )

    # ------------------------------------------------------------------ queries
    def running(self) -> list[TrafficFlow]:
        return [f for f in self.flows.values() if f.state == "running"]

    def offered_mbps(self, src: str, dst: str) -> float:
        return sum(f.rate_mbps for f in self.running() if f.src == src and f.dst == dst)

    def pair_stats(self, src: str, dst: str, window_s: float = 3.0, t_end: float | None = None) -> dict | None:
        """Receiver-side stats for one src->dst pair over [t_end - window_s, t_end].

        Throughput is the sum over concurrent iperf3 flows of each flow's mean 1 s rate.
        """
        t_end = t_end or time.time()
        t0 = t_end - window_s
        rx, lost, total, jit, n = 0.0, 0, 0, 0.0, 0
        for f in self.flows.values():
            if f.src != src or f.dst != dst:
                continue
            win = [s for s in list(f.samples) if t0 <= s[0] <= t_end]
            if not win:
                continue
            rx += sum(s[1] for s in win) / len(win)
            lost += sum(s[2] for s in win)
            total += sum(s[3] for s in win)
            jit += sum(s[4] for s in win)
            n += len(win)
        if n == 0:
            return None
        return {
            "rx_mbps": rx,
            "loss_pct": 100.0 * lost / total if total else 0.0,
            "jitter_ms": jit / n,
            "samples": n,
        }

    def snapshot(self) -> list[dict]:
        cutoff = time.time() - 120
        return [f.to_dict() for f in self.flows.values() if f.state in ("starting", "running") or (f.ended_at or 0) > cutoff]
