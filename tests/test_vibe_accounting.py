"""Hand-answer controls plus optional tests over the actual installed engine.

Default CI does not install Vibe. Optional tests state SKIP explicitly there;
run this suite with an existing Vibe interpreter for integration evidence.
"""
import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_base.reference import low_frequency_engine as reference
from research_base.reference import run_controls as oracle
from research_base import vibe_controls as controls
from research_base.vibe_accounting import accounting_class


CASES = {cid: (data, expected) for cid, data, expected in controls.shared_cases()}
HAS_VIBE = importlib.util.find_spec("backtest") is not None


class SharedAnswers(unittest.TestCase):
    def test_literal_answers_and_independent_reference_replay(self):
        self.assertEqual(len(CASES), 21)
        for cid, (data, expected) in CASES.items():
            with self.subTest(cid=cid):
                result = reference.run(data, "synthetic")
                self.assertEqual(controls.differences(expected, controls.reference_projection(data, result)), [])
                self.assertTrue(oracle.independent_replay(data, result)["passed"])

    def test_end_cutoff_retains_receivable_not_cash(self):
        data, _ = CASES["P17_final_receivable_not_cash"]
        last = reference.run(data, "synthetic")["snapshots"][-1]
        self.assertEqual(last["settled_cash"], "90")
        self.assertEqual(last["dividend_receivable"], "10")
        self.assertEqual(last["equity"], "100")

    def test_split_updates_locked_share_units(self):
        data, _ = CASES["P16_split_locked_shares_and_release"]
        snapshots = reference.run(data, "synthetic")["snapshots"]
        day = next(s for s in snapshots if s["date"] == "2024-10-09")
        self.assertEqual(day["holdings"]["SPY"], 20)
        self.assertEqual(day["sellable"]["SPY"], 0)

    def test_unimplemented_price_grids_are_explicit(self):
        data = copy.deepcopy(CASES["P01_no_trade"][0])
        data["instruments"]["SPY"]["lot_size"] = 100
        self.assertTrue(controls.unsupported(data, True))


@unittest.skipUnless(HAS_VIBE, "Optional installed Vibe environment required")
class ActualVibeAccounting(unittest.TestCase):
    def setUp(self):
        from backtest.engines.global_equity import GlobalEquityEngine
        self.native = GlobalEquityEngine

    def test_all_shared_cases_on_actual_path(self):
        with controls.offline_connections():
            for cid, (data, expected) in CASES.items():
                with self.subTest(cid=cid):
                    actual, details = controls.execute_vibe(data, self.native, True)
                    self.assertEqual(controls.differences(expected, actual), [])
                    self.assertEqual(details["replay_errors"], [])

    def test_wrong_engine_valuation_is_detected(self):
        # Run a deliberately wrong subclass through the same engine hooks,
        # rather than asserting against a standalone mock result.
        def broken(base):
            good = accounting_class(base)
            class WrongNoncashAssets(good):
                def assets_not_cash(self):
                    return 0
            return WrongNoncashAssets
        data, expected = CASES["P17_final_receivable_not_cash"]
        with controls.offline_connections(), patch.object(controls, "accounting_class", broken):
            actual, details = controls.execute_vibe(data, self.native, True)
        self.assertTrue(controls.differences(expected, actual))
        self.assertTrue(details["replay_errors"])

    def test_late_action_is_rejected_on_target_path(self):
        data = copy.deepcopy(CASES["P07_dividend_receivable_and_payment"][0])
        data["actions"][0]["known_at"] = "2024-10-09T21:00:00Z"
        with controls.offline_connections(), self.assertRaisesRegex(reference.ContractError, "late action"):
            controls.execute_vibe(data, self.native, True)

    def test_fractional_split_is_rejected_on_target_path(self):
        data = copy.deepcopy(CASES["P09_split_units"][0])
        data["initial"]["holdings"]["SPY"] = 3
        data["actions"][0].update(numerator=1, denominator=2)
        with controls.offline_connections(), self.assertRaisesRegex(reference.ContractError, "fractional split"):
            controls.execute_vibe(data, self.native, True)

    def test_calibration_rejects_five_accounting_mutations(self):
        with tempfile.TemporaryDirectory() as temp:
            result = controls.run_controls(Path(temp))
        self.assertTrue(result["instrument_calibrated"])
        self.assertEqual(result["summary"]["aligned"], {"MATCH": 21, "DIFFERS": 0, "UNSUPPORTED": 0})
        self.assertEqual(len(result["accounting_error_injections"]), 5)
        self.assertTrue(all(e["detected"] for e in result["accounting_error_injections"]))


if __name__ == "__main__":
    unittest.main()
