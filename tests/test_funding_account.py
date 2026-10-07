"""Fresh synthetic account controls with independent rational arithmetic.

These controls verify bookkeeping, not historical returns or venue liquidation.
"""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Inexact, Rounded, ROUND_DOWN, localcontext
from fractions import Fraction
import json
import unittest

from research_base.funding_account import (
    AccountingError, CarryAccount, NUMERIC_DOMAIN, canonical_bytes, sha256_value,
)


START = datetime(2025, 2, 1, tzinfo=timezone.utc)


def at(second):
    return (START + timedelta(seconds=second)).isoformat()


def event(sequence, kind, **fields):
    return {"id": f"fixture-{sequence}", "type": kind,
            "event_time": at(sequence), "applied_at": at(sequence),
            "known_at": at(sequence), "sequence": sequence, **fields}


def fill(sequence, kind, side, qty, price, slip="0", fee="0", **fields):
    return event(sequence, kind, side=side, qty=qty, reference_price=price,
                 slippage_rate=slip, fee_rate=fee, **fields)


def rational(value):
    return Fraction(str(value))


class FundingAccountTests(unittest.TestCase):
    def test_fee_inclusive_closed_account_and_frozen_funding_quantity(self):
        account = CarryAccount("1000", "500")
        account.apply_event(event(0, "mark", spot_mark="10", perp_mark="10"))
        account.apply_event(fill(1, "spot_fill", "buy", "3", "10", ".01", ".001"))
        account.apply_event(fill(2, "perp_fill", "sell", "3", "10", ".01", ".002"))
        account.apply_event(event(3, "mark", spot_mark="12", perp_mark="12.5"))
        before_capture = account.view()
        account.apply_event(event(
            4, "funding_settlement", settlement_id="fixture-funding",
            settlement_time=at(4), payment_time=at(6),
            eligible_perp_qty_signed="-3", settlement_mark="12.4", rate=".01"))
        self.assertEqual(account.view()["nav"], before_capture["nav"])
        self.assertEqual(account.view()["deriv_wallet_cash_signed"],
                         before_capture["deriv_wallet_cash_signed"])
        self.assertEqual(rational(account.view()["pending_funding_memo_not_in_nav"]),
                         Fraction(3) * Fraction("12.4") * Fraction(".01"))
        account.apply_event(fill(5, "perp_fill", "buy", "1", "12", ".01", ".002"))
        account.apply_event(event(6, "funding_post", settlement_id="fixture-funding"))
        self.assertEqual(account.settlements["fixture-funding"]["eligible_perp_qty_signed"], "-3")
        self.assertEqual(account.view()["perp_qty_signed"], "-2")
        account.apply_event(fill(7, "spot_fill", "sell", "1", "12", ".01", ".001"))
        nav_before_transfer = account.view()["nav"]
        account.apply_event(event(8, "transfer_send", transfer_id="fixture-transfer",
                                  source="spot", target="deriv", amount="20"))
        self.assertEqual(account.view()["nav"], nav_before_transfer)
        self.assertEqual(account.view()["transfer_inflight"], "20")
        account.apply_event(event(9, "transfer_receive", transfer_id="fixture-transfer"))
        self.assertEqual(account.view()["nav"], nav_before_transfer)
        account.apply_event(fill(10, "perp_fill", "buy", "2", "12.5", ".01", ".002"))
        account.apply_event(fill(11, "spot_fill", "sell", "2", "12", ".01", ".001"))

        # Rational oracle uses independently stated fills, never ledger PnL.
        spot_buy, spot_sell = Fraction("10.1"), Fraction("11.88")
        short_open = Fraction("9.9")
        short_closes = [(Fraction(1), Fraction("12.12")),
                        (Fraction(2), Fraction("12.625"))]
        spot_pnl = Fraction(3) * (spot_sell - spot_buy)
        perp_pnl = sum((q * (short_open - p) for q, p in short_closes), Fraction(0))
        fees = (Fraction(3) * spot_buy * Fraction(".001")
                + Fraction(3) * short_open * Fraction(".002")
                + sum((q * p * Fraction(".002") for q, p in short_closes), Fraction(0))
                + Fraction(3) * spot_sell * Fraction(".001"))
        funding = Fraction(3) * Fraction("12.4") * Fraction(".01")
        expected_nav = Fraction(1500) + spot_pnl + perp_pnl + funding - fees
        view = account.view()
        self.assertEqual(rational(view["nav"]), expected_nav)
        self.assertEqual(view["nav"], "1497.84192")
        self.assertEqual(rational(view["fees_total"]), fees)
        self.assertEqual(rational(view["realized_spot_gross"]), spot_pnl)
        self.assertEqual(rational(view["realized_perp_gross"]), perp_pnl)
        self.assertEqual(view["transfer_inflight"], "0")
        self.assertEqual(view["pending_funding_memo_not_in_nav"], "0")
        self.assertTrue(account.reconciliation()["closed_reconciliation"])

    def test_fifo_cost_uses_exact_lots_instead_of_repeating_average(self):
        account = CarryAccount("100", "100")
        account.apply_event(event(0, "mark", spot_mark="3", perp_mark="3"))
        account.apply_event(fill(1, "spot_fill", "buy", "1", "1"))
        account.apply_event(fill(2, "spot_fill", "buy", "2", "2"))
        account.apply_event(fill(3, "perp_fill", "sell", "1", "1"))
        account.apply_event(fill(4, "perp_fill", "sell", "2", "2"))
        view = account.view()
        self.assertEqual(view["spot_entry_notional_exact"], "5")
        self.assertEqual(view["perp_entry_notional_exact"], "5")
        self.assertNotEqual(rational(view["spot_average_entry"]), Fraction(5, 3))
        account.apply_event(fill(5, "spot_fill", "sell", "1.5", "3"))
        account.apply_event(fill(6, "perp_fill", "buy", "1.5", "3"))
        # Closing 1.5 consumes the first 1 at cost 1, then .5 at cost 2.
        exact_consumed_cost = Fraction(1) + Fraction(1, 2) * 2
        proceeds = Fraction(3, 2) * 3
        self.assertEqual(rational(account.view()["realized_spot_gross"]),
                         proceeds - exact_consumed_cost)
        self.assertEqual(rational(account.view()["realized_perp_gross"]),
                         exact_consumed_cost - proceeds)
        self.assertEqual(account.view()["nav"], "200")
        self.assertTrue(account.reconciliation()["identity_exact"])

    def test_money_is_exact_under_hostile_ambient_decimal_context(self):
        spot_cash = "1234567890123456789012345678901234567890.12345678901234567890"
        deriv_cash = "1000000000000000000000000000000000000000"
        qty = "0.1234567890123456789012345678901234567890"
        mark = "123.456789012345678901234567890123456789"
        price = "123.987654321098765432109876543210987654"
        slip, fee = ".00012345", ".0003456"
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_DOWN
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            account = CarryAccount(spot_cash, deriv_cash)
            account.apply_event(event(0, "mark", spot_mark=mark, perp_mark=mark))
            account.apply_event(fill(1, "spot_fill", "buy", qty, price, slip, fee))
            account.apply_event(fill(2, "perp_fill", "sell", qty, price, slip, fee))
            view = account.view()
            reconciliation = account.reconciliation()
        q, p, m = rational(qty), rational(price), rational(mark)
        buy_price = p * (1 + rational(slip))
        sell_price = p * (1 - rational(slip))
        buy_fee, sell_fee = q * buy_price * rational(fee), q * sell_price * rational(fee)
        expected_spot_cash = rational(spot_cash) - q * buy_price - buy_fee
        expected_wallet = rational(deriv_cash) - sell_fee
        expected_nav = expected_spot_cash + expected_wallet + q * m + q * (sell_price - m)
        self.assertEqual(rational(view["spot_cash"]), expected_spot_cash)
        self.assertEqual(rational(view["deriv_wallet_cash_signed"]), expected_wallet)
        self.assertEqual(rational(view["nav"]), expected_nav)
        self.assertEqual(rational(view["fees_total"]), buy_fee + sell_fee)
        self.assertTrue(reconciliation["identity_exact"])

    def test_duplicate_and_rejected_events_leave_complete_state_unchanged(self):
        account = CarryAccount("100", "30")
        mark = event(0, "mark", spot_mark="10", perp_mark="10")
        account.apply_event(mark)
        committed = fill(1, "spot_fill", "buy", "2", "10")
        account.apply_event(committed)
        before = canonical_bytes(account.snapshot())
        response = account.apply_event(copy.deepcopy(committed))
        self.assertEqual(response["status"], "IDEMPOTENT_COMMITTED_NOOP")
        response["state"]["spot_cash"] = "-999"
        self.assertEqual(canonical_bytes(account.snapshot()), before)
        conflicting = copy.deepcopy(committed)
        conflicting["qty"] = "1"
        future = fill(3, "perp_fill", "sell", "1", "10")
        future["known_at"] = at(4)
        bad_decimal = fill(4, "spot_fill", "buy", "1E61", "10")
        wrong_order = event(0, "mark", spot_mark="11", perp_mark="11")
        wrong_order["id"] = "out-of-order"
        unknown = event(5, "mark", spot_mark="10", perp_mark="10", magic=True)
        for rejected in (conflicting, future, bad_decimal, wrong_order, unknown,
                         fill(6, "perp_fill", "sell", "3", "10"),
                         fill(7, "spot_fill", "sell", "3", "10")):
            with self.subTest(event_id=rejected["id"]):
                with self.assertRaises(AccountingError):
                    account.apply_event(rejected)
                self.assertEqual(canonical_bytes(account.snapshot()), before)

    def test_restart_replays_pending_cash_and_rejects_tampered_state(self):
        original = CarryAccount("1000", "40")
        original.apply_event(event(0, "mark", spot_mark="100", perp_mark="100"))
        original.apply_event(fill(1, "spot_fill", "buy", "1", "100"))
        original.apply_event(fill(2, "perp_fill", "sell", "1", "100"))
        original.apply_event(event(
            3, "funding_settlement", settlement_id="negative-funding",
            settlement_time=at(3), payment_time=at(6),
            eligible_perp_qty_signed="-1", settlement_mark="100", rate="-.1"))
        original.apply_event(event(4, "transfer_send", transfer_id="pending-transfer",
                                  source="spot", target="deriv", amount="7"))
        snapshot = json.loads(json.dumps(original.snapshot()))
        resumed = CarryAccount.restore(snapshot)
        self.assertEqual(resumed.snapshot(), original.snapshot())
        self.assertEqual(resumed.view()["pending_funding_memo_not_in_nav"], "-10")
        self.assertEqual(resumed.view()["transfer_inflight"], "7")
        for account in (original, resumed):
            account.apply_event(event(5, "transfer_receive", transfer_id="pending-transfer"))
            account.apply_event(event(6, "funding_post", settlement_id="negative-funding"))
        self.assertEqual(resumed.snapshot(), original.snapshot())
        self.assertEqual(resumed.view()["deriv_wallet_cash_signed"], "37")
        self.assertEqual(resumed.view()["nav"], "1030")
        for mutation in ("final_state", "incomplete_intent", "duplicated_event"):
            broken = copy.deepcopy(snapshot)
            if mutation == "final_state":
                broken["final_state"]["nav"] = "9999"
            elif mutation == "incomplete_intent":
                broken["incomplete_intent"] = True
            else:
                broken["complete_committed_events"].append(
                    copy.deepcopy(broken["complete_committed_events"][-1]))
            broken["snapshot_sha256"] = sha256_value(
                {k: v for k, v in broken.items() if k != "snapshot_sha256"})
            with self.subTest(mutation=mutation):
                with self.assertRaises(AccountingError):
                    CarryAccount.restore(broken)

    def test_pending_positive_funding_cannot_supply_margin_or_transfer_cash(self):
        account = CarryAccount("1000", "21")
        account.apply_event(event(0, "mark", spot_mark="100", perp_mark="100"))
        account.apply_event(fill(1, "spot_fill", "buy", "1", "100"))
        account.apply_event(fill(2, "perp_fill", "sell", "1", "100"))
        account.apply_event(event(
            3, "funding_settlement", settlement_id="large-memo",
            settlement_time=at(3), payment_time=at(7),
            eligible_perp_qty_signed="-1", settlement_mark="100", rate="1"))
        before = canonical_bytes(account.snapshot())
        self.assertEqual(account.view()["pending_funding_memo_not_in_nav"], "100")
        self.assertEqual(account.view()["available_margin_diagnostic"], "1")
        with self.assertRaises(AccountingError):
            account.apply_event(event(4, "transfer_send", transfer_id="unsafe-transfer",
                                      source="deriv", target="spot", amount="2"))
        self.assertEqual(canonical_bytes(account.snapshot()), before)
        account.apply_event(event(5, "transfer_send", transfer_id="safe-transfer",
                                  source="deriv", target="spot", amount="1"))
        self.assertEqual(account.view()["available_margin_diagnostic"], "0")
        self.assertEqual(account.view()["nav"], "1021")

    def test_forced_close_records_debit_liability_once_without_spot_collateral(self):
        account = CarryAccount("1000", "30")
        account.apply_event(event(0, "mark", spot_mark="100", perp_mark="100"))
        account.apply_event(fill(1, "spot_fill", "buy", "1", "100"))
        account.apply_event(fill(2, "perp_fill", "sell", "1", "100"))
        account.apply_event(event(3, "mark", spot_mark="200", perp_mark="200"))
        self.assertTrue(account.view()["perp_margin_breached"])
        self.assertEqual(account.view()["margin_equity"], "-70")
        account.apply_event(fill(4, "liquidation_fill", "buy", "1", "200",
                                 fee=".01", fixed_fee="5"))
        view = account.view()
        self.assertEqual(view["deriv_wallet_asset"], "0")
        self.assertEqual(view["deriv_debit_liability"], "77")
        self.assertEqual(view["fees_total"], "7")
        self.assertEqual(rational(view["nav"]), Fraction(900) + 200 - 77)
        self.assertEqual(view["nav"], "1023")
        self.assertTrue(account.reconciliation()["identity_exact"])
        self.assertFalse(account.reconciliation()["no_derivative_debit_liability"])

    def test_exact_numeric_event_cap_rejects_new_commit_but_allows_duplicate(self):
        account = CarryAccount("1", "1")
        for index in range(NUMERIC_DOMAIN["max_committed_events"]):
            account.apply_event(event(index, "mark", spot_mark="1", perp_mark="1"))
        before = canonical_bytes(account.snapshot())
        duplicate = event(0, "mark", spot_mark="1", perp_mark="1")
        self.assertEqual(account.apply_event(duplicate)["status"], "IDEMPOTENT_COMMITTED_NOOP")
        with self.assertRaises(AccountingError):
            account.apply_event(event(128, "mark", spot_mark="1", perp_mark="1"))
        self.assertEqual(canonical_bytes(account.snapshot()), before)


if __name__ == "__main__":
    unittest.main()
