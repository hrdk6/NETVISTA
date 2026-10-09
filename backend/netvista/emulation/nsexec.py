"""Run commands inside a Mininet node's network namespace.

We deliberately do NOT use Mininet's `node.cmd()` after start-up: it drives a single shell
per node and is not safe to call from several threads (telemetry, chaos, controller).
`nsenter -t <pid> -n` gives each caller its own process in the right namespace instead.
A pid of None means the root namespace (where OVS switch ports live).
"""

from __future__ import annotations

import subprocess
from typing import Sequence


def ns_prefix(pid: int | None) -> list[str]:
    return [] if pid is None else ["nsenter", "-t", str(pid), "-n", "--"]


def run(pid: int | None, args: Sequence[str], input: str | None = None, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*ns_prefix(pid), *args],
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def popen(pid: int | None, args: Sequence[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen([*ns_prefix(pid), *args], **kwargs)


def ip_batch(pid: int | None, lines: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    """Apply many `ip` commands in one process; -force keeps going past individual errors."""
    return run(pid, ["ip", "-force", "-batch", "-"], input="\n".join(lines) + "\n", timeout=timeout)


def tc_batch(pid: int | None, lines: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return run(pid, ["tc", "-force", "-batch", "-"], input="\n".join(lines) + "\n", timeout=timeout)


def sysctl(pid: int | None, settings: dict[str, str | int]) -> subprocess.CompletedProcess[str]:
    return run(pid, ["sysctl", "-q", "-w", *[f"{k}={v}" for k, v in settings.items()]])
