"""Literal coupled journals; no production fee, FX or interest amount helpers."""
import copy
import unittest

from research_base.historical_replay import independent_replay


def literal_journals():
    start = "2026-01-28T00:00:00+00:00"
    clock = {"basis": "historical_point_in_time", "freeze_at": "2026-10-05T00:00:00+00:00"}
    evidence = {"reference": "invented journal control", "sha256": "0" * 64,
                "known_at": start, "classification": "synthetic_control"}
    funding = {"status": "synthetic_control", "lawful_and_usable": True,
        "route_description": "fictional initial CNY", "effective_at": start,
        "allowed_currencies": ["CNY", "USD"], "evidence": evidence}
    interest = []
    for currency, rate in [("CNY", "0"), ("USD", "0.365")]:
        interest.append({"schema_version": "cash-interest.v1", "currency": currency,
            "effective_date": "2026-01-28", "source_effective_at": start, "known_at": start,
            "balance_observation": "settled_at_calendar_cutoff", "net_rate_description": "invented net APR",
            "eligibility_threshold": "0", "eligibility_mode": "gate_all", "tier_mode": "marginal",
            "tiers": [{"lower": "0", "upper": None, "net_annual_rate": rate, "known_at": start}],
            "day_count": "ACT365", "rounding_quantum": "0.01", "rounding_mode": "HALF_UP",
            "rounding_timing": "daily_accrual", "crediting": "daily", "evidence": evidence})
    cash_rules = {"schema_version": "cash-account.v1", "classification": "synthetic_control", "start_at": start,
        "timezone": "UTC", "accrual_cutoff": "23:59:59", "initial_cny": "7000",
        "knowledge_clock": clock, "funding_evidence": funding,
        "money_rounding": {c: {"quantum": "0.01", "mode": "DOWN"} for c in ("CNY", "USD")},
        "interest_schedules": interest}
    rule_evidence = {"reference": "invented costs and settlement", "sha256": "0" * 64, "basis": "invented_control"}
    def rule(id, begin, end, fee):
        return {"id": id, "from": begin, "through": end, "known_at": start, "evidence": rule_evidence,
            "fees": [{"id": "broker", "category": "broker", "sides": ["BUY", "SELL"], "basis": "order",
                "rate": fee, "minimum": "0", "maximum": None, "quantum": "0.01", "rounding": "half_up"}],
            "execution": {"friction_rate": "0", "price_tick": "0.01", "evidence": rule_evidence},
            "settlement": {"cash_sessions": 1, "share_sessions": 2, "reuse_unsettled_proceeds": False,
                           "resell_unsettled_shares": False, "evidence": rule_evidence}}
    terms = {"schema_version": "trading-terms/1", "data_kind": "invented_control", "currency": "USD",
        "frozen_at": "2026-10-05T00:00:00+00:00", "rules": [
            rule("first", "2026-01-28", "2026-01-31", "1"), rule("new-fee", "2026-02-01", "2026-02-05", "2")]}
    days = ["2026-01-29", "2026-01-30", "2026-02-02", "2026-02-03", "2026-02-04"]
    def fx(at, execution=False):
        out = {"schema_version": "fx-execution.v1" if execution else "fx-mark.v1", "quoted_at": at,
            "known_at": at, "cny_per_usd": "7", "evidence": evidence}
        if execution:
            out.update(buy_usd_spread_bps="0", sell_usd_spread_bps="0", buy_fixed_fee_cny="0", sell_fixed_fee_usd="0")
        return out
    entry_quote = fx("2026-01-28T19:00:00+00:00", True)
    opening_mark = fx("2026-01-28T19:00:00+00:00")
    data = {"data_kind": "constructed_market_fixture", "start": days[0], "end": days[-1], "symbols": ["TEST"],
        "calendar": {"sessions": days + ["2026-02-05", "2026-02-06"]},
        "initial": {"settled_cash": "1000", "holdings": {"TEST": 0}, "marks": {"TEST": "10"}},
        "instruments": {"TEST": {"price_tick": "0.01", "lot_size": 1}},
        "bars": [{"date": d, "symbol": "TEST", "open": "5" if d >= "2026-02-03" else "10",
            "close": "5" if d >= "2026-02-03" else "10", "open_at": d + "T13:30:00+00:00",
            "close_at": d + "T20:00:00+00:00", "open_capacity_shares": 100000} for d in days],
        "actions": [{"id": "split", "type": "split", "symbol": "TEST", "effective_date": "2026-02-03",
                     "known_at": "2026-02-02T00:00:00+00:00", "numerator": 2, "denominator": 1},
                    {"id": "div", "type": "dividend", "symbol": "TEST", "effective_date": "2026-02-03",
                     "known_at": "2026-02-02T00:00:00+00:00", "gross_per_share": "0.1", "withholding_rate": "0.2", "pay_date": "2026-02-04"}],
        "historical_model": {"terms": terms, "cash_rules": cash_rules, "scenario": "C0", "fx": {
            "entry": {"at": "2026-01-29T12:00:00+00:00", "principal_cny": "7000", "execution_quote": entry_quote},
            "opening_mark": opening_mark, "close_marks": {d: fx(d + "T19:00:00+00:00") for d in days},
            "exit_quotes": {d: fx(d + "T19:00:00+00:00", True) for d in days}}}}
    events = []
    def stock(day, kind, **fields):
        events.append({"sequence": len(events) + 1, "date": day, "type": kind, **fields})
    def credit(day, amount, source, source_day):
        stock(day, "CASH_INTEREST_CREDIT", amount=amount, source_wallet_sequence=source, source_wallet_day=source_day)
    stock(days[0], "FILL", symbol="TEST", side="BUY", qty=50, raw_open="10", price="10", commission="1", platform="0", tax="0", fee="1",
          fees=[{"id": "broker", "category": "broker", "amount": "1"}], settlement_date="2026-02-02")
    credit(days[0], "0.50", 8, days[0])
    credit(days[1], "0.50", 12, days[1])
    credit(days[2], "0.50", 14, "2026-01-31")
    credit(days[2], "0.50", 16, "2026-02-01")
    stock(days[2], "SHARE_SETTLEMENT", fill_sequence=1, qty=50)
    stock(days[2], "FILL", symbol="TEST", side="SELL", qty=10, raw_open="10", price="10", commission="2", platform="0", tax="0", fee="2",
          fees=[{"id": "broker", "category": "broker", "amount": "2"}], settlement_date="2026-02-03")
    credit(days[2], "0.50", 20, days[2])
    stock(days[3], "CASH_SETTLEMENT", fill_sequence=7, amount="98")
    stock(days[3], "SPLIT", action_id="split", symbol="TEST", numerator=2, denominator=1)
    stock(days[3], "DIV_EX", action_id="div", symbol="TEST", entitled_qty=80, net_amount="6.4")
    credit(days[3], "0.60", 24, days[3])
    stock(days[4], "DIV_PAY", action_id="div", amount="6.4")
    credit(days[4], "0.61", 28, days[4])
    wallet = []
    def w(kind, at, **fields):
        wallet.append({"sequence": len(wallet) + 1, "type": kind, "at": at, "knowledge_clock": clock, **fields})
    def interest_row(day, currency, base, raw, credit_amount):
        w("CASH_INTEREST", day + "T23:59:59+00:00", currency=currency, day=day,
          eligible_balance=base, raw_accrual=raw, accrued=credit_amount, credited=credit_amount,
          unpaid_interest="0", effective_date="2026-01-28", day_count="ACT365", crediting="daily", evidence=evidence, credit_rounding_delta="0")
    def sync(day, at, previous, settled, receivable, cursor):
        w("EXTERNAL_USD_SYNC", day + at, event_id="sync-" + str(len(wallet)), previous_settled=previous,
          settled=settled, receivables=receivable, source_stock_event_sequence=cursor, stock_snapshot_date=day)
    def daily(day, usd_base, usd_raw, usd_credit):
        interest_row(day, "CNY", "0", "0", "0")
        interest_row(day, "USD", usd_base, usd_raw, usd_credit)
    w("FUNDING", start, initial_cny="7000", funding_evidence=funding)
    interest_row("2026-01-28", "CNY", "7000", "0", "0")
    interest_row("2026-01-28", "USD", "0", "0", "0")
    w("FX_EXECUTION", "2026-01-29T12:00:00+00:00", event_id="entry", direction="CNY_TO_USD", source="CNY", target="USD",
      principal="7000", source_fixed_fee="0", debit="7000", credit="1000", execution_cny_per_usd="7", quote=entry_quote, extra_spread_rate="0")
    sync(days[0], "T13:30:00+00:00", "1000", "1000", "0", 0)
    sync(days[0], "T20:00:00+00:00", "1000", "499", "0", 1)
    daily(days[0], "499", "0.499", "0.50")
    sync(days[1], "T13:30:00+00:00", "499.5", "499.5", "0", 2)
    sync(days[1], "T20:00:00+00:00", "499.5", "499.5", "0", 2)
    daily(days[1], "499.5", "0.4995", "0.50")
    daily("2026-01-31", "500", "0.5", "0.50")
    daily("2026-02-01", "500.5", "0.5005", "0.50")
    sync(days[2], "T13:30:00+00:00", "501", "501", "0", 6)
    sync(days[2], "T20:00:00+00:00", "501", "501", "98", 7)
    daily(days[2], "501", "0.501", "0.50")
    sync(days[3], "T13:30:00+00:00", "501.5", "599.5", "6.4", 11)
    sync(days[3], "T20:00:00+00:00", "599.5", "599.5", "6.4", 11)
    daily(days[3], "599.5", "0.5995", "0.60")
    sync(days[4], "T13:30:00+00:00", "600.1", "606.5", "0", 13)
    sync(days[4], "T20:00:00+00:00", "606.5", "606.5", "0", 13)
    daily(days[4], "606.5", "0.6065", "0.61")
    stock_states = [("499.5", "0", "0", 50, 0, "999.5"), ("500", "0", "0", 50, 0, "1000"),
        ("501.5", "98", "0", 40, 40, "999.5"), ("600.1", "0", "6.4", 80, 80, "1006.5"), ("607.11", "0", "0", 80, 80, "1007.11")]
    snapshots = [{"date": day, "settled_cash": state[0], "unsettled_cash": state[1], "dividend_receivable": state[2],
                  "holdings": {"TEST": state[3]}, "sellable": {"TEST": state[4]}, "equity": state[5]} for day, state in zip(days, stock_states)]
    cny_values = ["6996.5", "7000", "6996.5", "7045.5", "7049.77"]
    cash_exit = ["3496.5", "3500", "3510.5", "4200.7", "4249.77"]
    def wallet_values(cny, usd, recv="0"):
        return {"CNY": {"settled": cny, "available": cny, "receivables": "0", "unpaid_interest": "0"},
                "USD": {"settled": usd, "available": usd, "receivables": recv, "unpaid_interest": "0"}}
    cny_snapshots = [{"date": day, "as_of": day + "T23:59:59+00:00", "knowledge_clock": clock,
        "wallets": wallet_values("0", state[0], state[1] if state[1] != "0" else state[2]),
        "usd_external_assets": "500" if i < 2 else "400", "usd_total_equity": state[5], "fx_mark_cny_per_usd": "7",
        "marked_equity_cny": cny_values[i], "exit_quote": data["historical_model"]["fx"]["exit_quotes"][day],
        "settled_cash_exit_cny": cash_exit[i], "exit_equity_cny": None}
        for i, (day, state) in enumerate(zip(days, stock_states))]
    opening = {"as_of": "2026-01-28T20:00:00+00:00", "knowledge_clock": clock,
        "wallets": wallet_values("7000", "0"), "usd_external_assets": "0", "usd_total_equity": "0",
        "fx_mark_cny_per_usd": "7", "marked_equity_cny": "7000", "fx_mark": opening_mark}
    return data, {"events": events, "snapshots": snapshots, "wallet_events": wallet,
        "cny_snapshots": cny_snapshots, "opening_cny_snapshot": opening, "engine_source": {"identity": "literal journal control"}}


