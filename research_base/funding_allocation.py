"""Pure, bounded allocation proposals for explicit synthetic fixtures only.

A proposal is not a fill, cash receipt or order. Each confirmed fixture event
must be applied separately by a controller, then planned again from a new
snapshot. Existing accounting and funding contracts remain unchanged.
"""
from __future__ import annotations

import copy

from . import funding_account as ledger
from .funding_events import (CompilerError, EPOCH, SyntheticFundingBridge,
                             digest, iso_ms, time_ms)


MODE = "EXPLICIT_SYNTHETIC_FIXTURE_ONLY"
SCHEMA = "synthetic-allocation-planner/1"
CONTEXT_FIELDS = {
    "schema", "mode", "market", "symbol", "decision_time_ms", "target_qty",
    "target_known_at_ms", "spot_reference_price", "perp_reference_price",
    "spot_fee_rate", "perp_fee_rate", "spot_slippage_rate", "perp_slippage_rate",
    "quotes_known_at_ms", "fees_known_at_ms", "input_permission",
    "permission_known_at_ms", "transfer_fee", "transfer_arrival_time_ms",
    "transfer_known_at_ms", "reserved_future_operations", "expected_view_sha256",
}
POLICY_FIELDS = {"schema", "lower_buffer", "target_buffer", "upper_buffer"}


class PlannerInputError(ValueError):
    pass


def _milliseconds(stamp):
    delta = ledger.utc_time(stamp, "committed time") - EPOCH
    if delta.microseconds % 1000:
        raise PlannerInputError("Committed time is not an exact millisecond")
    return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000


def _last_order(snapshot):
    operations = snapshot["complete_committed_operations"]
    if not operations:
        return None
    op = operations[-1]
    if op["kind"] == "action":
        return _milliseconds(op["event"]["event_time"]), op["event"]["sequence"]
    if op["kind"] == "capture":
        c = op["contract"]
        return c["settlement_time_ms"], c["capture_sequence"]
    c = snapshot["final_synthetic_settlements"][op["event_id"]]["contract"]
    return c["payment_time_ms"], c["posting_sequence"]


def _mandatory_cleanup(bridge):
    """Count completion operations by full status, never by net cash amount.

    A negative or zero-amount pending funding obligation still needs a post.
    Flat eligibility creates NO_SYNTHETIC_OBLIGATION_FLAT and cannot be posted.
    Each in-flight transfer needs a distinct receive before reconciliation.
    """
    view = bridge.pre_event_view()
    legs = int(ledger.decimal(view["spot_qty"]) > ledger.ZERO) + int(
        ledger.decimal(view["perp_qty_signed"]) < ledger.ZERO)
    receives = sum(item["status"] == "inflight"
                   for item in bridge.account.transfers.values())
    posts = sum(item["status"] == "PENDING_SYNTHETIC_POST"
                for item in bridge.settlements.values())
    return {"close_legs": legs, "transfer_receives": receives,
            "funding_posts": posts, "total": legs + receives + posts}


