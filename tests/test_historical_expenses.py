import copy
import unittest
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from decimal import Decimal

from research_base.cash_account import CashAccount, CashContractError, _control_rules, _control_quote
from research_base.evidence import canonical_hash
from research_base.historical_expenses import ExpenseWallet


def expense_inputs(rules, rows=None):
    rows = copy.deepcopy(rows or [])
    review = {"schema_version": "fixed-expense-review/1", "reviewer": "invented control author",
              "scope": "private invented fixed expenses; no personal bill certification",
              "reviewed_at": rules["knowledge_clock"]["freeze_at"],
              "coverage_start_date": "2026-01-30", "coverage_end_date": "2026-02-05",
              "explicit_zero": not rows, "schedule_sha256": canonical_hash(rows),
              "evidence": copy.deepcopy(rules["funding_evidence"]["evidence"])}
    return review, rows


def row(rules, expense_id="subscription", amount="10", currency="USD", day="2026-01-30"):
    return {"schema_version": "fixed-expense/1", "id": expense_id, "currency": currency,
            "amount": amount, "book_at": day + "T23:59:59+00:00", "category": "fixed",
            "evidence": copy.deepcopy(rules["funding_evidence"]["evidence"])}


class HistoricalExpenseTests(unittest.TestCase):
    def wallet(self, initial="100", rows=None, **rule_changes):
        rules = _control_rules(initial)
        rules.update(rule_changes)
        review, expenses = expense_inputs(rules, rows(rules) if callable(rows) else rows)
        return ExpenseWallet(rules, expense_review=review, fixed_expenses=expenses)

    def test_literal_cash_payment_and_owed_reduce_equity_once(self):
        wallet = self.wallet(rows=lambda r: [row(r, amount="150", currency="CNY")])
        at = wallet.cutoff_at("2026-01-30")
        result = wallet.book_expenses(as_of=at)
        self.assertEqual([e["type"] for e in result["events"]], ["FIXED_EXPENSE_BOOKED", "FIXED_EXPENSE_PAYMENT"])
        self.assertEqual((wallet.settled["CNY"], wallet.owed["CNY"], wallet.available("CNY")),
                         (Decimal(0), Decimal(50), Decimal(0)))
        snapshot = wallet.snapshot(as_of=at, fx_mark=_control_quote(at), usd_assets="0")
        self.assertEqual(Decimal(snapshot["marked_equity_cny"]), Decimal(-50))
        self.assertEqual(wallet.book_expenses(as_of=at)["events"], [])
        self.assertEqual(wallet.owed["CNY"], Decimal(50))

    def test_receivables_and_unpaid_positive_interest_do_not_pay_fee(self):
        wallet = self.wallet("0", rows=lambda r: [row(r)])
        at = wallet.cutoff_at("2026-01-30")
        wallet.sync_usd(settled_usd="5", receivables_usd="1000", as_of="2026-01-30T12:00:00+00:00", event_id="stock")
        wallet.unpaid_interest["USD"] = Decimal(3)
        first = wallet.book_expenses(as_of=at)
        self.assertEqual(Decimal(first["usd_paid_total"]), Decimal(5))
        self.assertEqual(wallet.owed["USD"], Decimal(5))
        self.assertEqual(wallet.receivables["USD"], Decimal(1000))
        self.assertEqual(wallet.unpaid_interest["USD"], Decimal(3))
        self.assertEqual(wallet.available("USD"), Decimal(0))

    def test_negative_unpaid_interest_reservation_and_payment_retry(self):
        wallet = self.wallet("0", rows=lambda r: [row(r)])
        at = wallet.cutoff_at("2026-01-30")
        wallet.sync_usd(settled_usd="8", receivables_usd="0", as_of="2026-01-30T12:00:00+00:00", event_id="stock")
        wallet.unpaid_interest["USD"] = Decimal(-3)
        result = wallet.book_expenses(as_of=at)
        self.assertEqual((Decimal(result["usd_paid_total"]), wallet.settled["USD"], wallet.owed["USD"]),
                         (Decimal(5), Decimal(3), Decimal(5)))
        self.assertEqual(wallet.available("USD"), Decimal(0))
        wallet.sync_usd(settled_usd="13", receivables_usd="0", as_of=at, event_id="settlement")
        retry = wallet.book_expenses(as_of=at)
        self.assertEqual(Decimal(retry["usd_paid_total"]), Decimal(5))
        self.assertEqual((wallet.settled["USD"], wallet.owed["USD"]), (Decimal(8), Decimal(0)))

    def test_book_and_pay_before_interest_and_retry_after_credit(self):
        rules = _control_rules("100", crediting="daily")
        rules["interest_schedules"][0]["tiers"][0]["net_annual_rate"] = "0.365"
        review, rows = expense_inputs(rules, [row(rules, amount="50", currency="CNY")])
        wallet = ExpenseWallet(rules, expense_review=review, fixed_expenses=rows)
        at = wallet.cutoff_at("2026-01-30")
        with self.assertRaisesRegex(CashContractError, "book fixed expenses"):
            wallet.accrue_day("2026-01-30", as_of=at)
        wallet.book_expenses(as_of=at)
        credited = wallet.accrue_day("2026-01-30", as_of=at)
        self.assertEqual(credited["credited"]["CNY"], "0.05")
        self.assertEqual(wallet.settled["CNY"], Decimal("50.05"))
        zero = self.wallet("0", rows=lambda r: [row(r, amount="8")])
        zero.rules["interest_schedules"][1]["crediting"] = "daily"
        zero.unpaid_interest["USD"] = Decimal(3)
        zero.sync_usd(settled_usd="5", receivables_usd="0", as_of="2026-01-30T12:00:00+00:00", event_id="stock")
        self.assertEqual(Decimal(zero.book_expenses(as_of=at)["usd_paid_total"]), Decimal(5))
        zero.accrue_day("2026-01-30", as_of=at)
        self.assertEqual(Decimal(zero.book_expenses(as_of=at)["usd_paid_total"]), Decimal(3))
        self.assertEqual(zero.owed["USD"], Decimal(0))

    def test_owed_reserves_fx_and_exit_and_restores_from_journal(self):
        wallet = self.wallet("100", rows=lambda r: [row(r, amount="150", currency="CNY")])
        at = wallet.cutoff_at("2026-01-30")
        wallet.book_expenses(as_of=at)
        quote = _control_quote(at, execution=True)
        with self.assertRaisesRegex(CashContractError, "liabilities reserve"):
            wallet.exchange("CNY_TO_USD", "1", execution_quote=quote, as_of=at, event_id="no")
        snapshot = wallet.snapshot(as_of=at, fx_mark=_control_quote(at), usd_assets="0", exit_quote=quote)
        self.assertIsNone(snapshot["exit_equity_cny"])
        restored = ExpenseWallet(wallet.rules, expense_review=wallet.expense_review, fixed_expenses=wallet.fixed_expenses)
        restored.events, restored.last_at = copy.deepcopy(wallet.events), wallet.last_at
        restored.restore_expenses_from_events()
        self.assertEqual(restored.owed, wallet.owed)
        restored.events.append(copy.deepcopy(wallet.events[-1]))
        with self.assertRaises(CashContractError):
            restored.restore_expenses_from_events()

    def test_explicit_zero_preserves_v1_amounts_and_scope_is_defensive(self):
        rules = _control_rules("100")
        review, rows = expense_inputs(rules)
        wallet = ExpenseWallet(rules, expense_review=review, fixed_expenses=rows)
        old = CashAccount(rules)
        at = wallet.cutoff_at("2026-01-30")
        self.assertEqual(wallet.book_expenses(as_of=at)["usd_paid_total"], "0")
        self.assertEqual(wallet.accrue_day("2026-01-30", as_of=at)["credited"], old.accrue_day("2026-01-30", as_of=at)["credited"])
        self.assertEqual(wallet.settled, old.settled)
        review["explicit_zero"] = False
        self.assertTrue(wallet.expense_review["explicit_zero"])
        with self.assertRaises(CashContractError):
            ExpenseWallet(rules, expense_review=review, fixed_expenses=rows)
        with self.assertRaises(CashContractError):
            ExpenseWallet(rules, expense_review=wallet.expense_review, fixed_expenses=[], model_version="historical-account-model/1")

    def test_schema_receipt_clock_cutoff_grid_duplicates_and_no_backfill(self):
        rules = _control_rules("100")
        good = row(rules)
        mutations = [lambda r: r.update(amount=True), lambda r: r.update(amount="NaN"),
                     lambda r: r.update(amount="-1"), lambda r: r.update(amount="0.001"),
                     lambda r: r.update(currency="HKD"), lambda r: r.update(category="fx"),
                     lambda r: r.update(book_at="2026-01-30T12:00:00+00:00"),
                     lambda r: r["evidence"].update(known_at="2026-02-01T00:00:00+00:00"),
                     lambda r: r["evidence"].update(classification="conditional_historical_model")]
        for mutation in mutations:
            rows = [copy.deepcopy(good)]; mutation(rows[0]); review, rows = expense_inputs(rules, rows)
            with self.subTest(mutation=mutation), self.assertRaises(CashContractError):
                ExpenseWallet(rules, expense_review=review, fixed_expenses=rows)
        review, rows = expense_inputs(rules, [good, good])
        with self.assertRaises(CashContractError): ExpenseWallet(rules, expense_review=review, fixed_expenses=rows)
        wallet = self.wallet(rows=lambda r: [row(r)])
        with self.assertRaisesRegex(CashContractError, "missed expense"):
            wallet.book_expenses(as_of=wallet.cutoff_at("2026-01-31"))
        self.assertEqual(wallet.booked_expenses, {})
        with self.assertRaisesRegex(CashContractError, "reviewed calendar cutoff"):
            wallet.book_expenses(as_of="2026-01-30T12:00:00+00:00")


