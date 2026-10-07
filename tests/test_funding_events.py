"""Original, synthetic known answers for event identity and causal funding cash."""
import copy
from decimal import localcontext
from fractions import Fraction
import unittest

from research_base.funding_events import (
    CompilerError, SyntheticFundingBridge, canonical, compile_observations,
    digest, event_identity, iso_ms, make_synthetic_contract,
)


AT = 86400000  # An arbitrary synthetic UTC midnight, not a market fixture.
MARKET = "TEST_LINEAR"
SYMBOL = "UNIT_QUOTE"


def action(identity, kind, at, seq=0, **fields):
    stamp = iso_ms(at)
    return {"id": identity, "type": kind, "event_time": stamp,
            "applied_at": stamp, "known_at": stamp, "sequence": seq, **fields}


def sample_row(at, rate="0.015", interval="3", line=2):
    return {"calc_time": str(at), "funding_interval_hours": interval,
            "last_funding_rate": rate, "source_line": line}


def sample_mark(at, low="4.5", high="5.5"):
    return {"open_time_ms": at, "close_time_ms": at+59999,
            "open": "5", "close": "5.25", "low": low, "high": high}


def compile_rows(rows, marks=()):
    return compile_observations(rows, list(marks), market=MARKET, symbol=SYMBOL)


def funded_bridge(*, short=True):
    bridge = SyntheticFundingBridge("70", "40", market=MARKET, symbol=SYMBOL)
    bridge.apply_action(action("initial-mark", "mark", AT, spot_mark="8", perp_mark="8"))
    bridge.apply_action(action("funded-spot", "spot_fill", AT, 1, side="buy", qty="3",
                               reference_price="8", slippage_rate="0", fee_rate="0"))
    if short:
        bridge.apply_action(action("initial-short", "perp_fill", AT, 2, side="sell", qty="3",
                                   reference_price="8", slippage_rate="0", fee_rate="0"))
    return bridge


def synthetic_contract(bridge, *, at=AT+1000, paid=AT+5000,
                       qty="-3", rate="0.004", mark="8.25", seq=0, paid_seq=0,
                       market=MARKET, symbol=SYMBOL):
    return make_synthetic_contract(
        market=market, symbol=symbol, settlement_time_ms=at, payment_time_ms=paid,
        rate=rate, eligible_perp_qty_signed=qty, settlement_mark=mark,
        pre_event_account_view_sha256=bridge.pre_event_view_sha256(),
        capture_sequence=seq, posting_sequence=paid_seq,
        rate_known_at_ms=at, eligibility_known_at_ms=at, mark_known_at_ms=at,
    )


