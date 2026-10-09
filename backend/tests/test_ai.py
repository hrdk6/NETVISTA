"""AIOps layer: anomaly detector, probe-path root-cause analysis, copilot loop and grounding check."""

import random
from types import SimpleNamespace

import pytest

from netvista.ai.anomaly import WARMUP, AnomalyDetector
from netvista.ai.copilot import context_window, grounding_check
from netvista.ai.copilot.agent import Copilot
from netvista.ai.copilot.grounding import extract_claims
from netvista.ai.copilot.providers import (FallbackProvider, LLMResponse, OpenAICompatProvider, Provider, ProviderError, ToolCall,
                                           ToolSchema, to_anthropic, to_ollama, to_openai)
from netvista.ai.copilot.tools import Tool
from netvista.ai.rca import FlowInfo, LinkLoad, SignalState, StreamObs, diagnose
from netvista.ai.signals import SignalSpec, build_specs, gateway_path, path_elements
from netvista.config import Settings
from netvista.topology import build_address_plan, load_topology
from conftest import TOPO_DIR


@pytest.fixture(scope="module")
def topo():
    return load_topology(TOPO_DIR / "default.json")


@pytest.fixture(scope="module")
def plan(topo):
    return build_address_plan(topo)


RTT = SignalSpec("link:x:rtt", "link", "x", "rtt", "ms", "L:x", "up", 0.3, 0.05)
UTIL = SignalSpec("link:x:util", "link", "x", "util", "ratio", None, "up", 0.05, 0.1, min_level=0.7)


def feed(det, sid, values, t0=0.0):
    for i, v in enumerate(values):
        det.update(t0 + i, {sid: v})
    return t0 + len(values)


# ---------------------------------------------------------------------------- detector
def test_detector_learns_raises_and_clears():
    rnd = random.Random(1)
    det = AnomalyDetector([RTT])
    t = feed(det, RTT.id, [10 + rnd.gauss(0, 0.03) for _ in range(40)])
    s = det.state_of(RTT.id)
    assert s.state == "normal" and abs(s.mean - 10) < 0.05
    t = feed(det, RTT.id, [16.0], t)
    assert not det.active()  # z ~ 12, but one sample is never enough
    t = feed(det, RTT.id, [16.0], t)
    assert len(det.active()) == 1  # strong deviation: raised on the 2nd sample
    a = det.active()[0]
    assert a.t_start == 40 and a.t_raised == 41 and abs(a.baseline_mean - 10) < 0.05 and a.value == 16.0
    mean_during = det.state_of(RTT.id).mean
    t = feed(det, RTT.id, [16.0] * 10, t)
    assert det.state_of(RTT.id).mean == mean_during  # a fault never becomes "normal"
    t = feed(det, RTT.id, [10.0] * 5, t)
    assert not det.active() and len(det.recent()) == 1 and det.recent()[0].t_end is not None


def test_detector_ignores_noise_and_single_spikes():
    rnd = random.Random(2)
    det = AnomalyDetector([RTT])
    vals = [10 + rnd.gauss(0, 0.2) for _ in range(200)]
    vals[80] = 25.0  # one probe-window outlier
    vals[150] = 13.0
    feed(det, RTT.id, vals)
    assert not det.active() and not det.recent()


def test_detector_floor_keeps_flat_links_quiet():
    det = AnomalyDetector([RTT])
    t = feed(det, RTT.id, [10.0] * 30)
    # a perfectly flat link has std 0; the 5 % floor means +0.4 ms is still normal
    feed(det, RTT.id, [10.4] * 10, t)
    assert not det.active()


def test_util_needs_unusual_and_near_capacity():
    det = AnomalyDetector([UTIL])
    t = feed(det, UTIL.id, [0.2] * 30)
    t = feed(det, UTIL.id, [0.55] * 10, t)  # operator started traffic: a change, not a fault
    assert not det.active()
    det2 = AnomalyDetector([UTIL])
    t = feed(det2, UTIL.id, [0.2] * 30)
    feed(det2, UTIL.id, [0.97] * 4, t)
    assert len(det2.active()) == 1


