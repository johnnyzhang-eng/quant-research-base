import copy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research_base.evidence import ContractError, canonical_hash
from research_base.historical_account_controls import run_controls
from research_base.historical_account import execute
from research_base.historical_contract import prepare
from research_base.historical_signals import generate
from research_base.historical_windows import report_windows, compare_windows
from tests.study_fixture import write_study_fixture


@unittest.skipUnless(importlib.util.find_spec("backtest") is not None,
                     "requires an existing Vibe installation")
class HistoricalWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name) / "native-controls"
        controls = run_controls(cls.root)
        if not controls["accepted"]:
            raise AssertionError(controls["reports"])

    def case(self, name):
        return tuple(json.loads((self.root / name / file).read_text())
                     for file in ("account-input.json", "native-result.json"))

    def windows(self, name="baseline"):
        return report_windows(*self.case(name), {
            "full": {"start": "2020-10-01", "end": "2020-11-03"},
            "development": {"start": "2020-10-01", "end": "2020-10-16"},
            "validation": {"start": "2020-10-19", "end": "2020-10-30"},
            "confirmation": {"start": "2020-11-02", "end": "2020-11-03"},
        })

    def test_continuous_previous_session_boundary_without_new_account(self):
        data, result = self.case("baseline")
        report = self.windows()
        self.assertEqual(report["input_sha256"], canonical_hash(data))
        self.assertEqual(report["result_sha256"], canonical_hash(result))
        validation = report["windows"]["validation"]
        self.assertEqual(validation["boundary_date"], "2020-10-16")
        self.assertEqual(validation["currencies"]["CNY"]["ledger"]["observations"][0],
                         {"date": "2020-10-16", "equity": "14000.00000000", "external_flow_end": "0"})
        self.assertEqual(validation["currencies"]["CNY"]["metrics"]["fill_count"], 0)
        self.assertEqual(validation["execution_diagnostics"]["session_mean_exposure"]["actual_risky_weight"]["value_exact_fraction"], "1/2")
        for value in report["windows"].values():
            self.assertTrue(all(row["external_flow_end"] == "0" for row in value["currencies"]["CNY"]["ledger"]["observations"]))
        self.assertFalse(report["historical_engine_ready"])

    def test_whole_usd_numeraire_and_cny_risk_free_translation(self):
        window = self.windows("fx_move")["windows"]["full"]
        usd = window["currencies"]["USD"]
        self.assertEqual(Decimal(usd["metrics"]["initial_equity"]), Decimal("2000"))
        self.assertEqual(Decimal(usd["metrics"]["final_equity"]), Decimal("1875"))
        self.assertEqual(usd["metrics"]["period_net_return"]["value"], -0.0625)
        self.assertEqual(usd["ledger"]["risk_free"]["period_returns"][0]["return"], "-0.125")
        self.assertEqual(window["fx_decomposition"]["rows"][0]["exact_fractions"],
                         {"domestic_cny_change": "-7000", "usd_asset_change_at_prior_fx": "7000",
                          "fx_change_on_prior_usd": "0", "cross_term": "1000", "cny_change": "1000",
                          "reported_nav_rounding_residual": "0"})

    def test_daily_exact_decomposition_sums_to_whole_window(self):
        report = self.windows("dividend")
        full = report["windows"]["full"]
        self.assertEqual(full["fx_decomposition"]["totals_exact_fractions"]["cny_change"], "-7")
        metrics = full["currencies"]["CNY"]["metrics"]
        self.assertEqual(Decimal(metrics["monetary_profit"]), Decimal("-7"))
        self.assertEqual(full["execution_diagnostics"]["additional_cost_disclosures_cny_exact"]["dividend_withholding"], "7")
        # Payment changes settled/receivable form, not economic whole NAV.
        payment = next(row for row in full["fx_decomposition"]["rows"] if row["date"] == "2020-10-19")
        self.assertEqual(payment["exact_fractions"]["cny_change"], "0")

    def test_entry_cost_only_first_window_and_no_second_deduction(self):
        report = self.windows("c3")
        development = report["windows"]["development"]["currencies"]["CNY"]
        later = report["windows"]["validation"]["currencies"]["CNY"]
        self.assertEqual([row["id"] for row in development["ledger"]["cost_events"]], ["entry-fx-cost"])
        self.assertEqual(later["ledger"]["cost_events"], [])
        self.assertEqual(Decimal(development["metrics"]["final_equity"]),
                         Decimal(development["ledger"]["observations"][-1]["equity"]))
        self.assertGreater(Decimal(development["metrics"]["cost_total"]), 0)

    def test_beta_and_ten_percent_gate_defined_or_explicitly_undefined(self):
        positive = self.windows("fx_move")
        comparison = compare_windows(positive, positive)
        self.assertEqual(comparison["windows"]["full"]["CNY"]["beta_s_vs_br"]["value"], "1")
        self.assertTrue(comparison["windows"]["full"]["CNY"]["risk_matched"])
        self.assertFalse(comparison["windows"]["validation"]["CNY"]["risk_matched"])
        self.assertEqual(comparison["windows"]["validation"]["CNY"]["beta_s_vs_br"]["reason"], "ZERO_BR_RETURN_VARIANCE")
        one = report_windows(*self.case("baseline"), {"single": {"start": "2020-10-01", "end": "2020-10-01"}})
        self.assertEqual(one["windows"]["single"]["currencies"]["CNY"]["metric_completeness"], "PARTIALLY_DEFINED")
        self.assertIn("volatility_annual", one["windows"]["single"]["currencies"]["CNY"]["undefined_metrics"])

    def test_missing_boundary_calendar_or_tampered_tape_is_rejected(self):
        data, result = self.case("baseline")
        for bounds in ({"start": "2020-10-17", "end": "2020-10-30"},
                       {"start": "2020-10-30", "end": "2020-10-01"},
                       {"start": "2020-09-30", "end": "2020-10-30"}):
            with self.assertRaises(ContractError):
                report_windows(data, result, {"invalid": bounds})
        changed = copy.deepcopy(result)
        changed["cny_snapshots"].pop(3)
        with self.assertRaises(ContractError):
            report_windows(data, changed, {"full": {"start": "2020-10-01", "end": "2020-11-03"}})
        changed = copy.deepcopy(result)
        changed["cny_snapshots"][0]["marked_equity_cny"] = "14001"
        with self.assertRaises(ContractError):
            report_windows(data, changed, {"full": {"start": "2020-10-01", "end": "2020-11-03"}})

    def test_future_window_does_not_change_earlier_output_and_mutated_comparison_rejected(self):
        data, result = self.case("fx_move")
        first = {"development": {"start": "2020-10-01", "end": "2020-10-16"}}
        a = report_windows(data, result, first)
        b = self.windows("fx_move")
        self.assertEqual(a["windows"]["development"], b["windows"]["development"])
        changed = copy.deepcopy(b)
        changed["windows"]["full"]["currencies"]["CNY"]["metrics"]["volatility_annual"]["value"] = 999
        with self.assertRaises(ContractError):
            compare_windows(b, changed)
        changed = copy.deepcopy(b)
        changed["windows"]["full"]["currencies"]["USD"]["metrics"]["calendar"].pop()
        with self.assertRaises(ContractError):
            compare_windows(b, changed)

    def test_cash_background_is_independently_checked_and_cannot_gift_yield(self):
        data, result = self.case("baseline")
        bounds = {"full": {"start": "2020-10-01", "end": "2020-11-03"}}
        report = report_windows(data, result, bounds)
        self.assertTrue(report["background_replay"]["passed"])
        for mutation in (lambda r: r["cny_cash_background_snapshots"][0].update(marked_equity_cny="14001"),
                         lambda r: r["cny_cash_background_events"].pop(1),
                         lambda r: r["cny_cash_background_events"][1].update(credited="1")):
            changed = copy.deepcopy(result)
            mutation(changed)
            with self.assertRaises(ContractError):
                report_windows(data, changed, bounds)

    def test_v2_paid_cny_and_unpaid_usd_costs_net_report_without_boundary_reset(self):
        baseline, _ = self.case("baseline")
        artifact, model = copy.deepcopy(baseline["historical_artifact"]), copy.deepcopy(baseline["historical_model"])
        model["schema_version"] = "historical-account-model/2"
        evidence = copy.deepcopy(model["cash_rules"]["funding_evidence"]["evidence"])
        expenses = [{"schema_version": "fixed-expense/1", "id": identity, "currency": currency,
                     "amount": amount, "book_at": "2020-10-01T23:59:59+00:00", "category": "fixed",
                     "evidence": copy.deepcopy(evidence)}
                    for identity, currency, amount in (("cny-data", "CNY", "70"), ("usd-server", "USD", "5"))]
        model.update(fixed_expenses=expenses, expense_review={"schema_version": "fixed-expense-review/1",
            "reviewer": "invented window control author", "scope": "paid CNY and unpaid USD obligation control",
            "reviewed_at": model["cash_rules"]["knowledge_clock"]["freeze_at"],
            "coverage_start_date": model["cash_rules"]["start_at"][:10], "coverage_end_date": model["end"],
            "explicit_zero": False, "schedule_sha256": canonical_hash(expenses), "evidence": evidence})
        features = generate(artifact, "B0", trade_start=model["start"], end_date=model["end"])
        data = prepare(artifact, features, model)
        result = execute(data)
        report = report_windows(data, result, {"full": {"start": "2020-10-01", "end": "2020-11-03"},
                                              "later": {"start": "2020-10-19", "end": "2020-10-30"}})
        first = result["cny_snapshots"][0]
        self.assertEqual(Decimal(first["marked_equity_cny"]), Decimal("13895"))
        self.assertEqual(Decimal(first["usd_total_equity"]), Decimal("995"))
        self.assertEqual(Decimal(first["wallets"]["USD"]["fixed_expense_owed"]), Decimal("5"))
        self.assertEqual(Decimal(first["wallets"]["CNY"]["settled"]), Decimal("6930"))
        self.assertTrue(report["independent_replay"]["passed"])
        self.assertTrue(report["background_replay"]["passed"])
        full, later = report["windows"]["full"], report["windows"]["later"]
        self.assertEqual(full["fx_decomposition"]["rows"][0]["exact_fractions"],
                         {"domestic_cny_change": "-7070", "usd_asset_change_at_prior_fx": "6965",
                          "fx_change_on_prior_usd": "0", "cross_term": "0", "cny_change": "-105",
                          "reported_nav_rounding_residual": "0"})
        self.assertEqual(Decimal(full["currencies"]["CNY"]["metrics"]["cost_totals"]["fixed"]), Decimal("105"))
        self.assertEqual(Decimal(full["currencies"]["USD"]["metrics"]["cost_totals"]["fixed"]), Decimal("15"))
        self.assertEqual(Decimal(full["currencies"]["CNY"]["metrics"]["final_equity"]), Decimal("13895"))
        self.assertEqual(later["boundary_date"], "2020-10-16")
        self.assertEqual(Decimal(later["currencies"]["CNY"]["metrics"]["initial_equity"]), Decimal("13895"))
        self.assertEqual(later["currencies"]["CNY"]["ledger"]["cost_events"], [])
        self.assertEqual(later["currencies"]["CNY"]["ledger"]["risk_free"]["period_returns"][0]["return"], "0")
        self.assertEqual(Decimal(result["cny_cash_background_snapshots"][-1]["marked_equity_cny"]), Decimal("14000"))
        # A second actual run exercises nonzero CNY yield, including the prior
        # boundary day's cutoff, independently of the portfolio's expense debit.
        for schedule in model["cash_rules"]["interest_schedules"]:
            if schedule["currency"] == "CNY":
                schedule["tiers"][0]["net_annual_rate"] = "0.0365"
        data_with_yield = prepare(artifact, features, model)
        result_with_yield = execute(data_with_yield)
        yield_report = report_windows(data_with_yield, result_with_yield,
                                      {"first": {"start": "2020-10-01", "end": "2020-10-01"}})
        self.assertTrue(yield_report["background_replay"]["passed"])
        self.assertEqual(Decimal(result_with_yield["cny_cash_background_snapshots"][0]["marked_equity_cny"]), Decimal("14002.80"))
        self.assertEqual(Decimal(result_with_yield["cny_snapshots"][0]["marked_equity_cny"]), Decimal("13897.09"))
        self.assertEqual(Decimal(yield_report["windows"]["first"]["currencies"]["CNY"]["ledger"]["risk_free"]["period_returns"][0]["return"]), Decimal("0.0002"))

    def test_actual_partial_remainder_list_and_single_cancel_reason(self):
        manifest_path = write_study_fixture(Path(self.temp.name) / "partial-remainder-input")
        manifest = json.loads(manifest_path.read_text())
        artifact = json.loads((manifest_path.parent / manifest["artifact"]["file"]).read_text())
        model = json.loads((manifest_path.parent / manifest["models"]["50000-C0"]["file"]).read_text())
        features = generate(artifact, "S", trade_start=model["start"], end_date=model["end"])
        data = prepare(artifact, features, model)
        result = execute(data)
        partials = [e for e in result["events"] if e["type"] == "ORDER_REMAINDER_CANCEL"]
        self.assertTrue(partials)
        first = partials[0]
        self.assertEqual((first["symbol"], first["unfilled_qty"], first["reasons"]),
                         ("GLD", 1, ["INSUFFICIENT_SETTLED_CASH"]))
        self.assertTrue(any(e["type"] == "ORDER_CANCEL" and e["reason"] == "INSUFFICIENT_SETTLED_CASH" for e in result["events"]))
        report = report_windows(data, result, {"full": {"start": "2020-11-02", "end": "2021-01-29"}})
        counts = report["windows"]["full"]["execution_diagnostics"]["cancellations_by_reason"]
        self.assertGreaterEqual(counts["INSUFFICIENT_SETTLED_CASH"], 2)
        self.assertTrue(report["independent_replay"]["passed"])

    def test_inherited_bankruptcy_retains_negative_and_later_positive_boundary(self):
        baseline, _ = self.case("baseline")
        artifact, model = copy.deepcopy(baseline["historical_artifact"]), copy.deepcopy(baseline["historical_model"])
        for bar in artifact["bars"]:
            if bar["trade_date"] >= "2020-10-19":
                bar.update(open="900", high="900", low="900", close="900")
        model["schema_version"] = "historical-account-model/2"
        evidence = copy.deepcopy(model["cash_rules"]["funding_evidence"]["evidence"])
        costs = [{"schema_version": "fixed-expense/1", "id": "oversized-liability", "currency": "CNY",
                  "amount": "20000", "book_at": "2020-10-01T23:59:59+00:00", "category": "fixed", "evidence": evidence}]
        model.update(fixed_expenses=costs, expense_review={"schema_version": "fixed-expense-review/1",
            "reviewer": "invented insolvency window author", "scope": "unpaid obligation and later appreciated stocks",
            "reviewed_at": model["cash_rules"]["knowledge_clock"]["freeze_at"],
            "coverage_start_date": model["cash_rules"]["start_at"][:10], "coverage_end_date": model["end"],
            "explicit_zero": False, "schedule_sha256": canonical_hash(costs), "evidence": evidence})
        features = generate(artifact, "B0", trade_start=model["start"], end_date=model["end"])
        data = prepare(artifact, features, model)
        result = execute(data)
        report = report_windows(data, result, {"full": {"start": "2020-10-01", "end": "2020-11-03"},
            "negative-boundary": {"start": "2020-10-19", "end": "2020-10-19"},
            "positive-again": {"start": "2020-10-20", "end": "2020-10-30"}})
        self.assertTrue(report["independent_replay"]["passed"])
        negative = report["windows"]["negative-boundary"]["currencies"]["CNY"]
        positive = report["windows"]["positive-again"]["currencies"]["CNY"]
        self.assertEqual(Decimal(negative["metrics"]["initial_equity"]), Decimal("-6000"))
        self.assertEqual(Decimal(negative["metrics"]["final_equity"]), Decimal("50000"))
        self.assertEqual(Decimal(positive["metrics"]["initial_equity"]), Decimal("50000"))
        self.assertEqual(report["continuous_insolvency_history"]["CNY"][0]["date"], "2020-10-01")
        for record in (negative, positive):
            self.assertEqual(record["state"], "INHERITED_INSOLVENCY")
            self.assertEqual(record["metric_completeness"], "INHERITED_INSOLVENCY")
            self.assertEqual(record["metrics"]["period_net_return"],
                             {"value": None, "status": "UNDEFINED", "reason": "INHERITED_INSOLVENCY"})
            self.assertTrue(all(row["net_return"] is None for row in record["metrics"]["timeline"]))
            self.assertEqual(record["ledger"]["calendar"], record["metrics"]["calendar"])
        comparison = compare_windows(report, report)
        self.assertEqual(comparison["windows"]["positive-again"]["CNY"]["beta_s_vs_br"]["reason"], "INHERITED_INSOLVENCY")
        self.assertFalse(comparison["windows"]["negative-boundary"]["CNY"]["risk_matched"])
        for mutation in (lambda r: r["continuous_insolvency_history"]["CNY"].clear(),
                         lambda r: r["windows"]["positive-again"]["currencies"]["CNY"].update(state="NORMAL"),
                         lambda r: r["windows"]["negative-boundary"]["currencies"]["CNY"]["metrics"].update(initial_equity="6000")):
            changed = copy.deepcopy(report)
            mutation(changed)
            with self.assertRaises(ContractError):
                compare_windows(report, changed)


if __name__ == "__main__":
    unittest.main()
