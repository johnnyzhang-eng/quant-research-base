import json
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_base.evidence import verify_run
from research_base.phase2 import assess_controls, run_phase2_validation


class Phase2InstrumentTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("backtest") is not None, "requires an existing Vibe installation")
    def test_integrated_native_controls_are_sealed_without_formal_history_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            code, run = run_phase2_validation(directory, integrated_account_controls=True)
            diagnostic = json.loads((run / ("result.json" if (run / "result.json").exists() else "failure.json")).read_text())
            self.assertEqual(code, 0, diagnostic)
            result = json.loads((run / "result.json").read_text())
            self.assertEqual(set(result["controls"]), {"historical_input", "offline_orders", "historical_signals",
                                                     "trading_terms", "cash_account", "historical_account"})
            self.assertTrue(all(s["accepted"] for s in result["controls"].values()))
            self.assertTrue(result["integrated_account_controls_requested"])
            self.assertFalse(result["goal_complete"])
            self.assertEqual(result["formal_history_trials"], 0)
            self.assertEqual(result["official_paper_orders"], 0)
            self.assertTrue(verify_run(run, Path(directory) / "registry.jsonl")["verified"])

    def test_optional_model_components_have_sealed_bounded_results(self):
        with tempfile.TemporaryDirectory() as directory:
            code, run = run_phase2_validation(directory, historical_model_controls=True)
            self.assertEqual(code, 0)
            result = json.loads((run / "result.json").read_text())
            self.assertEqual(set(result["controls"]), {"historical_input", "offline_orders",
                                                     "historical_signals", "trading_terms", "cash_account"})
            self.assertTrue(all(s["accepted"] for s in result["controls"].values()))
            self.assertFalse(result["goal_complete"])
            for field in ["formal_history_trials", "official_paper_orders", "real_orders"]:
                self.assertEqual(result[field], 0)
            self.assertTrue(verify_run(run, Path(directory) / "registry.jsonl")["verified"])

    def test_summary_cannot_hide_wrong_answer_or_empty_or_duplicate_rows(self):
        wrong = {"id": "answer", "passed": True, "expected": 10, "actual": 11}
        self.assertFalse(assess_controls({"accepted": True, "reports": [wrong]}, {"answer"})["accepted"])
        correct = {**wrong, "actual": 10}
        self.assertFalse(assess_controls({"accepted": True, "reports": [correct, correct]}, {"answer"})["accepted"])
        self.assertFalse(assess_controls({"accepted": True, "reports": []}, {"answer"})["accepted"])
        self.assertFalse(assess_controls({"accepted": True, "reports": [correct]}, {"answer", "missing"})["accepted"])
        self.assertFalse(assess_controls({"accepted": True, "reports": [correct]}, ["answer", "answer"])["accepted"])
        self.assertFalse(assess_controls({"accepted": True, "reports": [
            {"id": "answer", "passed": True, "expected": 1, "actual": True}]}, {"answer"})["accepted"])
        self.assertFalse(assess_controls({"accepted": True, "reports": [
            {"id": "unsupported", "passed": True}]}, {"unsupported"})["accepted"])

    def test_failure_is_retained_sealed_and_not_strategy_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("research_base.historical_input.run_controls", side_effect=RuntimeError("injected instrument interruption")):
                code, run = run_phase2_validation(root)
            self.assertEqual(code, 1)
            failure = json.loads((run / "failure.json").read_text())
            self.assertEqual(failure["classification"], "RUN_ERROR_NOT_STRATEGY_OR_BROKER_FAILURE")
            events = [json.loads(line) for line in (root / "registry.jsonl").read_text().splitlines()]
            self.assertEqual([e["event"] for e in events], ["STARTED", "FINISHED"])
            self.assertEqual(events[-1]["status"], "RUN_FAILED")
            self.assertTrue(verify_run(run, root / "registry.jsonl")["verified"])
            (run / "failure.json").write_text("{}")
            self.assertFalse(verify_run(run, root / "registry.jsonl")["verified"])

    def test_wrong_control_survives_producer_accepted_label(self):
        false_report = {"classification": "INJECTED_CONTROL", "accepted": True,
                        "reports": [{"id": "wrong", "passed": True, "expected": 1, "actual": 2}]}
        right_report = {"classification": "INJECTED_CONTROL", "accepted": True,
                        "reports": [{"id": "right", "passed": True, "expected": 1, "actual": 1}]}
        with tempfile.TemporaryDirectory() as directory:
            with patch("research_base.historical_input.run_controls", return_value=false_report), \
                 patch("research_base.execution.run_offline_controls", return_value=right_report):
                code, run = run_phase2_validation(directory)
            self.assertEqual(code, 1)
            result = json.loads((run / "result.json").read_text())
            self.assertEqual(result["status"], "CONTROL_ERRORS")
            self.assertFalse(result["goal_complete"])
            self.assertEqual(result["formal_history_trials"], 0)
            self.assertEqual(result["official_paper_orders"], 0)


if __name__ == "__main__":
    unittest.main()
