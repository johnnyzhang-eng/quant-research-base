"""Finite synthetic next-intent controls; no market, account or order adapters."""
import argparse
import copy
from decimal import localcontext
from fractions import Fraction
import json
from pathlib import Path
import sys
import unittest

from research_base.funding_allocation import (
    MODE, SCHEMA, PlannerInputError, plan_allocation, validate_fixture_intent,
)
from research_base.funding_events import (
    SyntheticFundingBridge, canonical, iso_ms, make_synthetic_contract,
)


AT = 86400000
MARKET, SYMBOL = "TEST_LINEAR", "UNIT_QUOTE"
POLICY = {"schema": SCHEMA, "lower_buffer": "5", "target_buffer": "8",
          "upper_buffer": "1200"}


def event(identity, kind, offset, seq=0, **fields):
    stamp = iso_ms(AT+offset)
    return {"id": identity, "type": kind, "event_time": stamp,
            "applied_at": stamp, "known_at": stamp, "sequence": seq, **fields}


def context(bridge, target, offset, fee="0.001", reserve=0, **changes):
    view = bridge.pre_event_view()
    at = AT+offset
    data = {"schema": SCHEMA, "mode": MODE, "market": MARKET, "symbol": SYMBOL,
        "decision_time_ms": at, "target_qty": target, "target_known_at_ms": at,
        "spot_reference_price": view["spot_mark"], "perp_reference_price": view["perp_mark"],
        "spot_fee_rate": fee, "perp_fee_rate": fee, "spot_slippage_rate": "0",
        "perp_slippage_rate": "0", "quotes_known_at_ms": at, "fees_known_at_ms": at,
        "input_permission": "DECLARED_SYNTHETIC_FIXTURE", "permission_known_at_ms": at,
        "transfer_fee": "0", "transfer_arrival_time_ms": at+1,
        "transfer_known_at_ms": at, "reserved_future_operations": reserve,
        "expected_view_sha256": bridge.pre_event_view_sha256()}
    data.update(changes)
    return data


def decide(bridge, target, offset, fee="0.001", reserve=0, **changes):
    return plan_allocation(bridge_snapshot=bridge.snapshot(),
        decision_context=context(bridge, target, offset, fee, reserve, **changes), policy=POLICY)


def confirm_one(bridge, plan, trace):
    # Only this explicit fixture controller confirms the proposed event.
    request = validate_fixture_intent(plan, bridge_snapshot=bridge.snapshot())
    before = bridge.pre_event_view_sha256()
    receipt = bridge.apply_action(request)
    if receipt["status"] != "COMMITTED":
        raise AssertionError("Expected exactly one confirmed fixture event")
    trace.append({"plan_sha256": plan["plan_sha256"], "status": plan["status"],
        "reason": plan["reason"], "event": request, "before_view_sha256": before,
        "after_view_sha256": bridge.pre_event_view_sha256(), "receipt": receipt})
    return request


def move_target(bridge, target, offset, trace, fee="0.001"):
    for _ in range(6):
        plan = decide(bridge, target, offset, fee)
        if plan["intent"] is None:
            if plan["status"] not in {"HOLD", "STOP_RISK"}:
                raise AssertionError(plan)
            return plan
        confirm_one(bridge, plan, trace)
    raise AssertionError("Bounded target transition did not finish")


def boot(spot="1000", deriv="1000", qty="2", fee="0.001"):
    bridge = SyntheticFundingBridge(spot, deriv, market=MARKET, symbol=SYMBOL)
    bridge.apply_action(event("initial-mark", "mark", 0, spot_mark="100", perp_mark="100"))
    trace = []
    move_target(bridge, qty, 1, trace, fee)
    return bridge, trace


def funding(bridge, offset, paid, seq=0, flat=False, rate="0.005"):
    contract = make_synthetic_contract(market=MARKET, symbol=SYMBOL,
        settlement_time_ms=AT+offset, payment_time_ms=AT+paid, rate=rate,
        eligible_perp_qty_signed="0" if flat else bridge.pre_event_view()["perp_qty_signed"],
        settlement_mark="100", pre_event_account_view_sha256=bridge.pre_event_view_sha256(),
        capture_sequence=seq, posting_sequence=0, rate_known_at_ms=AT+offset,
        eligibility_known_at_ms=AT+offset, mark_known_at_ms=AT+offset)
    bridge.capture(contract)
    return contract


