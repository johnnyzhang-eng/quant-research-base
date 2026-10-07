"""Exercise the independent matcher's own known answers and bad instruments."""
import importlib.util
from pathlib import Path
import sys
import unittest


def independent_module():
    path = (Path(__file__).resolve().parents[1] / "tools" /
            "binance_spot_grid_economic_independent_audit_20261008_v0.py")
    name = "spot_grid_independent_public_controls"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class IndependentGridControls(unittest.TestCase):
    def test_independent_known_answers_without_producer_helpers(self):
        result = independent_module().self_controls()
        self.assertEqual(result["known_answer_groups"], 9)
        self.assertEqual(result["assertions"], 2260)
        self.assertEqual(result["producer_helpers_called"], 0)
        self.assertEqual(result["actual_market_return_runs"], 0)

    def test_bad_fee_clock_quantum_and_phase_instruments_rejected(self):
        result = independent_module().instrument_controls()
        self.assertEqual(result["groups"], 5)
        self.assertEqual(result["assertions"], 23)
        self.assertEqual(result["in_memory_wrong_fee_clock_quantum_phase_cases_rejected"], 4)
        self.assertEqual(result["producer_files_mutated"], 0)
