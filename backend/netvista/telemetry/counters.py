"""Interface counters (bytes/packets/drops) for every veth in the emulation.

`/proc/<pid>/net/dev` shows the counters of the network namespace that <pid> lives in, so a
plain file read per namespace is enough (no subprocess). Queue drops (netem loss, buffer
overflow) are qdisc statistics, read with `tc -s -j qdisc show` once per second.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from ..emulation import nsexec
from ..emulation.tc import parse_qdisc_stats

log = logging.getLogger(__name__)


def parse_net_dev(text: str) -> dict[str, tuple[int, ...]]:
    """-> {ifname: (rx_bytes, rx_packets, rx_errs, rx_drop, tx_bytes, tx_packets, tx_errs, tx_drop)}"""
    out: dict[str, tuple[int, ...]] = {}
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        f = rest.split()
        if len(f) < 16:
            continue
        v = [int(x) for x in f[:16]]
        out[name.strip()] = (v[0], v[1], v[2], v[3], v[8], v[9], v[10], v[11])
    return out


@dataclass
class IntfRates:
    rx_bps: float = 0.0
    tx_bps: float = 0.0
    rx_pps: float = 0.0
    tx_pps: float = 0.0
    qdisc_drops_ps: float = 0.0
    qdisc_drops_total: int = 0
    rx_drops_total: int = 0
    backlog_pkts: int = 0
    tx_bytes_total: int = 0
    rx_bytes_total: int = 0
    t: float = 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class _Prev:
    t: float
    vals: tuple[int, ...]


class CounterPoller:
    def __init__(self, ns_intfs: dict[int | None, list[str]], interval_s: float = 0.5, tc_interval_s: float = 1.0) -> None:
        self.ns_intfs = ns_intfs  # pid (None = root ns) -> interface names to report
        self.interval = interval_s
        self.tc_interval = tc_interval_s
        self.rates: dict[str, IntfRates] = {}
        self._prev: dict[str, _Prev] = {}
        self._prev_drops: dict[str, tuple[float, int]] = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="counters", daemon=True)
        self.tc_thread = threading.Thread(target=self._run_tc, name="tc-stats", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self.tc_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get(self, intf: str) -> IntfRates:
        with self.lock:
            return self.rates.get(intf) or IntfRates()

    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            for pid, names in self.ns_intfs.items():
                path = "/proc/self/net/dev" if pid is None else f"/proc/{pid}/net/dev"
                try:
                    with open(path, encoding="ascii") as f:
                        stats = parse_net_dev(f.read())
                except OSError:
                    continue
                now = time.time()
                with self.lock:
                    for name in names:
                        v = stats.get(name)
                        if v is None:
                            continue
                        r = self.rates.setdefault(name, IntfRates())
                        prev = self._prev.get(name)
                        if prev is not None:
                            dt = now - prev.t
                            if dt > 0:
                                r.rx_bps = max(0, v[0] - prev.vals[0]) * 8 / dt
                                r.rx_pps = max(0, v[1] - prev.vals[1]) / dt
                                r.tx_bps = max(0, v[4] - prev.vals[4]) * 8 / dt
                                r.tx_pps = max(0, v[5] - prev.vals[5]) / dt
                        r.rx_drops_total = v[3]
                        r.rx_bytes_total, r.tx_bytes_total = v[0], v[4]
                        r.t = now
                        self._prev[name] = _Prev(now, v)
            self._stop.wait(max(0.05, self.interval - (time.time() - t0)))

    def _run_tc(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            for pid, names in self.ns_intfs.items():
                try:
                    res = nsexec.run(pid, ["tc", "-s", "-j", "qdisc", "show"], timeout=3)
                except Exception as e:  # pragma: no cover
                    log.debug("tc stats failed: %s", e)
                    continue
                q = parse_qdisc_stats(res.stdout)
                now = time.time()
                with self.lock:
                    for name in names:
                        s = q.get(name)
                        if s is None:
                            continue
                        r = self.rates.setdefault(name, IntfRates())
                        prev = self._prev_drops.get(name)
                        if prev is not None and now > prev[0]:
                            r.qdisc_drops_ps = max(0, s["drops"] - prev[1]) / (now - prev[0])
                        r.qdisc_drops_total = s["drops"]
                        r.backlog_pkts = s["backlog_pkts"]
                        self._prev_drops[name] = (now, s["drops"])
            self._stop.wait(max(0.1, self.tc_interval - (time.time() - t0)))
