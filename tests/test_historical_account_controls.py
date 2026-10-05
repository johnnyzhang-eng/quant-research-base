import copy
from decimal import Decimal
import importlib.util
import inspect
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research_base.evidence import canonical_hash, digest
from research_base.historical_account_controls import (
    CASE_IDS, EXPECTED_CONTROL_IDS, control_report_accepted,
    native_fill_reconciliation, run_controls,
)


@unittest.skipUnless(importlib.util.find_spec("backtest") is not None,
                     "requires an existing Vibe installation")
class IntegratedAccountControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.output = Path(cls.temp.name) / "actual-controls"
        cls.report = run_controls(cls.output)

    def read(self, name):
        return json.loads((self.output / name).read_text())

    def test_actual_inventory_and_installed_engine_fingerprints(self):
        from backtest.engines.base import BaseEngine
        from backtest.engines.global_equity import GlobalEquityEngine
        fingerprints = {"BaseEngine": digest(Path(inspect.getfile(BaseEngine))),
                        "GlobalEquityEngine": digest(Path(inspect.getfile(GlobalEquityEngine)))}
        self.assertTrue(control_report_accepted(self.report))
        self.assertEqual(len(self.report["reports"]), 32)
        self.assertEqual(len(EXPECTED_CONTROL_IDS), 32)
        self.assertEqual(len(CASE_IDS), 10)
        for case in CASE_IDS:
            result = self.read(case + "/native-result.json")
            replay = self.read(case + "/fraction-replay.json")
            self.assertEqual(result["native_engine_source_sha256"], fingerprints)
            self.assertTrue(replay["passed"], replay["errors"])
            self.assertFalse(replay["errors"])
            self.assertEqual(result["data_kind"], "invented_control")
            self.assertFalse(result["historical_engine_ready"])
            self.assertEqual(result["network_attempts"], 0)

    def test_funding_constraint_observed_on_actual_native_tape(self):
        result = self.read("baseline/native-result.json")
        first_fills = [row for row in result["native_fill_records"]
                       if row["timestamp"].startswith("2020-10-01")]
        self.assertEqual([(row["symbol"], row["signed_quantity"], row["execution_price"])
                          for row in first_fills], [("SPY", 5, 100), ("EFA", 5, 100)])
        plan = next(row for row in result["events"] if row["type"] == "TARGET_PLAN")
        self.assertEqual(plan["deltas"], {"SPY": 5, "EFA": 5, "IEF": 5, "GLD": 5})
        self.assertEqual(Decimal(self.read("fx_move/native-result.json")["cny_snapshots"][0]["marked_equity_cny"]), Decimal("15000"))
        self.assertEqual(self.read("bc/native-result.json")["native_fill_records"], [])

    def test_denominator_cannot_be_reduced_or_relabelled(self):
        for mutation in (lambda r: r["reports"].pop(),
                         lambda r: r["reports"].append(copy.deepcopy(r["reports"][0])),
                         lambda r: r["actual_execution_cases"].pop(),
                         lambda r: r.update(classification="HISTORICAL_READY"),
                         lambda r: r.update(historical_engine_ready=True),
                         lambda r: r.update(formal_history_runs=False)):
            candidate = copy.deepcopy(self.report)
            mutation(candidate)
            self.assertFalse(control_report_accepted(candidate))

    def test_literal_answer_cannot_be_self_declared_or_type_coerced(self):
        candidate = copy.deepcopy(self.report)
        row = next(row for row in candidate["reports"] if row["id"] == "bc_metrics")
        # Python 0 == 0.0; typed canonical encoding must still refuse this.
        row["actual"]["period_net_return"] = 0
        row["expected"]["period_net_return"] = 0
        row["passed"] = True
        candidate["accepted"] = True
        self.assertFalse(control_report_accepted(candidate))
        candidate = copy.deepcopy(self.report)
        candidate["reports"][0].update(actual="forged", expected="forged", passed=True)
        self.assertFalse(control_report_accepted(candidate))

    def test_native_comparison_rejects_fractional_quantity_fee_and_direction(self):
        baseline = self.read("baseline/native-result.json")
        self.assertTrue(native_fill_reconciliation(baseline))
        for field, value in (("signed_quantity", 5.25), ("signed_quantity", -5),
                             ("fee", "0.01"), ("execution_price", 101)):
            candidate = copy.deepcopy(baseline)
            candidate["native_fill_records"][0][field] = value
            self.assertFalse(native_fill_reconciliation(candidate), (field, value))

    def test_late_open_price_does_not_change_actual_order_prefix(self):
        tapes = [self.read("unknown_" + price + "/native-result.json") for price in ("100", "200")]
        prefixes = [[row for row in tape["events"]
                     if row["date"] == "2020-10-01" and row["type"] in ("TARGET_PLAN", "ORDER_ATTEMPT")]
                    for tape in tapes]
        self.assertEqual(prefixes[0], prefixes[1])
        self.assertTrue(prefixes[0])
        for tape in tapes:
            self.assertFalse(any(row["date"] == "2020-10-01" and row["type"] == "FILL" for row in tape["events"]))

    def test_rejected_mutants_bind_actual_source_and_replay_errors(self):
        for identifier, case in (("fee_tamper", "baseline"), ("fx_nav_tamper", "fx_move"),
                                 ("duplicate_credit_tamper", "interest")):
            source = self.read(identifier + "/source.json")
            self.assertEqual(source["input_sha256"], canonical_hash(self.read(case + "/account-input.json")))
            self.assertEqual(source["actual_result_sha256"], canonical_hash(self.read(case + "/native-result.json")))
            replay = self.read(identifier + "/fraction-replay.json")
            self.assertFalse(replay["passed"])
            self.assertTrue(replay["errors"])

    def test_all_evidence_is_retained_hashed_and_destination_exclusive(self):
        manifest = self.read("manifest.json")
        files = {str(path.relative_to(self.output)) for path in self.output.rglob("*")
                 if path.is_file() and path.name != "manifest.json"}
        self.assertEqual(set(manifest["files"]), files)
        for name, expected in manifest["files"].items():
            self.assertEqual(digest(self.output / name), expected)
        for case in CASE_IDS:
            self.assertEqual({path.name for path in (self.output / case).iterdir()},
                             {"artifact.json", "model.json", "features.json", "account-input.json",
                              "native-result.json", "fraction-replay.json", "performance-input.json", "metrics.json"})
        before = digest(self.output / "manifest.json")
        with self.assertRaises(FileExistsError):
            run_controls(self.output)
        self.assertEqual(digest(self.output / "manifest.json"), before)


if __name__ == "__main__":
    unittest.main()
