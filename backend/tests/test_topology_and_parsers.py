"""Topology validation, addressing plan, tc/iperf/proc parsers, route generation, validation maths."""

import json

import pytest

from netvista.chaos import ChaosError, validate_change
from netvista.emulation.tc import LinkParams, parse_qdisc_stats, tc_lines
from netvista.emulation.traffic import parse_iperf_interval
from netvista.routing import build_graph
from netvista.routing.installer import RouteInstaller
from netvista.telemetry import parse_net_dev, percentile
from netvista.topology import TopologyError, build_address_plan, load_topology, topology_from_dict
from netvista.validation import compare, rel_err
from conftest import TOPO_DIR


@pytest.fixture(scope="module")
def topo():
    return load_topology(TOPO_DIR / "default.json")


def test_default_topology_shape(topo):
    assert len(topo.routers) == 5 and len(topo.clients) == 2 and len(topo.servers) == 2
    assert topo.flow_pairs() == [("c1", "srv1"), ("c1", "srv2"), ("c2", "srv1"), ("c2", "srv2")]


def test_every_topology_file_loads():
    for p in TOPO_DIR.glob("*.json"):
        t = load_topology(p)
        build_address_plan(t)


def test_address_plan(topo):
    plan = build_address_plan(topo)
    assert plan.host_ip == {"c1": "10.0.1.11", "c2": "10.0.1.12", "srv1": "10.0.2.11", "srv2": "10.0.2.12"}
    assert plan.host_gateway == {"c1": "r1", "c2": "r1", "srv1": "r5", "srv2": "r5"}
    assert plan.router_ip_on_link("r2-r5", "r2") == "10.10.5.1"
    assert plan.router_ip_on_link("r2-r5", "r5") == "10.10.5.2"
    names = [i.name for ends in plan.link_intfs.values() for i in ends.values()]
    assert len(names) == len(set(names)) and all(len(n) <= 15 for n in names)
    assert plan.intf("sw1-r1", "sw1").ip is None  # switch ports are pure L2
    ips = [i.ip for ends in plan.link_intfs.values() for i in ends.values() if i.ip]
    assert len(ips) == len(set(ips))


@pytest.mark.parametrize("bad, msg", [
    ({"nodes": [{"id": "Bad-Id", "type": "router"}]}, "id must match"),
    ({"nodes": [{"id": "r1", "type": "router"}, {"id": "r1", "type": "router"}]}, "duplicate node"),
    ({"nodes": [{"id": "c1", "type": "client"}, {"id": "r1", "type": "router"}], "links": [{"a": "c1", "b": "zz"}]}, "unknown endpoint"),
    ({"nodes": [{"id": "c1", "type": "client"}, {"id": "s1", "type": "server"}, {"id": "r1", "type": "router"}, {"id": "r2", "type": "router"}],
      "links": [{"a": "c1", "b": "r1"}, {"a": "s1", "b": "r1"}]}, "not connected"),
    ({"nodes": [{"id": "c1", "type": "client"}, {"id": "r1", "type": "router"}], "links": [{"a": "c1", "b": "r1", "bw_mbps": -3}]}, "out of range"),
])
def test_invalid_topologies_rejected(bad, msg):
    with pytest.raises(TopologyError, match=msg):
        topology_from_dict(bad)


def test_tc_lines():
    p = LinkParams(bw_mbps=30, delay_ms=5, jitter_ms=1.5, loss_pct=2, queue_pkts=1000)
    full = tc_lines("r2-eth2", p)
    assert full[0] == "qdisc replace dev r2-eth2 root handle 1: htb default 1"
    assert "rate 30mbit ceil 30mbit" in full[1]
    assert full[2].endswith("netem limit 1000 delay 5ms 1.5ms distribution normal loss 2%")
    assert len(tc_lines("r2-eth2", p, create_root=False)) == 2  # HTB root cannot be changed in place


def test_parse_qdisc_stats_uses_root_only():
    js = json.dumps([
        {"kind": "htb", "handle": "1:", "root": True, "dev": "r1-eth1", "drops": 7, "backlog": 2484, "qlen": 2, "overlimits": 3},
        {"kind": "netem", "handle": "10:", "parent": "1:1", "dev": "r1-eth1", "drops": 7, "backlog": 2484, "qlen": 2},
    ])
    assert parse_qdisc_stats(js)["r1-eth1"] == {"drops": 7, "backlog_bytes": 2484, "backlog_pkts": 2, "overlimits": 3}