def test_missing_values_and_relearn():
    det = AnomalyDetector([RTT])
    t = feed(det, RTT.id, [None] * 10 + [10.0] * (WARMUP - 1))
    assert det.state_of(RTT.id).state == "learning"
    t = feed(det, RTT.id, [10.0], t)
    assert det.state_of(RTT.id).state == "normal"
    feed(det, RTT.id, [30.0] * 3, t)
    assert det.active()
    det.relearn()
    assert not det.active() and det.state_of(RTT.id).state == "learning" and len(det.recent()) == 1


def test_specs_cover_every_probe_stream(topo):
    specs = build_specs(topo)
    streams = {s.stream for s in specs if s.stream}
    core = [l for l in topo.links.values() if topo.nodes[l.a].type == "router" and topo.nodes[l.b].type == "router"]
    assert {f"L:{l.id}" for l in core} <= streams
    assert {f"G:{h}" for h in topo.hosts} <= streams
    assert {f"F:{c}>{s}" for c, s in topo.flow_pairs()} <= streams
    assert sum(1 for s in specs if s.metric == "util") == len(topo.links)


# ---------------------------------------------------------------------------- root cause
STATIC = {
    "c1>srv1": ["c1", "sw1", "r1", "r2", "r5", "sw2", "srv1"],
    "c1>srv2": ["c1", "sw1", "r1", "r2", "r5", "sw2", "srv2"],
    "c2>srv1": ["c2", "sw1", "r1", "r2", "r5", "sw2", "srv1"],
    "c2>srv2": ["c2", "sw1", "r1", "r2", "r5", "sw2", "srv2"],
}
NORMAL = SignalState("normal", 10.0, 10.0, 0.1)


def world(topo, plan, dead_links=(), dead_nodes=(), latency_links=(), loss_links=(), util=None, paths=None, baseline=None):
    """Synthesise what the probes would measure for a given set of broken elements."""
    paths = paths or STATIC
    baseline = baseline or paths
    broken = {f"link:{l}" for l in dead_links} | {f"node:{n}" for n in dead_nodes}
    slow = {f"link:{l}" for l in latency_links}
    lossy = {f"link:{l}" for l in loss_links}

    def obs(sid, label, elems, flow=None, rerouted=False):
        dead = bool(elems & broken)
        rtt = SignalState("anomalous", 22.0, 10.0, 24.0, 100.0) if elems & slow else NORMAL
        loss = SignalState("anomalous", 9.0, 0.0, 20.0, 100.0) if elems & lossy else SignalState("normal", 0.0, 0.0, 0.0)
        return StreamObs(sid, label, elems, alive=not dead, silent_s=3.0 if dead else 0.1, rtt=rtt, loss=loss, flow=flow, rerouted=rerouted)

    streams = []
    for l in topo.links.values():
        if topo.nodes[l.a].type == "router" and topo.nodes[l.b].type == "router":
            streams.append(obs(f"L:{l.id}", f"{l.id} probes", frozenset({f"link:{l.id}", f"node:{l.a}", f"node:{l.b}"})))
    for h in topo.hosts:
        streams.append(obs(f"G:{h}", f"{h} gateway probes", path_elements(topo, gateway_path(topo, h, plan.host_gateway[h]))))
    flows = {}
    for pair, p in paths.items():
        fi = FlowInfo(pair, p, path_elements(topo, p), baseline[pair], path_elements(topo, baseline[pair]), 6.0)
        flows[pair] = fi
        streams.append(obs(f"F:{pair}", f"{pair} probes", fi.elements, flow=pair, rerouted=p != baseline[pair]))
    loads = {lid: LinkLoad((util or {}).get(lid, 0.2), topo.links[lid].bw_mbps, 6.0) for lid in topo.links}
    return streams, loads, flows


def run(topo, plan, bursts=(), **kw):
    streams, loads, flows = world(topo, plan, **kw)
    return diagnose(streams, loads, flows, list(bursts), {n: topo.nodes[n].type for n in topo.nodes}, 1000.0)


def test_rca_normal(topo, plan):
    d = run(topo, plan)
    assert d["status"] == "normal" and d["causes"] == []


