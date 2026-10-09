"""Path score, feasibility, hysteresis and hold-down decisions."""

import math

import pytest

from netvista.routing.scoring import HopMetrics, Weights, best, decide, evaluate


def hop(lid, lat=5.0, loss=0.0, util=0.0, alive=True):
    return HopMetrics(lid, "u", "v", lat, loss, util, alive)


def test_score_formula():
    w = Weights(latency=1.0, loss=5.0, util=0.2)
    ev = evaluate(["a", "b", "c"], [hop("x", 4, 0.01, 0.30), hop("y", 6, 0.02, 0.50)], w)
    assert ev.latency_ms == pytest.approx(10)
    assert ev.loss_pct == pytest.approx(100 * (1 - 0.99 * 0.98))  # independent losses compound
    assert ev.util_pct == pytest.approx(50)  # bottleneck, not the sum
    assert ev.bottleneck == "y"
    assert ev.score == pytest.approx(10 + 5 * ev.loss_pct + 0.2 * 50)


def test_dead_link_makes_path_infeasible():
    ev = evaluate(["a", "b"], [hop("x"), hop("y", alive=False)], Weights())
    assert not ev.feasible and math.isinf(ev.score)
    assert ev.to_dict()["score"] is None and ev.to_dict()["dead_links"] == ["y"]


def test_best_skips_infeasible_and_breaks_ties_by_length():
    w = Weights()
    a = evaluate(["s", "x", "t"], [hop("1", 5), hop("2", 5)], w)
    b = evaluate(["s", "t"], [hop("3", 10)], w)
    dead = evaluate(["s", "y", "t"], [hop("4", 1, alive=False)], w)
    assert best([a, b, dead]).path == ["s", "t"]
    assert best([dead]) is None


def test_hysteresis_prevents_small_improvements():
    w = Weights()
    cur = evaluate(["p1"], [hop("a", 10)], w)
    slightly = evaluate(["p2"], [hop("b", 9)], w)  # 10 % better < 15 % margin
    chosen, reason = decide(cur, [cur, slightly], hysteresis=0.15, hold_ok=True)
    assert chosen.path == ["p1"] and reason == "keep"
    much = evaluate(["p3"], [hop("c", 5)], w)
    chosen, reason = decide(cur, [cur, much], hysteresis=0.15, hold_ok=True)
    assert chosen.path == ["p3"] and reason == "better"


def test_hold_down_blocks_voluntary_switch_but_not_failover():
    w = Weights()
    cur = evaluate(["p1"], [hop("a", 50)], w)
    good = evaluate(["p2"], [hop("b", 5)], w)
    assert decide(cur, [cur, good], 0.15, hold_ok=False)[1] == "keep"
    broken = evaluate(["p1"], [hop("a", alive=False)], w)
    chosen, reason = decide(broken, [broken, good], 0.15, hold_ok=False)
    assert reason == "failover" and chosen.path == ["p2"]


def test_no_path_keeps_current():
    w = Weights()
    broken = evaluate(["p1"], [hop("a", alive=False)], w)
    other = evaluate(["p2"], [hop("b", alive=False)], w)
    chosen, reason = decide(broken, [broken, other], 0.15, True)
    assert reason == "no-path" and chosen.path == ["p1"]


def test_weights_change_the_decision():
    fast_lossy = [hop("a", 5, loss=0.03)]
    slow_clean = [hop("b", 15)]
    latency_first = Weights(latency=1, loss=0.1, util=0)
    loss_first = Weights(latency=1, loss=10, util=0)
    assert best([evaluate(["fast"], fast_lossy, latency_first), evaluate(["slow"], slow_clean, latency_first)]).path == ["fast"]
    assert best([evaluate(["fast"], fast_lossy, loss_first), evaluate(["slow"], slow_clean, loss_first)]).path == ["slow"]
