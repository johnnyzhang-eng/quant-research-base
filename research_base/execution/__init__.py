"""Offline-only simulation binding, persistent OMS and control runner."""
from .manager import (ACCOUNT, ENV, DEFAULT_CONFIG, SimulationBinding,
                      FictionalVenue, OrderManager, OfflineAdapter)

from ._expected import EXPECTED_CONTROL_IDS, EXPECTED_FROZEN_CASE_IDS
from .reporting import validate_control_report

def run_offline_controls(output_dir):
    from .controls import run_offline_controls as run
    return run(output_dir)


__all__ = ["ACCOUNT", "ENV", "DEFAULT_CONFIG", "SimulationBinding", "FictionalVenue",
           "OrderManager", "OfflineAdapter", "run_offline_controls"]
