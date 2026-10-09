"""One-button demo: start traffic -> inject latency -> watch reroute -> kill a router -> recover.
After each fault it also reports what the AI layer diagnosed from the probes alone.

Targets are picked from the LIVE state at each step (the link the main flow is using right
now, the router on its current path), so the demo always exercises a real change.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from ..routing.graph import core_hops


class DemoRunner:
    STEPS = [
        "Reset: clear faults, adaptive routing, start traffic",
        "Inject +80 ms latency on the link the main flow uses",
        "Kill a router on the main flow's new path",
        "Recover: revert every fault",
        "Summary",
    ]

    def __init__(self, rt) -> None:
        self.rt = rt
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict[str, Any] = {"running": False, "steps": self.STEPS}

    def status(self) -> dict:
        return dict(self._status)

    def stop(self) -> None:
        self._stop.set()

    def start(self) -> dict:
        if self._thread and self._thread.is_alive():
            raise ValueError("demo already running")
        self._stop.clear()
        self._status = {"running": True, "steps": self.STEPS, "index": 0, "started": time.time(), "notes": []}
        self._thread = threading.Thread(target=self._run, name="demo", daemon=True)
        self._thread.start()
        return self.status()

    # ------------------------------------------------------------------
    def _say(self, msg: str, severity: str = "info") -> None:
        self._status.setdefault("notes", []).append(msg)
        self.rt.events.emit("demo.step", msg, severity=severity, source="demo")

    def _wait(self, secs: float) -> bool:
        self._status["step_ends_at"] = time.time() + secs
        return not self._stop.wait(secs)

    def _ai_note(self, injected: str) -> None:
        """What the AI layer concluded from probes alone, next to what was really injected."""
        ai = self.rt.extensions.get("ai")
        causes = (ai.diagnosis.get("causes") or []) if ai else []
        if not ai:
            return
        if causes:
            c = causes[0]
            self._say(f"AI diagnosis from probes only: {c['title']} ({c['confidence']} confidence); injected: {injected}", "success")
        else:
            self._say(f"AI diagnosis: no cause found yet; injected: {injected}", "warn")

    def _main_pair(self) -> str:
        t = self.rt.topo.traffic
        return f"{t[0].src}>{t[0].dst}" if t else next(iter(self.rt.controller.flows))

    def _run(self) -> None:
        rt = self.rt
        ctrl = rt.controller
        pair = self._main_pair()
        inc0 = len(ctrl.incidents)
        try:
            # 1. reset
            self._status["index"] = 0
            rt.chaos.revert_all(source="demo")
            ctrl.set_mode("adaptive")
            rt.start_default_traffic()
            self._say(f"Traffic running; {pair.replace('>', '→')} uses {'–'.join(ctrl.flows[pair].path)}")
            if not self._wait(8):
                return

            # 2. latency on the slowest-capacity core link of the current path
            self._status["index"] = 1
            path = ctrl.flows[pair].path
            hops = core_hops(rt.topo, path)
            link = min((lid for _, _, lid in hops), key=lambda l: rt.topo.links[l].bw_mbps)
            rt.chaos.inject("link_latency", link, {"add_ms": 80}, source="demo")
            self._say(f"Injected +80 ms on {link}; the controller should score the path worse and move away", "warn")
            if not self._wait(10):
                return
            new = ctrl.flows[pair].path
            if new != path:
                self._say(f"Re-routed: {'–'.join(path)} ⇒ {'–'.join(new)}", "success")
            else:
                self._say("No re-route (the alternative was not better by the hysteresis margin)", "warn")
            self._ai_note(f"+80 ms on {link}")

            # 3. kill a router on the current path (not an edge router that owns a LAN)
            self._status["index"] = 2
            lans = {r for s in rt.plan.subnets if s.kind == "lan" for r in s.routers}
            victim = next((n for n in ctrl.flows[pair].path if rt.topo.nodes[n].type == "router" and n not in lans), None)
            if victim:
                rt.chaos.inject("node_down", victim, source="demo")
                self._say(f"Crashed router {victim}: its interfaces are down, neighbours only see silence", "error")
                if not self._wait(10):
                    return
                self._say(f"{pair.replace('>', '→')} now uses {'–'.join(ctrl.flows[pair].path)}", "success")
                self._ai_note(f"router {victim} down")
            else:
                self._say("No core router on the path to crash; skipping", "warn")

            # 4. recover
            self._status["index"] = 3
            n = rt.chaos.revert_all(source="demo")
            self._say(f"Reverted {n} faults; links come back as probes answer again")
            if not self._wait(10):
                return

            # 5. summary
            self._status["index"] = 4
            incs = [i.to_dict() for i in list(ctrl.incidents)[inc0:] if i.pair == pair]
            for i in incs:
                bits = [f"{k} {i[k + '_ms']:.0f} ms" for k in ("detection", "reroute", "recovery") if i.get(k + "_ms") is not None]
                self._say(f"Incident #{i['id']} ({i['kind']}, {i['cause']}): " + ", ".join(bits), "success")
            self._say("Demo complete")
        except Exception as e:
            self._say(f"Demo aborted: {e}", "error")
        finally:
            self._status.update(running=False, finished=time.time())
