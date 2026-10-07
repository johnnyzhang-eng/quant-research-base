import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_base.evidence import ContractError, load_json, verify_run, write_json
from research_base.historical_contract import control_model, prepare
from research_base.historical_run import run_account
from research_base.historical_signals import write_control_fixture
from research_base.runner import package_inventory, PACKAGE


@unittest.skipUnless(importlib.util.find_spec("backtest") is not None, "requires an existing Vibe installation")
class HistoricalRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        artifact = write_control_fixture(self.root / "fixture")
        model, features = control_model(artifact)
        self.data = prepare(artifact, features, model)
        self.input = self.root / "prepared.json"
        write_json(self.input, self.data)
        self.output = self.root / "acceptance"

    def test_native_replay_metrics_and_input_are_retained_and_sealed(self):
        code, run = run_account(self.input, self.output, self.root)
        self.assertEqual(code, 0)
        self.assertEqual((run / "input.json").read_bytes(), self.input.read_bytes())
        summary = load_json(run / "result.json")
        self.assertEqual(summary["status"], "MODEL_DIAGNOSTICS_RECONCILED")
        self.assertFalse(summary["goal_complete"])
        self.assertFalse(summary["historical_engine_ready"])
        self.assertEqual(summary["data_kind"], "invented_control")
        self.assertEqual([summary[k] for k in ("formal_s02_studies", "official_paper_orders", "real_orders")], [0, 0, 0])
        self.assertTrue(load_json(run / "independent_replay.json")["passed"])
        self.assertEqual(load_json(run / "performance_input.json")["currency"], "CNY")
        self.assertTrue(verify_run(run, self.output / "registry.jsonl")["verified"])
        (run / "account.json").write_text("{}")
        self.assertFalse(verify_run(run, self.output / "registry.jsonl")["verified"])

    def test_invalid_input_attempt_is_preserved_and_not_strategy_failure(self):
        self.input.write_text('{"data_kind":"historical_market"}')
        code, run = run_account(self.input, self.output, self.root)
        self.assertEqual(code, 1)
        self.assertTrue(verify_run(run, self.output / "registry.jsonl")["verified"])
        self.assertEqual(load_json(run / "failure.json")["classification"], "RUN_ERROR_NOT_STRATEGY_OR_BROKER_FAILURE")
        self.assertFalse((run / "performance.json").exists())

    def test_failed_replay_cannot_emit_performance_or_success(self):
        with patch("research_base.historical_replay.independent_replay", return_value={"passed": False,
                   "errors": [{"field": "cash", "expected": "1", "actual": "2"}], "replayed": []}):
            code, run = run_account(self.input, self.output, self.root)
        self.assertEqual(code, 1)
        self.assertEqual(load_json(run / "result.json")["status"], "ACCOUNT_EVIDENCE_ERRORS")
        self.assertFalse((run / "performance.json").exists())
        self.assertTrue((run / "account.json").exists())
        self.assertTrue(verify_run(run, self.output / "registry.jsonl")["verified"])

    def test_source_change_is_an_evidence_error(self):
        with patch("research_base.historical_run.package_inventory", side_effect=[package_inventory(), {}]):
            code, run = run_account(self.input, self.output, self.root)
        self.assertEqual(code, 1)
        self.assertIn("program source changed during execution", load_json(run / "result.json")["errors"])

    def test_changed_native_fill_cannot_pass_linked_account_replay(self):
        from research_base.historical_account import execute
        def changed(data):
            result = execute(data)
            result["native_fill_records"][0]["signed_quantity"] += 1
            return result
        with patch("research_base.historical_account.execute", side_effect=changed):
            code, run = run_account(self.input, self.output, self.root)
        self.assertEqual(code, 1)
        result = load_json(run / "result.json")
        self.assertTrue(result["independent_replay_passed"])
        self.assertFalse(result["native_fill_records_reconciled"])
        self.assertFalse((run / "performance.json").exists())

    def test_input_and_output_must_stay_outside_distributable_checkout(self):
        with self.assertRaises(ContractError):
            run_account(self.input, PACKAGE.parent / "restricted-output", self.root)
        with self.assertRaisesRegex(ContractError, "distributable"):
            run_account(PACKAGE.parent / "restricted-input.json", PACKAGE.parent / "restricted-output", PACKAGE.parent)
