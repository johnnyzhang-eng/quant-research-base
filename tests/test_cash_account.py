import copy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from research_base.cash_account import (CashAccount, CashContractError, EXPECTED_CONTROL_IDS,
    _control_quote, _control_rules, control_report_accepted, loads_rules, run_controls,
    validate_control_report)


class CashInstrumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.report = run_controls(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_fixed_known_answers(self):
        self.assertTrue(self.report["accepted"], self.report["completeness"])
        self.assertEqual(len(self.report["reports"]), 40)
        self.assertEqual(len(EXPECTED_CONTROL_IDS), 40)
        self.assertFalse(self.report["funding_certified"])
        self.assertFalse(self.report["historical_return_experiment"])

    def test_missing_and_duplicate_row(self):
        report = copy.deepcopy(self.report)
        missing = report["reports"].pop()
        self.assertFalse(control_report_accepted(report))
        self.assertIn(missing["id"], validate_control_report(report)["missing_ids"])
        report = copy.deepcopy(self.report)
        report["reports"].append(report["reports"][0])
        self.assertFalse(control_report_accepted(report))

    def test_wrong_actual_cannot_relabel_expected(self):
        report = copy.deepcopy(self.report)
        report["reports"][0].update(expected=["0", "9999", "7.07"], actual=["0", "9999", "7.07"], passed=True)
        self.assertFalse(control_report_accepted(report))

    def test_type_and_classification_forgery(self):
        report = copy.deepcopy(self.report)
        row = next(r for r in report["reports"] if r["id"] == "missing_rate_rejected")
        row["actual"] = 1
        self.assertFalse(control_report_accepted(report))
        report = copy.deepcopy(self.report)
        report["classification"] = "FORMAL_HISTORICAL_RETURNS"
        self.assertFalse(control_report_accepted(report))


class CashAccountTests(unittest.TestCase):
    def setUp(self):
        self.at = "2026-01-30T12:00:00+00:00"
        self.rules = _control_rules("7077")
        self.account = CashAccount(self.rules)

    def test_unknown_fields_and_bad_versions_rejected(self):
        for path in ["root", "funding", "tier"]:
            rule = copy.deepcopy(self.rules)
            target = rule if path == "root" else (rule["funding_evidence"] if path == "funding" else rule["interest_schedules"][0]["tiers"][0])
            target["future_magic"] = True
            with self.assertRaises(CashContractError):
                CashAccount(rule)
        rule = copy.deepcopy(self.rules)
        rule["schema_version"] = "cash-account.v2"
        with self.assertRaises(CashContractError):
            CashAccount(rule)

    def test_noncanonical_date_cannot_select_stale_rate(self):
        rule = _control_rules("36500", crediting="daily")
        next_rate = copy.deepcopy(rule["interest_schedules"][0])
        next_rate["effective_date"] = "2026-01-31"
        next_rate["tiers"][0]["net_annual_rate"] = "0.073"
        rule["interest_schedules"].append(next_rate)
        aliased = copy.deepcopy(rule)
        aliased["interest_schedules"][0]["effective_date"] = "20260130"
        with self.assertRaisesRegex(CashContractError, "noncanonical"):
            CashAccount(aliased)
        account = CashAccount(rule)
        account.accrue_day("2026-01-30", as_of=account.cutoff_at("2026-01-30"))
        credited = account.accrue_day("2026-01-31", as_of=account.cutoff_at("2026-01-31"))["credited"]["CNY"]
        self.assertEqual(Decimal(credited), Decimal("7.30"))
        with self.assertRaisesRegex(CashContractError, "noncanonical"):
            account.cutoff_at("20260201")

    def test_funding_is_required_not_default_confirmed(self):
        rule = copy.deepcopy(self.rules)
        rule["classification"] = "conditional_historical_model"
        with self.assertRaisesRegex(CashContractError, "funding path"):
            CashAccount(rule)
        rule = copy.deepcopy(self.rules)
        rule["funding_evidence"]["lawful_and_usable"] = False
        with self.assertRaises(CashContractError):
            CashAccount(rule)
        del rule["funding_evidence"]
        with self.assertRaises(CashContractError):
            CashAccount(rule)

    def test_failed_exchange_and_future_quote_are_atomic(self):
        before = copy.deepcopy(self.account.balances())
        quote = _control_quote(self.at, execution=True)
        with self.assertRaises(CashContractError):
            self.account.exchange("CNY_TO_USD", "7078", execution_quote=quote, as_of=self.at, event_id="too-big")
        future = _control_quote("2026-01-31T00:00:00+00:00", execution=True)
        with self.assertRaises(CashContractError):
            self.account.exchange("CNY_TO_USD", "100", execution_quote=future, as_of=self.at, event_id="future")
        self.assertEqual(self.account.balances(), before)
        self.assertEqual(len(self.account.events), 1)

    def test_interest_failure_is_atomic_for_both_wallets(self):
        rule = _control_rules("1", crediting="daily")
        rule["interest_schedules"][0]["tiers"][0]["net_annual_rate"] = "-730"
        account = CashAccount(rule)
        before = account.balances()
        with self.assertRaisesRegex(CashContractError, "negative wallet"):
            account.accrue_day("2026-01-30", as_of=account.cutoff_at("2026-01-30"))
        self.assertEqual(account.balances(), before)
        self.assertEqual(len(account.events), 1)

    def test_marking_does_not_liquidate_or_exchange(self):
        quote = _control_quote(self.at, execution=True)
        self.account.exchange("CNY_TO_USD", "7070", execution_quote=quote, as_of=self.at, event_id="entry")
        before = self.account.balances()
        length = len(self.account.events)
        snapshot = self.account.snapshot(as_of=self.at, fx_mark=_control_quote(self.at), usd_assets="9000", exit_quote=quote)
        self.assertIsNone(snapshot["exit_equity_cny"])
        self.assertEqual(snapshot["usd_external_assets"], "9000")
        self.assertEqual(self.account.balances(), before)
        self.assertEqual(len(self.account.events), length)

    def test_unpaid_interest_and_external_receivables_cannot_fund_exchange(self):
        self.account.sync_usd(settled_usd="0", receivables_usd="1000", as_of=self.at, event_id="receivable")
        with self.assertRaises(CashContractError):
            self.account.exchange("USD_TO_CNY", "1", execution_quote=_control_quote(self.at, execution=True), as_of=self.at, event_id="unavailable")
        account = CashAccount(_control_rules("36500"))
        account.accrue_day("2026-01-30", as_of=account.cutoff_at("2026-01-30"))
        self.assertEqual(account.balances()["CNY"]["available"], "36500")
        self.assertEqual(account.balances()["CNY"]["unpaid_interest"], "3.65")

    def test_unknown_fx_fields_and_negative_stress_rejected(self):
        quote = _control_quote(self.at, execution=True)
        quote["unknown_fee"] = "0"
        with self.assertRaises(CashContractError):
            self.account.exchange("CNY_TO_USD", "1", execution_quote=quote, as_of=self.at, event_id="unknown")
        with self.assertRaises(CashContractError):
            self.account.exchange("CNY_TO_USD", "1", execution_quote=_control_quote(self.at, execution=True), as_of=self.at, event_id="negative", extra_spread_rate="-0.01")

    def test_rules_and_returned_events_are_defensive_copies(self):
        self.rules["initial_cny"] = "99999999"
        self.assertEqual(self.account.settled["CNY"], Decimal("7077"))
        result = self.account.exchange("CNY_TO_USD", "7070", execution_quote=_control_quote(self.at, execution=True), as_of=self.at, event_id="entry")
        result["events"][0]["quote"]["cny_per_usd"] = "999"
        self.assertEqual(self.account.events[-1]["quote"]["cny_per_usd"], "7")

    def test_replay_or_reverse_cannot_double_debit(self):
        quote = _control_quote(self.at, execution=True)
        self.account.exchange("CNY_TO_USD", "7070", execution_quote=quote, as_of=self.at, event_id="entry")
        before = self.account.balances()
        with self.assertRaises(CashContractError):
            self.account.exchange("CNY_TO_USD", "1", execution_quote=quote, as_of=self.at, event_id="entry")
        with self.assertRaises(CashContractError):
            self.account.sync_usd(settled_usd="0", receivables_usd="0", as_of=self.rules["start_at"], event_id="reverse")
        self.assertEqual(self.account.balances(), before)

    def test_negative_unpaid_interest_is_reserved_until_monthend_posting(self):
        rule = _control_rules("100", start="2026-01-01")
        rule["interest_schedules"][0]["tiers"][0]["net_annual_rate"] = "-0.365"
        account = CashAccount(rule)
        for day in range(1, 31):
            d = "2026-01-" + str(day).zfill(2)
            account.accrue_day(d, as_of=account.cutoff_at(d))
        self.assertEqual(Decimal(account.balances()["CNY"]["available"]), Decimal("97"))
        self.assertEqual(account.settled["CNY"], Decimal("100"))
        quote = _control_quote(account.cutoff_at("2026-01-30"), execution=True)
        quote.update(buy_usd_spread_bps="0", sell_usd_spread_bps="0", buy_fixed_fee_cny="0", sell_fixed_fee_usd="0")
        with self.assertRaisesRegex(CashContractError, "insufficient"):
            account.exchange("CNY_TO_USD", "100", execution_quote=quote, as_of=account.cutoff_at("2026-01-30"), event_id="unsafe")
        account.accrue_day("2026-01-31", as_of=account.cutoff_at("2026-01-31"))
        self.assertEqual(account.settled["CNY"], Decimal("96.90"))
        self.assertEqual(account.unpaid_interest["CNY"], Decimal("0"))

    def test_counterfactual_cannot_backfill_future_fx_price(self):
        rule = _control_rules("100", start="2006-01-01")
        rule["knowledge_clock"]["basis"] = "frozen_current_counterfactual"
        account = CashAccount(rule)
        current_quote = _control_quote("2026-10-01T00:00:00+00:00")
        with self.assertRaisesRegex(CashContractError, "future"):
            account.snapshot(as_of="2006-01-01T12:00:00+00:00", fx_mark=current_quote, usd_assets="0")
        current_quote["quoted_at"] = "2006-01-01T00:00:00+00:00"
        snapshot = account.snapshot(as_of="2006-01-01T12:00:00+00:00", fx_mark=current_quote, usd_assets="0")
        self.assertEqual(snapshot["knowledge_clock"]["basis"], "frozen_current_counterfactual")

    def test_duplicate_json_key_rejected(self):
        with self.assertRaises(CashContractError):
            loads_rules('{"schema_version":"cash-account.v1","schema_version":"cash-account.v2"}')


if __name__ == "__main__":
    unittest.main()