def test_rca_link_down_beats_its_routers(topo, plan):
    d = run(topo, plan, dead_links=["r2-r5"])
    assert [c["id"] for c in d["causes"]] == ["link_down:r2-r5"]
    c = d["causes"][0]
    assert c["confidence"] == "high" and c["explains"] == 5  # its own probes + the 4 flows over it
    assert set(c["affected_flows"]) == set(STATIC)


def test_rca_router_down_is_one_cause_not_three_links(topo, plan):
    d = run(topo, plan, dead_nodes=["r2"])
    assert [c["id"] for c in d["causes"]] == ["node_down:r2"]
    assert d["causes"][0]["title"] == "Router r2 is down"


def test_rca_edge_router_down(topo, plan):
    d = run(topo, plan, dead_nodes=["r1"])
    assert [c["id"] for c in d["causes"]] == ["node_down:r1"]


def test_rca_two_independent_link_failures(topo, plan):
    d = run(topo, plan, dead_links=["r2-r4", "r3-r5"])
    assert sorted(c["id"] for c in d["causes"]) == ["link_down:r2-r4", "link_down:r3-r5"]


def test_rca_switch_down_is_ambiguous_with_its_uplink(topo, plan):
    d = run(topo, plan, dead_nodes=["sw1"])
    c = d["causes"][0]
    assert {c["element"], *c["alternatives"]} >= {"sw1", "sw1-r1"} and c["confidence"] == "medium"


def test_rca_latency_and_loss_are_localised(topo, plan):
    d = run(topo, plan, latency_links=["r2-r5"])
    assert [(c["type"], c["element"]) for c in d["causes"]] == [("latency", "r2-r5")]
    d = run(topo, plan, loss_links=["r1-r2"])
    assert [(c["type"], c["element"]) for c in d["causes"]] == [("packet_loss", "r1-r2")]


def test_rca_congestion_and_surge(topo, plan):
    d = run(topo, plan, util={"r2-r5": 0.98}, latency_links=["r2-r5"])
    assert d["causes"][0]["type"] == "congestion" and d["causes"][0]["element"] == "r2-r5"
    d = run(topo, plan, util={"r2-r5": 0.98}, latency_links=["r2-r5"], bursts=[{"pair": "c2>srv1", "rate_mbps": 28}])
    c = d["causes"][0]
    assert c["type"] == "traffic_surge" and c["element"] == "r2-r5" and c["surge"]["pair"] == "c2>srv1"


def test_rca_reroute_side_effect_is_not_a_fault(topo, plan):
    moved = dict(STATIC, **{"c1>srv1": ["c1", "sw1", "r1", "r3", "r5", "sw2", "srv1"]})
    streams, loads, flows = world(topo, plan, paths=moved, baseline=STATIC)
    for s in streams:  # the rerouted flow's RTT is up (longer path); nothing is broken
        if s.sid == "F:c1>srv1":
            s.rtt = SignalState("anomalous", 34.0, 24.0, 8.0, 100.0)
    d = diagnose(streams, loads, flows, [], {n: topo.nodes[n].type for n in topo.nodes}, 1000.0)
    assert d["causes"] == [] and len(d["consequences"]) == 1 and "rerouted flow c1 → srv1" in d["consequences"][0].lower()


# ---------------------------------------------------------------------------- grounding
def test_grounding_extracts_measurements_only():
    text = "r2-r5 RTT is 16.2 ms, load 42%, c1 → srv1 at 6 Mbit/s; 2 flows; host 10.0.1.11 at 12:03:04; z 12; 3 servers"
    assert [c["text"] for c in extract_claims(text)] == ["16.2 ms", "42%", "6 Mbit/s"]


def test_grounding_unit_scaling_and_misses():
    src = [{"rtt_ms": 16.21, "util": 0.4213, "bps": 6012345.0, "detection_ms": 1312.4}]
    g = grounding_check("RTT 16.2 ms, load 42%, 6.01 Mbit/s, detected after 1.3 s, peak 99.9 ms", src)
    assert g["checked"] == 5 and g["grounded"] == 4 and g["ungrounded"] == ["99.9 ms"]