def summary(bridge, trace):
    snapshot = bridge.snapshot()
    restored = SyntheticFundingBridge.restore(snapshot)
    if restored.snapshot() != snapshot:
        raise AssertionError("Independent complete fixture replay differs")
    return {"state": bridge.pre_event_view(), "reconciliation": bridge.account.reconciliation(),
        "bridge_operations": len(bridge.operations), "snapshot_sha256": snapshot["snapshot_sha256"],
        "restore_matches": True, "trace": trace, "operations": copy.deepcopy(bridge.operations)}


def p1():
    bridge, trace = boot()
    before = canonical(bridge.snapshot())
    first, second = decide(bridge, "2", 3), decide(bridge, "2", 3)
    return {"state": bridge.pre_event_view(), "decision": first,
        "same_plan": first == second, "input_unchanged": before == canonical(bridge.snapshot()),
        "opening_trace": trace}


def p2_branch(dynamic):
    bridge, trace = boot()
    first = funding(bridge, 10, 11)
    bridge.post(first["event_id"])
    if dynamic:
        move_target(bridge, "1", 12, trace)
    second = funding(bridge, 20, 21)
    bridge.post(second["event_id"])
    if dynamic:
        move_target(bridge, "2", 22, trace)
    move_target(bridge, "0", 30, trace)
    return summary(bridge, trace)


def p2():
    static, dynamic = p2_branch(False), p2_branch(True)
    sv, dv = static["state"], dynamic["state"]
    return {"static": static, "dynamic": dynamic,
        "difference_nav": str(Fraction(dv["nav"])-Fraction(sv["nav"])),
        "difference_funding": str(Fraction(dv["funding_cash_posted"])-Fraction(sv["funding_cash_posted"])),
        "difference_fee_effect": str(Fraction(sv["fees_total"])-Fraction(dv["fees_total"]))}


def p3_branch(early):
    bridge, trace = boot(deriv="30", qty="1", fee="0")
    bridge.apply_action(event("mark110", "mark", 3, spot_mark="110", perp_mark="110"))
    pre_send = bridge.pre_event_view()
    plan = decide(bridge, "1", 4, "0")
    send = confirm_one(bridge, plan, trace)
    after_send = bridge.pre_event_view()
    waiting = decide(bridge, "2", 4, "0")
    transfer_id = send["transfer_id"]
    if early:
        bridge.apply_action(event("receive", "transfer_receive", 5, transfer_id=transfer_id))
        bridge.apply_action(event("mark120", "mark", 6, spot_mark="120", perp_mark="120"))
    else:
        bridge.apply_action(event("mark120", "mark", 6, spot_mark="120", perp_mark="120"))
        before_late_receive = bridge.pre_event_view()
        bridge.apply_action(event("receive", "transfer_receive", 7, transfer_id=transfer_id))
    after_receive = bridge.pre_event_view()
    risk_plan = decide(bridge, "2" if not early else "0", 8, "0")
    move_target(bridge, "2" if not early else "0", 8, trace, "0")
    return {"pre_send": pre_send, "send_intent": send, "after_send": after_send,
        "wait_plan": waiting, "before_late_receive": None if early else before_late_receive,
        "after_receive": after_receive, "risk_decision": risk_plan, "final": summary(bridge, trace)}


def p3():
    return {"early": p3_branch(True), "late": p3_branch(False)}


def p4_branch(before_capture):
    bridge, trace = boot(fee="0")
    if before_capture:
        move_target(bridge, "1", 10, trace, "0")
        c = funding(bridge, 10, 11, seq=2)
    else:
        c = funding(bridge, 10, 11)
        move_target(bridge, "1", 10, trace, "0")
    bridge.post(c["event_id"])
    move_target(bridge, "0", 12, trace, "0")
    return summary(bridge, trace)


def p4():
    return {"before_capture": p4_branch(True), "after_capture": p4_branch(False)}


