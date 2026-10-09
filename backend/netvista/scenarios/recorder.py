"""Scenario record & replay.

Recording captures every *action* (fault injected/reverted, traffic started/stopped,
routing mode/weights changed) with its time offset, plus the 1 Hz telemetry history of the
recording window and the event log. Replay re-executes the actions on the LIVE network at
the same offsets, so a demo or experiment can be repeated exactly.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from ..events import Event

_NAME_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,60}$")


def _safe(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise ValueError("scenario name: 1-60 letters, digits, space, _ . -")
    return name.strip().replace(" ", "_")


class ScenarioRecorder:
    def __init__(self, rt) -> None:
        self.rt = rt
        self.dir: Path = rt.s.scenarios_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.recording: dict[str, Any] | None = None
        rt.events.subscribe(self._on_event)

    # ------------------------------------------------------------------ capture
    def _on_event(self, ev: Event) -> None:
        rec = self.recording
        if rec is None or ev.data.get("source") in ("replay",):
            return
        rel = round(ev.t - rec["t0"], 3)
        a = None
        if ev.kind == "chaos.inject":
            inj = ev.data["injection"]
            a = {"action": "inject", "kind": inj["kind"], "target": inj["target"], "params": inj["params"], "ref": inj["id"], "label": inj["label"]}
        elif ev.kind == "chaos.revert":
            a = {"action": "revert", "ref": ev.data["injection"]["id"], "label": ev.message}
        elif ev.kind == "traffic.start" and ev.data.get("traffic_kind") == "background":
            a = {"action": "traffic_start", "src": ev.data["src"], "dst": ev.data["dst"], "rate_mbps": ev.data["rate_mbps"], "ref": ev.data["flow"], "label": ev.message}
        elif ev.kind == "traffic.stop":
            a = {"action": "traffic_stop", "ref": ev.data.get("flow"), "label": ev.message}
        elif ev.kind == "routing.mode":
            a = {"action": "mode", "mode": ev.data["mode"], "label": ev.message}
        elif ev.kind == "routing.weights":
            a = {"action": "weights", "weights": ev.data["weights"], "label": ev.message}
        if a:
            a["t"] = rel
            rec["actions"].append(a)

    def start(self, name: str) -> dict:
        if self.recording:
            raise ValueError(f"already recording '{self.recording['name']}'")
        replayer = self.rt.extensions.get("replayer")
        if replayer and replayer.status().get("running"):
            raise ValueError("cannot record while a replay is running")
        self.recording = {"name": name, "file": _safe(name), "t0": time.time(), "actions": [], "ev_seq0": self.rt.events.recent(1)[-1].seq if self.rt.events.recent(1) else 0}
        # remember the starting conditions so a replay can recreate them
        self.recording["initial"] = {
            "mode": self.rt.controller.mode,
            "weights": self.rt.controller.weights.to_dict(),
            "traffic": [{"src": f.src, "dst": f.dst, "rate_mbps": f.rate_mbps} for f in self.rt.traffic.running() if f.kind == "background"],
        }
        self.rt.events.emit("scenario.record", f"Recording scenario '{name}'", severity="info")
        return self.status()

    def stop_recording(self) -> dict:
        rec = self.recording
        if rec is None:
            raise ValueError("not recording")
        self.recording = None
        t1 = time.time()
        doc = {
            "name": rec["name"],
            "recorded_at": rec["t0"],
            "duration_s": round(t1 - rec["t0"], 2),
            "topology": self.rt.topo.name,
            "initial": rec["initial"],
            "actions": rec["actions"],
            "history": [h for h in list(self.rt.history) if rec["t0"] <= h["t"] <= t1],
            "events": [e.to_dict() for e in self.rt.events.since(rec["ev_seq0"])],
        }
        (self.dir / f"{rec['file']}.json").write_text(json.dumps(doc), encoding="utf-8")
        self.rt.events.emit("scenario.saved", f"Saved scenario '{rec['name']}' ({len(rec['actions'])} actions, {doc['duration_s']:.0f}s)", severity="success")
        return {"name": rec["name"], "actions": len(rec["actions"]), "duration_s": doc["duration_s"]}

    # ------------------------------------------------------------------ storage
    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"name": d.get("name", p.stem), "file": p.stem, "recorded_at": d.get("recorded_at"),
                        "duration_s": d.get("duration_s"), "actions": len(d.get("actions", [])), "topology": d.get("topology")})
        return out

    def load(self, name: str) -> dict:
        p = self.dir / f"{_safe(name)}.json"
        if not p.exists():
            raise KeyError(f"no scenario '{name}'")
        return json.loads(p.read_text(encoding="utf-8"))

    def delete(self, name: str) -> dict:
        p = self.dir / f"{_safe(name)}.json"
        if not p.exists():
            raise KeyError(f"no scenario '{name}'")
        p.unlink()
        return {"deleted": name}

    def status(self) -> dict:
        r = self.recording
        return {"recording": r is not None, "name": r["name"] if r else None, "since": r["t0"] if r else None, "actions": len(r["actions"]) if r else 0}


class ScenarioReplayer:
    def __init__(self, rt) -> None:
        self.rt = rt
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict[str, Any] = {"running": False}

    def status(self) -> dict:
        return dict(self._status)

    def stop(self) -> None:
        self._stop.set()

    def start(self, name: str) -> dict:
        if self._thread and self._thread.is_alive():
            raise ValueError("a replay is already running")
        doc = self.rt.extensions["recorder"].load(name)
        self._stop.clear()
        self._status = {"running": True, "name": doc["name"], "index": 0, "total": len(doc["actions"]), "started": time.time(), "duration_s": doc["duration_s"]}
        self._thread = threading.Thread(target=self._run, args=(doc,), name="replay", daemon=True)
        self._thread.start()
        return self.status()

    def _run(self, doc: dict) -> None:
        rt = self.rt
        refs: dict[str, str] = {}
        try:
            rt.events.emit("scenario.replay", f"Replaying scenario '{doc['name']}' ({len(doc['actions'])} actions)", source="replay")
            rt.chaos.revert_all(source="replay")
            init = doc.get("initial", {})
            if init.get("mode"):
                rt.controller.set_mode(init["mode"])
            if init.get("weights"):
                w = init["weights"]
                rt.controller.set_weights(w["latency"], w["loss"], w["util"])
            rt.traffic.stop_all("background")
            for f in init.get("traffic", []):
                rt.traffic.start_flow(f["src"], f["dst"], f["rate_mbps"])
            t0 = time.time()
            for i, a in enumerate(doc["actions"]):
                wait = a["t"] - (time.time() - t0)
                if wait > 0 and self._stop.wait(wait):
                    break
                self._status.update(index=i + 1, current=a.get("label"))
                try:
                    self._apply(a, refs)
                except Exception as e:
                    rt.events.emit("scenario.error", f"Replay step '{a.get('label')}' failed: {e}", severity="error", source="replay")
            rest = doc.get("duration_s", 0) - (time.time() - t0)
            if rest > 0:
                self._stop.wait(rest)
            rt.events.emit("scenario.replay_done", f"Replay of '{doc['name']}' finished", severity="success", source="replay")
        finally:
            self._status = {"running": False, "finished": time.time()}

    def _apply(self, a: dict, refs: dict[str, str]) -> None:
        rt = self.rt
        act = a["action"]
        if act == "inject":
            inj = rt.chaos.inject(a["kind"], a["target"], a["params"], source="replay")
            refs[a["ref"]] = inj.id
        elif act == "revert":
            if a["ref"] in refs:
                rt.chaos.revert(refs[a["ref"]], source="replay")
        elif act == "traffic_start":
            f = rt.traffic.start_flow(a["src"], a["dst"], a["rate_mbps"])
            refs[a["ref"]] = f.id
        elif act == "traffic_stop":
            if a.get("ref") in refs:
                rt.traffic.stop_flow(refs[a["ref"]])
        elif act == "mode":
            rt.controller.set_mode(a["mode"])
        elif act == "weights":
            w = a["weights"]
            rt.controller.set_weights(w["latency"], w["loss"], w["util"])