def test_parse_net_dev():
    text = (
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
        "r1-eth0: 1000 10 0 1 0 0 0 0 2000 20 0 3 0 0 0 0\n"
    )
    assert parse_net_dev(text)["r1-eth0"] == (1000, 10, 0, 1, 2000, 20, 0, 3)


def test_parse_iperf_interval():
    s = parse_iperf_interval("[  5]   1.00-2.00   sec   732 KBytes  6.00 Mbits/sec  0.022 ms  3/625 (0.48%)")
    assert s == {"rx_mbps": 6.0, "jitter_ms": 0.022, "lost": 3, "total": 625}
    assert parse_iperf_interval("[  5]   0.00-10.00  sec  7.15 MBytes  6.00 Mbits/sec  0.019 ms  0/6250 (0%)  receiver") is None
    assert parse_iperf_interval("Server listening on 5201") is None
    # iperf3's sequence-gap counter can wrap on reordered datagrams: an impossible interval is dropped
    assert parse_iperf_interval("[  5]   7.00-8.00   sec   732 KBytes  6.00 Mbits/sec  0.022 ms  4919131752989214/625 (491913175298921408%)") is None


def test_percentile_matches_numpy_linear():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)


def test_policy_route_lines(topo):
    plan = build_address_plan(topo)
    inst = RouteInstaller(topo, plan, {n: None for n in topo.nodes})
    lines = inst.flow_lines(["c1", "sw1", "r1", "r2", "r5", "sw2", "srv1"], "10.0.2.11", 100)
    assert lines == {
        "r1": "route replace 10.0.2.11/32 via 10.10.1.2 dev r1-eth1 table 100",
        "r2": "route replace 10.0.2.11/32 via 10.10.5.2 dev r2-eth2 table 100",
    }
    main = inst.main_table_lines(build_graph(topo), dead_links={"r2-r5"})
    assert "route replace 10.0.2.0/24 via 10.10.3.2 dev r2-eth1" in main["r2"]  # around the dead link via r4


def test_change_validation(topo):
    assert validate_change(topo, "link_latency", "r2-r5", {"add_ms": 40}) == ("r2-r5", {"add_ms": 40.0, "jitter_ms": 0.0})
    assert validate_change(topo, "traffic_burst", "c2>srv1", {"rate_mbps": 20})[0] == "c2>srv1"
    with pytest.raises(ChaosError):
        validate_change(topo, "link_loss", "nope", {"loss_pct": 5})
    with pytest.raises(ChaosError):
        validate_change(topo, "node_down", "c1", {})
    with pytest.raises(ChaosError):
        validate_change(topo, "link_latency", "r2-r5", {"add_ms": 9999})


def test_validation_error_definitions():
    assert rel_err(11, 10) == pytest.approx(10)
    assert rel_err(5, 0) is None
    rows, summary = compare(
        {"c>s": {"path": ["c", "s"], "rtt_p50": 22, "rtt_p95": 25, "rtt_p99": 30, "rx_mbps": 5.8, "loss_pct": 1.0, "probe_loss_pct": 0}},
        {"c>s": {"path": ["c", "s"], "rtt_p50": 20, "rtt_p95": 25, "rtt_p99": 30, "rx_mbps": 6.0, "loss_pct": 0.5, "probe_loss_pct": 0, "offered_mbps": 6}},
    )
    assert summary["latency_p50_mape"] == pytest.approx(10)
    assert summary["throughput_mape"] == pytest.approx(100 * 0.2 / 6)
    assert summary["loss_mae_pp"] == pytest.approx(0.5)
    assert summary["path_match_pct"] == 100


def test_validation_aggregate_rows_for_shared_bottleneck():
    pred = {"a>s": {"path": ["a"], "rx_mbps": 2.3, "loss_pct": 62.0}, "b>s": {"path": ["b"], "rx_mbps": 5.4, "loss_pct": 9.0}}
    meas = {"a>s": {"path": ["a"], "rx_mbps": 4.2, "loss_pct": 30.0, "offered_mbps": 6},
            "b>s": {"path": ["b"], "rx_mbps": 3.5, "loss_pct": 41.0, "offered_mbps": 6}}
    rows, summary = compare(pred, meas)
    total = next(r for r in rows if r["pair"] == "all" and r["metric"] == "rx_mbps")
    assert total["predicted"] == pytest.approx(7.7) and total["measured"] == pytest.approx(7.7)
    assert summary["aggregate_throughput_err"] == pytest.approx(0.0, abs=1e-9)
    assert summary["aggregate_loss_err_pp"] == pytest.approx(0.0, abs=1e-9)  # (62+9)/2 == (30+41)/2
    assert summary["throughput_mape"] > 40  # the per-flow split is still reported honestly