def p5():
    bridge, _ = boot()
    base = canonical(bridge.snapshot())
    variants = [
        ("future_quote", {"quotes_known_at_ms": AT+4}, "BLOCKED"),
        ("future_target", {"target_known_at_ms": AT+4}, "BLOCKED"),
        ("unknown_fee", {"spot_fee_rate": None}, "BLOCKED"),
        ("unknown_slippage", {"perp_slippage_rate": None}, "BLOCKED"),
        ("unknown_permission", {"input_permission": None}, "BLOCKED"),
        ("live_mode", {"mode": "LIVE"}, "BLOCKED"),
        ("archive_permission", {"input_permission": "ARCHIVE_OBSERVATION_ONLY"}, "BLOCKED"),
        ("transfer_fee", {"transfer_fee": "1"}, "BLOCKED"),
        ("stale_view", {"expected_view_sha256": "0"*64}, "BLOCKED"),
        ("budget", {"reserved_future_operations": 126}, "BLOCKED"),
    ]
    rows = []
    for label, changes, expected in variants:
        plan = decide(bridge, "3", 3, **changes)
        if plan["status"] != expected or plan["intent"] is not None:
            raise AssertionError((label, plan))
        if canonical(bridge.snapshot()) != base:
            raise AssertionError("Planner mutated source snapshot")
        rows.append({"case": label, "status": plan["status"], "reason": plan["reason"]})
    invalid = context(bridge, "3", 3)
    invalid["margin_override"] = "0"
    try:
        plan_allocation(bridge_snapshot=bridge.snapshot(), decision_context=invalid, policy=POLICY)
    except PlannerInputError:
        rows.append({"case": "unknown_field", "status": "PlannerInputError"})
    else:
        raise AssertionError("Unknown field accepted")
    allowed = decide(bridge, "3", 3, reserve=124)
    if allowed["status"] != "NEXT_INTENT":
        raise AssertionError(allowed)
    bridge.apply_action(event("state-changed", "mark", 4, spot_mark="100", perp_mark="100"))
    try:
        validate_fixture_intent(allowed, bridge_snapshot=bridge.snapshot())
    except PlannerInputError:
        rows.append({"case": "changed_snapshot_before_confirmation", "status": "PlannerInputError"})
    else:
        raise AssertionError("Stale proposal released")
    low, _ = boot(deriv="30", qty="1", fee="0")
    low.apply_action(event("mark110", "mark", 3, spot_mark="110", perp_mark="110"))
    arrival_unknown = decide(low, "1", 4, "0", transfer_arrival_time_ms=None)
    if arrival_unknown["status"] != "WAIT" or arrival_unknown["intent"] is not None:
        raise AssertionError(arrival_unknown)
    rows.append({"case": "unknown_arrival", "status": arrival_unknown["status"],
                 "reason": arrival_unknown["reason"]})
    flat = SyntheticFundingBridge("10", "10", market=MARKET, symbol=SYMBOL)
    flat.apply_action(event("mark", "mark", 0, spot_mark="100", perp_mark="100"))
    funding(flat, 1, 2, flat=True)
    separate_budget = decide(flat, "0", 3, reserve=127)
    if separate_budget["status"] != "BLOCKED" or separate_budget["reason"] != "EVENT_BUDGET":
        raise AssertionError(separate_budget)
    rows.append({"case": "bridge_count_includes_flat_capture", "status": "BLOCKED",
        "bridge_operations": len(flat.operations), "account_events": len(flat.account.events)})
    return {"rejections": rows, "budget_exact_boundary_allowed": True,
            "source_snapshot_unchanged_before_explicit_counterfactual_mark": True}


def pad_to(bridge, count, price="100"):
    for index in range(len(bridge.operations), count):
        bridge.apply_action(event("completion-pad-"+str(index), "mark", index,
                                  spot_mark=price, perp_mark=price))
    if len(bridge.operations) != count:
        raise AssertionError("Incorrect independently counted prefix")