def plan_allocation(*, bridge_snapshot, decision_context, policy):
    """Return at most one intent; never change the supplied snapshot or bridge.

    Policy thresholds use current marked initial margin: lower=IM+lower_buffer,
    target=IM+target_buffer, upper=IM+upper_buffer. They are fixed toy rules,
    not a solved optimal-control policy or a real venue margin schedule.
    """
    if not isinstance(decision_context, dict) or set(decision_context) != CONTEXT_FIELDS:
        raise PlannerInputError("Unknown or missing context fields")
    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
        raise PlannerInputError("Unknown or missing policy fields")
    context, fixed_policy = copy.deepcopy(decision_context), copy.deepcopy(policy)
    snapshot = copy.deepcopy(bridge_snapshot)
    try:
        bridge = SyntheticFundingBridge.restore(snapshot)
    except (CompilerError, ledger.AccountingError) as exc:
        raise PlannerInputError("Invalid complete synthetic snapshot") from exc
    view = bridge.pre_event_view()
    view_hash = bridge.pre_event_view_sha256()
    op_count = len(snapshot["complete_committed_operations"])
    account_count = view["committed_event_count"]
    result = {
        "schema": SCHEMA, "mode": MODE, "status": "BLOCKED", "reason": "",
        "snapshot_sha256": snapshot["snapshot_sha256"], "view_sha256": view_hash,
        "context_sha256": digest(context), "policy_sha256": digest(fixed_policy),
        "intent": None, "bridge_operation_count": op_count,
        "account_event_count": account_count, "actual_execution_verified": False,
        "live_dispatch_allowed": False,
        "mandatory_cleanup": _mandatory_cleanup(bridge),
    }

    def finish(status, reason):
        result["status"], result["reason"] = status, reason
        result["plan_sha256"] = digest(result)
        return copy.deepcopy(result)

    if context["schema"] != SCHEMA or fixed_policy["schema"] != SCHEMA:
        raise PlannerInputError("Unknown planner schema")
    if context["mode"] != MODE or context["input_permission"] != "DECLARED_SYNTHETIC_FIXTURE":
        return finish("BLOCKED", "SCOPE_NOT_QUALIFIED_REAL_MODE_UNSUPPORTED")
    if (context["market"], context["symbol"]) != (snapshot["market"], snapshot["symbol"]):
        return finish("BLOCKED", "INSTRUMENT_MISMATCH")
    if context["expected_view_sha256"] != view_hash:
        return finish("BLOCKED", "STALE_STATE")
    try:
        at = time_ms(context["decision_time_ms"])
        known = {k: time_ms(context[k], k) for k in (
            "target_known_at_ms", "quotes_known_at_ms", "fees_known_at_ms",
            "permission_known_at_ms")}
        target = ledger.input_decimal(context["target_qty"], "target", nonnegative=True)
        prices = {w: ledger.input_decimal(context[w+"_reference_price"], w+" price", positive=True)
                  for w in ("spot", "perp")}
        fees = {w: ledger.input_decimal(context[w+"_fee_rate"], w+" fee", nonnegative=True)
                for w in ("spot", "perp")}
        slips = {w: ledger.input_decimal(context[w+"_slippage_rate"], w+" slip", nonnegative=True)
                 for w in ("spot", "perp")}
        transfer_fee = ledger.input_decimal(context["transfer_fee"], "transfer fee", nonnegative=True)
        buffers = {k: ledger.input_decimal(fixed_policy[k], k, nonnegative=True)
                   for k in ("lower_buffer", "target_buffer", "upper_buffer")}
    except (CompilerError, ledger.AccountingError):
        return finish("BLOCKED", "REQUIRED_NUMERIC_OR_CLOCK_INPUT_UNKNOWN")
    if max(*fees.values(), *slips.values()) >= ledger.ONE:
        return finish("BLOCKED", "COST_OUTSIDE_PROTOCOL")
    if transfer_fee != ledger.ZERO:
        return finish("BLOCKED", "TRANSFER_FEE_REQUIRES_ANOTHER_PROTOCOL")
    if not buffers["lower_buffer"] <= buffers["target_buffer"] <= buffers["upper_buffer"]:
        raise PlannerInputError("Policy buffers must be ordered")
    if any(k > at for k in known.values()):
        return finish("BLOCKED", "FUTURE_KNOWLEDGE")
    reserve = context["reserved_future_operations"]
    if isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < 0:
        raise PlannerInputError("Explicit nonnegative operation reserve required")
    bound = ledger.NUMERIC_DOMAIN["max_committed_events"]
    if max(op_count, account_count) + reserve > bound:
        return finish("BLOCKED", "EVENT_BUDGET")
    result["reserved_future_operations"] = max(reserve, result["mandatory_cleanup"]["total"])
    # HOLD/WAIT/STOP and action candidates share the same current-state budget.
    # A snapshot already unable to complete must not consume its last slots.
    if max(op_count, account_count) + result["reserved_future_operations"] > bound:
        return finish("BLOCKED", "COMPLETION_EVENT_BUDGET")
    last = _last_order(snapshot)
    if last is not None and at < last[0]:
        return finish("BLOCKED", "DECISION_PRECEDES_COMMITTED_STATE")
    seq = last[1] + 1 if last and at == last[0] else 0
    stamp = iso_ms(at)
    with ledger.exact_money_context():
        spot = ledger.decimal(view["spot_qty"])
        short = -ledger.decimal(view["perp_qty_signed"])
        risk = (view["perp_margin_breached"] or view["ever_margin_breached"] or
                view["derivative_wallet_insolvent"])
        # This conservative fixture policy remains halted after an ever-breach.
        # Neither a later top-up nor a flat state resets permission to grow.
        effective_target = ledger.ZERO if risk else target
        intent_fields = None
        reason = "FIXED_TARGET"
        status = "STOP_RISK" if risk else "NEXT_INTENT"
        if short > effective_target:
            wallet, side, qty = "perp", "buy", short - effective_target
        elif spot > max(effective_target, short):
            wallet, side, qty = "spot", "sell", spot - max(effective_target, short)
        elif risk:
            return finish("STOP_RISK", "NO_REMAINING_POSITION_GROWTH_FORBIDDEN")
        elif any(t["status"] == "inflight" for t in bridge.account.transfers.values()):
            return finish("WAIT", "TRANSFER_RECEIPT_REQUIRED")
        elif spot < effective_target:
            wallet, side, qty = "spot", "buy", effective_target - spot
        elif short < effective_target:
            wallet, side, qty = "perp", "sell", effective_target - short
        else:
            equity = ledger.decimal(view["margin_equity"])
            im = ledger.decimal(view["reserved_initial_margin_partition"])
            amount, source, target_wallet = ledger.ZERO, "spot", "deriv"
            if short and equity < im + buffers["lower_buffer"]:
                amount = im + buffers["target_buffer"] - equity
                reason = "LOWER_MARGIN_BUFFER"
            elif short and equity > im + buffers["upper_buffer"]:
                amount = equity - im - buffers["target_buffer"]
                source, target_wallet, reason = "deriv", "spot", "UPPER_MARGIN_BUFFER"
            if not amount:
                return finish("HOLD", "TARGET_AND_BUFFER_SATISFIED")
            try:
                arrival = time_ms(context["transfer_arrival_time_ms"])
                arrival_known = time_ms(context["transfer_known_at_ms"])
            except CompilerError:
                return finish("WAIT", "TRANSFER_CLOCK_UNKNOWN")
            if arrival <= at or arrival_known > at:
                return finish("BLOCKED", "TRANSFER_CLOCK_NOT_CAUSAL")
            intent_fields = {"type": "transfer_send", "transfer_id": "allocation-transfer:"+digest({
                "view": view_hash, "context": result["context_sha256"]}),
                "source": source, "target": target_wallet,
                "amount": ledger.decimal_text(amount)}
        if intent_fields is None:
            intent_fields = {"type": wallet+"_fill", "side": side,
                "qty": ledger.decimal_text(qty),
                "reference_price": ledger.decimal_text(prices[wallet]),
                "fee_rate": ledger.decimal_text(fees[wallet]),
                "slippage_rate": ledger.decimal_text(slips[wallet]), "fee_currency": "USDT"}
    if max(op_count, account_count) + reserve + 1 > bound:
        return finish("BLOCKED", "EVENT_BUDGET")
    event = {"id": "allocation:"+digest({"state": view_hash, "context": context,
                "policy": fixed_policy, "fields": intent_fields}),
             "event_time": stamp, "applied_at": stamp, "known_at": stamp,
             "decision_time": stamp, "sequence": seq,
             "knowledge_inputs": [{"id": key, "known_at": iso_ms(value)}
                                  for key, value in sorted(known.items())], **intent_fields}
    # Hypothetical feasibility on an isolated copy is not fixture confirmation.
    try:
        shadow = SyntheticFundingBridge.restore(snapshot)
        shadow.apply_action(event)
    except (CompilerError, ledger.AccountingError):
        return finish("BLOCKED", "INFEASIBLE_UNDER_UNCHANGED_ACCOUNT_PROTOCOL")
    # A proposed send adds a future receive; reductions do not discharge
    # captured funding. Count the full hypothetical state after this one step.
    cleanup = _mandatory_cleanup(shadow)
    effective_reserve = max(reserve, cleanup["total"])
    result["mandatory_cleanup"] = cleanup
    result["minimum_terminal_close_operations"] = cleanup["close_legs"]
    result["reserved_future_operations"] = effective_reserve
    if max(op_count, account_count) + 1 + effective_reserve > bound:
        return finish("BLOCKED", "COMPLETION_EVENT_BUDGET")
    result["intent"] = {"kind": "UNCONFIRMED_SYNTHETIC_EVENT_INTENT",
        "requires_explicit_fixture_confirmation": True,
        "must_replan_after_receipt": True, "ledger_event_template": event}
    return finish(status, reason)


def validate_fixture_intent(plan, *, bridge_snapshot):
    """Release a template to a synthetic controller only after fresh hash checks."""
    if not isinstance(plan, dict) or not isinstance(plan.get("intent"), dict):
        raise PlannerInputError("No next synthetic intent")
    unsigned = {k: v for k, v in plan.items() if k != "plan_sha256"}
    if digest(unsigned) != plan.get("plan_sha256"):
        raise PlannerInputError("Plan hash differs")
    if (plan.get("mode") != MODE or plan.get("live_dispatch_allowed") is not False or
            plan.get("status") not in {"NEXT_INTENT", "STOP_RISK"}):
        raise PlannerInputError("Only unconfirmed synthetic proposals may be released")
    try:
        current = SyntheticFundingBridge.restore(copy.deepcopy(bridge_snapshot))
    except (CompilerError, ledger.AccountingError) as exc:
        raise PlannerInputError("Invalid current fixture snapshot") from exc
    if (current.pre_event_view_sha256() != plan["view_sha256"] or
            bridge_snapshot["snapshot_sha256"] != plan["snapshot_sha256"]):
        raise PlannerInputError("Stale planned state; replan required")
    return copy.deepcopy(plan["intent"]["ledger_event_template"])
