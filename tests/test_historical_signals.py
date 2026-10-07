import copy
import csv
import json
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research_base.evidence import ContractError, canonical_hash, digest, load_json
from research_base import historical_input as h
from research_base import historical_signals as s


def fixture(root):
    return s.write_control_fixture(root)

class HistoricalSignalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifact = fixture(Path(self.temp.name) / "fixture")

    def generate(self, artifact=None, variant="S", **kwargs):
        return s.generate(artifact or self.artifact, variant, trade_start="2020-10-01", end_date="2020-12-31", **kwargs)

    def test_control_report_literal_inventory_and_immutable_evidence(self):
        output = Path(self.temp.name) / "controls"
        report = s.run_controls(output)
        self.assertEqual(19, len(report["reports"]))
        self.assertTrue(report["accepted"], report)
        self.assertTrue(s.control_report_accepted(report))
        inventory = load_json(output / "manifest.json")["files"]
        self.assertTrue(all(digest(output / name) == checksum for name, checksum in inventory.items()))
        for mutation in ("drop", "duplicate", "forge", "forge_expected"):
            changed = copy.deepcopy(report)
            if mutation == "drop": changed["reports"].pop()
            if mutation == "duplicate": changed["reports"][-1] = copy.deepcopy(changed["reports"][0])
            if mutation == "forge": changed["reports"][0]["actual"] = {"index": "wrong"}
            if mutation == "forge_expected":
                changed["reports"][0]["expected"] = {"index": "wrong"}
                changed["reports"][0]["actual"] = {"index": "wrong"}
            self.assertFalse(s.control_report_accepted(changed), mutation)
        with self.assertRaises(FileExistsError): s.run_controls(output)

    def test_ten_complete_months_and_strict_equal(self):
        report = self.generate()
        self.assertEqual(9, sum(row["reason"] == "TEN_COMPLETE_MONTH_WARMUP" for row in report["skipped"]))
        self.assertEqual(3, len(report["decisions"]))
        first = report["decisions"][0]
        self.assertEqual("2020-10-30", first["date"])
        self.assertEqual("2020-11-02", first["execution_date"])
        self.assertEqual([f"2020-{m:02}" for m in range(1, 11)], first["sma_months"])
        self.assertEqual({sym: "100" for sym in s.ASSETS}, first["index_fraction"])
        self.assertEqual({sym: "100" for sym in s.ASSETS}, first["sma_fraction"])
        self.assertEqual({sym: "0" for sym in s.ASSETS}, first["weights"])
        self.assertEqual("OUTSIDE_EXECUTION_WINDOW", report["unexecuted_intents"][-1]["status"])

    def test_split_dividend_combination_known_answer(self):
        artifact = copy.deepcopy(self.artifact)
        day = "2020-10-01"
        for bar in artifact["bars"]:
            if bar["symbol"] == "SPY" and bar["trade_date"] >= day:
                bar.update(open="49", high="49", low="49", close="49")
        common = {"symbol": "SPY", "effective_date": day, "source_id": "CONTROL", "currency": "USD",
                  "available_at": day + "T21:05:00Z", "availability_basis": "modelled", "revision_status": "unknown"}
        artifact["actions"] = [{**common, "id": "split", "type": "split", "numerator": 2, "denominator": 1},
                               {**common, "id": "div", "type": "dividend", "gross_per_share": "1", "share_basis": "post_split", "pay_date": "2021-02-02"}]
        first = self.generate(artifact)["decisions"][0]
        # Hand answer: 100 * 2 * (49 + 1) / 100 = 100, equality stays cash.
        self.assertEqual("100", first["index_fraction"]["SPY"])
        self.assertEqual("100", first["sma_fraction"]["SPY"])
        self.assertEqual("0", first["weights"]["SPY"])

    def test_first_close_anchor_does_not_invent_pre_anchor_return(self):
        changed = copy.deepcopy(self.artifact)
        changed["actions"] = [{"id": "first-div", "symbol": "SPY", "effective_date": "2020-01-01",
              "type": "dividend", "gross_per_share": "1", "share_basis": "post_split",
              "available_at": "2020-01-01T21:05:00Z", "availability_basis": "modelled", "revision_status": "unknown"}]
        first = self.generate(changed)["decisions"][0]
        self.assertEqual("100", first["index_fraction"]["SPY"])
        self.assertEqual("100", first["sma_fraction"]["SPY"])
        self.assertEqual("0", first["weights"]["SPY"])

    def test_actual_positive_output_changes(self):
        changed = copy.deepcopy(self.artifact)
        for bar in changed["bars"]:
            if bar["trade_date"] == "2020-10-30" and bar["symbol"] == "SPY":
                bar.update(open="200", high="200", low="200", close="200")
        first = self.generate(changed)["decisions"][0]
        self.assertEqual("200", first["index_fraction"]["SPY"])
        self.assertEqual("110", first["sma_fraction"]["SPY"])
        self.assertEqual("0.25", first["weights"]["SPY"])
        self.assertNotEqual(self.generate()["intents"][0]["decision_hash"], self.generate(changed)["intents"][0]["decision_hash"])

    def test_after_close_before_open_allowed(self):
        changed = copy.deepcopy(self.artifact)
        for bar in changed["bars"]:
            if bar["trade_date"] == "2020-10-30":
                bar["available_at"] = "2020-11-02T14:29:59Z"
        first = self.generate(changed)["decisions"][0]
        self.assertEqual("2020-11-02T14:29:59Z", first["available_at"])
        self.assertEqual("2020-11-02", first["execution_date"])

    def test_late_old_component_skips_original_due_without_auto_move(self):
        changed = copy.deepcopy(self.artifact)
        changed["bars"][0]["available_at"] = "2020-11-02T14:30:00Z"
        report = self.generate(changed)
        late = next(r for r in report["skipped"] if r["reason"] == "COMPONENT_NOT_AVAILABLE_BEFORE_PLANNED_OPEN")
        self.assertEqual("2020-11-02", late["execution_date"])
        self.assertEqual("2020-01-01", late["late_components"][0]["date"])
        self.assertFalse(any(i["decision_date"] == "2020-10-30" for i in report["intents"]))
        self.assertEqual("2020-11-30", report["decisions"][0]["date"])
        c3 = self.generate(changed, delay_sessions=1)
        self.assertEqual("2020-11-03", c3["decisions"][0]["execution_date"])

    def test_future_perturbation_and_truncation_preserve_earlier_decision(self):
        prefix = self.generate()["decisions"][0]
        changed = copy.deepcopy(self.artifact)
        for bar in changed["bars"]:
            if bar["trade_date"] > "2020-11-02":
                bar.update(open="999", high="999", low="999", close="999")
        self.assertEqual(prefix, self.generate(changed)["decisions"][0])
        truncated = copy.deepcopy(self.artifact)
        truncated["calendar"] = [r for r in truncated["calendar"] if r["trade_date"] <= "2020-11-02"]
        truncated["bars"] = [r for r in truncated["bars"] if r["trade_date"] <= "2020-11-02"]
        report = s.generate(truncated, "S", trade_start="2020-10-01", end_date="2020-11-02")
        self.assertEqual(prefix, report["decisions"][0])

    def test_missing_session_inventory_disagreement_skips(self):
        changed = copy.deepcopy(self.artifact)
        changed["calendar"] = [r for r in changed["calendar"] if r["trade_date"] != "2020-05-06"]
        changed["bars"] = [r for r in changed["bars"] if r["trade_date"] != "2020-05-06"]
        report = self.generate(changed)
        self.assertFalse(report["intents"])
        self.assertTrue(any(r["reason"] == "INCOMPLETE_MONTH_CALENDAR" and r["month"] == "2020-05" for r in report["skipped"]))

    def test_whole_month_gap_not_ten_sparse_samples(self):
        changed = copy.deepcopy(self.artifact)
        changed["calendar"] = [r for r in changed["calendar"] if r["trade_date"][:7] != "2020-05"]
        changed["bars"] = [r for r in changed["bars"] if r["trade_date"][:7] != "2020-05"]
        report = self.generate(changed)
        self.assertFalse(report["intents"])
        self.assertTrue(any(r["reason"] == "CALENDAR_MONTH_GAP" for r in report["skipped"]))

    def test_sparse_month_end_only_calendar_is_not_full_months(self):
        changed = copy.deepcopy(self.artifact)
        ends = {r["month_end_session"] for r in changed["signal_calendar"]["inventory"].values()}
        ends.add("2021-01-04")
        changed["calendar"] = [r for r in changed["calendar"] if r["trade_date"] in ends]
        changed["bars"] = [r for r in changed["bars"] if r["trade_date"] in ends]
        report = s.generate(changed, "S", trade_start="2020-10-30", end_date="2020-12-31")
        self.assertFalse(report["intents"])
        self.assertEqual(12, sum(r["reason"] == "INCOMPLETE_MONTH_CALENDAR" for r in report["skipped"]))

    def test_late_action_and_future_effective_action_causality(self):
        changed = copy.deepcopy(self.artifact)
        action = {"id": "late-div", "symbol": "SPY", "effective_date": "2020-01-02",
                  "type": "dividend", "gross_per_share": "1", "share_basis": "post_split",
                  "available_at": "2020-11-02T14:30:00Z", "availability_basis": "observed", "revision_status": "as_of_archive"}
        changed["actions"] = [action]
        report = self.generate(changed)
        self.assertFalse(any(r["decision_date"] == "2020-10-30" for r in report["intents"]))
        late = next(r for r in report["skipped"] if r["reason"] == "COMPONENT_NOT_AVAILABLE_BEFORE_PLANNED_OPEN")
        self.assertEqual("action", late["late_components"][0]["kind"])
        future = copy.deepcopy(self.artifact)
        action.update(id="future-div", effective_date="2020-11-02", available_at="2020-01-01T21:05:00Z")
        future["actions"] = [action]
        first = self.generate(future)["decisions"][0]
        self.assertEqual("100", first["index_fraction"]["SPY"])
        self.assertEqual("0", first["weights"]["SPY"])
        second = self.generate(future)["decisions"][1]
        self.assertEqual("101", second["index_fraction"]["SPY"])
        self.assertEqual("0.25", second["weights"]["SPY"])

    def test_historical_label_preserved_and_static_clock_ignores_price_features(self):
        changed = copy.deepcopy(self.artifact)
        changed["data_kind"] = "historical_market"
        changed["signal_calendar"]["review"]["source_nature"] = "historical_market"
        changed["bars"][0]["available_at"] = "2021-02-01T00:00:00Z"
        report = self.generate(changed, variant="B0")
        self.assertEqual("historical_market", report["data_kind"])
        self.assertTrue(all(i["data_kind"] == "historical_market" for i in report["intents"]))
        self.assertEqual("2020-09-30", report["intents"][0]["decision_date"])
        self.assertEqual("2020-09-30T21:00:00Z", report["intents"][0]["available_at"])
        self.assertFalse(report["historical_engine_ready"])

    def test_terminal_month_without_next_session_preserves_prior(self):
        changed = copy.deepcopy(self.artifact)
        changed["calendar"] = [r for r in changed["calendar"] if r["trade_date"] <= "2020-12-31"]
        changed["bars"] = [r for r in changed["bars"] if r["trade_date"] <= "2020-12-31"]
        report = self.generate(changed)
        self.assertEqual(self.generate()["decisions"][:2], report["decisions"][:2])
        self.assertEqual("NO_NEXT_SESSION", report["unexecuted_intents"][-1]["status"])

    def test_static_comparators_and_cent_grid(self):
        expected = {"B0": "0.25", "BR": "0.0875", "BC": "0"}
        for variant, weight in expected.items():
            result = self.generate(variant=variant, **({"k": "0.35"} if variant == "BR" else {}))
            self.assertEqual({a: weight for a in s.ASSETS}, result["intents"][0]["weights"])
            self.assertIsNone(result["decisions"][0]["index_fraction"])
        for k in ("0.351", "1.01", "-0.01", True, None):
            with self.assertRaises(ContractError): self.generate(variant="BR", k=k)
        with self.assertRaises(ContractError): self.generate(k="0.2")

    def test_missing_or_tampered_completeness_declaration_rejected(self):
        for mode in ("absent", "inventory", "provenance"):
            changed = copy.deepcopy(self.artifact)
            if mode == "absent": changed.pop("signal_calendar")
            if mode == "inventory": changed["signal_calendar"]["inventory"]["2020-01"]["expected_sessions"].pop()
            if mode == "provenance": changed["signal_calendar"]["review"]["source_nature"] = "historical_market"
            with self.assertRaises(ContractError): self.generate(changed)


if __name__ == "__main__":
    unittest.main()
