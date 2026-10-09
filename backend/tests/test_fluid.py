"""Fluid model (the fast twin): closed-form checks and agreement with the packet-level twin."""

import pytest

from netvista.simulator import FluidNet, run_fluid_prediction, run_prediction
from netvista.simulator.fluid import fluid_simulate

from test_simulator import one_link, two_path_cfg


def net_one_link(**kw):
    cfg = one_link(**kw)
    return FluidNet(cfg["links"]), cfg


def test_light_load_rtt_is_propagation_only():
    net, _ = net_one_link(bw=10, delay=5, rate=1)
    ev = net.evaluate({"p": ["a", "b"]}, {"p": [1.0]})
    p = ev.pairs["p"]
    assert p.rtt_p50 == pytest.approx(10.0, abs=0.05)
    assert p.loss_pct == 0 and p.rx_mbps == pytest.approx(1.0)


def test_overload_delivers_capacity_and_fills_the_buffer():
    net, _ = net_one_link(bw=10, rate=12, queue=200)
    ev = net.evaluate({"p": ["a", "b"]}, {"p": [12.0]})
    p = ev.pairs["p"]
    assert p.rx_mbps == pytest.approx(10 * 1200 / 1242, rel=1e-6)
    assert p.loss_pct == pytest.approx(100 * (1 - 10 * 1200 / 1242 / 12), abs=0.01)
    hop = ev.hops[("a", "b")]
    assert hop.saturated
    assert hop.latency_s * 1000 == pytest.approx(200 * 1242 * 8 / 10e6 * 1000, rel=1e-6)  # Little: Q * s / C


def test_netem_buffer_counts_the_delay_line():
    # 500 ms of delay at ~9.7 Mbit/s holds ~490 packets in netem: a 300-packet limit drops even below capacity
    net, _ = net_one_link(bw=20, delay=500, rate=9.7, queue=300)
    ev = net.evaluate({"p": ["a", "b"]}, {"p": [9.7]})
    assert ev.pairs["p"].loss_pct > 30


def test_bernoulli_loss_and_down_link():
    net, _ = net_one_link(loss=10, rate=4)
    ev = net.evaluate({"p": ["a", "b"]}, {"p": [4.0]})
    assert ev.pairs["p"].loss_pct == pytest.approx(10, abs=1e-9)
    assert ev.pairs["p"].probe_loss_pct == pytest.approx(19, abs=1e-9)  # both directions
    net, _ = net_one_link(up=False)
    ev = net.evaluate({"p": ["a", "b"]}, {"p": [4.0]})
    assert ev.pairs["p"].rx_mbps == 0 and not ev.pairs["p"].alive and ev.pairs["p"].rtt_p50 is None


def test_shared_bottleneck_splits_fairly_and_drops_propagate_downstream():
    links = {
        "a-m": {"a": "a", "b": "m", "bw_mbps": 100, "delay_ms": 1},
        "c-m": {"a": "c", "b": "m", "bw_mbps": 100, "delay_ms": 1},
        "m-b": {"a": "m", "b": "b", "bw_mbps": 10, "delay_ms": 1},
        "b-d": {"a": "b", "b": "d", "bw_mbps": 8, "delay_ms": 1},
    }
    net = FluidNet(links)
    ev = net.evaluate({"x": ["a", "m", "b", "d"], "y": ["c", "m", "b"]}, {"x": [8.0], "y": [8.0]})
    x, y = ev.pairs["x"], ev.pairs["y"]
    assert x.loss_pct == pytest.approx(y.loss_pct, abs=1e-9)  # fair tail drop at m-b
    assert x.rx_mbps + y.rx_mbps == pytest.approx(10 * 1200 / 1242, rel=1e-6)
    # after m-b, x carries ~4.8 Mbit/s into the 8 Mbit/s b-d link: no second bottleneck
    assert not ev.hops[("b", "d")].saturated


@pytest.mark.parametrize("delay", [5.0, 60.0])
@pytest.mark.parametrize("up", [True, False])
def test_routing_prediction_matches_the_packet_twin(delay, up):
    cfg = two_path_cfg(r2r5_up=up, delay_r2r5=delay)
    pk, fl = run_prediction(cfg), run_fluid_prediction(cfg)
    for pair in cfg["pairs"]:
        a, b = pk["pairs"][pair], fl["pairs"][pair]
        assert a["path"] == b["path"] and a["reason"] == b["reason"]
        assert b["rtt_p50"] == pytest.approx(a["rtt_p50"], rel=0.02)
        assert b["rx_mbps"] == pytest.approx(a["rx_mbps"], rel=0.02)


