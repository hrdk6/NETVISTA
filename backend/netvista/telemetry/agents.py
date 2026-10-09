"""Spawn one probe agent per namespaced node and feed its output into the ProbeStore."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..emulation import nsexec
from ..topology import AddressPlan, Topology
from .health import ProbeStore

log = logging.getLogger(__name__)
AGENT_PATH = Path(__file__).with_name("probe_agent.py")


@dataclass(frozen=True)
class ProbeTarget:
    stream_id: str
    ip: str


def plan_probe_targets(topo: Topology, plan: AddressPlan) -> dict[str, list[ProbeTarget]]:
    """Which node probes what.

    * routers probe the far end of every router-router link (one stream per link)
    * every host probes its gateway (covers its access segment)
    * every client probes every server (the end-to-end path of each managed flow)
    """
    targets: dict[str, list[ProbeTarget]] = {n: [] for n in topo.nodes if topo.nodes[n].type != "switch"}
    for link in topo.links.values():
        if topo.nodes[link.a].type == "router" and topo.nodes[link.b].type == "router":
            targets[link.a].append(ProbeTarget(f"L:{link.id}", plan.router_ip_on_link(link.id, link.b)))
    for h in topo.hosts:
        targets[h].append(ProbeTarget(f"G:{h}", plan.host_gateway_ip[h]))
    for c, s in topo.flow_pairs():
        targets[c].append(ProbeTarget(f"F:{c}>{s}", plan.host_ip[s]))
    return targets


class AgentManager:
    def __init__(self, store: ProbeStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.procs: dict[str, subprocess.Popen] = {}
        self.targets: dict[str, list[ProbeTarget]] = {}
        self.lines_seen: dict[str, int] = {}

    def start(self, pids: dict[str, int | None], targets: dict[str, list[ProbeTarget]]) -> None:
        self.targets = targets
        now = time.time()
        for node, tlist in targets.items():
            for t in tlist:
                self.store.ensure(t.stream_id, now)
            args = [
                sys.executable, "-u", str(AGENT_PATH),
                "--port", str(self.settings.probe_port),
                "--interval", str(self.settings.probe_interval_s),
                "--timeout", str(self.settings.probe_timeout_s),
                "--payload", str(self.settings.probe_payload_bytes),
                "--targets", json.dumps([{"ip": t.ip} for t in tlist]),
                "--tag", f"netvista-agent-{node}",
            ]
            proc = nsexec.popen(
                pids[node], args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            self.procs[node] = proc
            self.lines_seen[node] = 0
            threading.Thread(target=self._reader, args=(node, proc, tlist), name=f"agent-{node}", daemon=True).start()

    def _reader(self, node: str, proc: subprocess.Popen, tlist: list[ProbeTarget]) -> None:
        assert proc.stdout
        timeout = self.settings.probe_timeout_s
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.lines_seen[node] += 1
            if "r" not in msg:
                continue
            now = time.time()  # backend receive time drives liveness (agent clock only used for RTT)
            with self.store.lock:
                for idx, _seq, rtt in msg["r"]:
                    if idx < len(tlist):
                        self.store.ensure(tlist[idx].stream_id, now).on_reply(now, float(rtt))
                for idx, _seq in msg["l"]:
                    if idx < len(tlist):
                        # a loss is only known `timeout` after the probe left; date it at send time
                        self.store.ensure(tlist[idx].stream_id, now).on_loss(now - timeout)
        log.warning("probe agent on %s exited (code %s)", node, proc.poll())

    def alive(self) -> dict[str, bool]:
        return {n: p.poll() is None for n, p in self.procs.items()}

    def stop(self) -> None:
        for p in self.procs.values():
            if p.poll() is None:
                try:
                    p.stdin and p.stdin.close()
                except OSError:
                    pass
                p.terminate()
        for p in self.procs.values():
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