class FundingObservationTests(unittest.TestCase):
    def test_preserves_millisecond_identity_and_minute_boundary(self):
        times = [AT, AT+1, AT+2, AT+59999, AT+60000]
        rows = [sample_row(t, line=i+2) for i, t in enumerate(times)]
        result = compile_rows(rows, [sample_mark(AT), sample_mark(AT+60000, "4", "6")])
        observations = result["accepted_observations"]
        self.assertEqual([row["calc_time_ms"] for row in observations], times)
        self.assertEqual(len({row["event_id"] for row in observations}), len(times))
        self.assertEqual([row["offset_from_containing_minute_ms"] for row in observations],
                         [0, 1, 2, 59999, 0])
        self.assertEqual(observations[3]["mark_reference_envelope"]["minute_open_ms"], AT)
        self.assertEqual(observations[4]["mark_reference_envelope"]["minute_open_ms"], AT+60000)
        self.assertEqual(observations[1]["calc_time_utc"], "1970-01-02T00:00:00.001+00:00")

    def test_namespace_is_explicit_and_part_of_event_identity(self):
        base = event_identity(MARKET, SYMBOL, AT)
        self.assertNotEqual(base, event_identity("OTHER_LINEAR", SYMBOL, AT))
        self.assertNotEqual(base, event_identity(MARKET, "OTHER_QUOTE", AT))
        with self.assertRaises(TypeError):
            compile_observations([], [])

    def test_numeric_duplicates_keep_raw_tokens_and_are_order_invariant(self):
        rows = [sample_row(AT, "0.0150", "3.000", 3), sample_row(AT, ".015", "3", 2)]
        original = copy.deepcopy(rows)
        result = compile_rows(rows, [sample_mark(AT)])
        self.assertEqual(rows, original)
        self.assertEqual(result, compile_rows(list(reversed(rows)), [sample_mark(AT)]))
        observation, = result["accepted_observations"]
        self.assertEqual(observation["observed_rate"], "0.015")
        self.assertEqual(observation["duplicate_instances"], 1)
        self.assertEqual({item["raw_observed_rate"] for item in observation["source_instances"]},
                         {"0.0150", ".015"})

    def test_conflicting_rate_or_interval_is_quarantined(self):
        for conflict in [sample_row(AT, "0.02", line=3),
                         sample_row(AT, interval="1", line=3)]:
            with self.subTest(conflict=conflict):
                result = compile_rows([sample_row(AT), conflict])
                self.assertEqual(result["accepted_observations"], [])
                quarantine, = result["quarantined_conflicts"]
                self.assertEqual(quarantine["reason"], "CONFLICTING_OBSERVATION_IDENTITY")
                self.assertTrue(quarantine["cannot_post_actual_cash"])
                self.assertEqual(result["actual_cash_postings"], [])

    def test_observed_mark_range_and_rate_never_authorize_cash(self):
        result = compile_rows([sample_row(AT)], [sample_mark(AT)])
        observed, = result["accepted_observations"]
        self.assertEqual(observed["mark_reference_envelope"]["low"], "4.5")
        self.assertEqual(observed["mark_reference_envelope"]["high"], "5.5")
        self.assertFalse(observed["mark_reference_envelope"]["exact_settlement_mark_provided"])
        self.assertTrue(observed["cannot_post_actual_cash"])
        for field in ["eligible_perp_qty_signed", "exact_settlement_mark", "actual_payment_time_ms",
                      "historical_known_at_ms", "account_cash_credit_evidence", "fee_evidence"]:
            self.assertIsNone(observed[field])
        self.assertFalse(result["actual_cash_posting_qualified"])
        self.assertEqual(result["actual_cash_postings"], [])
        missing, = compile_rows([sample_row(AT)])["accepted_observations"]
        self.assertIsNone(missing["mark_reference_envelope"])
        self.assertTrue(missing["cannot_post_actual_cash"])
        changed_interval, = compile_rows([sample_row(AT, interval="1")])["accepted_observations"]
        self.assertEqual(observed["observed_rate"], changed_interval["observed_rate"])
        self.assertFalse(changed_interval["declared_interval_is_rate_multiplier"])

    def test_ambiguous_timestamp_or_reference_fails_closed(self):
        for bad_time in [True, 86400000.0, "086400000", "8.64e7", -1]:
            with self.subTest(bad_time=bad_time), self.assertRaises(CompilerError):
                compile_rows([sample_row(bad_time)])
        with self.assertRaises(CompilerError):
            compile_rows([sample_row(AT)], [sample_mark(AT), sample_mark(AT, "4", "6")])
        wrong_interval = sample_mark(AT)
        wrong_interval["close_time_ms"] += 1
        with self.assertRaises(CompilerError):
            compile_rows([sample_row(AT)], [wrong_interval])

    def test_decimal_context_cannot_round_an_out_of_domain_rate_into_acceptance(self):
        too_large = "1."+"0"*58+"1"
        with localcontext() as context:
            context.prec = 2
            with self.assertRaises(CompilerError):
                compile_rows([sample_row(AT, too_large)])
            tiny, = compile_rows([sample_row(AT, "1E-60")])["accepted_observations"]
        self.assertEqual(Fraction(tiny["observed_rate"]), Fraction(1, 10**60))