def completion_budget_controls():
    data = {}
    # Independent hand count: prefix124 + send1 + receive1 + close2 =128.
    # Prefix125 has the identical economic path but needs129 operations.
    for count in (124, 125):
        bridge, trace = boot(deriv="30", qty="1", fee="0")
        bridge.apply_action(event("mark110", "mark", 3, spot_mark="110", perp_mark="110"))
        pad_to(bridge, count, "110")
        before = canonical(bridge.snapshot())
        plan = decide(bridge, "1", 200, "0")
        row = {"starting_operations": count, "planned_required_total": count+4,
               "plan": plan, "unchanged_before_confirmation": before == canonical(bridge.snapshot())}
        if count == 124:
            send = confirm_one(bridge, plan, trace)
            row["waiting"] = decide(bridge, "1", 200, "0")
            bridge.apply_action(event("receive", "transfer_receive", 201,
                                      transfer_id=send["transfer_id"]))
            move_target(bridge, "0", 202, trace, "0")
            row["final"] = summary(bridge, trace)
        data["new_send_"+str(count)] = row
    # An existing unposted obligation needs one post even after both legs flat.
    for count in (125, 126):
        bridge, trace = boot(qty="1", fee="0")
        pad_to(bridge, count-1)
        c = funding(bridge, 150, 250)
        before = canonical(bridge.snapshot())
        plan = decide(bridge, "0", 200, "0")
        row = {"starting_operations": count, "planned_required_total": count+3,
               "plan": plan, "hold_plan": decide(bridge, "1", 200, "0"),
               "unchanged_before_confirmation": before == canonical(bridge.snapshot())}
        if count == 125:
            move_target(bridge, "0", 200, trace, "0")
            row["before_post_operations"] = len(bridge.operations)
            bridge.post(c["event_id"])
            row["final"] = summary(bridge, trace)
        else:
            risk = SyntheticFundingBridge.restore(bridge.snapshot())
            risk.apply_action(event("risk-mark", "mark", 201,
                                    spot_mark="10000", perp_mark="10000"))
            row["risk_plan"] = decide(risk, "2", 202, "0")
        data["pending_post_"+str(count)] = row
    # A receive is required independently of net base position or desired target.
    for count in (125, 126):
        bridge, trace = boot(qty="1", fee="0")
        pad_to(bridge, count-1)
        bridge.apply_action(event("inflight-send", "transfer_send", 150,
            transfer_id="existing-inflight", source="spot", target="deriv", amount="10"))
        before = canonical(bridge.snapshot())
        plan = decide(bridge, "0", 200, "0")
        row = {"starting_operations": count, "planned_required_total": count+3,
               "plan": plan, "wait_plan": decide(bridge, "1", 200, "0"),
               "unchanged_before_confirmation": before == canonical(bridge.snapshot())}
        if count == 125:
            bridge.apply_action(event("receive", "transfer_receive", 201,
                                      transfer_id="existing-inflight"))
            move_target(bridge, "0", 202, trace, "0")
            row["final"] = summary(bridge, trace)
        data["existing_receive_"+str(count)] = row
    # Sign and amount never determine whether a pending bridge post exists.
    for label, rate in [("negative", "-0.005"), ("zero_amount", "0")]:
        bridge, trace = boot(qty="1", fee="0")
        pad_to(bridge, 124)
        c = funding(bridge, 150, 250, rate=rate)
        plan = decide(bridge, "0", 200, "0")
        move_target(bridge, "0", 200, trace, "0")
        before_post = len(bridge.operations)
        bridge.post(c["event_id"])
        data[label+"_funding"] = {"starting_operations": 125, "plan": plan,
            "before_post_operations": before_post, "final": summary(bridge, trace)}
    flat = SyntheticFundingBridge("1000", "1000", market=MARKET, symbol=SYMBOL)
    flat.apply_action(event("initial-mark", "mark", 0, spot_mark="100", perp_mark="100"))
    pad_to(flat, 127)
    c = funding(flat, 150, 250, flat=True)
    flat_plan = decide(flat, "0", 200, "0")
    # Prove flat-post semantics below the cap, so a128-bound error cannot
    # masquerade as the reason that no payable obligation exists.
    low_flat = SyntheticFundingBridge("1000", "1000", market=MARKET, symbol=SYMBOL)
    low_flat.apply_action(event("mark", "mark", 0, spot_mark="100", perp_mark="100"))
    low_c = funding(low_flat, 1, 2, flat=True)
    try:
        low_flat.post(low_c["event_id"])
    except ValueError as exc:
        cannot_post = str(exc) == "Flat settlement creates no payable cash obligation"
    else:
        cannot_post = False
    data["flat_eligibility"] = {"operations": len(flat.operations), "plan": flat_plan,
        "no_payable_post_exists": cannot_post,
        "obligation_status": flat.settlements[c["event_id"]]["status"],
        "below_cap_post_semantics_checked": len(low_flat.operations) == 2,
        "closed_reconciliation": flat.account.reconciliation()["closed_reconciliation"]}
    return data


def run_controls():
    return {"schema": "synthetic-allocation-controls/1", "P1": p1(), "P2": p2(),
            "P3": p3(), "P4": p4(), "P5": p5(), "market_data_used": False,
            "actual_execution_verified": False, "profitability_verified": False,
            "completion_budget": completion_budget_controls()}


class AllocationControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.controls = run_controls()

    def test_p1_hold_is_pure_and_stable(self):
        row = self.controls["P1"]
        self.assertTrue(row["same_plan"] and row["input_unchanged"])
        self.assertEqual(row["decision"]["status"], "HOLD")
        self.assertIsNone(row["decision"]["intent"])
        self.assertEqual(row["state"]["nav"], "1999.6")
        self.assertEqual([(r["event"]["type"], r["event"]["side"])
                          for r in row["opening_trace"]],
                         [("spot_fill", "buy"), ("perp_fill", "sell")])

    def test_p2_static_and_resize_closed_known_answers(self):
        row = self.controls["P2"]
        for label, nav, fee, funding_cash, ops in [
                ("static", "2001.2", "0.8", "2", 9),
                ("dynamic", "2000.3", "1.2", "1.5", 13)]:
            branch = row[label]
            self.assertEqual(branch["state"]["nav"], nav)
            self.assertEqual(branch["state"]["fees_total"], fee)
            self.assertEqual(branch["state"]["funding_cash_posted"], funding_cash)
            self.assertEqual(branch["bridge_operations"], ops)
            self.assertTrue(branch["reconciliation"]["closed_reconciliation"])
            self.assertTrue(branch["restore_matches"])
        self.assertEqual(Fraction(row["difference_nav"]), Fraction("-0.9"))
        self.assertEqual(Fraction(row["difference_funding"]), Fraction("-0.5"))
        self.assertEqual(Fraction(row["difference_fee_effect"]), Fraction("-0.4"))

    def test_p3_im_buffer_arrival_and_sticky_breach(self):
        early, late = self.controls["P3"]["early"], self.controls["P3"]["late"]
        for branch in [early, late]:
            self.assertEqual(branch["send_intent"]["amount"], "10")
            self.assertEqual(branch["after_send"]["transfer_inflight"], "10")
            self.assertEqual(branch["after_send"]["margin_equity"], "20")
            self.assertEqual(branch["wait_plan"]["status"], "WAIT")
            self.assertEqual(branch["final"]["state"]["nav"], "1030")
            self.assertTrue(branch["final"]["reconciliation"]["closed_reconciliation"])
        self.assertFalse(early["after_receive"]["perp_margin_breached"])
        self.assertEqual(late["before_late_receive"]["margin_equity"], "10")
        self.assertTrue(late["after_receive"]["perp_margin_breached"])
        self.assertEqual(late["after_receive"]["margin_equity"], "20")
        self.assertEqual(late["risk_decision"]["status"], "STOP_RISK")
        self.assertEqual(late["risk_decision"]["intent"]["ledger_event_template"]["side"], "buy")
        self.assertTrue(late["final"]["state"]["ever_margin_breached"])

    def test_p4_capture_clocks_and_two_leg_dependencies(self):
        row = self.controls["P4"]
        self.assertEqual(row["before_capture"]["state"]["funding_cash_posted"], "0.5")
        self.assertEqual(row["after_capture"]["state"]["funding_cash_posted"], "1")
        for branch in row.values():
            self.assertTrue(branch["reconciliation"]["closed_reconciliation"])
            resize = [r["event"] for r in branch["trace"] if r["event"]["event_time"] == iso_ms(AT+10)]
            self.assertEqual([(r["type"], r["side"]) for r in resize],
                             [("perp_fill", "buy"), ("spot_fill", "sell")])
            for receipt in branch["trace"]:
                self.assertEqual(receipt["plan_sha256"] is not None, True)
                self.assertNotEqual(receipt["before_view_sha256"], receipt["after_view_sha256"])

    def test_p5_required_inputs_and_budgets_are_blocked(self):
        row = self.controls["P5"]
        self.assertEqual(len(row["rejections"]), 14)
        self.assertTrue(row["budget_exact_boundary_allowed"])
        self.assertTrue(row["source_snapshot_unchanged_before_explicit_counterfactual_mark"])

    def test_ambient_decimal_context_does_not_change_answers(self):
        with localcontext() as ambient:
            ambient.prec = 2
            row = p2()
        self.assertEqual(row["static"]["state"]["nav"], "2001.2")
        self.assertEqual(row["dynamic"]["state"]["nav"], "2000.3")

    def test_terminal_close_slots_survive_a_zero_caller_reserve(self):
        bridge, _ = boot()
        for offset in range(2, 125):
            bridge.apply_action(event("budget-mark-"+str(offset), "mark", offset,
                                      spot_mark="100", perp_mark="100"))
        self.assertEqual(len(bridge.operations), 126)
        before = canonical(bridge.snapshot())
        plan = decide(bridge, "3", 125, reserve=0)
        self.assertEqual(plan["status"], "BLOCKED")
        self.assertEqual(plan["reason"], "COMPLETION_EVENT_BUDGET")
        self.assertIsNone(plan["intent"])
        self.assertEqual(canonical(bridge.snapshot()), before)
        hold = decide(bridge, "2", 125, reserve=0)
        self.assertEqual(hold["status"], "HOLD")
        trace = []
        move_target(bridge, "0", 125, trace)
        self.assertEqual(len(bridge.operations), 128)
        self.assertTrue(bridge.account.reconciliation()["closed_reconciliation"])
        self.controls["P5"]["terminal_close_boundary"] = {
            "starting_operations": 126, "growth_blocked_even_caller_reserve0": True,
            "closed_operations": 128, "closed_nav": bridge.pre_event_view()["nav"],
            "close_trace": trace}

    def test_full_completion_counts_new_and_existing_receipts(self):
        rows = self.controls["completion_budget"]
        for key in ["new_send_125", "pending_post_126", "existing_receive_126"]:
            row = rows[key]
            self.assertEqual(row["plan"]["status"], "BLOCKED")
            self.assertEqual(row["plan"]["reason"], "COMPLETION_EVENT_BUDGET")
            self.assertIsNone(row["plan"]["intent"])
            self.assertTrue(row["unchanged_before_confirmation"])
            self.assertEqual(row["planned_required_total"], 129)
        for key, nav in [("new_send_124", "1030"), ("pending_post_125", "2000.5"),
                         ("existing_receive_125", "2000")]:
            row = rows[key]
            self.assertEqual(row["plan"]["status"], "NEXT_INTENT")
            self.assertEqual(row["final"]["bridge_operations"], 128)
            self.assertEqual(row["final"]["state"]["nav"], nav)
            self.assertTrue(row["final"]["reconciliation"]["closed_reconciliation"])
            self.assertTrue(row["final"]["restore_matches"])
        self.assertEqual(rows["new_send_124"]["plan"]["mandatory_cleanup"], {
            "close_legs": 2, "transfer_receives": 1, "funding_posts": 0, "total": 3})
        for key in ["pending_post_126"]:
            self.assertEqual(rows[key]["hold_plan"]["status"], "BLOCKED")
            self.assertEqual(rows[key]["risk_plan"]["status"], "BLOCKED")
        self.assertEqual(rows["existing_receive_126"]["wait_plan"]["status"], "BLOCKED")
        self.assertEqual(rows["new_send_124"]["waiting"]["status"], "WAIT")

    def test_negative_zero_and_flat_funding_require_status_based_counts(self):
        rows = self.controls["completion_budget"]
        for key, nav, posted in [("negative_funding", "1999.5", "-0.5"),
                                 ("zero_amount_funding", "2000", "0")]:
            row = rows[key]
            self.assertEqual(row["plan"]["mandatory_cleanup"]["funding_posts"], 1)
            self.assertEqual(row["before_post_operations"], 127)
            self.assertEqual(row["final"]["bridge_operations"], 128)
            self.assertEqual(row["final"]["state"]["nav"], nav)
            self.assertEqual(row["final"]["state"]["funding_cash_posted"], posted)
            self.assertTrue(row["final"]["reconciliation"]["closed_reconciliation"])
        flat = rows["flat_eligibility"]
        self.assertEqual(flat["operations"], 128)
        self.assertEqual(flat["plan"]["status"], "HOLD")
        self.assertEqual(flat["plan"]["mandatory_cleanup"]["total"], 0)
        self.assertEqual(flat["obligation_status"], "NO_SYNTHETIC_OBLIGATION_FLAT")
        self.assertTrue(flat["below_cap_post_semantics_checked"])
        self.assertTrue(flat["no_payable_post_exists"] and flat["closed_reconciliation"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--report-output")
    args, remaining = parser.parse_known_args()
    result = unittest.main(argv=[sys.argv[0], *remaining], exit=False)
    if result.result.wasSuccessful() and args.report_output:
        with Path(args.report_output).open("x") as stream:
            json.dump(AllocationControls.controls, stream, sort_keys=True, indent=2)
            stream.write("\n")
    raise SystemExit(0 if result.result.wasSuccessful() else 1)
