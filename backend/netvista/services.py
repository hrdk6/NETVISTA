"""Attach the Phase 4/5 services (simulator, validation, scenarios, demo), the AIOps layer and Assure to a Runtime."""

from __future__ import annotations


def attach_services(rt) -> None:
    from .scenarios import DemoRunner, ScenarioRecorder, ScenarioReplayer
    from .simulator.service import SimulatorService
    from .validation import ValidationService

    sim = SimulatorService(rt)
    rt.extensions["simulator"] = sim
    rt.extensions["validation"] = ValidationService(rt)
    rt.extensions["recorder"] = ScenarioRecorder(rt)
    rt.extensions["replayer"] = ScenarioReplayer(rt)
    rt.extensions["demo"] = DemoRunner(rt)
    from .ai.service import AIService

    rt.extensions["ai"] = AIService(rt)
    from .assure.service import AssureService

    rt.extensions["assure"] = AssureService(rt)
    # calibrate once the probes have ~15 s of calm, unloaded data (before anyone starts traffic)
    sim.auto_calibrate(delay_s=15.0)
