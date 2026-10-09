"""Digital twin of the emulated network, calibrated from live data.

Two engines share one configuration format and one result shape:
  * model.py / whatif.py  packet-level SimPy model (accurate, ~1 s per prediction)
  * fluid.py              closed-form steady state (fast: used for failure analysis and planning)
"""

from .calibration import fit_overheads
from .fluid import FluidNet, fluid_simulate, run_fluid_prediction
from .model import PROBE_WIRE_BYTES, NetworkModel
from .whatif import decide_paths, predict_with, run_prediction, simulate

__all__ = [
    "PROBE_WIRE_BYTES", "FluidNet", "NetworkModel", "decide_paths", "fit_overheads", "fluid_simulate", "predict_with",
    "run_fluid_prediction", "run_prediction", "simulate",
]
