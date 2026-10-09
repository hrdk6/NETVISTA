"""Central tunables. Every timing constant that matters for the viva lives here."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    topology_path: Path = REPO_ROOT / "topologies" / "default.json"
    runs_dir: Path = REPO_ROOT / "runs"
    frontend_dist: Path = REPO_ROOT / "frontend" / "dist"
    # "ovs" = Open vSwitch in standalone (learning-switch) mode; "linuxbridge" is a fallback.
    switch_impl: str = os.environ.get("NETVISTA_SWITCH", "ovs")

    # --- probing (BFD-like liveness + RTT/loss) ---
    probe_port: int = 47000
    probe_interval_s: float = 0.1  # 10 probes/s per target
    probe_timeout_s: float = 2.0  # a probe with no echo after this is counted as lost
    probe_payload_bytes: int = 64
    # a probed adjacency is declared DEAD when no echo arrived for max(dead_min, dead_mult * srtt)
    dead_min_s: float = 1.2
    dead_mult: float = 3.0

    # --- counters ---
    counter_interval_s: float = 0.5
    tc_stats_interval_s: float = 1.0

    # --- routing controller ---
    controller_tick_s: float = 0.1
    rescore_interval_s: float = 1.0
    reconcile_interval_s: float = 2.0
    hysteresis: float = 0.15  # switch only if the new path is >=15% better
    hold_down_s: float = 3.0  # minimum time between two voluntary switches of one flow
    k_paths: int = 8

    # --- broadcast ---
    ws_interval_s: float = 0.5
    history_len: int = 900  # seconds of 1 Hz history kept for the metrics page

    # --- traffic ---
    iperf_payload_bytes: int = 1200
    iperf_base_port: int = 5201

    extra: dict = field(default_factory=dict)

    @property
    def scenarios_dir(self) -> Path:
        return self.runs_dir / "scenarios"


# On-the-wire size of one iperf3 UDP datagram: payload + UDP(8) + IPv4(20) + Ethernet(14).
# tc/HTB on a veth accounts the Ethernet header, so the simulator uses the same number.
L2_L3_L4_OVERHEAD = 8 + 20 + 14


def wire_bytes(payload: int) -> int:
    return payload + L2_L3_L4_OVERHEAD
