"""Smoke test: boot REAL Mininet topologies, push traffic through tc + policy routes, tear down.

Needs root + mininet (+ openvswitch for the default topology). Skipped otherwise.
    sudo /opt/netvista/venv/bin/python -m pytest -m emulation
"""

import os
import re
import shutil

import pytest

from netvista.emulation.network import EmulatedNetwork
from netvista.emulation.tc import LinkParams
from netvista.routing import build_graph, shortest_path
from netvista.routing.installer import RouteInstaller
from netvista.topology import build_address_plan, load_topology
from conftest import TOPO_DIR

pytestmark = pytest.mark.emulation


def _can_emulate() -> str | None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return "needs root"
    try:
        import mininet.net  # noqa: F401
    except ImportError:
        return "mininet not installed"
    if not shutil.which("tc"):
        return "iproute2/tc missing"
    return None


skip_reason = _can_emulate()
needs_emulation = pytest.mark.skipif(skip_reason is not None, reason=skip_reason or "")


def ping_avg(net, src, dst_ip, count=10):
    """(loss %, median RTT ms). A warm-up ping first, so ARP resolution on every hop
    does not inflate the first sample."""
    net.node_run(src, ["ping", "-c", "1", "-W", "2", dst_ip], timeout=10)
    r = net.node_run(src, ["ping", "-c", str(count), "-i", "0.2", "-W", "1", dst_ip], timeout=30)
    loss = re.search(r"(\d+(?:\.\d+)?)% packet loss", r.stdout)
    rtts = sorted(float(x) for x in re.findall(r"time=([\d.]+) ms", r.stdout))
    med = rtts[len(rtts) // 2] if rtts else None
    return float(loss.group(1)) if loss else 100.0, med


def boot(name):
    topo = load_topology(TOPO_DIR / name)
    plan = build_address_plan(topo)
    net = EmulatedNetwork(topo, plan)
    net.start()
    for lid, l in topo.links.items():
        net.apply_link_params(lid, LinkParams(**l.params()))
    inst = RouteInstaller(topo, plan, net.pids)
    inst.setup_rules()
    return topo, plan, net, inst, build_graph(topo)


@needs_emulation
def test_small_topology_tc_routes_and_failover():
    topo, plan, net, inst, g = boot("small.json")
    try:
        _, path = shortest_path(g, "c1", "srv1")
        assert path == ["c1", "r1", "r2", "r3", "srv1"]
        inst.reconcile(g, set(), {"c1>srv1": path})
        loss, avg = ping_avg(net, "c1", plan.host_ip["srv1"])
        assert loss == 0
        assert avg == pytest.approx(2 * (0.5 + 4 + 4 + 0.5), abs=2.0)  # netem delay is really applied

        qd = net.node_run("r1", ["tc", "qdisc", "show", "dev", plan.intf("r1-r2", "r1").name]).stdout
        assert "htb" in qd and "netem" in qd and "delay 4ms" in qd

        # latency injection changes the measured RTT
        net.apply_link_params("r1-r2", LinkParams(50, 24))
        _, avg2 = ping_avg(net, "c1", plan.host_ip["srv1"])
        assert avg2 == pytest.approx(avg + 40, abs=3.0)
        net.apply_link_params("r1-r2", LinkParams(50, 4))

        # fail r1-r2 and re-route over the direct r1-r3 link via policy routes
        net.set_link_admin("r1-r2", False)
        loss, _ = ping_avg(net, "c1", plan.host_ip["srv1"], count=5)
        assert loss == 100
        alt = ["c1", "r1", "r3", "srv1"]
        inst.install_flow("c1>srv1", alt, path)
        inst.reconcile(g, {"r1-r2"}, {"c1>srv1": alt})
        loss, avg3 = ping_avg(net, "c1", plan.host_ip["srv1"])
        assert loss == 0 and avg3 == pytest.approx(2 * (0.5 + 10 + 0.5), abs=2.0)
        tr = net.node_run("c1", ["traceroute", "-n", "-q", "1", "-w", "1", plan.host_ip["srv1"]], timeout=20).stdout
        assert plan.router_ip_on_link("r1-r3", "r3") in tr  # really forwarded via r3 directly
    finally:
        net.stop()


@needs_emulation
@pytest.mark.skipif(shutil.which("ovs-vsctl") is None, reason="openvswitch not installed")
def test_default_topology_boots_with_ovs_switches():
    topo, plan, net, inst, g = boot("default.json")
    try:
        paths = {}
        for c, s in topo.flow_pairs():
            _, p = shortest_path(g, c, s)
            paths[f"{c}>{s}"] = p
        inst.reconcile(g, set(), paths)
        br = net.node_run("r1", ["true"])  # namespaces alive
        assert br.returncode == 0
        for c, s in topo.flow_pairs():
            loss, avg = ping_avg(net, c, plan.host_ip[s], count=5)
            assert loss == 0, f"{c}->{s} unreachable"
            assert avg == pytest.approx(2 * 12, abs=3.0)  # 0.5+0.5+5+5+0.5+0.5 ms one way
    finally:
        net.stop()