def test_grounding_reads_json_strings_and_labels():
    g = grounding_check("You added +40 ms; the link now measures 90.4 ms.", ['{"rtt_ms":90.38}', "Latency +40 ms on r2-r5"])
    assert g["ungrounded"] == [] and g["grounded"] == 2


# ---------------------------------------------------------------------------- provider formats
def test_message_conversion():
    msgs = [
        {"role": "user", "content": "why?"},
        {"role": "assistant", "content": "Checking.", "tool_calls": [{"id": "a", "name": "x", "input": {}}, {"id": "b", "name": "y", "input": {"k": 1}}]},
        {"role": "tool", "tool_call_id": "a", "name": "x", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "name": "y", "content": "2", "is_error": True},
        {"role": "assistant", "content": "Because.", "tool_calls": []},
    ]
    an = to_anthropic(msgs)
    assert [m["role"] for m in an] == ["user", "assistant", "user", "assistant"]
    assert [b["type"] for b in an[1]["content"]] == ["text", "tool_use", "tool_use"]
    assert [b["tool_use_id"] for b in an[2]["content"]] == ["a", "b"] and an[2]["content"][1]["is_error"]
    ol = to_ollama("SYS", msgs)
    assert ol[0] == {"role": "system", "content": "SYS"}
    assert ol[2]["tool_calls"][1]["function"] == {"name": "y", "arguments": {"k": 1}}
    assert ol[3] == {"role": "tool", "content": "1", "tool_name": "x"}


def test_openai_conversion_carries_gemini_thought_signatures():
    sig = {"google": {"thought_signature": "SIG-A"}}
    msgs = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "o", "name": "x", "input": {}}]},  # an old turn, no signature
        {"role": "tool", "tool_call_id": "o", "name": "x", "content": "0"},
        {"role": "user", "content": "why?"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "x", "input": {"k": 1}, "extra": sig}]},
        {"role": "tool", "tool_call_id": "a", "name": "x", "content": "1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b", "name": "y", "input": {}}, {"id": "c", "name": "y", "input": {}}]},
    ]
    gem = to_openai("SYS", msgs, gemini=True)
    assert gem[0] == {"role": "system", "content": "SYS"}
    assert "extra_content" not in gem[2]["tool_calls"][0]  # earlier turns are not validated
    assert gem[5]["tool_calls"][0]["extra_content"] == sig and gem[5]["tool_calls"][0]["function"]["arguments"] == '{"k": 1}'
    # a step the backup made in this turn: the first call gets Google's skip value, the parallel one nothing
    assert gem[7]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "skip_thought_signature_validator"
    assert "extra_content" not in gem[7]["tool_calls"][1]
    assert gem[6] == {"role": "tool", "tool_call_id": "a", "content": "1"}
    groq = to_openai("SYS", msgs, gemini=False)
    assert all("extra_content" not in c for m in groq for c in m.get("tool_calls", []))


def _sse_provider(chunks, name="gemini"):
    p = OpenAICompatProvider(name, name.title(), "https://example.invalid", "k", "m")
    p.sent = []
    p._stream = lambda payload: (p.sent.append(payload), iter(chunks))[1]
    return p


