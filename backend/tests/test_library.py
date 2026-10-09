"""Topology library and design-time checks (the designer page)."""

import json

import pytest

from conftest import TOPO_DIR
from netvista.topology.library import clean, design_report, list_topologies, resolve, slug


def raw(name):
    return json.loads((TOPO_DIR / name).read_text())


@pytest.mark.parametrize("name", ["default.json", "small.json", "abilene.json", "metro-ring.json"])
def test_every_preset_boots_and_reports(name):
    r = design_report(raw(name))
    assert r["ok"], r["errors"]
    assert r["stats"]["flows"] == len(r["flows"]) > 0
    assert all(f["candidates"] >= 2 for f in r["flows"])  # every preset offers an alternative path


def test_default_topology_facts():
    r = design_report(raw("default.json"))
    assert {s["scenario"] for s in r["spofs"]} == {"node:r1", "node:r5"}
    assert all(s["edge"] for s in r["spofs"])  # only site gateways: no core single point of failure
    f = next(x for x in r["flows"] if x["pair"] == "c1>srv1")
    assert f["shortest"] == ["r1", "r2", "r5"] and f["design_rtt_ms"] == 24.0 and f["disjoint_paths"] == 2


def test_errors_and_warnings_are_explained():
    d = raw("small.json")
    d["links"] = [l for l in d["links"] if {l["a"], l["b"]} != {"r1", "r2"}]  # remove the detour: one path left
    r = design_report(d)
    assert r["ok"] and any("single path" in w for w in r["warnings"])
    assert any("core single point of failure" in w for w in r["warnings"])
    bad = raw("small.json")
    bad["links"].append({"a": "c1", "b": "r2", "bw_mbps": 10, "delay_ms": 1})  # a host with two cables
    r = design_report(bad)
    assert not r["ok"] and "exactly one link" in r["errors"][0]


def test_traffic_that_cannot_fit_is_flagged():
    d = raw("small.json")
    d["traffic"] = [{"src": "c1", "dst": "srv1", "rate_mbps": 70}]
    assert any("exceeds its shortest path" in w for w in design_report(d)["warnings"])


def test_library_paths_are_confined():
    assert resolve("default.json").name == "default.json"
    for bad in ("../backend/pyproject.toml", "../../etc/passwd.json", "nope.json"):
        with pytest.raises(ValueError):
            resolve(bad)
    assert any(t["file"] == "abilene.json" for t in list_topologies(None))


def test_clean_and_slug():
    d = raw("small.json")
    d["nodes"][0]["selected"] = True  # editor state never reaches the file
    c = clean(d)
    assert "selected" not in c["nodes"][0] and c["traffic"] == d["traffic"]
    assert slug("My Campus / v2!") == "my-campus-v2"
