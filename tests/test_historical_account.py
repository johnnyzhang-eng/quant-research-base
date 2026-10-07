import copy
from decimal import Decimal
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research_base.evidence import ContractError, canonical_hash
from research_base.historical_signals import write_control_fixture, generate, ASSETS
from research_base.historical_contract import control_model, prepare
from research_base.historical_account import execute, performance_input
from research_base.performance import report
from research_base import cash_account


@unittest.skipUnless(importlib.util.find_spec("backtest") is not None, "requires an existing Vibe installation")
class HistoricalAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifact = write_control_fixture(Path(self.temp.name) / "fixture")
        for bar in self.artifact["bars"]:
            bar["volume"] = "1000000"
        self.model, _ = control_model(self.artifact)
        self.model["cash_rules"]["initial_cny"] = "14000"
        for row in self.model["cash_rules"]["interest_schedules"]:
            row["tiers"][0]["net_annual_rate"] = "0"
        quote = self.model["fx"]["entry"]["execution_quote"]
        quote.update(buy_usd_spread_bps="0", sell_usd_spread_bps="0", buy_fixed_fee_cny="0", sell_fixed_fee_usd="0")
        self.model["fx"]["entry"]["principal_cny"] = "7000"
        for rule in self.model["terms"]["rules"]:
            rule["execution"]["friction_rate"] = "0"
            for fee in rule["fees"]:
                fee.update(rate="0", minimum="0")
        for row in self.model["execution"]["rows"]:
            row["open_capacity_shares"] = 10000

    def run_model(self, model=None, artifact=None, variant="B0", k=None):
        model, artifact = model or self.model, artifact or self.artifact
        features = generate(artifact, variant, trade_start=model["start"], end_date=model["end"],
                            k=k, delay_sessions=1 if model["scenario"] == "C3" else 0)
        data = prepare(artifact, features, model)
        return data, execute(data)

    def expense_model(self, rows):
        model = copy.deepcopy(self.model)
        model["schema_version"] = "historical-account-model/2"
        evidence = copy.deepcopy(model["cash_rules"]["funding_evidence"]["evidence"])
        wallet = cash_account.CashAccount(model["cash_rules"])
        costs = [{"schema_version": "fixed-expense/1", "id": identity, "currency": currency,
                  "amount": amount, "book_at": wallet.cutoff_at(day), "category": "fixed", "evidence": evidence}
                 for identity, currency, amount, day in rows]
        model.update(fixed_expenses=costs, expense_review={"schema_version": "fixed-expense-review/1",
            "reviewer": "invented accounting control author", "scope": "invented fixed-cost ledger",
            "reviewed_at": model["cash_rules"]["knowledge_clock"]["freeze_at"],
            "coverage_start_date": model["cash_rules"]["start_at"][:10], "coverage_end_date": model["end"],
            "explicit_zero": not bool(costs), "schedule_sha256": canonical_hash(costs), "evidence": evidence})
        return model

    def test_fixed_expenses_reduce_nav_once_and_background_stays_uninvested(self):
        model = self.expense_model([("cny-server", "CNY", "100", "2020-10-01"),
                                    ("usd-data", "USD", "10", "2020-10-01")])
        data, result = self.run_model(model=model, variant="BC")
        self.assertEqual(Decimal(result["cny_snapshots"][-1]["marked_equity_cny"]), Decimal("13830"))
        self.assertEqual(Decimal(result["cny_cash_background_snapshots"][-1]["marked_equity_cny"]), Decimal("14000"))
        self.assertEqual(len([e for e in result["events"] if e["type"] == "CASH_EXPENSE_DEBIT"]), 1)
        self.assertEqual(report(performance_input(data, result))["cost_totals"]["fixed"], "170")
        from research_base.historical_replay import independent_replay
        replay = independent_replay(data, result)
        self.assertTrue(replay["passed"], replay["errors"])

    def test_unpaid_liabilities_preserve_insolvency_and_all_sessions(self):
        model = self.expense_model([("oversized-obligation", "CNY", "20000", "2020-10-01")])
        data, result = self.run_model(model=model, variant="B0")
        self.assertEqual(Decimal(result["cny_snapshots"][-1]["marked_equity_cny"]), Decimal("-6000"))
        self.assertEqual(result["snapshots"][-1]["holdings"], {"SPY": 5, "EFA": 5, "IEF": 0, "GLD": 0})
        self.assertEqual([s["date"] for s in result["cny_snapshots"]],
                         [d for d in data["calendar"]["sessions"] if data["start"] <= d <= data["end"]])
        plans = [e for e in result["events"] if e["type"] == "TARGET_PLAN" and e["date"] > "2020-10-01"]
        self.assertTrue(plans)
        self.assertTrue(all(e["allocation_policy"] == "TRADE_HALTED_NONPOSITIVE_NAV" and not any(e["deltas"].values()) for e in plans))
        self.assertFalse(any(e["type"] == "ORDER_ATTEMPT" and e["date"] > "2020-10-01" for e in result["events"]))
        self.assertTrue(report(performance_input(data, result))["insolvencies"])
        from research_base.historical_replay import independent_replay
        replay = independent_replay(data, result)
        self.assertTrue(replay["passed"], replay["errors"])

    def test_full_cny_denominator_and_cash_constraint(self):
        data, result = self.run_model()
        first = next(e for e in result["events"] if e["type"] == "TARGET_PLAN")
        self.assertEqual(Decimal(first["opening_equity"]), Decimal("2000"))
        self.assertEqual(first["deltas"], {s: 5 for s in ASSETS})
        fills = [e for e in result["events"] if e["type"] == "FILL" and e["date"] == "2020-10-01"]
        self.assertEqual([(e["symbol"], e["qty"]) for e in fills], [("SPY", 5), ("EFA", 5)])
        self.assertEqual(Decimal(result["cny_snapshots"][-1]["marked_equity_cny"]), Decimal("14000"))
        self.assertEqual(result["data_kind"], "invented_control")
        self.assertFalse(result["historical_engine_ready"])
        self.assertGreater(len(result["native_fill_records"]), 0)

    def test_fx_move_changes_close_nav_not_earlier_open_size(self):
        model = copy.deepcopy(self.model)
        for quote in model["fx"]["close_marks"].values():
            quote["cny_per_usd"] = "8"
        _, result = self.run_model(model=model)
        first = next(e for e in result["events"] if e["type"] == "TARGET_PLAN")
        self.assertEqual(first["deltas"], {s: 5 for s in ASSETS})
        self.assertEqual(Decimal(result["cny_snapshots"][0]["marked_equity_cny"]), Decimal("15000"))

    def test_late_close_carries_known_mark_and_prior_volume_stays_causal(self):
        artifact = copy.deepcopy(self.artifact)
        for bar in artifact["bars"]:
            if bar["trade_date"] == "2020-10-01":
                bar.update(close="900", high="900", available_at="2020-10-05T21:05:00Z")
            if bar["trade_date"] == "2020-10-30":
                bar.update(volume="999999999", available_at="2020-11-03T21:05:00Z")
            if bar["trade_date"] == "2020-11-02":
                bar.update(open="200", high="200", low="200", close="200")
        _, result = self.run_model(artifact=artifact)
        first = result["snapshots"][0]
        self.assertEqual(first["valuation_marks"], {s: "100" for s in ASSETS})
        self.assertEqual(set(first["stale_mark_symbols"]), set(ASSETS))
        self.assertEqual(Decimal(result["cny_snapshots"][0]["marked_equity_cny"]), Decimal("14000"))
        self.assertFalse(any(e["type"] == "FILL" and e["date"] == "2020-11-02" for e in result["events"]))
        self.assertTrue(any(e["type"] == "ORDER_CANCEL" and e["reason"] == "VOLUME_WARMUP_INCOMPLETE"
                            and e["date"] == "2020-11-02" for e in result["events"]))

    def test_c3_uses_actual_delayed_open_rule_and_settlement(self):
        model, artifact = copy.deepcopy(self.model), copy.deepcopy(self.artifact)
        model["scenario"] = "C3"
        first_rule = model["terms"]["rules"][0]
        first_rule["through"] = "2020-10-01"
        later = copy.deepcopy(first_rule)
        later.update(id="new", **{"from": "2020-10-02", "through": "2021-01-31"})
        later["fees"][0]["minimum"] = "2"
        later["settlement"].update(cash_sessions=1, share_sessions=1)
        model["terms"]["rules"].append(later)
        for bar in artifact["bars"]:
            if bar["trade_date"] >= "2020-10-02":
                bar.update(open="200", high="200", low="200", close="200")
        _, result = self.run_model(model=model, artifact=artifact)
        first = next(e for e in result["events"] if e["type"] == "FILL")
        self.assertEqual((first["date"], first["rule_id"], first["price"], first["fee"], first["settlement_date"]),
                         ("2020-10-02", "new", "200.2", "2.0", "2020-10-05"))

    def test_metrics_retain_entry_cost_and_cny_cash_background(self):
        data, result = self.run_model(variant="BC")
        ledger = performance_input(data, result)
        metrics = report(ledger)
        self.assertEqual(ledger["currency"], "CNY")
        self.assertEqual(ledger["observations"][0]["equity"], "14000")
        self.assertEqual(ledger["risk_free"]["currency"], "CNY")
        self.assertEqual(len(ledger["risk_free"]["period_returns"]), len(ledger["observations"])-1)
        self.assertFalse(result["native_fill_records"])
        self.assertEqual(metrics["period_net_return"]["value"], 0)
        self.assertEqual(metrics["final_equity"], "14000.00000000")

    def test_nontrading_payment_preserves_tax_and_unspendable_receivable(self):
        artifact, model = copy.deepcopy(self.artifact), copy.deepcopy(self.model)
        artifact["actions"] = [{"id": "bankday", "type": "dividend", "symbol": "SPY", "currency": "USD", "source_id": "CONTROL",
            "effective_date": "2020-10-16", "pay_date": "2020-10-17", "gross_per_share": "1", "share_basis": "post_split",
            "available_at": "2020-10-16T00:00:00Z", "availability_basis": "modelled", "revision_status": "unknown"}]
        model["withholding"] = {"bankday": "0.2"}
        for bar in artifact["bars"]:
            if bar["symbol"] == "SPY" and bar["trade_date"] >= "2020-10-16":
                bar.update(open="99", high="99", low="99", close="99")
        _, result = self.run_model(model=model, artifact=artifact)
        ex = next(s for s in result["snapshots"] if s["date"] == "2020-10-16")
        after = next(s for s in result["snapshots"] if s["date"] == "2020-10-19")
        self.assertEqual((Decimal(ex["settled_cash"]), Decimal(ex["dividend_receivable"])), (Decimal(0), Decimal(4)))
        self.assertEqual((Decimal(after["settled_cash"]), Decimal(after["dividend_receivable"])), (Decimal(4), Decimal(0)))
        paid = [e for e in result["events"] if e["type"] == "DIV_PAY"]
        self.assertEqual(len(paid), 1)
        self.assertEqual(paid[0]["effective_pay_date"], "2020-10-17")
        self.assertEqual(paid[0]["payment_at"], "2020-10-17T16:30:00-04:00")
        self.assertEqual(Decimal(next(s for s in result["cny_snapshots"] if s["date"] == "2020-10-19")["marked_equity_cny"]), Decimal("13993"))

    def test_old_synthetic_path_does_not_accept_bound_history_layout(self):
        data, _ = self.run_model()
        from research_base.reference.low_frequency_engine import validate, ContractError as OldError
        with self.assertRaises(OldError):
            validate(data, "synthetic")
        changed = copy.deepcopy(data)
        changed["control_intents"][0]["weights"]["SPY"] = "1"
        with self.assertRaises(ContractError):
            execute(changed)

    def test_pay_after_stock_close_refreshes_cutoff_receivable(self):
        artifact, model = copy.deepcopy(self.artifact), copy.deepcopy(self.model)
        artifact["actions"] = [{"id": "latepay", "type": "dividend", "symbol": "SPY", "currency": "USD", "source_id": "CONTROL",
            "effective_date": "2020-10-16", "pay_date": "2020-10-19", "gross_per_share": "1", "share_basis": "post_split",
            "available_at": "2020-10-16T00:00:00Z", "availability_basis": "modelled", "revision_status": "unknown"}]
        model["withholding"] = {"latepay": "0.2"}
        for bar in artifact["bars"]:
            if bar["symbol"] == "SPY" and bar["trade_date"] >= "2020-10-16":
                bar.update(open="99", high="99", low="99", close="99")
        data, result = self.run_model(model=model, artifact=artifact)
        after = next(s for s in result["snapshots"] if s["date"] == "2020-10-19")
        self.assertEqual(Decimal(after["settled_cash"]), Decimal(4))
        self.assertEqual(Decimal(after["dividend_receivable"]), Decimal(0))
        from research_base.historical_replay import independent_replay
        replay = independent_replay(data, result)
        self.assertTrue(replay["passed"], replay["errors"])

    def test_execution_rechecks_declared_retention_at_current_use(self):
        artifact = copy.deepcopy(self.artifact)
        artifact["sources"]["CONTROL"].update(retention="until_expiry", expires_at="2021-02-01T00:00:00Z",
            delete_by="2021-02-02T00:00:00Z", deletion_conditions=["expiry"])
        features = generate(artifact, "BC", trade_start=self.model["start"], end_date=self.model["end"])
        data = prepare(artifact, features, self.model)
        with self.assertRaisesRegex(ContractError, "expired"):
            execute(data)

    def test_late_open_price_perturbation_cannot_change_orders(self):
        model = copy.deepcopy(self.model)
        for row in model["execution"]["rows"]:
            if row["date"] == "2020-10-01" and row["symbol"] == "SPY":
                row.update(open_quote_known_at="2020-10-01T15:00:00Z", open_status="unknown")
        before = self.run_model(model=model)[1]
        artifact = copy.deepcopy(self.artifact)
        for bar in artifact["bars"]:
            if bar["trade_date"] == "2020-10-01" and bar["symbol"] == "SPY":
                bar.update(open="200", high="200")
        after = self.run_model(model=model, artifact=artifact)[1]
        first = lambda result: [e for e in result["events"] if e["date"] == "2020-10-01"]
        self.assertEqual(first(before), first(after))
        plan = next(e for e in first(after) if e["type"] == "TARGET_PLAN")
        self.assertEqual(plan["deltas"]["SPY"], 5)
        self.assertTrue(any(e["type"] == "ORDER_CANCEL" and e["reason"] == "LATE_OPEN_QUOTE" for e in first(after)))