class HistoricalReplayTests(unittest.TestCase):
    def setUp(self):
        self.data, self.result = literal_journals()

    def test_known_answer_weekend_fees_settlement_split_dividend_fx(self):
        replay = independent_replay(self.data, self.result)
        self.assertTrue(replay["passed"], replay["errors"])
        self.assertEqual(replay["replayed"][-1]["cash_exact_fraction"], "60711/100")
        self.assertEqual(replay["replayed"][-1]["equity_exact_fraction"], "100711/100")
        self.assertEqual(replay["replayed"][-1]["cny_equity_exact_fraction"], "704977/100")

    def test_bound_target_plan_and_requested_quantity_are_independent(self):
        data, result = copy.deepcopy(self.data), copy.deepcopy(self.result)
        day = data["start"]
        quote = data["historical_model"]["fx"]["opening_mark"]
        data["historical_model"]["performance_boundary"] = {"date": "2026-01-28", "fx_mark": quote}
        data["control_intents"] = [{"decision_date": "2026-01-28", "execution_date": day,
            "available_at": "2026-01-28T20:00:00+00:00", "weights": {"TEST": "0.5"}}]
        for event in result["events"]:
            event["sequence"] += 2
            if "fill_sequence" in event:
                event["fill_sequence"] += 2
        for event in result["wallet_events"]:
            if event.get("source_stock_event_sequence", 0):
                event["source_stock_event_sequence"] += 2
        result["events"][:0] = [
            {"sequence": 1, "date": day, "type": "TARGET_PLAN", "decision_date": "2026-01-28",
             "opening_equity": "1000", "deltas": {"TEST": 50},
             "allocation_basis": "FULL_CNY_ACCOUNT_CONVERTED_AT_NONFUTURE_FX", "allocation_fx_quote": quote},
            {"sequence": 2, "date": day, "type": "ORDER_ATTEMPT", "decision_date": "2026-01-28",
             "symbol": "TEST", "side": "BUY", "requested_qty": 50, "observed_at": day + "T13:30:00+00:00"}]
        replay = independent_replay(data, result)
        self.assertTrue(replay["passed"], replay["errors"])
        for field, value in [("deltas", {"TEST": 49}), ("opening_equity", "999")]:
            wrong = copy.deepcopy(result)
            wrong["events"][0][field] = value
            self.assertFalse(independent_replay(data, wrong)["passed"], field)
        for field, value in [("requested_qty", 49), ("side", "SELL"), ("decision_date", "2026-01-27")]:
            wrong = copy.deepcopy(result)
            wrong["events"][1][field] = value
            self.assertFalse(independent_replay(data, wrong)["passed"], field)
        data["control_intents"][0]["weights"]["TEST"] = "0.49"
        self.assertFalse(independent_replay(data, result)["passed"])

    def test_native_source_fingerprints_and_boundary_quote(self):
        result = copy.deepcopy(self.result)
        result.pop("engine_source")
        result["native_engine_source_sha256"] = {"GlobalEquityEngine": "1" * 64, "BaseEngine": "2" * 64}
        self.data["historical_model"]["performance_boundary"] = {
            "date": "2026-01-28", "fx_mark": self.result["opening_cny_snapshot"]["fx_mark"]}
        self.assertTrue(independent_replay(self.data, result)["passed"])
        result["native_engine_source_sha256"]["BaseEngine"] = "unverified label"
        self.assertFalse(independent_replay(self.data, result)["passed"])
        self.data["historical_model"]["performance_boundary"]["fx_mark"]["quoted_at"] = "2026-01-29T00:00:00+00:00"
        self.assertFalse(independent_replay(self.data, self.result)["passed"])

    def test_snapshot_cursors_and_calendar_cutoff_are_checked(self):
        for field, value in [("source_stock_event_sequence", 0), ("source_wallet_sequence", 1),
                             ("as_of", "2026-01-29T23:59:58+00:00")]:
            wrong = copy.deepcopy(self.result)
            wrong["cny_snapshots"][0][field] = value
            self.assertFalse(independent_replay(self.data, wrong)["passed"], field)

    def test_sync_cannot_use_a_future_fill_or_reverse_cursor(self):
        wrong = copy.deepcopy(self.result)
        wrong["wallet_events"][4]["source_stock_event_sequence"] = 1
        self.assertFalse(independent_replay(self.data, wrong)["passed"])
        wrong = copy.deepcopy(self.result)
        wrong["wallet_events"][8]["source_stock_event_sequence"] = 0
        self.assertFalse(independent_replay(self.data, wrong)["passed"])

    def test_unknown_quote_at_open_and_nontradable_fill_are_rejected(self):
        for field, value in [("open_quote_known_at", "2026-01-29T14:00:00+00:00"), ("open_status", "halted")]:
            data = copy.deepcopy(self.data)
            data["bars"][0][field] = value
            self.assertFalse(independent_replay(data, self.result)["passed"], field)

    def test_literal_calendar_month_end_cny_credit_and_unpaid_equity(self):
        data, result = copy.deepcopy(self.data), copy.deepcopy(self.result)
        schedule = data["historical_model"]["cash_rules"]["interest_schedules"][0]
        schedule["crediting"] = "calendar_month_end"
        schedule["tiers"][0]["net_annual_rate"] = "0.365"
        # Literal CNY journal: Jan28 accrues7 on7000; entry consumes only
        # settled7000. Jan31 posts7; Feb1-4 accrue .01/day without posting.
        bases = ["7000", "0", "0", "0", "7", "7", "7", "7"]
        raws = ["7", "0", "0", "0", "0.007", "0.007", "0.007", "0.007"]
        accrued = ["7", "0", "0", "0", "0.01", "0.01", "0.01", "0.01"]
        credits = ["0", "0", "0", "7", "0", "0", "0", "0"]
        unpaid = ["7", "7", "7", "0", "0.01", "0.02", "0.03", "0.04"]
        rows = [e for e in result["wallet_events"] if e["type"] == "CASH_INTEREST" and e["currency"] == "CNY"]
        for i, row in enumerate(rows):
            row.update(crediting="calendar_month_end", eligible_balance=bases[i], raw_accrual=raws[i],
                       accrued=accrued[i], credited=credits[i], unpaid_interest=unpaid[i])
        values = ["7003.5", "7007", "7003.52", "7052.53", "7056.81"]
        for i, snap in enumerate(result["cny_snapshots"]):
            settled = "0" if i < 2 else "7"
            snap["wallets"]["CNY"].update(settled=settled, available=settled,
                unpaid_interest=["7", "7", "0.02", "0.03", "0.04"][i])
            snap["marked_equity_cny"] = values[i]
            if i >= 2:
                snap["settled_cash_exit_cny"] = ["3517.5", "4207.7", "4256.77"][i-2]
        replay = independent_replay(data, result)
        self.assertTrue(replay["passed"], replay["errors"])
        self.assertEqual(replay["replayed"][-1]["cny_equity_exact_fraction"], "705681/100")
        wrong = copy.deepcopy(result)
        rows = [e for e in wrong["wallet_events"] if e["type"] == "CASH_INTEREST" and e["currency"] == "CNY"]
        rows[3]["credited"] = "7.01"
        self.assertFalse(independent_replay(data, wrong)["passed"])
        schedule["crediting"] = "monthend"
        self.assertFalse(independent_replay(data, result)["passed"])

    def test_unknown_interest_semantics_are_not_defaulted(self):
        for field in ("day_count", "tier_mode", "eligibility_mode", "crediting", "rounding_timing"):
            data = copy.deepcopy(self.data)
            data["historical_model"]["cash_rules"]["interest_schedules"][0][field] = "unknown"
            self.assertFalse(independent_replay(data, self.result)["passed"], field)

    def test_tampered_fill_fee_and_component_are_rejected(self):
        for field in ("fee", "commission", "price", "settlement_date"):
            wrong = copy.deepcopy(self.result)
            fill = wrong["events"][0]
            fill[field] = "2026-01-30" if field == "settlement_date" else "99"
            self.assertFalse(independent_replay(self.data, wrong)["passed"], field)
        wrong = copy.deepcopy(self.result)
        wrong["events"][0]["fees"][0]["amount"] = "0"
        self.assertFalse(independent_replay(self.data, wrong)["passed"])

    def test_tampered_engine_credit_and_wallet_interest_are_rejected(self):
        wrong = copy.deepcopy(self.result)
        wrong["events"][1]["amount"] = "50"
        self.assertFalse(independent_replay(self.data, wrong)["passed"])
        wrong = copy.deepcopy(self.result)
        wrong["wallet_events"][7]["credited"] = "50"
        self.assertFalse(independent_replay(self.data, wrong)["passed"])
        wrong["events"][1]["amount"] = "50"
        self.assertFalse(independent_replay(self.data, wrong)["passed"])

    def test_sync_cannot_create_cash_or_receivable(self):
        for field in ("settled", "receivables"):
            wrong = copy.deepcopy(self.result)
            wrong["wallet_events"][5][field] = "9999"
            self.assertFalse(independent_replay(self.data, wrong)["passed"])

    def test_both_currency_nav_mutations_are_rejected(self):
        wrong = copy.deepcopy(self.result)
        wrong["snapshots"][-1]["equity"] = "1008"
        self.assertFalse(independent_replay(self.data, wrong)["passed"])
        wrong = copy.deepcopy(self.result)
        wrong["cny_snapshots"][-1]["marked_equity_cny"] = "7000"
        self.assertFalse(independent_replay(self.data, wrong)["passed"])

    def test_dividend_tax_and_split_quantity_mutations_are_rejected(self):
        wrong = copy.deepcopy(self.result)
        wrong["events"][10]["net_amount"] = "8"
        self.assertFalse(independent_replay(self.data, wrong)["passed"])
        wrong = copy.deepcopy(self.result)
        wrong["events"][10]["entitled_qty"] = 40
        self.assertFalse(independent_replay(self.data, wrong)["passed"])

    def test_future_fx_price_rejected_even_with_counterfactual_clock(self):
        data, wrong = copy.deepcopy((self.data, self.result))
        data["historical_model"]["cash_rules"]["knowledge_clock"]["basis"] = "frozen_current_counterfactual"
        for event in wrong["wallet_events"]:
            event["knowledge_clock"]["basis"] = "frozen_current_counterfactual"
        for snap in wrong["cny_snapshots"] + [wrong["opening_cny_snapshot"]]:
            snap["knowledge_clock"]["basis"] = "frozen_current_counterfactual"
        data["historical_model"]["fx"]["close_marks"]["2026-01-29"]["quoted_at"] = "2026-10-01T00:00:00+00:00"
        self.assertFalse(independent_replay(data, wrong)["passed"])

    def test_cyclic_source_and_duplicate_sequences_are_rejected(self):
        wrong = copy.deepcopy(self.result)
        wrong["wallet_events"][5]["source_stock_event_sequence"] = 2
        self.assertFalse(independent_replay(self.data, wrong)["passed"])
        wrong = copy.deepcopy(self.result)
        wrong["events"][1]["sequence"] = 1
        self.assertFalse(independent_replay(self.data, wrong)["passed"])

    def test_missing_snapshot_and_missing_weekend_interest_are_rejected(self):
        wrong = copy.deepcopy(self.result)
        wrong["cny_snapshots"].pop()
        self.assertFalse(independent_replay(self.data, wrong)["passed"])
        wrong = copy.deepcopy(self.result)
        wrong["wallet_events"][13]["day"] = "2026-02-01"
        self.assertFalse(independent_replay(self.data, wrong)["passed"])

    def test_opening_conversion_is_not_return_and_no_exit_liquidation(self):
        wrong = copy.deepcopy(self.result)
        wrong["opening_cny_snapshot"]["marked_equity_cny"] = "0"
        self.assertFalse(independent_replay(self.data, wrong)["passed"])
        wrong = copy.deepcopy(self.result)
        wrong["cny_snapshots"][-1]["exit_equity_cny"] = "7049.77"
        self.assertFalse(independent_replay(self.data, wrong)["passed"])


if __name__ == "__main__":
    unittest.main()
