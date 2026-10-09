"""SimPy twin: queueing/loss behaviour against closed-form expectations."""

import pytest

from netvista.simulator import NetworkModel, PROBE_WIRE_BYTES, fit_overheads, run_prediction


def one_link(bw=10.0, delay=5.0, loss=0.0, up=True, queue=1000, rate=4.0, duration=8.0, seed=1, jitter=0.0):
    return {
        "links": {"a-b": {"a": "a", "b": "b", "bw_mbps": bw, "delay_ms": delay, "jitter_ms": jitter, "loss_pct": loss, "queue_pkts": queue, "up": up}},
        "flows": [{"id": "f", "path": ["a", "b"], "rate_mbps": rate, "payload_bytes": 1200, "wire_bytes": 1242}] if rate else [],
        "probes": [{"id": "p", "path": ["a", "b"], "interval_s": 0.1}],
        "duration_s": duration,
        "warmup_s": 2.0,
        "seed": seed,
    }


def test_light_load_latency_is_propagation_plus_serialisation():
    r = NetworkModel(one_link(bw=10, delay=5, rate=1)).run()
    tx_probe = PROBE_WIRE_BYTES * 8 / 10e6 * 1000
    assert r["probes"]["p"]["rtt_p50"] == pytest.approx(2 * (5 + tx_probe), abs=0.3)
    assert r["flows"]["f"]["loss_pct"] == 0
    assert r["flows"]["f"]["rx_mbps"] == pytest.approx(1.0, rel=0.02)


def test_bernoulli_loss_rate():
    r = NetworkModel(one_link(loss=10, rate=4, duration=20)).run()
    assert r["flows"]["f"]["loss_pct"] == pytest.approx(10, abs=1.5)
    # a probe crosses the lossy link twice: 1 - 0.9^2 = 19 %
    assert r["probes"]["p"]["loss_pct"] == pytest.approx(19, abs=6)


def test_overload_caps_throughput_at_capacity_and_fills_the_buffer():
    # 12 Mbit/s of payload into a 10 Mbit/s link: goodput = 10 * 1200/1242
    r = NetworkModel(one_link(bw=10, rate=12, queue=200, duration=12)).run()
    f = r["flows"]["f"]
    assert f["rx_mbps"] == pytest.approx(10 * 1200 / 1242, rel=0.02)
    assert f["loss_pct"] == pytest.approx(100 * (1 - 10 * 1200 / 1242 / 12), abs=1.5)
    # full 200-packet buffer drained at 10 Mbit/s ~ 200 * 1242 * 8 / 10e6 = 199 ms of queueing
    assert r["links"]["a-b"]["a>b"]["queue_ms"] == pytest.approx(199, rel=0.1)


def test_link_down_drops_everything():
    r = NetworkModel(one_link(up=False)).run()
    assert r["flows"]["f"]["received"] == 0 and r["flows"]["f"]["loss_pct"] == 100
    assert r["probes"]["p"]["loss_pct"] == 100


def test_same_seed_is_reproducible_different_seed_is_not_identical():
    a = NetworkModel(one_link(loss=5, jitter=1)).run()
    b = NetworkModel(one_link(loss=5, jitter=1)).run()
    c = NetworkModel(one_link(loss=5, jitter=1, seed=2)).run()
    assert a == b
    assert a["flows"]["f"]["received"] != c["flows"]["f"]["received"]


def test_token_bucket_lets_small_probe_pass_behind_data_packet():
    # HTB with a 1600 B bucket: a 106 B probe right behind a 1242 B packet is not delayed by
    # the big packet's serialisation time (a pure FIFO server would add ~1 ms at 10 Mbit/s)
    r = NetworkModel(one_link(bw=10, delay=5, rate=2, duration=10)).run()
    assert r["probes"]["p"]["rtt_p95"] < 2 * 5 + 0.3


def test_token_bucket_converges_to_rate_under_load():
    r = NetworkModel(one_link(bw=10, rate=9.5, duration=12)).run()
    assert r["links"]["a-b"]["a>b"]["util"] == pytest.approx(9.5 * 1242 / 1200 / 10, rel=0.02)