class SyntheticFundingBridgeTests(unittest.TestCase):
    def assert_rejected_without_mutation(self, bridge, callback):
        before = canonical(bridge.snapshot())
        with self.assertRaises(CompilerError):
            callback()
        self.assertEqual(canonical(bridge.snapshot()), before)

    def test_pre_action_capture_survives_close_and_later_post(self):
        bridge = funded_bridge()
        contract = synthetic_contract(bridge)
        captured = bridge.capture(contract)
        expected = -Fraction(-3)*Fraction("8.25")*Fraction("0.004")
        self.assertEqual(Fraction(captured["details"]["captured_synthetic_cash_amount"]), expected)
        self.assertEqual(bridge.account.view()["funding_cash_posted"], "0")
        self.assertEqual(bridge.account.view()["nav"], "110")
        bridge.apply_action(action("close-short", "perp_fill", AT+2000, side="buy", qty="3",
                                   reference_price="8.5", slippage_rate="0", fee_rate="0"))
        bridge.apply_action(action("close-spot", "spot_fill", AT+2000, 1, side="sell", qty="3",
                                   reference_price="8.5", slippage_rate="0", fee_rate="0"))
        self.assertEqual(bridge.account.view()["perp_qty_signed"], "0")
        self.assertEqual(bridge.account.view()["deriv_wallet_cash_signed"], "38.5")
        recovered = SyntheticFundingBridge.restore(bridge.snapshot())
        result = bridge.post(contract["event_id"])
        recovered.post(contract["event_id"])
        self.assertEqual(Fraction(result["details"]["synthetic_cash_amount"]), expected)
        self.assertEqual(bridge.account.view()["deriv_wallet_cash_signed"], "38.599")
        self.assertEqual(bridge.account.view()["nav"], "110.099")
        self.assertFalse(result["details"]["actual_cash_posting_verified"])
        self.assertEqual(recovered.snapshot(), bridge.snapshot())

    def test_new_short_after_flat_settlement_does_not_receive_old_funding(self):
        bridge = funded_bridge(short=False)
        contract = synthetic_contract(bridge, qty="0")
        bridge.capture(contract)
        bridge.apply_action(action("new-short", "perp_fill", AT+2000, side="sell", qty="3",
                                   reference_price="8", slippage_rate="0", fee_rate="0"))
        self.assert_rejected_without_mutation(bridge, lambda: bridge.post(contract["event_id"]))
        self.assertEqual(bridge.account.view()["funding_cash_posted"], "0")
        restored = SyntheticFundingBridge.restore(bridge.snapshot())
        self.assertEqual(restored.capture(contract)["status"], "IDEMPOTENT_COMMITTED_NOOP")
        self.assertEqual(restored.snapshot(), bridge.snapshot())

    def test_negative_rate_debits_short_once(self):
        bridge = funded_bridge()
        contract = synthetic_contract(bridge, mark="8", rate="-0.02")
        bridge.capture(contract)
        bridge.post(contract["event_id"])
        self.assertEqual(bridge.account.view()["funding_cash_posted"], "-0.48")
        self.assertEqual(bridge.account.view()["deriv_wallet_cash_signed"], "39.52")
        after = bridge.snapshot()
        self.assertEqual(bridge.post(contract["event_id"])["status"], "IDEMPOTENT_COMMITTED_NOOP")
        self.assertEqual(bridge.capture(contract)["status"], "IDEMPOTENT_COMMITTED_NOOP")
        self.assertEqual(bridge.snapshot(), after)

    def test_same_millisecond_capture_close_post_uses_explicit_sequence(self):
        bridge = funded_bridge()
        contract = synthetic_contract(bridge, paid=AT+1000, paid_seq=2)
        bridge.capture(contract)
        bridge.apply_action(action("same-time-close", "perp_fill", AT+1000, 1,
                                   side="buy", qty="3", reference_price="8",
                                   slippage_rate="0", fee_rate="0"))
        bridge.post(contract["event_id"])
        self.assertEqual(bridge.account.view()["funding_cash_posted"], "0.099")
        self.assertEqual(bridge.account.view()["perp_qty_signed"], "0")

    def test_archive_observation_or_cash_action_bypass_is_rejected(self):
        bridge = funded_bridge()
        observed, = compile_rows([sample_row(AT+1000)])["accepted_observations"]
        self.assert_rejected_without_mutation(bridge, lambda: bridge.capture(observed))
        for kind in ["funding_settlement", "funding_post"]:
            self.assert_rejected_without_mutation(
                bridge, lambda kind=kind: bridge.apply_action({"id": "bypass", "type": kind}))

    def test_unknown_mark_eligibility_or_instrument_cannot_be_posted(self):
        bridge = funded_bridge()
        contract = synthetic_contract(bridge)
        for field in ["settlement_mark", "eligible_perp_qty_signed", "payment_time_ms",
                      "eligibility_known_at_ms"]:
            unknown = copy.deepcopy(contract)
            unknown[field] = None
            self.assert_rejected_without_mutation(bridge, lambda unknown=unknown: bridge.capture(unknown))
        foreign = synthetic_contract(bridge, symbol="FOREIGN_QUOTE")
        self.assert_rejected_without_mutation(bridge, lambda: bridge.capture(foreign))
        self.assert_rejected_without_mutation(bridge, lambda: bridge.post(contract["event_id"]))

    def test_stale_position_future_knowledge_and_identity_conflict_are_atomic(self):
        bridge = funded_bridge()
        contract = synthetic_contract(bridge)
        for field, value in [("eligible_perp_qty_signed", "-2"),
                             ("pre_event_account_view_sha256", "0"*64),
                             ("rate_known_at_ms", AT+1001)]:
            invalid = copy.deepcopy(contract)
            invalid[field] = value
            self.assert_rejected_without_mutation(bridge, lambda invalid=invalid: bridge.capture(invalid))
        bridge.capture(contract)
        conflict = copy.deepcopy(contract)
        conflict["rate"] = "0.02"
        self.assert_rejected_without_mutation(bridge, lambda: bridge.capture(conflict))
        bridge.apply_action(action("later-mark", "mark", AT+6000, spot_mark="8", perp_mark="8"))
        self.assert_rejected_without_mutation(bridge, lambda: bridge.post(contract["event_id"]))

    def test_snapshot_replay_rejects_tampered_or_unresolved_state(self):
        bridge = funded_bridge()
        bridge.capture(synthetic_contract(bridge))
        original = bridge.snapshot()
        restored = SyntheticFundingBridge.restore(original)
        self.assertEqual(restored.snapshot(), original)
        for field, value in [("incomplete_intent", True), ("symbol", "FOREIGN_QUOTE")]:
            changed = copy.deepcopy(original)
            changed[field] = value
            changed["snapshot_sha256"] = digest({k: v for k, v in changed.items() if k != "snapshot_sha256"})
            with self.assertRaises(CompilerError):
                SyntheticFundingBridge.restore(changed)
        changed = copy.deepcopy(original)
        key = next(iter(changed["final_synthetic_settlements"]))
        changed["final_synthetic_settlements"][key]["captured_synthetic_cash_amount"] = "99"
        changed["snapshot_sha256"] = digest({k: v for k, v in changed.items() if k != "snapshot_sha256"})
        with self.assertRaises(CompilerError):
            SyntheticFundingBridge.restore(changed)

    def test_zero_eligibility_steps_cannot_expand_the_128_step_bridge_bound(self):
        bridge = SyntheticFundingBridge("9", "6", market=MARKET, symbol=SYMBOL)
        for step in range(128):
            at = AT+step
            bridge.capture(synthetic_contract(bridge, at=at, paid=at+1, qty="0"))
        self.assertEqual(len(bridge.operations), 128)
        self.assertEqual(len(bridge.account.events), 0)
        extra = synthetic_contract(bridge, at=AT+128, paid=AT+129, qty="0")
        self.assert_rejected_without_mutation(bridge, lambda: bridge.capture(extra))


if __name__ == "__main__":
    unittest.main()