def test_liveness_is_judged_up_to_what_the_agent_has_reported():
    from netvista.telemetry.health import ProbeStream

    s = ProbeStream("L:r1-r2", created_t=0.0)
    s.on_reply(100.0, 10.0)
    s.seen_until = 100.0
    # the backend was starved for 1.5 s: no report since the last echo. Not a failure, just late news
    assert s.is_dead(101.5, 1.2, 3.0) is False
    # the agent's reports keep coming but carry no echo: that IS silence on the wire
    s.seen_until = 101.5
    assert s.is_dead(101.6, 1.2, 3.0) is True
    # an agent that stopped reporting altogether is judged on the backend's own clock
    t = ProbeStream("L:r2-r5", created_t=0.0)
    t.on_reply(100.0, 10.0)
    t.seen_until = 100.0
    assert t.is_dead(104.0, 1.2, 3.0) is True


def _store_with(n: int, t: float):
    from netvista.telemetry.health import ProbeStore

    store = ProbeStore()
    for i in range(n):
        s = store.ensure(f"L:s{i}", 0.0)
        s.on_reply(t, 10.0)
        s.seen_until = t
    return store


def _tick(store, t: float, answering=()):
    for sid, s in store.streams.items():
        if sid in answering:
            s.on_reply(t, 10.0)
        s.seen_until = t
    return store.check_stall(t)


def test_host_stall_is_not_a_link_failure():
    """Every stream mute at once (WSL2 freezes all namespaces ~1.3 s every ~32 s) must not kill links,
    and the echoes that sat through the freeze must not pollute the RTT statistics."""
    store = _store_with(8, 100.0)
    ended = []
    store.on_stall = ended.append
    t = 100.0
    while t < 101.5:  # 1.5 s of total silence: longer than the 1.2 s dead interval
        t = round(t + 0.1, 1)
        _tick(store, t)
    s0 = store.streams["L:s0"]
    assert store.stalls and store.stalls[-1].end is None
    assert s0.is_dead(t, 1.2, 3.0) is False and s0.silence(t) < 0.6
    # the freeze lifts: the held echoes arrive with ~1.4 s RTT, then normal ones
    for s in store.streams.values():
        s.on_reply(t + 0.05, 1400.0)
    _tick(store, t + 0.1, answering=set(store.streams))
    assert ended and 1.3 < ended[0].end - ended[0].start < 1.8 and not ended[0].outage
    assert s0.excluded >= 1 and max(r for _, r in s0.outcomes) == 10.0
    assert s0.is_dead(t + 0.2, 1.2, 3.0) is False
    assert store.stall_summary()["count"] == 1


def test_real_failure_during_normal_operation_is_still_detected():
    store = _store_with(8, 100.0)
    t = 100.0
    others = {sid for sid in store.streams if sid not in ("L:s0", "L:s1")}  # 2 of 8 links fail: 25%
    while t < 101.4:
        t = round(t + 0.1, 1)
        _tick(store, t, answering=others)
    assert not store.stalls
    assert store.streams["L:s0"].is_dead(t, 1.2, 3.0) is True
    assert store.streams["L:s2"].is_dead(t, 1.2, 3.0) is False


def test_a_long_total_silence_is_judged_as_an_outage():
    store = _store_with(8, 100.0)
    t = 100.0
    while t < 105.0:
        t = round(t + 0.1, 1)
        _tick(store, t)
    assert store.stalls[-1].outage
    assert store.streams["L:s0"].is_dead(t, 1.2, 3.0) is True  # the excuse is withdrawn after STALL_MAX_S


def test_a_link_dead_before_the_stall_stays_dead():
    store = _store_with(8, 100.0)
    t = 100.0
    others = set(store.streams) - {"L:s0"}
    while t < 102.0:  # s0 fails at 100.0 and is declared dead at ~101.2
        t = round(t + 0.1, 1)
        _tick(store, t, answering=others)
    assert store.streams["L:s0"].is_dead(t, 1.2, 3.0) is True
    while t < 103.4:  # then the host freezes
        t = round(t + 0.1, 1)
        _tick(store, t)
    assert store.stalls and store.streams["L:s0"].is_dead(t, 1.2, 3.0) is True
    assert store.streams["L:s3"].is_dead(t, 1.2, 3.0) is False
