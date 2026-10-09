"""FastAPI application: REST for actions, one WebSocket for the live stream, static UI."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..chaos import ChaosError
from ..config import Settings
from ..journey import NoPathError, packet_journey
from ..runtime import Runtime, sanitize
from ..topology import topology_from_dict
from ..topology.library import design_report, list_topologies
from ..topology.library import read as read_topology
from ..topology.library import resolve as resolve_topology
from ..topology.library import save as save_topology

log = logging.getLogger(__name__)


def _cleanup_mininet() -> None:
    """What scripts/run.sh does before a start: no agent, iperf3 or Mininet leftovers."""
    import subprocess

    for cmd in (["pkill", "-f", "netvista-agent"], ["pkill", "-x", "iperf3"], ["mn", "-c"]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=90)
        except Exception:
            pass


# ---------------------------------------------------------------------------- schemas
class FlowReq(BaseModel):
    src: str
    dst: str
    rate_mbps: float = Field(gt=0, le=500)


class TrafficStartReq(BaseModel):
    flows: list[FlowReq] | None = None  # None = the topology's default traffic profile


class TrafficStopReq(BaseModel):
    id: str | None = None  # None = stop all


class ChangeReq(BaseModel):
    kind: str
    target: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ModeReq(BaseModel):
    mode: str


class WeightsReq(BaseModel):
    latency: float = Field(ge=0, le=1000)
    loss: float = Field(ge=0, le=1000)
    util: float = Field(ge=0, le=1000)


class TraceReq(BaseModel):
    src: str
    dst: str


class PredictReq(BaseModel):
    changes: list[ChangeReq] = Field(default_factory=list)
    duration_s: float = Field(default=8.0, ge=2, le=60)
    routing_mode: str | None = None
    weights: WeightsReq | None = None
    flow_rates: dict[str, float] | None = None  # pair -> Mbit/s override (what-if demand)
    include_baseline: bool = True
    seed: int = 1


class ValidationReq(BaseModel):
    changes: list[ChangeReq]
    label: str | None = None
    settle_s: float = Field(default=6.0, ge=1, le=60)
    measure_s: float = Field(default=15.0, ge=3, le=120)
    duration_s: float = Field(default=10.0, ge=2, le=60)


class NameReq(BaseModel):
    name: str


class ChatReq(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    context: str | None = Field(default=None, max_length=600)


class IntentReq(BaseModel):
    kind: str
    flows: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    protect: str = "none"
    priority: str = "normal"
    enabled: bool = True
    note: str = ""


class IntentPatch(BaseModel):
    kind: str | None = None
    flows: list[str] | None = None
    links: list[str] | None = None
    params: dict[str, Any] | None = None
    protect: str | None = None
    priority: str | None = None
    enabled: bool | None = None
    note: str | None = None


class PresetReq(BaseModel):
    name: str
    replace: bool = True


class ResilienceReq(BaseModel):
    double: bool = False


class ToggleReq(BaseModel):
    on: bool


class DrillReq(BaseModel):
    scenario: str


class BenchmarkReq(BaseModel):
    strategies: list[str] | None = None
    scenarios: list[str] | None = None
    rates: dict[str, float] | None = None


class TopologyReq(BaseModel):
    topology: dict[str, Any]


class DeployReq(BaseModel):
    file: str | None = None
    topology: dict[str, Any] | None = None


# ---------------------------------------------------------------------------- app
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    state: dict[str, Any] = {"runtime": None, "clients": set()}

    async def broadcaster() -> None:
        last_seq = 0
        while True:
            await asyncio.sleep(settings.ws_interval_s)
            rt: Runtime | None = state["runtime"]
            clients: set[WebSocket] = state["clients"]
            if rt is None:
                continue
            evs = rt.events.since(last_seq)
            if evs:
                last_seq = evs[-1].seq
            if not clients or rt.status != "running":
                if clients:
                    msg = json.dumps({"type": "status", "status": rt.status, "error": rt.error, "events": [e.to_dict() for e in evs]}, default=str)
                    await _send_all(clients, msg)
                continue
            try:
                snap = await asyncio.to_thread(rt.snapshot)
            except Exception as e:  # never kill the broadcaster
                log.exception("snapshot failed: %s", e)
                continue
            msg = json.dumps({"type": "tick", "snapshot": snap, "events": sanitize([e.to_dict() for e in evs])}, default=str)
            await _send_all(clients, msg)

    async def _send_all(clients: set[WebSocket], msg: str) -> None:
        dead = []
        for ws in list(clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)

    async def boot(rt: Runtime) -> None:
        try:
            await asyncio.to_thread(rt.start)
        except Exception as e:
            log.exception("emulation failed to start")
            rt.status = "error"
            rt.error = f"{type(e).__name__}: {e}"
            rt.events.emit("system.error", f"Emulation failed to start: {rt.error}", severity="error")

    deploy_lock = asyncio.Lock()

    async def redeploy(path) -> None:
        """Stop the running emulation and boot another topology in the same process."""
        async with deploy_lock:
            old: Runtime | None = state["runtime"]
            if old is not None:
                old.events.emit("system.redeploy", f"Redeploying: stopping '{old.topo.name}' to boot {path.name}", severity="warn")
                old.status = "restarting"
                await asyncio.to_thread(old.stop)
                old.status = "restarting"
            # leftovers of the old network (namespaces, OVS bridges, agents) must not clash with the new one
            await asyncio.to_thread(_cleanup_mininet)
            settings.topology_path = path
            last_seq = (old.events.recent(1) or [None])[-1] if old is not None else None
            try:
                rt = Runtime(settings)
            except Exception as e:  # an invalid file: put the previous topology back
                log.exception("cannot load %s", path)
                if old is not None:
                    settings.topology_path = old.s.topology_path
                    rt = Runtime(settings)
                    rt.events.emit("system.error", f"Could not deploy {path.name}: {e}; booting the previous topology again", severity="error")
                else:
                    raise
            if last_seq is not None:
                # browsers keep only events newer than the last one they saw: keep counting
                rt.events._seq = itertools.count(last_seq.seq + 1)
            state["runtime"] = rt
            await boot(rt)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = Runtime(settings)
        state["runtime"] = rt
        task = asyncio.create_task(broadcaster())
        await boot(rt)
        try:
            yield
        finally:
            task.cancel()
            cur: Runtime | None = state["runtime"]
            if cur is not None:
                await asyncio.to_thread(cur.stop)

    app = FastAPI(title="NETVISTA", version="1.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def rt() -> Runtime:
        r: Runtime | None = state["runtime"]
        if r is None or r.status != "running":
            raise HTTPException(503, detail=(r.error if r and r.error else "emulation not running yet"))
        return r

    def ext(name: str):
        e = rt().extensions.get(name)
        if e is None:
            raise HTTPException(501, detail=f"{name} service not available")
        return e

    def guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ChaosError, ValueError, KeyError) as e:
            raise HTTPException(400, detail=str(e)) from e

    # ------------------------------------------------------------------ basics
    @app.get("/api/health")
    def health():
        r: Runtime | None = state["runtime"]
        return {"status": r.status if r else "starting", "error": r.error if r else None, "time": time.time()}

    # ------------------------------------------------------------------ topology library + designer
    @app.get("/api/topologies")
    def topologies():
        return list_topologies(settings.topology_path)

    @app.get("/api/topologies/file")
    def topology_file(file: str):
        return guard(read_topology, file)

    @app.post("/api/topologies/check")
    def topology_check(req: TopologyReq):
        return sanitize(design_report(req.topology))

    @app.post("/api/topologies/save")
    def topology_save(req: TopologyReq):
        return {"file": guard(save_topology, req.topology)}

    @app.post("/api/topologies/deploy")
    async def topology_deploy(req: DeployReq):
        if req.topology is not None:
            file = guard(save_topology, req.topology)
        elif req.file:
            file = req.file
        else:
            raise HTTPException(400, "give a library file or a topology")
        path = guard(resolve_topology, file)
        guard(topology_from_dict, json.loads(path.read_text(encoding="utf-8")))
        r: Runtime | None = state["runtime"]
        if deploy_lock.locked():
            raise HTTPException(409, "a deployment is already in progress")
        if r is not None and r.status == "running":
            a = r.extensions.get("assure")
            busy = a.busy_job() if a else None
            if busy:
                raise HTTPException(409, f"the {busy} job is using the network; wait for it to finish")
        asyncio.create_task(redeploy(path))
        return {"ok": True, "file": file}

    @app.get("/api/topology")
    def topology():
        r: Runtime | None = state["runtime"]
        if r is None:
            raise HTTPException(503, "starting")
        return {"topology": r.topo.to_dict(), "plan": r.plan.to_dict(), "flow_pairs": [f"{c}>{s}" for c, s in r.topo.flow_pairs()], "status": r.status, "error": r.error, "boot_id": r.boot_id, "file": r.s.topology_path.name}

    @app.get("/api/state")
    async def get_state():
        return await asyncio.to_thread(rt().snapshot)

    @app.get("/api/events")
    def events(limit: int = 200):
        r: Runtime | None = state["runtime"]
        return [e.to_dict() for e in (r.events.recent(limit) if r else [])]

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        r: Runtime | None = state["runtime"]
        hello = {"type": "hello", "status": r.status if r else "starting", "error": r.error if r else None,
                 "events": [e.to_dict() for e in r.events.recent(300)] if r else []}
        await ws.send_text(json.dumps(sanitize(hello), default=str))
        state["clients"].add(ws)
        try:
            while True:
                await ws.receive_text()  # client pings; content ignored
        except WebSocketDisconnect:
            pass
        finally:
            state["clients"].discard(ws)

    # ------------------------------------------------------------------ traffic
    @app.post("/api/traffic/start")
    async def traffic_start(req: TrafficStartReq):
        r = rt()
        if req.flows is None:
            return await asyncio.to_thread(r.start_default_traffic)
        out = []
        for f in req.flows:
            flow = await asyncio.to_thread(guard, r.traffic.start_flow, f.src, f.dst, f.rate_mbps)
            out.append(flow.to_dict())
        return out

    @app.post("/api/traffic/stop")
    async def traffic_stop(req: TrafficStopReq):
        r = rt()
        if req.id:
            await asyncio.to_thread(r.traffic.stop_flow, req.id)
        else:
            await asyncio.to_thread(r.traffic.stop_all, "background")
        return {"ok": True}

    # ------------------------------------------------------------------ chaos
    @app.post("/api/chaos/inject")
    async def chaos_inject(req: ChangeReq):
        inj = await asyncio.to_thread(guard, rt().chaos.inject, req.kind, req.target, req.params)
        return inj.to_dict()

    @app.post("/api/chaos/revert/{inj_id}")
    async def chaos_revert(inj_id: str):
        inj = await asyncio.to_thread(guard, rt().chaos.revert, inj_id)
        return inj.to_dict()

    @app.post("/api/chaos/revert_all")
    async def chaos_revert_all():
        n = await asyncio.to_thread(rt().chaos.revert_all)
        return {"reverted": n}

    # ------------------------------------------------------------------ routing
    @app.get("/api/routing")
    def routing():
        return sanitize(rt().controller.state())

    @app.post("/api/routing/mode")
    async def routing_mode(req: ModeReq):
        await asyncio.to_thread(guard, rt().controller.set_mode, req.mode)
        return {"mode": rt().controller.mode}

    @app.post("/api/routing/weights")
    def routing_weights(req: WeightsReq):
        guard(rt().controller.set_weights, req.latency, req.loss, req.util)
        return rt().controller.weights.to_dict()

    # ------------------------------------------------------------------ packet journey
    @app.get("/api/journey")
    async def journey(src: str, dst: str):
        r = rt()
        try:
            return sanitize(await asyncio.to_thread(packet_journey, r, src, dst))
        except NoPathError as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    _TR_RE = re.compile(r"^\s*(\d+)\s+(.*)$")

    @app.post("/api/journey/traceroute")
    async def traceroute(req: TraceReq):
        r = rt()
        if req.src not in r.plan.host_ip or req.dst not in r.plan.host_ip:
            raise HTTPException(400, "src and dst must be hosts")
        res = await asyncio.to_thread(
            r.net.node_run, req.src, ["traceroute", "-n", "-q", "3", "-w", "1", "-m", "12", r.plan.host_ip[req.dst]], 25
        )
        hops = []
        for line in res.stdout.splitlines()[1:]:
            m = _TR_RE.match(line)
            if not m:
                continue
            parts = m.group(2).split()
            ip = next((p for p in parts if re.match(r"^\d+\.\d+\.\d+\.\d+$", p)), None)
            rtts = [float(parts[i - 1]) for i, p in enumerate(parts) if p == "ms" and i > 0]
            hops.append({"ttl": int(m.group(1)), "ip": ip, "node": r.plan.owner_of_ip(ip) if ip else None, "rtts_ms": rtts})
        return {"raw": res.stdout + res.stderr, "hops": hops, "t": time.time()}

    # ------------------------------------------------------------------ metrics
    @app.get("/api/metrics/history")
    def metrics_history(seconds: int = 300):
        r = rt()
        cutoff = time.time() - seconds
        return [h for h in list(r.history) if h["t"] >= cutoff]

    # ------------------------------------------------------------------ simulator (phase 4)
    @app.get("/api/sim/calibration")
    def sim_calibration():
        return ext("simulator").calibration_dict()

    @app.post("/api/sim/calibrate")
    async def sim_calibrate():
        return await asyncio.to_thread(guard, ext("simulator").calibrate)

    @app.post("/api/sim/predict")
    async def sim_predict(req: PredictReq):
        sim = ext("simulator")
        return await asyncio.to_thread(
            guard, sim.predict,
            [c.model_dump() for c in req.changes], req.duration_s, req.routing_mode,
            req.weights.model_dump() if req.weights else None, req.flow_rates, req.include_baseline, req.seed,
        )

    @app.get("/api/sim/predictions")
    def sim_predictions():
        return ext("simulator").list_predictions()

    # ------------------------------------------------------------------ validation (phase 4)
    @app.post("/api/validation/run")
    def validation_run(req: ValidationReq):
        v = ext("validation")
        return guard(v.start_run, [c.model_dump() for c in req.changes], req.label, req.settle_s, req.measure_s, req.duration_s)

    @app.post("/api/validation/suite")
    def validation_suite():
        return guard(ext("validation").start_suite)

    @app.post("/api/validation/cancel")
    def validation_cancel():
        ext("validation").stop()
        return {"ok": True}

    @app.get("/api/validation/runs")
    def validation_runs():
        return ext("validation").list_runs()

    @app.get("/api/validation/status")
    def validation_status():
        return ext("validation").status()

    # ------------------------------------------------------------------ scenarios + demo (phase 5)
    @app.post("/api/scenarios/record/start")
    def rec_start(req: NameReq):
        return guard(ext("recorder").start, req.name)

    @app.post("/api/scenarios/record/stop")
    def rec_stop():
        return guard(ext("recorder").stop_recording)

    @app.get("/api/scenarios")
    def scenarios():
        return ext("recorder").list()

    @app.get("/api/scenarios/{name}")
    def scenario(name: str):
        return guard(ext("recorder").load, name)

    @app.delete("/api/scenarios/{name}")
    def scenario_delete(name: str):
        return guard(ext("recorder").delete, name)

    @app.post("/api/scenarios/{name}/replay")
    def scenario_replay(name: str):
        return guard(ext("replayer").start, name)

    @app.post("/api/replay/stop")
    def replay_stop():
        ext("replayer").stop()
        return {"ok": True}

    @app.post("/api/demo/start")
    def demo_start():
        return guard(ext("demo").start)

    @app.post("/api/demo/stop")
    def demo_stop():
        ext("demo").stop()
        return {"ok": True}

    @app.get("/api/demo")
    def demo_status():
        return ext("demo").status()

    # ------------------------------------------------------------------ AI: AIOps + copilot
    @app.get("/api/ai/status")
    def ai_status():
        a = ext("ai")
        return sanitize({"copilot": a.copilot.status(), "detector": a.detector.summary(), "evaluation": a.evaluation.status(),
                         "tick_ms": round(a.tick_ms, 2)})

    @app.post("/api/ai/copilot/refresh")
    async def ai_copilot_refresh():
        return sanitize(await asyncio.to_thread(ext("ai").copilot.refresh))

    @app.get("/api/ai/insights")
    def ai_insights():
        return sanitize(ext("ai").insights())

    @app.get("/api/ai/signals")
    def ai_signals():
        return sanitize(ext("ai").detector.signal_table())

    @app.get("/api/ai/signal")
    def ai_signal(id: str, seconds: int = 300):
        return sanitize(guard(ext("ai").detector.history, id, seconds))

    @app.post("/api/ai/relearn")
    def ai_relearn():
        return ext("ai").relearn()

    @app.post("/api/ai/chat")
    def ai_chat(req: ChatReq):
        copilot = ext("ai").copilot

        def stream():
            for ev in copilot.chat(req.conversation_id, req.message, req.context):
                yield "data: " + json.dumps(sanitize(ev), default=str) + "\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/ai/chat/cancel")
    def ai_chat_cancel():
        return {"cancelled": ext("ai").copilot.cancel()}

    @app.delete("/api/ai/conversations/{cid}")
    def ai_conversation_reset(cid: str):
        ext("ai").copilot.reset(cid)
        return {"ok": True}

    @app.post("/api/ai/proposals/{pid}/apply")
    async def ai_proposal_apply(pid: str):
        return sanitize(await asyncio.to_thread(guard, ext("ai").copilot.apply_proposal, pid))

    @app.post("/api/ai/proposals/{pid}/dismiss")
    def ai_proposal_dismiss(pid: str):
        return guard(ext("ai").copilot.dismiss_proposal, pid)

    @app.post("/api/ai/proposals/{pid}/predict")
    async def ai_proposal_predict(pid: str):
        return sanitize(await asyncio.to_thread(guard, ext("ai").copilot.predict_proposal, pid))

    @app.post("/api/ai/evaluate")
    def ai_evaluate():
        return guard(ext("ai").evaluation.start)

    @app.post("/api/ai/evaluate/cancel")
    def ai_evaluate_cancel():
        ext("ai").evaluation.stop()
        return {"ok": True}

    @app.get("/api/ai/evaluations")
    def ai_evaluations():
        return ext("ai").evaluation.list_runs()

    # ------------------------------------------------------------------ Assure: intents, resilience, planning
    def guard_assure(fn, *args, **kwargs):
        from ..assure.intents import IntentError

        try:
            return fn(*args, **kwargs)
        except (IntentError, ChaosError, ValueError, KeyError) as e:
            raise HTTPException(400, detail=str(e).strip("'\"")) from e

    @app.get("/api/assure/intents")
    def assure_intents():
        return sanitize(ext("assure").intent_rows())

    @app.post("/api/assure/intents")
    def assure_intent_add(req: IntentReq):
        it = guard_assure(ext("assure").intents.add, req.model_dump())
        rt().events.emit("intent.add", f"Intent {it.id} added: {it.label}", intent=it.id)
        return it.to_dict()

    @app.put("/api/assure/intents/{iid}")
    def assure_intent_update(iid: str, req: IntentPatch):
        it = guard_assure(ext("assure").intents.update, iid, {k: v for k, v in req.model_dump().items() if v is not None})
        rt().events.emit("intent.update", f"Intent {it.id} changed: {it.label}", intent=it.id)
        return it.to_dict()

    @app.delete("/api/assure/intents/{iid}")
    def assure_intent_delete(iid: str):
        guard_assure(ext("assure").intents.remove, iid)
        rt().events.emit("intent.remove", f"Intent {iid} removed", intent=iid)
        return {"ok": True}

    @app.post("/api/assure/intents/preset")
    def assure_intent_preset(req: PresetReq):
        a = ext("assure")
        raws = guard_assure(a.preset, req.name)
        if req.replace:
            items = guard_assure(a.intents.replace_all, raws, "preset")
        else:
            items = [guard_assure(a.intents.add, r, "preset") for r in raws]
        rt().events.emit("intent.preset", f"Intent preset '{req.name}' loaded: {len(items)} intents", severity="info")
        return [i.to_dict() for i in items]

    @app.get("/api/assure/scenarios")
    def assure_scenarios():
        return ext("assure").scenarios()

    @app.get("/api/assure/resilience")
    def assure_resilience(double: bool = False):
        a = ext("assure")
        return (a.resilience_n2 if double else a.resilience) or {"t": None}

    @app.post("/api/assure/resilience")
    async def assure_resilience_run(req: ResilienceReq):
        return await asyncio.to_thread(guard_assure, ext("assure").run_resilience, req.double)

    @app.post("/api/assure/plan")
    async def assure_plan():
        return await asyncio.to_thread(guard_assure, ext("assure").make_plan, "user")

    @app.get("/api/assure/plans")
    def assure_plans():
        return list(ext("assure").plans.values())[::-1]

    @app.get("/api/assure/plans/{pid}")
    def assure_plan_get(pid: str):
        return guard_assure(ext("assure").get_plan, pid)

    @app.post("/api/assure/plans/{pid}/confirm")
    async def assure_plan_confirm(pid: str):
        return await asyncio.to_thread(guard_assure, ext("assure").confirm_plan, pid)

    @app.post("/api/assure/plans/{pid}/apply")
    async def assure_plan_apply(pid: str):
        return await asyncio.to_thread(guard_assure, ext("assure").apply_plan, pid, "operator")

    @app.post("/api/assure/plan/clear")
    def assure_plan_clear(req: ModeReq):
        return guard_assure(ext("assure").clear_plan, req.mode)

    @app.get("/api/assure/autopilot")
    def assure_autopilot():
        return ext("assure").autopilot.view()

    @app.post("/api/assure/autopilot/mode")
    def assure_autopilot_mode(req: ModeReq):
        return guard_assure(ext("assure").autopilot.set_mode, req.mode)

    @app.post("/api/assure/autopilot/decisions/{did}/approve")
    def assure_autopilot_approve(did: str):
        d = guard_assure(ext("assure").autopilot.approve, did)
        return sanitize({k: v for k, v in d.items() if not k.startswith("_")})

    @app.post("/api/assure/autopilot/decisions/{did}/dismiss")
    def assure_autopilot_dismiss(did: str):
        d = guard_assure(ext("assure").autopilot.dismiss, did)
        return sanitize({k: v for k, v in d.items() if not k.startswith("_")})

    @app.get("/api/assure/uncertainty")
    def assure_uncertainty():
        return ext("assure").pool.summary()

    @app.post("/api/assure/drill")
    def assure_drill(req: DrillReq):
        return guard_assure(ext("assure").drills.start, req.scenario)

    @app.post("/api/assure/drill/cancel")
    def assure_drill_cancel():
        ext("assure").drills.stop()
        return {"ok": True}

    @app.get("/api/assure/drills")
    def assure_drills():
        return ext("assure").drills.list_runs()

    @app.post("/api/assure/benchmark")
    def assure_benchmark(req: BenchmarkReq):
        return guard_assure(ext("assure").benchmark.start, req.strategies, req.scenarios, req.rates)

    @app.post("/api/assure/benchmark/cancel")
    def assure_benchmark_cancel():
        ext("assure").benchmark.stop()
        return {"ok": True}

    @app.get("/api/assure/benchmarks")
    def assure_benchmarks():
        return ext("assure").benchmark.list_runs()

    @app.post("/api/assure/demo/start")
    def assure_demo_start():
        return guard_assure(ext("assure").demo.start)

    @app.post("/api/assure/demo/stop")
    def assure_demo_stop():
        ext("assure").demo.stop()
        return {"ok": True}

    @app.get("/api/assure/stress_profile")
    def assure_stress_profile():
        return ext("assure").stress_profile()

    @app.post("/api/routing/herd_guard")
    def routing_herd_guard(req: ToggleReq):
        rt().controller.set_herd_guard(req.on)
        return {"herd_guard": rt().controller.herd_guard}

    # ------------------------------------------------------------------ static UI
    if settings.frontend_dist.is_dir():

        class UIFiles(StaticFiles):
            """index.html must never be cached (it names the hashed bundles of the current build)."""

            async def get_response(self, path, scope):
                resp = await super().get_response(path, scope)
                if path in ("", ".", "index.html") or path.endswith(".html"):
                    resp.headers["Cache-Control"] = "no-cache"
                return resp

        app.mount("/", UIFiles(directory=str(settings.frontend_dist), html=True), name="ui")
    else:
        @app.get("/")
        def no_ui():
            return {"message": "NETVISTA API is running. Build the UI with: cd frontend && npm install && npm run build"}

    return app