@unittest.skipUnless(importlib.util.find_spec("backtest") is not None, "existing Vibe installation required")
class ActualHistoricalExpenseTests(unittest.TestCase):
    def setUp(self):
        from research_base.historical_signals import write_control_fixture
        from research_base.historical_contract import control_model
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifact = write_control_fixture(Path(self.temp.name) / "fixture")
        for bar in self.artifact["bars"]:
            bar["volume"] = "1000000"
        self.model, _ = control_model(self.artifact)
        self.model["schema_version"] = "historical-account-model/2"
        self.model["cash_rules"]["initial_cny"] = "14000"
        for rate in self.model["cash_rules"]["interest_schedules"]:
            rate["tiers"][0]["net_annual_rate"] = "0"
        self.model["fx"]["entry"]["principal_cny"] = "7000"
        self.model["fx"]["entry"]["execution_quote"].update(buy_usd_spread_bps="0", sell_usd_spread_bps="0", buy_fixed_fee_cny="0", sell_fixed_fee_usd="0")
        for rule in self.model["terms"]["rules"]:
            rule["execution"]["friction_rate"] = "0"
            for fee in rule["fees"]:
                fee.update(rate="0", minimum="0")
        for opening in self.model["execution"]["rows"]:
            opening["open_capacity_shares"] = 10000

    def run_expenses(self, rows, variant="BC"):
        from research_base.historical_contract import prepare
        from research_base.historical_signals import generate
        from research_base.historical_account import execute
        from research_base.historical_replay import independent_replay
        rules = self.model["cash_rules"]
        evidence = copy.deepcopy(rules["funding_evidence"]["evidence"])
        self.model["fixed_expenses"] = [{"schema_version": "fixed-expense/1", "id": ident,
             "currency": currency, "amount": amount, "book_at": day + "T23:59:59+00:00",
             "category": "fixed", "evidence": copy.deepcopy(evidence)} for ident, currency, amount, day in rows]
        self.model["expense_review"] = {"schema_version": "fixed-expense-review/1", "reviewer": "invented integration control",
             "scope": "CNY/USD fixed expenses; no actual subscription claim", "reviewed_at": rules["knowledge_clock"]["freeze_at"],
             "coverage_start_date": rules["start_at"][:10], "coverage_end_date": "2021-01-04",
             "explicit_zero": not rows, "schedule_sha256": canonical_hash(self.model["fixed_expenses"]), "evidence": evidence}
        features = generate(self.artifact, variant, trade_start=self.model["start"], end_date=self.model["end"])
        data = prepare(self.artifact, features, self.model)
        result = execute(data)
        replay = independent_replay(data, result)
        self.assertTrue(replay["passed"], replay)
        return data, result

    def test_actual_cny_debit_and_unspendable_usd_liability(self):
        _, result = self.run_expenses([("cny-data", "CNY", "70", "2020-10-01"),
                                      ("usd-server", "USD", "5", "2020-10-01")], "B0")
        first = result["cny_snapshots"][0]
        self.assertEqual(Decimal(first["marked_equity_cny"]), Decimal(13895))
        self.assertEqual(Decimal(first["wallets"]["USD"]["fixed_expense_owed"]), Decimal(5))
        self.assertEqual(Decimal(first["wallets"]["USD"]["available"]), Decimal(0))
        self.assertFalse(any(e["type"] == "CASH_EXPENSE_DEBIT" for e in result["events"]))

    def test_actual_partial_payment_then_monthend_credit_retry(self):
        for rate in self.model["cash_rules"]["interest_schedules"]:
            rate["crediting"] = "calendar_month_end"
            rate["tiers"][0]["net_annual_rate"] = "0.365" if rate["currency"] == "USD" else "0"
        data, result = self.run_expenses([("server", "USD", "1020", "2020-10-31")])
        payments = [e for e in result["wallet_events"] if e["type"] == "FIXED_EXPENSE_PAYMENT"]
        debits = [e for e in result["events"] if e["type"] == "CASH_EXPENSE_DEBIT"]
        self.assertEqual([Decimal(e["amount"]) for e in payments], [Decimal(1000), Decimal(20)])
        self.assertEqual([e["source_wallet_sequence"] for e in debits], [e["sequence"] for e in payments])
        self.assertEqual(Decimal(result["cny_snapshots"][-1]["marked_equity_cny"]), Decimal("7070.21"))
        from research_base.historical_replay import independent_replay
        bad = copy.deepcopy(result)
        debit = copy.deepcopy(debits[0])
        debit.update(sequence=len(bad["events"])+1, date=data["end"])
        bad["events"].append(debit)
        checked = independent_replay(data, bad)
        self.assertFalse(checked["passed"])
        self.assertTrue(any("unique expense debit" in e["field"] or "payment sources exactly once" in e["field"] for e in checked["errors"]), checked)

    def test_actual_declared_fee_amount_cannot_be_changed_by_journal(self):
        data, result = self.run_expenses([("server", "USD", "10", "2020-10-01")])
        from research_base.historical_replay import independent_replay
        bad = copy.deepcopy(result)
        booking = next(e for e in bad["wallet_events"] if e["type"] == "FIXED_EXPENSE_BOOKED")
        booking["amount"] = "0"
        checked = independent_replay(data, bad)
        self.assertFalse(checked["passed"])
        self.assertTrue(any("book amount" in e["field"] for e in checked["errors"]), checked)

    def test_actual_both_monthend_currency_credits_can_pay_after_accrual(self):
        for rate in self.model["cash_rules"]["interest_schedules"]:
            rate["crediting"] = "calendar_month_end"
            rate["tiers"][0]["net_annual_rate"] = "0.365"
        _, result = self.run_expenses([("cny-data", "CNY", "7200", "2020-10-31"),
                                       ("usd-server", "USD", "1020", "2020-10-31")])
        payments = [e for e in result["wallet_events"] if e["type"] == "FIXED_EXPENSE_PAYMENT"]
        self.assertEqual([(e["currency"], Decimal(e["amount"])) for e in payments],
                         [("CNY", Decimal(7014)), ("USD", Decimal(1000)),
                          ("CNY", Decimal(186)), ("USD", Decimal(20))])
        # CNY 24.30 remains after the monthly credit, then accrues .02 for
        # each of Nov 1--3; USD 10.03 is marked at CNY 7/USD.
        self.assertEqual(Decimal(result["cny_snapshots"][-1]["marked_equity_cny"]), Decimal("94.57"))


if __name__ == "__main__":
    unittest.main()