def test_source_jitter_prevents_phase_locked_unfairness():
    # two identical 6 Mbit/s flows into an 8 Mbit/s tail-drop link should share it roughly
    # fairly; perfectly periodic sources phase-lock and split it ~11 % / 60 % loss instead
    def split(jitter):
        cfg = one_link(bw=8, rate=0, duration=16)
        cfg["flows"] = [{"id": f, "path": ["a", "b"], "rate_mbps": 6} for f in ("f1", "f2")]
        cfg["source_jitter"] = jitter
        cfg["warmup_s"] = 5
        r = NetworkModel(cfg).run()["flows"]
        return abs(r["f1"]["loss_pct"] - r["f2"]["loss_pct"]), r["f1"]["rx_mbps"] + r["f2"]["rx_mbps"]

    locked_gap, _ = split(0.0)
    fair_gap, total = split(0.3)
    assert locked_gap > 20 and fair_gap < 5
    assert total == pytest.approx(8 * 1200 / 1242, rel=0.02)  # the bottleneck total is the same either way


def test_mm1_like_queueing_grows_with_utilisation():
    lo = NetworkModel(one_link(bw=10, rate=3, jitter=2)).run()["links"]["a-b"]["a>b"]["queue_ms"]
    hi = NetworkModel(one_link(bw=10, rate=9, jitter=2)).run()["links"]["a-b"]["a>b"]["queue_ms"]
    assert hi > lo


def two_path_cfg(mode="adaptive", r2r5_up=True, delay_r2r5=5.0):
    links = {
        "c-r1": {"a": "c", "b": "r1", "bw_mbps": 100, "delay_ms": 0.5},
        "r1-r2": {"a": "r1", "b": "r2", "bw_mbps": 50, "delay_ms": 5},
        "r2-r5": {"a": "r2", "b": "r5", "bw_mbps": 30, "delay_ms": delay_r2r5, "up": r2r5_up},
        "r1-r3": {"a": "r1", "b": "r3", "bw_mbps": 50, "delay_ms": 8},
        "r3-r5": {"a": "r3", "b": "r5", "bw_mbps": 50, "delay_ms": 8},
        "r5-s": {"a": "r5", "b": "s", "bw_mbps": 100, "delay_ms": 0.5},
    }
    top = ["c", "r1", "r2", "r5", "s"]
    bottom = ["c", "r1", "r3", "r5", "s"]
    return {
        "links": links,
        "pairs": {"c>s": {"src": "c", "dst": "s", "path": top, "candidates": [top, bottom], "static_path": top,
                          "flows": [{"rate_mbps": 5}], "offered_mbps": 5}},
        "routers": ["r1", "r2", "r3", "r5"],
        "mode": mode,
        "weights": {"latency": 1.0, "loss": 5.0, "util": 0.2},
        "hysteresis": 0.15,
        "duration_s": 6,
        "warmup_s": 2,
        "seed": 3,
    }


def test_twin_predicts_failover_in_adaptive_mode():
    res = run_prediction(two_path_cfg(r2r5_up=False))
    p = res["pairs"]["c>s"]
    assert p["path"] == ["c", "r1", "r3", "r5", "s"] and p["rerouted"] and p["reason"] == "failover"
    assert p["loss_pct"] == 0


def test_twin_predicts_outage_in_static_mode():
    res = run_prediction(two_path_cfg(mode="static", r2r5_up=False))
    assert res["pairs"]["c>s"]["loss_pct"] == 100
    assert res["pairs"]["c>s"]["rtt_p50"] is None


def test_twin_predicts_latency_driven_reroute():
    res = run_prediction(two_path_cfg(delay_r2r5=60))
    p = res["pairs"]["c>s"]
    assert p["path"] == ["c", "r1", "r3", "r5", "s"] and p["reason"] == "better"
    assert p["rtt_p50"] == pytest.approx(2 * (0.5 + 8 + 8 + 0.5), abs=1.0)


def test_overhead_fit_recovers_known_parameters():
    e_true, h_true = 0.15, 0.05
    obs = [(n, e_true + 2 * n * h_true) for n in (1, 1, 2, 2, 6, 6, 7)]
    e, h, rms = fit_overheads(obs)
    assert e == pytest.approx(e_true, abs=1e-9) and h == pytest.approx(h_true, abs=1e-9) and rms < 1e-9
