"""FastAPI application: REST for actions, one WebSocket for the live stream, static UI."""

from __future__ import annotations

import asyncio
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

log = logging.getLogger(__name__)


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = Runtime(settings)
        state["runtime"] = rt
        task = asyncio.create_task(broadcaster())
        try:
            await asyncio.to_thread(rt.start)
        except Exception as e:
            log.exception("emulation failed to start")
            rt.status = "error"
            rt.error = f"{type(e).__name__}: {e}"
            rt.events.emit("system.error", f"Emulation failed to start: {rt.error}", severity="error")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.to_thread(rt.stop)

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

    @app.get("/api/topology")
    def topology():
        r: Runtime | None = state["runtime"]
        if r is None:
            raise HTTPException(503, "starting")
        return {"topology": r.topo.to_dict(), "plan": r.plan.to_dict(), "flow_pairs": [f"{c}>{s}" for c, s in r.topo.flow_pairs()], "status": r.status, "error": r.error}

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
