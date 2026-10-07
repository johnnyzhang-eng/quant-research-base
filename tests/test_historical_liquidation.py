"""Actual native continuation with invented evidence and literal cash answers."""
import copy
from decimal import Decimal
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research_base.evidence import ContractError, canonical_hash
from research_base.cash_account import CashContractError, CashAccount
from research_base.historical_signals import write_control_fixture, generate, ASSETS
from research_base.historical_contract import control_model, prepare
from research_base.historical_account import execute
from research_base.historical_liquidation import execute_exit, reconcile_exit, VERSION, VERSION2


def bind_forward(forward):
    forward["source_binding"]["payload_sha256"] = canonical_hash({k:v for k,v in forward.items() if k != "source_binding"})
    return forward


@unittest.skipUnless(importlib.util.find_spec("backtest") is not None, "requires an existing Vibe installation")
class HistoricalLiquidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        artifact = write_control_fixture(Path(self.temp.name)/"fixture")
        for bar in artifact["bars"]:
            bar["volume"] = "1000000"
        model, _ = control_model(artifact, end="2020-10-30")
        model["cash_rules"]["initial_cny"] = "14000"
        for row in model["cash_rules"]["interest_schedules"]:
            row["tiers"][0]["net_annual_rate"] = "0"
        quote = model["fx"]["entry"]["execution_quote"]
        quote.update(buy_usd_spread_bps="0", sell_usd_spread_bps="0", buy_fixed_fee_cny="0", sell_fixed_fee_usd="0")
        model["fx"]["entry"]["principal_cny"] = "7000"
        for rule in model["terms"]["rules"]:
            rule["execution"]["friction_rate"] = "0"
            for fee in rule["fees"]:
                fee.update(rate="0", minimum="0")
        for row in model["execution"]["rows"]:
            row["open_capacity_shares"] = 10000
        features = generate(artifact, "B0", trade_start=model["start"], end_date=model["end"])
        self.artifact, self.model = artifact, model
        self.data = prepare(artifact, features, model)
        self.study = execute(self.data)
        calendar = [{k:r[k] for k in ("trade_date", "open_at", "close_at")} for r in artifact["calendar"]]
        bars = []
        for raw in artifact["bars"]:
            row = {k:raw[k] for k in ("symbol", "open", "high", "low", "close", "volume", "open_at", "close_at", "available_at")}
            row.update(date=raw["trade_date"], open_quote_known_at=raw["open_at"], open_status="tradable", open_capacity_shares=10000)
            bars.append(row)
        evidence = model["fx"]["entry"]["execution_quote"]["evidence"]
        marks, quotes = {}, {}
        for day in ("2020-11-02", "2020-11-03", "2020-11-04"):
            mark = copy.deepcopy(model["fx"]["close_marks"]["2020-10-30"])
            mark.update(quoted_at=day+"T21:00:00Z", known_at=day+"T21:00:00Z")
            marks[day] = mark
            quote = copy.deepcopy(model["fx"]["entry"]["execution_quote"])
            quote.update(quoted_at=day+"T21:00:00Z", known_at=day+"T21:00:00Z", sell_fixed_fee_usd="1", sell_usd_spread_bps="100")
            quotes[day] = quote
        self.forward = bind_forward({"schema_version": VERSION, "through": "2020-11-04", "calendar": calendar,
            "bars": bars, "actions": [], "terms": copy.deepcopy(model["terms"]), "cash_rules": copy.deepcopy(model["cash_rules"]),
            "fx": {"close_marks": marks, "execution_quotes": quotes}, "source_binding": {"data_kind": self.data["data_kind"],
                "evidence": {"reference": "invented forward liquidation control", "sha256": "0"*64,
                    "known_at": evidence["known_at"], "basis": "invented_control"}, "payload_sha256": "0"*64}})

    def test_native_exit_preserves_study_and_known_cash_conversion(self):
        original = copy.deepcopy(self.study)
        result = execute_exit(self.data, self.study, self.forward)
        self.assertEqual(self.study, original)
        self.assertEqual(result["realizability"], "COMPLETE_CASH_EXIT")
        self.assertTrue(result["independent_reconciliation"]["passed"])
        fills = [e for e in result["events"] if e["type"] == "FILL"]
        self.assertEqual([(e["symbol"], e["side"], e["qty"], e["date"]) for e in fills],
            [("SPY", "SELL", 5, "2020-11-02"), ("EFA", "SELL", 5, "2020-11-02")])
        # USD1000 minus explicit USD1 fee, bid7*.99 => CNY6923.07;
        # retained original CNY7000 => CNY13923.07. No sale on Oct30.
        self.assertEqual(Decimal(result["exit_wallet_snapshot"]["marked_equity_cny"]), Decimal("13923.07"))
        wrong = copy.deepcopy(result)
        wrong["conversion"]["events"][0]["credit"] = "6924.07"
        wrong["wallet_events"][-1]["credit"] = "6924.07"
        rejected = reconcile_exit(self.data, self.study, self.forward, wrong)
        self.assertFalse(rejected["passed"])
        self.assertTrue(any(e["field"] == "exit FX:credit" for e in rejected["errors"]))

    def test_positive_dated_fees_split_dividend_and_calendar_interest(self):
        model = copy.deepcopy(self.model)
        model["terms"]["rules"][0]["through"] = "2020-10-30"
        features = generate(self.artifact, "B0", trade_start=model["start"], end_date=model["end"])
        data = prepare(self.artifact, features, model)
        study = execute(data)
        forward = copy.deepcopy(self.forward)
        forward["terms"] = copy.deepcopy(model["terms"])
        rule = copy.deepcopy(model["terms"]["rules"][0])
        rule.update(id="exit-positive-cost", **{"from": "2020-10-31", "through": "2021-01-31"})
        rule["fees"][0].update(minimum="1")
        rule["fees"][2].update(rate="0.01")
        rule["settlement"]["cash_sessions"] = 1
        forward["terms"]["rules"].append(rule)
        schedule = copy.deepcopy(model["cash_rules"]["interest_schedules"][1])
        schedule.update(effective_date="2020-10-31")
        schedule["tiers"][0]["net_annual_rate"] = "0.365"
        forward["cash_rules"]["interest_schedules"].append(schedule)
        for bar in forward["bars"]:
            if bar["date"] > data["end"] and bar["symbol"] == "SPY":
                bar.update(open="50", high="50", low="50", close="50")
        forward["actions"] = [
            {"id": "exit-split", "type": "split", "symbol": "SPY", "effective_date": "2020-11-02",
             "known_at": "2020-10-30T00:00:00Z", "numerator": 2, "denominator": 1},
            {"id": "exit-dividend", "type": "dividend", "symbol": "SPY", "effective_date": "2020-11-02",
             "known_at": "2020-10-30T00:00:00Z", "gross_per_share": "0.1", "withholding_rate": "0.2",
             "pay_date": "2020-11-03", "payment_at": "2020-11-03T21:30:00Z"}]
        bind_forward(forward)
        result = execute_exit(data, study, forward)
        self.assertEqual(result["realizability"], "COMPLETE_CASH_EXIT")
        fills = [e for e in result["events"] if e["type"] == "FILL"]
        self.assertEqual([(e["symbol"], e["qty"], Decimal(e["fee"])) for e in fills],
                         [("SPY", 10, Decimal("6")), ("EFA", 5, Decimal("6"))])
        self.assertEqual(Decimal(result["snapshots"][-1]["settled_cash"]), Decimal("990.78"))
        self.assertEqual(Decimal(result["exit_wallet_snapshot"]["marked_equity_cny"]), Decimal("13859.17"))
        dividend = next(e for e in result["events"] if e["type"] == "DIV_EX")
        self.assertEqual(dividend["entitled_qty"], 10)
        self.assertEqual(Decimal(dividend["net_amount"]), Decimal("0.8"))
        weekend = [e for e in result["wallet_events"] if e["type"] == "CASH_INTEREST" and e["currency"] == "USD" and e["day"] in {"2020-10-31", "2020-11-01"}]
        self.assertEqual(len(weekend), 2)
        wrong = copy.deepcopy(result)
        for rows in (wrong["events"], wrong["reconciliation_result"]["events"]):
            next(e for e in rows if e.get("action_id") == "exit-dividend" and e["type"] == "DIV_PAY")["amount"] = "1.6"
        rejected = reconcile_exit(data, study, forward, wrong)
        self.assertFalse(rejected["passed"])
        self.assertTrue(any("net amount" in e["field"] for e in rejected["errors"]))
        wrong = copy.deepcopy(result)
        for rows in (wrong["events"], wrong["reconciliation_result"]["events"]):
            next(e for e in rows if e["type"] == "FILL" and e["date"] > data["end"])["fee"] = "7"
        rejected = reconcile_exit(data, study, forward, wrong)
        self.assertFalse(rejected["passed"])
        self.assertTrue(any("fee total" in e["field"] for e in rejected["errors"]))

    def test_no_forward_prices_is_pending(self):
        forward = copy.deepcopy(self.forward)
        forward["bars"] = [b for b in forward["bars"] if b["date"] <= self.data["end"]]
        bind_forward(forward)
        result = execute_exit(self.data, self.study, forward)
        self.assertEqual(result["realizability"], "PENDING")
        self.assertIn("MISSING_FORWARD_BARS", result["pending_reasons"])
        self.assertEqual(result["events"], [])

    def test_late_quote_and_zero_capacity_cannot_force_liquidation(self):
        forward = copy.deepcopy(self.forward)
        for row in forward["bars"]:
            if row["date"] > self.data["end"]:
                row["open_capacity_shares"] = 0
        bind_forward(forward)
        result = execute_exit(self.data, self.study, forward)
        self.assertEqual(result["realizability"], "PENDING")
        self.assertIn("UNSOLD_SHARES", result["pending_reasons"])
        self.assertFalse(any(e["type"] == "FILL" for e in result["events"]))

    def test_partial_capacity_keeps_stock_and_proceeds_pending(self):
        forward = copy.deepcopy(self.forward)
        for row in forward["bars"]:
            if row["date"] > self.data["end"]:
                row["open_capacity_shares"] = 1
        bind_forward(forward)
        result = execute_exit(self.data, self.study, forward)
        self.assertEqual(result["realizability"], "PENDING")
        self.assertEqual(result["snapshots"][-1]["holdings"], {"SPY": 2, "EFA": 2, "IEF": 0, "GLD": 0})
        self.assertEqual(Decimal(result["snapshots"][-1]["unsettled_cash"]), Decimal("400"))
        self.assertEqual(Decimal(result["exit_wallet_snapshot"]["wallets"]["USD"]["settled"]), Decimal("0"))
        self.assertEqual(Decimal(result["exit_wallet_snapshot"]["marked_equity_cny"]), Decimal("13979.07"))
        wrong = copy.deepcopy(result)
        wrong["realizability"] = "COMPLETE_CASH_EXIT"
        self.assertFalse(reconcile_exit(self.data, self.study, forward, wrong)["passed"])
        wrong = copy.deepcopy(result)
        wrong["events"][0]["requested_qty"] = 999
        self.assertFalse(reconcile_exit(self.data, self.study, forward, wrong)["passed"])

    def test_unknown_late_open_and_future_fx_cannot_authorize_exit(self):
        for field, value, reason in [("open_quote_known_at", "2020-11-05T00:00:00Z", "LATE_OPEN_QUOTE"),
                                     ("open_status", "unknown", "UNKNOWN_OPEN_TRADABILITY"),
                                     ("open_capacity_shares", None, "UNKNOWN_OPEN_LIQUIDITY")]:
            forward = copy.deepcopy(self.forward)
            for row in forward["bars"]:
                if row["date"] > self.data["end"]:
                    row[field] = value
            bind_forward(forward)
            result = execute_exit(self.data, self.study, forward)
            self.assertIn("UNSOLD_SHARES", result["pending_reasons"])
            self.assertTrue(any(e.get("reason") == reason for e in result["events"]))
        forward = copy.deepcopy(self.forward)
        forward["fx"]["execution_quotes"]["2020-11-04"]["quoted_at"] = "2020-11-05T00:00:00Z"
        forward["fx"]["execution_quotes"]["2020-11-04"]["known_at"] = "2020-11-05T00:00:00Z"
        bind_forward(forward)
        with self.assertRaises((ContractError, CashContractError)):
            execute_exit(self.data, self.study, forward)

    def test_c3_delays_one_session_and_keeps_proceeds_pending(self):
        model = copy.deepcopy(self.model)
        model["scenario"] = "C3"
        features = generate(self.artifact, "B0", trade_start=model["start"], end_date=model["end"], delay_sessions=1)
        data = prepare(self.artifact, features, model)
        study = execute(data)
        result = execute_exit(data, study, self.forward)
        cancelled = [e for e in result["events"] if e["type"] == "EXIT_CANCEL" and e["date"] == "2020-11-02"]
        self.assertEqual([e["reason"] for e in cancelled], ["SCENARIO_DELAY", "SCENARIO_DELAY", "SCENARIO_DELAY"])
        fills = [e for e in result["events"] if e["type"] == "FILL"]
        self.assertEqual([e["date"] for e in fills], ["2020-11-03", "2020-11-03", "2020-11-03"])
        self.assertIn("UNSETTLED_PROCEEDS", result["pending_reasons"])
        event = result["conversion"]["events"][0]
        self.assertEqual(Decimal(event["extra_spread_rate"]), Decimal("0.0025"))
        self.assertEqual(Decimal(event["credit"]), Decimal("660.85"))

    def test_model2_restores_unpaid_expense_and_future_schedule(self):
        model = copy.deepcopy(self.model)
        model["schema_version"] = "historical-account-model/2"
        clock_wallet = CashAccount(model["cash_rules"])
        evidence = copy.deepcopy(model["cash_rules"]["funding_evidence"]["evidence"])
        rows = [{"schema_version": "fixed-expense/1", "id": "unpaid-USD", "currency": "USD", "amount": "1200",
                 "book_at": clock_wallet.cutoff_at("2020-10-30"), "category": "fixed", "evidence": evidence},
                {"schema_version": "fixed-expense/1", "id": "future-CNY", "currency": "CNY", "amount": "100",
                 "book_at": clock_wallet.cutoff_at("2020-11-02"), "category": "fixed", "evidence": evidence}]
        review = {"schema_version": "fixed-expense-review/1", "reviewer": "invented exit control author",
                  "scope": "invented fixed cost continuation", "reviewed_at": model["cash_rules"]["knowledge_clock"]["freeze_at"],
                  "coverage_start_date": model["cash_rules"]["start_at"][:10], "coverage_end_date": "2020-11-04",
                  "explicit_zero": False, "schedule_sha256": canonical_hash(rows), "evidence": evidence}
        model.update(fixed_expenses=rows, expense_review=review)
        features = generate(self.artifact, "B0", trade_start=model["start"], end_date=model["end"])
        data = prepare(self.artifact, features, model)
        study = execute(data)
        self.assertEqual(Decimal(study["cny_snapshots"][-1]["wallets"]["USD"]["fixed_expense_owed"]), Decimal("1200"))
        forward = copy.deepcopy(self.forward)
        forward.update(schema_version=VERSION2, expense_review=review, fixed_expenses=rows)
        bind_forward(forward)
        result = execute_exit(data, study, forward)
        self.assertEqual(result["realizability"], "PENDING")
        self.assertIn("UNPAID_FIXED_EXPENSES", result["pending_reasons"])
        self.assertIsNone(result["conversion"])
        self.assertEqual(Decimal(result["exit_wallet_snapshot"]["wallets"]["USD"]["fixed_expense_owed"]), Decimal("200"))
        self.assertEqual(Decimal(result["exit_wallet_snapshot"]["marked_equity_cny"]), Decimal("5500"))
        payment = [e for e in result["wallet_events"] if e["type"] == "FIXED_EXPENSE_PAYMENT" and e["expense_id"] == "unpaid-USD"]
        self.assertEqual(sum(Decimal(e["amount"]) for e in payment), Decimal("1000"))
        forward["through"] = "2020-11-05"
        bind_forward(forward)
        pending = execute_exit(data, study, forward)
        self.assertIn("EXPENSE_REVIEW_COVERAGE_ENDED", pending["pending_reasons"])

    def test_frozen_payload_and_old_costs_cannot_be_revised(self):
        forward = copy.deepcopy(self.forward)
        forward["bars"][0]["volume"] = "999"
        with self.assertRaises((ContractError, CashContractError)):
            execute_exit(self.data, self.study, forward)
        forward = copy.deepcopy(self.forward)
        forward["terms"]["rules"][0]["fees"][0]["minimum"] = "9"
        bind_forward(forward)
        with self.assertRaises((ContractError, CashContractError)):
            execute_exit(self.data, self.study, forward)