@pytest.mark.parametrize("bw,rate", [(10, 4), (10, 8.5), (10, 12), (8, 12)])
def test_queue_and_loss_track_the_packet_twin(bw, rate):
    cfg = one_link(bw=bw, rate=rate, duration=14)
    cfg["warmup_s"] = 5
    from netvista.simulator import NetworkModel

    pk = NetworkModel(cfg).run()
    fl = fluid_simulate({"links": cfg["links"], "pairs": {"p": {"flows": [{"rate_mbps": rate}]}}}, {"p": ["a", "b"]})
    assert fl["flows"]["p#0"]["rx_mbps"] == pytest.approx(pk["flows"]["f"]["rx_mbps"], rel=0.03)
    assert fl["flows"]["p#0"]["loss_pct"] == pytest.approx(pk["flows"]["f"]["loss_pct"], abs=1.5)
    assert fl["links"]["a-b"]["a>b"]["util"] == pytest.approx(pk["links"]["a-b"]["a>b"]["util"], abs=0.02)


def test_fluid_is_orders_of_magnitude_faster():
    import time

    cfg = two_path_cfg()
    t0 = time.perf_counter()
    for _ in range(20):
        run_fluid_prediction(cfg)
    fluid = (time.perf_counter() - t0) / 20
    t0 = time.perf_counter()
    run_prediction(cfg)
    packet = time.perf_counter() - t0
    assert fluid * 50 < packet


def test_herd_guard_spreads_simultaneous_movers():
    # two 18 Mbit/s flows leave a dead path at once; a 30 Mbit/s alternative fits only one of them
    links = {
        "s-r1": {"a": "s", "b": "r1", "bw_mbps": 1000, "delay_ms": 0.5},
        "r1-r2": {"a": "r1", "b": "r2", "bw_mbps": 100, "delay_ms": 2, "up": False},
        "r1-r3": {"a": "r1", "b": "r3", "bw_mbps": 30, "delay_ms": 3},
        "r1-r4": {"a": "r1", "b": "r4", "bw_mbps": 100, "delay_ms": 10},
        "r2-r5": {"a": "r2", "b": "r5", "bw_mbps": 100, "delay_ms": 2},
        "r3-r5": {"a": "r3", "b": "r5", "bw_mbps": 30, "delay_ms": 3},
        "r4-r5": {"a": "r4", "b": "r5", "bw_mbps": 100, "delay_ms": 10},
        "r5-d": {"a": "r5", "b": "d", "bw_mbps": 1000, "delay_ms": 0.5},
        "t-r1": {"a": "t", "b": "r1", "bw_mbps": 1000, "delay_ms": 0.5},
    }
    top, mid, low = ["r1", "r2", "r5"], ["r1", "r3", "r5"], ["r1", "r4", "r5"]

    def pair(src):
        cands = [[src, *c, "d"] for c in (top, mid, low)]
        return {"src": src, "dst": "d", "path": cands[0], "candidates": cands, "static_path": cands[0],
                "flows": [{"rate_mbps": 18}], "offered_mbps": 18}

    cfg = {"links": links, "pairs": {"s>d": pair("s"), "t>d": pair("t")}, "routers": ["r1", "r2", "r3", "r4", "r5"],
           "mode": "adaptive", "weights": {"latency": 1.0, "loss": 5.0, "util": 0.2}, "hysteresis": 0.15, "duration_s": 6}
    herd = run_fluid_prediction(dict(cfg, herd_guard=False))
    first = herd["routing_rounds"][0]
    assert first["s>d"]["chosen"][1:] == first["t>d"]["chosen"][1:]  # both pick the same "empty" path
    guarded = run_fluid_prediction(dict(cfg, herd_guard=True))
    g0 = guarded["routing_rounds"][0]
    assert g0["s>d"]["chosen"][1:] != g0["t>d"]["chosen"][1:]  # the second sees the first one's load
