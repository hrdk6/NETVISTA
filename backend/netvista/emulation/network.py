"""Launch the topology JSON as a real Mininet network.

* routers  -> Mininet Hosts in their own netns with ip_forward=1 (Linux routers)
* switches -> Open vSwitch bridges in standalone (MAC-learning) mode, no OpenFlow controller
* clients/servers -> Mininet Hosts
* every link -> a veth pair; both ends get an htb+netem tree (see tc.py)

Routes are NOT installed here; the routing controller owns them.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

from ..topology import AddressPlan, Topology
from . import nsexec
from .tc import LinkParams, tc_lines

log = logging.getLogger(__name__)


class EmulationError(RuntimeError):
    pass


def _router_class():
    from mininet.node import Host

    class LinuxRouter(Host):
        """A Mininet host that forwards IPv4 packets like a router."""

        def config(self, **params):
            super().config(**params)
            self.cmd("sysctl -q -w net.ipv4.ip_forward=1")

        def terminate(self):
            self.cmd("sysctl -q -w net.ipv4.ip_forward=0")
            super().terminate()

    return LinuxRouter


class EmulatedNetwork:
    def __init__(self, topo: Topology, plan: AddressPlan, switch_impl: str = "ovs") -> None:
        self.topo = topo
        self.plan = plan
        self.switch_impl = switch_impl
        self.net = None
        self.pids: dict[str, int | None] = {}
        self._lock = threading.RLock()
        self.started_at: float | None = None
        self._tc_ready: set[str] = set()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        try:
            from mininet.log import setLogLevel
            from mininet.net import Mininet
            from mininet.node import Host, OVSSwitch
            from mininet.link import Link
        except ImportError as e:  # pragma: no cover - only on machines without mininet
            raise EmulationError("Mininet is not installed (run scripts/setup_wsl.sh as root)") from e

        setLogLevel("warning")
        LinuxRouter = _router_class()
        net = Mininet(controller=None, build=False, autoSetMacs=False, autoStaticArp=False, waitConnected=False)

        for i, n in enumerate(self.topo.nodes.values()):
            if n.type == "router":
                net.addHost(n.id, cls=LinuxRouter, ip=None)
            elif n.is_host:
                net.addHost(n.id, cls=Host, ip=None)
            else:
                if self.switch_impl == "linuxbridge":
                    from mininet.nodelib import LinuxBridge

                    net.addSwitch(n.id, cls=LinuxBridge)
                else:
                    net.addSwitch(n.id, cls=OVSSwitch, failMode="standalone", dpid=f"{i + 1:016x}", stp=False)

        for link in self.topo.links.values():
            net.addLink(
                link.a,
                link.b,
                cls=Link,
                intfName1=self.plan.intf(link.id, link.a).name,
                intfName2=self.plan.intf(link.id, link.b).name,
            )

        net.build()
        net.start()
        self.net = net
        for n in self.topo.nodes.values():
            self.pids[n.id] = None if n.type == "switch" else net.get(n.id).pid
        self._configure_l3()
        self.started_at = time.time()

    def stop(self) -> None:
        with self._lock:
            if self.net is not None:
                try:
                    self.net.stop()
                finally:
                    self.net = None

    # ------------------------------------------------------------------ configuration
    def _configure_l3(self) -> None:
        for node in self.topo.nodes.values():
            pid = self.pids[node.id]
            intfs = self.plan.intfs_of(node.id)
            if node.type == "switch":
                for intf in intfs:
                    nsexec.sysctl(None, {f"net.ipv6.conf.{intf.name}.disable_ipv6": 1})
                    nsexec.ip_batch(None, [f"link set dev {intf.name} up"])
                continue
            sys = {
                "net.ipv6.conf.all.disable_ipv6": 1,
                "net.ipv6.conf.default.disable_ipv6": 1,
                "net.ipv4.conf.all.rp_filter": 0,
                "net.ipv4.conf.default.rp_filter": 0,
                "net.ipv4.icmp_ratelimit": 0,
            }
            for intf in intfs:
                sys[f"net.ipv4.conf.{intf.name}.rp_filter"] = 0
            if node.type == "router":
                sys["net.ipv4.ip_forward"] = 1
            r = nsexec.sysctl(pid, sys)
            if r.returncode != 0:
                log.warning("sysctl on %s: %s", node.id, r.stderr.strip())
            lines = []
            for intf in intfs:
                lines.append(f"addr flush dev {intf.name}")
                if intf.cidr:
                    lines.append(f"addr add {intf.cidr} dev {intf.name}")
                lines.append(f"link set dev {intf.name} up")
            if node.is_host:
                lines.append(f"route replace default via {self.plan.host_gateway_ip[node.id]}")
            r = nsexec.ip_batch(pid, lines)
            if r.returncode != 0:
                raise EmulationError(f"ip config failed on {node.id}: {r.stderr.strip()}")

    # ------------------------------------------------------------------ operations
    def pid(self, node: str) -> int | None:
        return self.pids[node]

    def apply_link_params(self, link_id: str, p: LinkParams) -> None:
        """Shape both directions of a link (each on its sender's egress interface)."""
        link = self.topo.links[link_id]
        for node in (link.a, link.b):
            dev = self.plan.intf(link_id, node).name
            pid = self.pids[node]
            r = nsexec.tc_batch(pid, tc_lines(dev, p, create_root=dev not in self._tc_ready))
            if r.returncode != 0:
                # tree missing or foreign (e.g. interface re-created): rebuild it from scratch
                r = nsexec.tc_batch(pid, [f"qdisc del dev {dev} root", *tc_lines(dev, p, create_root=True)])
                if r.returncode != 0:
                    raise EmulationError(f"tc on {dev}: {r.stderr.strip()}")
            self._tc_ready.add(dev)

    def set_intf_admin(self, node: str, intf_names: Iterable[str], up: bool) -> None:
        state = "up" if up else "down"
        nsexec.ip_batch(self.pids[node], [f"link set dev {n} {state}" for n in intf_names])

    def set_link_admin(self, link_id: str, up: bool) -> None:
        link = self.topo.links[link_id]
        for node in (link.a, link.b):
            self.set_intf_admin(node, [self.plan.intf(link_id, node).name], up)

    def node_run(self, node: str, args: list[str], timeout: float = 10.0):
        return nsexec.run(self.pids[node], args, timeout=timeout)
