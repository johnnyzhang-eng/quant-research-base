import copy
import csv
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research_base.evidence import ContractError, digest, load_json, verify_run
from research_base.historical_study import run_study, resolve_scope, _load_inputs, FULL_SCOPE, _cohort
from tests.study_fixture import write_study_fixture


@unittest.skipUnless(importlib.util.find_spec("backtest") is not None, "requires an existing Vibe installation")
class HistoricalStudyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.manifest = write_study_fixture(cls.root / "input")
        cls.code, cls.run_folder = run_study(cls.manifest, cls.root / "evidence", cls.root)
        cls.result = load_json(cls.run_folder / "result.json") if (cls.run_folder / "result.json").exists() else {}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_actual_complete_accounts_and_calibration_inventory(self):
        failure = load_json(self.run_folder / "failure.json") if (self.run_folder / "failure.json").exists() else None
        self.assertEqual(self.code, 0, {"result": self.result, "failure": failure})
        self.assertEqual(self.result["matrix_cells"], 32)
        self.assertEqual(self.result["complete_cells"], 32)
        self.assertEqual(self.result["comparison_cells"], 8)
        self.assertEqual(self.result["calibration_candidates"], 101)
        self.assertEqual(self.result["frozen_k"], "0.00")
        self.assertTrue(self.result["calibration_risk_matched"])
        self.assertEqual(self.result["formal_history_account_trials"], 0)
        self.assertFalse(self.result["scope_full_original"])
        self.assertFalse(self.result["historical_engine_ready"])
        self.assertFalse(self.result["goal_complete"])
        self.assertTrue(verify_run(self.run_folder, self.root / "evidence" / "registry.jsonl")["verified"])

    def test_weight_frozen_before_full_account_attempts(self):
        events = [json.loads(line) for line in (self.run_folder / "trials.jsonl").read_text().splitlines()]
        freezes = [i for i, event in enumerate(events) if event["event"] == "K_FROZEN"]
        self.assertEqual(len(freezes), 1)
        starts = [(i, e) for i, e in enumerate(events) if e["event"] == "TRIAL_STARTED"]
        development = [(i, e) for i, e in starts if e["trial"].startswith("development-")]
        full = [(i, e) for i, e in starts if not e["trial"].startswith("development-")]
        matrix = [(i, e) for i, e in full if not e["trial"].startswith("friction-")]
        friction = [(i, e) for i, e in full if e["trial"].startswith("friction-")]
        self.assertEqual((len(development), len(matrix), len(friction)), (102, 32, 28))
        self.assertTrue(all(i < freezes[0] for i, _ in development))
        self.assertTrue(all(i > freezes[0] for i, _ in full))
        self.assertTrue(all(e["k"] == "0.00" for _, e in full if e["variant"] == "BR"))

    def test_actual_friction_reruns_keep_fixed_grid_and_k(self):
        friction = load_json(self.run_folder / "friction.json")
        self.assertEqual(set(friction), {"50000-C0", "200000-C0"})
        for scan in friction.values():
            self.assertEqual(scan["grid_bp"], ["0", "1", "2", "5", "10", "20", "50", "100"])
            self.assertEqual(scan["validated_trial_count"], 8)
            self.assertTrue(scan["all_trials_validated"])
            self.assertFalse(scan["retuned_k"])
            self.assertFalse(scan["global_unique_root_claim"])
            self.assertEqual(scan["frozen_k"], "0.00")

    def test_continuous_boundaries_costs_and_all_report_rows(self):
        matrix = load_json(self.run_folder / "matrix.json")
        for cell, entry in matrix.items():
            windows = entry["windows"]
            for left, right in (("development", "validation"), ("validation", "confirmation")):
                self.assertEqual(windows[left]["end_snapshot_sha256"], windows[right]["boundary_snapshot_sha256"])
                for currency in ("CNY", "USD"):
                    self.assertEqual(windows[left]["currencies"][currency]["ledger"]["observations"][-1]["equity"],
                                     windows[right]["currencies"][currency]["ledger"]["observations"][0]["equity"])
            self.assertGreater(float(windows["full"]["currencies"]["CNY"]["metrics"]["cost_totals"]["fixed"]), 0)
        with (self.run_folder / "matrix.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), sum(len(e["windows"])*2 for e in matrix.values()))
        self.assertEqual({r["currency"] for r in rows}, {"CNY", "USD"})

    def test_short_control_inference_and_exit_remain_pending(self):
        self.assertFalse(self.result["exit_inputs_complete"])
        self.assertFalse(self.result["terminal_realizability_complete"])
        self.assertEqual(self.result["complete_cash_exit_cells"], 0)
        exits = load_json(self.run_folder / "exit_matrix.json")
        self.assertEqual(len(exits), 32)
        self.assertTrue(all(e["status"] == "PENDING" for e in exits.values()))
        comparisons = load_json(self.run_folder / "comparisons.json")
        for comparison in comparisons.values():
            self.assertEqual(comparison["diagnostic"]["status"], "EVIDENCE_INSUFFICIENT")
            for name in ("validation", "confirmation"):
                pair = comparison["paired_monthly"][name]
                self.assertFalse(pair["sample_sufficient"])
                self.assertEqual(pair["interval"]["reason"], "FEWER_THAN_60_MONTHS")

    def test_market_cannot_use_control_scope_or_relabel_protocol(self):
        manifest = load_json(self.manifest)
        artifact = load_json(self.manifest.parent / manifest["artifact"]["file"])
        artifact["data_kind"] = "historical_market"
        with self.assertRaisesRegex(ContractError, "may not shorten"):
            resolve_scope(artifact, manifest["scope"])
        self.assertEqual(FULL_SCOPE["warmup"]["start"], "2005-01-01")
        self.assertEqual(FULL_SCOPE["periods"]["confirmation"]["end"], "2026-09-30")

    def test_cohort_does_not_hide_cost_or_funding_changes(self):
        manifest = load_json(self.manifest)
        model = load_json(self.manifest.parent / manifest["models"]["50000-C0"]["file"])
        altered = copy.deepcopy(model)
        altered["terms"]["rules"][0]["fees"][0]["minimum"] = "99"
        self.assertNotEqual(_cohort(model), _cohort(altered))
        altered = copy.deepcopy(model)
        altered["cash_rules"]["initial_cny"] = "200000"
        altered["fx"]["entry"]["principal_cny"] = "199000"
        altered["scenario"] = "C3"
        self.assertEqual(_cohort(model), _cohort(altered))

    def test_modified_frozen_input_retains_failed_run(self):
        target = self.root / "changed"
        target.mkdir()
        manifest = write_study_fixture(target / "input")
        value = load_json(manifest)
        protocol = manifest.parent / value["protocol"]["file"]
        protocol.write_text(protocol.read_text() + "changed after freeze")
        code, run = run_study(manifest, target / "evidence", self.root)
        self.assertEqual(code, 1)
        self.assertIn("differs from frozen descriptor", load_json(run / "failure.json")["reason"])
        self.assertTrue(verify_run(run, target / "evidence" / "registry.jsonl")["verified"])


if __name__ == "__main__":
    unittest.main()