def test_openai_stream_assembles_text_and_fragmented_tool_calls():
    chunks = [
        {"choices": [{"delta": {"content": "Look"}}]},
        {"choices": [{"delta": {"content": "ing."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "get_link_details", "arguments": '{"link'},
                                                "extra_content": {"google": {"thought_signature": "S"}}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '_id": "r2-r5"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 900, "completion_tokens": 40}},
    ]
    p = _sse_provider(chunks)
    seen = []
    tools = [ToolSchema("get_link_details", "d", {"type": "object", "properties": {}})]
    r = p.chat("SYS", [{"role": "user", "content": "q"}], tools, seen.append)
    assert r.text == "Looking." and "".join(seen) == "Looking."
    assert r.tool_calls[0].name == "get_link_details" and r.tool_calls[0].input == {"link_id": "r2-r5"}
    assert r.tool_calls[0].extra == {"google": {"thought_signature": "S"}} and r.stop_reason == "tool_use"
    assert r.usage == {"input_tokens": 900, "output_tokens": 40}
    assert p.sent[0]["tools"][0]["function"]["name"] == "get_link_details" and p.sent[0]["stream"] is True


def test_fallback_uses_the_backup_while_the_primary_rests():
    class Flaky(Provider):
        def __init__(self, name, fail):
            super().__init__(name)
            self.name, self.fail, self.calls = name, fail, 0

        def chat(self, system, messages, tools, on_text, max_tokens=2048, should_stop=lambda: False):
            self.calls += 1
            if self.fail:
                raise ProviderError("Gemini rate limit or free-tier quota reached: quota", transient=True, retry_after=30)
            return LLMResponse(f"from {self.name}")

    gem, groq = Flaky("gemini", True), Flaky("groq", False)
    fb = FallbackProvider(gem, groq)
    notes = []
    assert fb.chat("S", [], [], notes.append).text == "from groq"
    assert "unavailable" in notes[0] and gem.calls == 1
    fb.chat("S", [], [], notes.append)
    assert gem.calls == 1 and groq.calls == 2  # resting: not even tried
    fb.resting_until = 0
    gem.fail = False
    assert fb.chat("S", [], [], notes.append).text == "from gemini"
    # a non-transient error (a bug in the request) is not hidden behind the backup
    gem.chat = lambda *a, **k: (_ for _ in ()).throw(ProviderError("Gemini error 400: bad schema"))
    with pytest.raises(ProviderError, match="bad schema"):
        fb.chat("S", [], [], notes.append)


def test_http_errors_say_whether_the_backup_should_answer():
    p = OpenAICompatProvider("gemini", "Gemini", "https://example.invalid", "k", "m")
    bad_key = p._http_error(400, '[{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}}]', None)
    assert "rejected the API key" in str(bad_key) and bad_key.transient  # Gemini answers 400, not 401
    quota = p._http_error(429, '{"error": {"message": "Resource exhausted"}}', 12.0)
    assert quota.transient and quota.retry_after == 12.0 and "~12 s" in str(quota)
    assert p._http_error(503, "overloaded", None).transient
    schema = p._http_error(400, '{"error": {"message": "Invalid JSON payload: tools[0]"}}', None)
    assert not schema.transient and "tools[0]" in str(schema)  # our bug: shown, not hidden behind the backup


def test_cloud_selection_and_bad_keys(monkeypatch):
    from netvista.ai.copilot import providers as pv

    for k in ("ANTHROPIC_API_KEY", "NETVISTA_AI_PROVIDER", "NETVISTA_AI_BACKUP", "NETVISTA_GEMINI_MODEL", "NETVISTA_GROQ_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GROQ_API_KEY", "q")
    verdict = {"gemini": "", "groq": ""}
    monkeypatch.setattr(pv.OpenAICompatProvider, "check_key", lambda self, timeout=6.0: verdict[self.name])
    p, st = pv.select_provider()
    assert isinstance(p, pv.FallbackProvider) and p.primary.name == "gemini" and p.backup.name == "groq"
    assert st.available and st.provider == "gemini" and "Groq" in st.backup and not p.compact
    assert p.backup.compact and p.primary.model == "gemini-3.8-flash" and p.backup.model == "openai/gpt-oss-120b"
    verdict["gemini"] = "Gemini rejected the API key (HTTP 400)"
    p, st = pv.select_provider()
    assert p.name == "groq" and "using Groq" in st.reason  # bad primary key: the backup runs alone
    verdict["gemini"] = ""
    monkeypatch.setenv("NETVISTA_AI_PROVIDER", "groq")
    monkeypatch.setenv("NETVISTA_AI_BACKUP", "none")
    p, st = pv.select_provider()
    assert isinstance(p, pv.OpenAICompatProvider) and p.name == "groq" and st.backup is None
    monkeypatch.setenv("NETVISTA_AI_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY")
    p, st = pv.select_provider()
    assert p is None and "GEMINI_API_KEY is not set" in st.reason


def test_context_window_stubs_old_tool_results():
    msgs = []
    for i in range(3):
        msgs += [{"role": "user", "content": f"q{i}"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}", "name": "x", "input": {}}]},
                 {"role": "tool", "tool_call_id": f"t{i}", "name": "x", "content": f"big{i}"},
                 {"role": "assistant", "content": f"a{i}", "tool_calls": []}]
    out = context_window(msgs, keep_turns=1)
    tools = [m["content"] for m in out if m["role"] == "tool"]
    assert tools[-1] == "big2" and all("omitted" in t for t in tools[:-1])
    assert len(out) == len(msgs)


# ---------------------------------------------------------------------------- agent loop
class ScriptedProvider(Provider):
    name = "scripted"

    def __init__(self, script):
        super().__init__("test-model")
        self.script = list(script)
        self.seen = []

    def chat(self, system, messages, tools, on_text, max_tokens=2048, should_stop=lambda: False):
        self.seen.append([m["role"] for m in messages])
        resp = self.script.pop(0)
        for part in resp.text.split(" "):
            on_text(part + " ")
        return resp


@pytest.fixture
def copilot(topo, plan, monkeypatch):
    monkeypatch.setenv("NETVISTA_AI_PROVIDER", "off")
    injected = []
    rt = SimpleNamespace(
        topo=topo, plan=plan, s=Settings(), events=SimpleNamespace(emit=lambda *a, **k: None),
        chaos=SimpleNamespace(inject=lambda kind, target, params, source: injected.append((kind, target, params, source))
                              or SimpleNamespace(id="i1", label=f"{kind} {target}")),
    )
    cp = Copilot(rt, ai=SimpleNamespace())
    cp.toolbox.tools = {
        "get_link_details": Tool("get_link_details", "read", "d", {"type": "object", "properties": {}}, lambda a: {"link": a["link_id"], "rtt_ms": 16.21}),
        "propose_change": Tool("propose_change", "propose", "d", {"type": "object", "properties": {}},
                               lambda a: cp.proposals.add(cp.toolbox.conversation_id, "chaos_inject",
                                                          {"kind": "link_latency", "target": "r2-r5", "params": {"add_ms": 40.0}},
                                                          "Latency +40 ms on r2-r5", "test")),
    }
    cp._injected = injected
    return cp


def test_agent_tool_round_then_grounded_answer(copilot):
    copilot.provider = ScriptedProvider([
        LLMResponse("", [ToolCall("c1", "get_link_details", {"link_id": "r2-r5"})], "tool_use"),
        LLMResponse("r2-r5 measures 16.2 ms, which is 99.0 ms faster than nothing.", [], "end_turn"),
    ])
    evs = list(copilot.chat(None, "How is r2-r5?", "Live page"))
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "start" and kinds[-1] == "done" and "tool_call" in kinds and "tool_result" in kinds
    res = next(e for e in evs if e["type"] == "tool_result")
    assert res["ok"] and '"rtt_ms":16.21' in res["result"]
    g = next(e for e in evs if e["type"] == "grounding")
    assert g["grounded"] == 1 and g["ungrounded"] == ["99.0 ms"]
    # the second model call saw the tool result, and the dashboard context reached the model
    assert copilot.provider.seen[1] == ["user", "assistant", "tool"]
    conv = copilot.convs[evs[0]["conversation_id"]]
    assert conv.messages[0]["content"].startswith("[Dashboard context: Live page]")


def test_agent_proposals_need_the_user(copilot):
    copilot.provider = ScriptedProvider([
        LLMResponse("I can add it.", [ToolCall("c1", "propose_change", {})], "tool_use"),
        LLMResponse("Proposed; apply it from the card.", [], "end_turn"),
    ])
    evs = list(copilot.chat(None, "add 40 ms to r2-r5", None))
    prop = next(e for e in evs if e["type"] == "proposal")
    assert prop["status"] == "pending" and copilot._injected == []  # nothing touched the network
    applied = copilot.apply_proposal(prop["id"])
    assert applied["status"] == "applied" and copilot._injected == [("link_latency", "r2-r5", {"add_ms": 40.0}, "ai-copilot")]
    with pytest.raises(ValueError):
        copilot.apply_proposal(prop["id"])  # one approval, one action
    conv = copilot.convs[evs[0]["conversation_id"]]
    assert conv.notes and "applied proposal" in conv.notes[0]


def test_agent_without_provider_says_how_to_configure(copilot):
    copilot.provider = None
    evs = list(copilot.chat(None, "hi", None))
    assert evs == [{"type": "error", "message": evs[0]["message"]}] and "language model" in evs[0]["message"]


def test_rca_router_down_after_fast_reroute(topo, plan):
    """The controller reroutes in ~1.3 s, before the loss window empties: the flows are alive on
    the new path, their recent loss came from the old one. Must stay one high-confidence cause."""
    moved = {p: path[:3] + ["r3", "r5"] + path[5:] for p, path in STATIC.items()}
    streams, loads, flows = world(topo, plan, dead_nodes=["r2"], paths=moved, baseline=STATIC)
    for s in streams:
        if s.sid.startswith("F:"):
            assert s.alive and s.rerouted
            s.loss = SignalState("anomalous", 13.0, 0.0, 30.0, 100.0)
            s.window_elements = flows[s.flow].elements | flows[s.flow].baseline_elements
    d = diagnose(streams, loads, flows, [], {n: topo.nodes[n].type for n in topo.nodes}, 1000.0)
    assert [c["id"] for c in d["causes"]] == ["node_down:r2"]
    c = d["causes"][0]
    assert c["confidence"] == "high" and not c["contradicted_by"] and len(c["rerouted_flows"]) == 4


def test_rca_low_random_loss_is_not_cleared_by_lucky_streams(topo, plan):
    """1 % loss per direction: some flows over the link see no loss in their 10 s window by
    chance. That must not clear the link, and must not make r2/r5 equally likely."""
    streams, loads, flows = world(topo, plan, loss_links=["r2-r5"])
    lucky = 0
    for s in streams:
        if s.loss.state == "anomalous":
            s.loss = SignalState("anomalous", 2.0, 0.0, 5.0, 100.0)
            if s.sid.startswith("F:") and lucky < 2:
                s.loss = SignalState("normal", 0.0, 0.0, 0.0)  # zero losses in this window
                lucky += 1
    d = diagnose(streams, loads, flows, [], {n: topo.nodes[n].type for n in topo.nodes}, 1000.0)
    c = d["causes"][0]
    assert (c["type"], c["element"]) == ("packet_loss", "r2-r5")
    assert c["confidence"] == "high" and c["alternatives"] == [] and not c["contradicted_by"]


def test_rca_full_queue_silence_is_congestion_not_a_cut(topo, plan):
    """Bufferbloat: a 1000-packet queue at 100 % load drops probes for > 1.2 s at a time.
    The counters show the link sending at capacity, so it is congested, not down."""
    d = run(topo, plan, dead_links=["r2-r5"], util={"r2-r5": 1.0}, bursts=[{"pair": "c2>srv1", "rate_mbps": 28.5}])
    c = d["causes"][0]
    assert (c["type"], c["element"]) == ("traffic_surge", "r2-r5")
    d = run(topo, plan, dead_links=["r2-r5"], util={"r2-r5": 0.0})
    assert d["causes"][0]["type"] == "link_down"  # an idle silent link is a cut


def test_rca_congested_link_keeps_confidence_when_some_probes_survive(topo, plan):
    streams, loads, flows = world(topo, plan, util={"r2-r5": 1.0}, latency_links=["r2-r5"])
    for s in streams:  # the link's own probes were tail-dropped for > 1.2 s; the flows' got through
        if s.sid == "L:r2-r5":
            s.alive, s.silent_s = False, 1.4
    d = diagnose(streams, loads, flows, [{"pair": "c2>srv1", "rate_mbps": 28.5}], {n: topo.nodes[n].type for n in topo.nodes}, 1000.0)
    c = d["causes"][0]
    assert (c["type"], c["element"], c["confidence"]) == ("traffic_surge", "r2-r5", "high")
