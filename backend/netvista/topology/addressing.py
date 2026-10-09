"""Deterministic IPv4 addressing plan derived from a Topology.

Rules (documented in DESIGN.md so they can be explained in the viva):
  * every switch is a LAN: 10.0.<k>.0/24, gateway router .1, hosts .11, .12, ...
  * a host wired straight to a router also gets its own LAN 10.0.<k>.0/24 (router .1, host .11)
  * every router-router link is a point-to-point subnet 10.10.<m>.0/24 (endpoint a = .1, b = .2)
  * interface names are "<node>-eth<i>", i = index of the link among that node's links (JSON order)
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from .model import Topology


@dataclass(frozen=True)
class Interface:
    node: str
    name: str
    link_id: str
    ip: str | None  # None for switch ports (pure L2)
    prefixlen: int | None

    @property
    def cidr(self) -> str | None:
        return f"{self.ip}/{self.prefixlen}" if self.ip else None


@dataclass
class Subnet:
    cidr: str
    kind: str  # "lan" | "p2p"
    routers: dict[str, str] = field(default_factory=dict)  # router id -> its IP in this subnet
    hosts: dict[str, str] = field(default_factory=dict)  # host id -> IP
    links: list[str] = field(default_factory=list)


@dataclass
class AddressPlan:
    # link id -> {endpoint node id -> Interface}
    link_intfs: dict[str, dict[str, Interface]]
    subnets: list[Subnet]
    host_ip: dict[str, str]
    host_gateway: dict[str, str]  # host -> gateway router id
    host_gateway_ip: dict[str, str]
    host_subnet: dict[str, str]

    def intf(self, link_id: str, node: str) -> Interface:
        return self.link_intfs[link_id][node]

    def intfs_of(self, node: str) -> list[Interface]:
        return [ends[node] for ends in self.link_intfs.values() if node in ends]

    def router_ip_on_link(self, link_id: str, router: str) -> str:
        ip = self.link_intfs[link_id][router].ip
        if ip is None:
            raise KeyError(f"{router} has no IP on {link_id}")
        return ip

    def subnet_of_link(self, link_id: str) -> Subnet:
        for s in self.subnets:
            if link_id in s.links:
                return s
        raise KeyError(link_id)

    def owner_of_ip(self, ip: str) -> str | None:
        for ends in self.link_intfs.values():
            for intf in ends.values():
                if intf.ip == ip:
                    return intf.node
        return None

    def to_dict(self) -> dict:
        return {
            "links": {
                lid: {n: {"name": i.name, "ip": i.ip, "prefixlen": i.prefixlen} for n, i in ends.items()}
                for lid, ends in self.link_intfs.items()
            },
            "subnets": [
                {"cidr": s.cidr, "kind": s.kind, "routers": s.routers, "hosts": s.hosts, "links": s.links}
                for s in self.subnets
            ],
            "hosts": {
                h: {"ip": self.host_ip[h], "gateway": self.host_gateway[h], "gateway_ip": self.host_gateway_ip[h], "subnet": self.host_subnet[h]}
                for h in self.host_ip
            },
        }


def build_address_plan(topo: Topology) -> AddressPlan:
    # 1. interface names
    names: dict[tuple[str, str], str] = {}
    for node in topo.nodes:
        for i, link in enumerate(topo.links_of(node)):
            names[(link.id, node)] = f"{node}-eth{i}"

    ips: dict[tuple[str, str], tuple[str, int]] = {}
    subnets: list[Subnet] = []
    host_ip: dict[str, str] = {}
    host_gw: dict[str, str] = {}
    host_gw_ip: dict[str, str] = {}
    host_subnet: dict[str, str] = {}
    lan_k = 1
    p2p_m = 1

    # 2. LANs behind switches
    for sw in topo.switches:
        net = ipaddress.ip_network(f"10.0.{lan_k}.0/24")
        lan_k += 1
        sub = Subnet(cidr=str(net), kind="lan")
        hosts_seen = 0
        for link in topo.links_of(sw):
            peer = link.other(sw)
            sub.links.append(link.id)
            if topo.nodes[peer].type == "router":
                ip = str(net.network_address + 1)
                sub.routers[peer] = ip
            else:
                ip = str(net.network_address + 11 + hosts_seen)
                hosts_seen += 1
                sub.hosts[peer] = ip
            ips[(link.id, peer)] = (ip, 24)
        gw = next(iter(sub.routers))
        for h, ip in sub.hosts.items():
            host_ip[h] = ip
            host_gw[h] = gw
            host_gw_ip[h] = sub.routers[gw]
            host_subnet[h] = sub.cidr
        subnets.append(sub)

    # 3. hosts wired straight to a router
    for h in topo.hosts:
        if h in host_ip:
            continue
        link = topo.links_of(h)[0]
        router = link.other(h)
        net = ipaddress.ip_network(f"10.0.{lan_k}.0/24")
        lan_k += 1
        gw_ip = str(net.network_address + 1)
        hip = str(net.network_address + 11)
        ips[(link.id, router)] = (gw_ip, 24)
        ips[(link.id, h)] = (hip, 24)
        subnets.append(Subnet(cidr=str(net), kind="lan", routers={router: gw_ip}, hosts={h: hip}, links=[link.id]))
        host_ip[h], host_gw[h], host_gw_ip[h], host_subnet[h] = hip, router, gw_ip, str(net)

    # 4. router-router point-to-point links
    for link in topo.links.values():
        if topo.nodes[link.a].type == "router" and topo.nodes[link.b].type == "router":
            net = ipaddress.ip_network(f"10.10.{p2p_m}.0/24")
            p2p_m += 1
            ip_a, ip_b = str(net.network_address + 1), str(net.network_address + 2)
            ips[(link.id, link.a)] = (ip_a, 24)
            ips[(link.id, link.b)] = (ip_b, 24)
            subnets.append(Subnet(cidr=str(net), kind="p2p", routers={link.a: ip_a, link.b: ip_b}, links=[link.id]))

    link_intfs: dict[str, dict[str, Interface]] = {}
    for link in topo.links.values():
        ends = {}
        for node in (link.a, link.b):
            ip, plen = ips.get((link.id, node), (None, None))
            ends[node] = Interface(node=node, name=names[(link.id, node)], link_id=link.id, ip=ip, prefixlen=plen)
        link_intfs[link.id] = ends

    return AddressPlan(
        link_intfs=link_intfs,
        subnets=subnets,
        host_ip=host_ip,
        host_gateway=host_gw,
        host_gateway_ip=host_gw_ip,
        host_subnet=host_subnet,
    )
