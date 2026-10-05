"""Known-answer revision and recovery controls through actual persistent paths."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .reporting import same_typed_value
from .manager import ACCOUNT, ENV, DEFAULT_CONFIG, FictionalVenue, OrderManager, SimulationBinding

NOW = "2026-10-08T10:00:00+08:00"
BINDING = SimulationBinding(ENV, ACCOUNT, (ACCOUNT,))


def setup(folder, *, quantity=100, side="BUY", config=None):
    folder.mkdir(parents=True)
    configuration = {**DEFAULT_CONFIG, **(config or {})}
    venue = FictionalVenue(folder / "venue.sqlite3", configuration, NOW, binding=BINDING)
    oms = OrderManager(folder / "oms.sqlite3", venue, configuration, binding=BINDING)
    order = {"environment": ENV, "account": ACCOUNT, "intent_id": "control",
             "symbol": "TEST.CNY", "currency": "CNY", "side": side,
             "quantity": quantity, "price": "10.00"}
    return oms, venue, order


def amendment(fill, *, revision=1, qty=None, price=None, fee=None):
    return {"adjustment_id": "revision-" + str(revision), "revision": revision,
            **{k: fill[k] for k in ("environment", "account", "intent_id", "order_id", "exec_id")},
            "qty": fill["qty"] if qty is None else qty,
            "price": fill["price"] if price is None else price,
            "fee": fill["fee"] if fee is None else fee}


def run_extended_controls(folder):
    folder = Path(folder)
    folder.mkdir(parents=True)
    rows = []

    def record(id, expected, actual):
        row = {"id": id, "expected": expected, "actual": actual,
               "passed": same_typed_value(actual, expected)}
        rows.append(row)

    def save(id, oms, venue):
        (folder / id / "observations.json").write_text(json.dumps(
            {"local": oms.snapshot(), "venue": venue.snapshot(), "audit": oms.audit()}, indent=2))

    # Instrument: callback sees a committed intent before venue invocation.
    oms, venue, raw = setup(folder / "durable-before-call")
    original = venue.submit
    seen = []
    def observe(intent, payload):
        with sqlite3.connect(oms.path) as db:
            seen.append(db.execute("SELECT state FROM orders WHERE intent=?", (intent,)).fetchone()[0])
        return original(intent, payload)
    venue.submit = observe
    oms.submit(raw, NOW)
    record("durable_before_adapter_call", ["SUBMITTING"], seen)
    save("durable-before-call", oms, venue)

    # Lost ACK after venue acceptance: durable UNKNOWN, no blind resend.
    oms, venue, raw = setup(folder / "unknown-accepted")
    original = venue.submit
    def lost(intent, payload):
        original(intent, payload)
        raise TimeoutError("FICTIONAL_REPLY_LOST")
    venue.submit = lost
    result = oms.submit(raw, NOW)
    record("unknown_send_result", {"accepted": None, "state": "UNKNOWN"},
           {k: result[k] for k in ("accepted", "state")})
    oms.submit(raw, NOW)
    record("unknown_duplicate_has_one_send", 1, venue.snapshot()["submit_call_count"])
    venue.submit = original
    recovered = oms.recover_submission("control", retry_if_absent=True, now=NOW)
    record("accepted_unknown_query_never_resends", [True, False, 1],
           [recovered["resolved"], recovered["resent"], venue.snapshot()["submit_call_count"]])
    record("query_record_persisted", True, any(e["kind"] == "QUERY_BEFORE_RETRY" for e in oms.audit()))
    save("unknown-accepted", oms, venue)

    # Unknown before acceptance: query establishes final absence before retry.
    oms, venue, raw = setup(folder / "unknown-absent")
    original = venue.submit
    venue.submit = lambda *args: (_ for _ in ()).throw(TimeoutError("FICTIONAL_PRE_ACCEPT"))
    oms.submit(raw, NOW)
    no_retry = oms.recover_submission("control")
    record("absence_without_explicit_retry_stays_unknown", [False, False, "UNKNOWN"],
           [no_retry["resolved"], no_retry["resent"], oms.snapshot()["orders"][0]["state"]])
    venue.submit = original
    retry = oms.recover_submission("control", retry_if_absent=True, now=NOW)
    record("absence_explicit_retry_queries_first", [True, True, 1],
           [retry["resolved"], retry["resent"], venue.snapshot()["accept_count"]])
    kinds = [event["kind"] for event in oms.audit()]
    record("retry_audit_order", True, kinds.index("QUERY_BEFORE_RETRY") < kinds.index("RETRY_PREPARE"))
    save("unknown-absent", oms, venue)

    # Recovery may query while paused, but an explicit retry cannot bypass an independent pause.
    for pause_reason in ("MANUAL", "TEST_LOSS_LIMIT"):
        id = "paused-retry-" + pause_reason.lower()
        oms, venue, raw = setup(folder / id)
        venue.submit = lambda *args: (_ for _ in ()).throw(TimeoutError("FICTIONAL_PRE_ACCEPT"))
        oms.submit(raw, NOW)
        oms.pause(pause_reason)
        result = oms.recover_submission("control", retry_if_absent=True, now=NOW)
        record("independent_pause_prevents_retry_" + pause_reason.lower(),
               [False, False, "INDEPENDENT_PAUSE_BLOCKS_RETRY", 0, True],
               [result["resolved"], result["resent"], result["reason"], venue.snapshot()["submit_call_count"],
                pause_reason in result["blocking_pause_reasons"]])
        save(id, oms, venue)

    oms, venue, raw = setup(folder / "retry-clock")
    venue.submit = lambda *args: (_ for _ in ()).throw(TimeoutError("FICTIONAL_PRE_ACCEPT"))
    oms.submit(raw, NOW)
    absent_clock = oms.recover_submission("control", retry_if_absent=True)
    stale_quote = oms.recover_submission("control", retry_if_absent=True, now="2026-10-08T10:02:00+08:00")
    venue.set_quote("TEST.CNY", stamp="2026-10-08T12:00:00+08:00")
    outside_session = oms.recover_submission("control", retry_if_absent=True, now="2026-10-08T12:00:00+08:00")
    record("retry_requires_explicit_current_clock", [False, "RETRY_TIME_REQUIRED", 0],
           [absent_clock["resent"], absent_clock["reason"], venue.snapshot()["submit_call_count"]])
    record("retry_rechecks_quote_age", [False, "RETRY_QUOTE_TIME_INVALID", 0],
           [stale_quote["resent"], stale_quote["reason"], venue.snapshot()["submit_call_count"]])
    record("retry_rechecks_session", [False, "RETRY_OUTSIDE_SYNTHETIC_SESSION", 0],
           [outside_session["resent"], outside_session["reason"], venue.snapshot()["submit_call_count"]])
    save("retry-clock", oms, venue)

    # Cumulative-only reports must not manufacture execution IDs or book twice.
    oms, venue, raw = setup(folder / "cumulative")
    oms.submit(raw, NOW)
    fill = venue.fill("control", "partial", 40, "10.00", 50)
    oms.receive_fill(fill)
    status = {**{k: fill[k] for k in ("environment", "account", "intent_id", "order_id")}, "cumulative_qty": 40}
    oms.receive_cumulative(status)
    oms.receive_cumulative(status)
    record("cumulative_does_not_add_executions", [959950, 40, 1],
           [oms.snapshot()["account"]["cash"], oms.snapshot()["orders"][0]["filled"], len(oms.snapshot()["fills"])])
    missing = venue.fill("control", "lost", 10, "10.00")
    result = oms.receive_cumulative({**status, "cumulative_qty": 50})
    record("cumulative_ahead_requires_query", [False, False, 959950],
           [result["matched"], result["applied"], oms.snapshot()["account"]["cash"]])
    result = oms.reconcile(resume=True)
    record("cumulative_gap_recovered_from_unique_executions", [True, True, 949950, 50],
           [result["matched"], result["resumed"], oms.snapshot()["account"]["cash"], oms.snapshot()["orders"][0]["filled"]])
    save("cumulative", oms, venue)

    # Cancel confirmation can arrive before an already executed partial fill.
    oms, venue, raw = setup(folder / "late-fill-after-cancel")
    oms.submit(raw, NOW)
    oms.request_cancel("control")
    fill = venue.fill("control", "race", 40, "10.00", 10)
    venue.cancel("control", confirm=True)
    oms.cancel_response("control", "CANCELLED")
    oms.receive_fill(fill)
    result = oms.reconcile()
    local = oms.snapshot()
    record("fill_delivered_after_cancel_confirmation_booked", [True, 959990, 40, "CANCELLED", 0],
           [result["matched"], local["account"]["cash"], local["orders"][0]["filled"], local["orders"][0]["state"], local["reserved_cash"]])
    save("late-fill-after-cancel", oms, venue)

    # Reserve freed by cancel can be reused before a prior authoritative fill arrives.
    oms, venue, raw = setup(folder / "late-fill-reservation", config={"initial_cash": 150000, "fee_cap": 0})
    oms.submit(raw, NOW)
    fill = venue.fill("control", "prior-fill", 100, "10.00")
    venue.cancel("control", confirm=True)
    oms.cancel_response("control", "CANCELLED")
    oms.submit({**raw, "intent_id": "new-reservation"}, NOW)
    applied = oms.receive_fill(fill)
    local = oms.snapshot()
    record("late_fill_books_then_halts_cash_reservation_breach", [True, 50000, 100000, 100, 1],
           [applied["applied"], local["account"]["cash"], local["reserved_cash"], local["positions"]["TEST.CNY"]["qty"], local["account"]["halted"]])
    result = oms.reconcile(resume=True)
    record("reservation_risk_not_auto_resumed", [True, False, True],
           [result["matched"], result["resumed"], "AUTHORITATIVE_FILL_RESERVATION_BREACH" in result["blocking_pause_reasons"]])
    save("late-fill-reservation", oms, venue)

    # The reused reserve can already be consumed: late truth still books negative cash.
    oms, venue, raw = setup(folder / "late-fill-negative", config={"initial_cash": 150000, "fee_cap": 0})
    oms.submit(raw, NOW)
    prior_fill = venue.fill("control", "prior-fill", 100, "10.00")
    venue.cancel("control", confirm=True)
    oms.cancel_response("control", "CANCELLED")
    oms.submit({**raw, "intent_id": "new-consumed"}, NOW)
    next_fill = venue.fill("new-consumed", "next-fill", 100, "10.00")
    oms.receive_fill(next_fill)
    applied = oms.receive_fill(prior_fill)
    result = oms.reconcile(resume=True)
    record("late_fill_negative_cash_accounted_then_halted", [True, True, False, -50000, 200, 2, 1],
           [applied["applied"], result["matched"], result["resumed"], oms.snapshot()["account"]["cash"], oms.snapshot()["positions"]["TEST.CNY"]["qty"], len(oms.snapshot()["fills"]), oms.snapshot()["account"]["halted"]])
    save("late-fill-negative", oms, venue)

    # Late authoritative fee is booked, duplicate is idempotent, restart retains it.
    oms, venue, raw = setup(folder / "late-fee")
    oms.submit(raw, NOW)
    fill = venue.fill("control", "fee", 100, "10.00", 0)
    oms.receive_fill(fill)
    correction = amendment(fill, fee=250)
    venue.correct_execution(correction)
    oms.receive_correction(correction)
    duplicate = oms.receive_correction(correction)
    replay = oms.receive_fill(fill)
    record("late_fee_applied_and_replayed_once", [899750, 250, False, True, True],
           [oms.snapshot()["account"]["cash"], oms.snapshot()["orders"][0]["fees"], duplicate["applied"], duplicate["duplicate"], replay.get("stale_revision")])
    oms = OrderManager(oms.path, venue, binding=BINDING)
    result = oms.reconcile(resume=True)
    record("late_fee_restart_reconciles", [True, True, 899750, 1],
           [result["matched"], result["resumed"], oms.snapshot()["account"]["cash"], len(oms.snapshot()["corrections"])])
    save("late-fee", oms, venue)

    # Quantity/price revision updates cash/positions/reservations, not just fees.
    oms, venue, raw = setup(folder / "execution-revision")
    oms.submit(raw, NOW)
    fill = venue.fill("control", "rev", 100, "10.00", 100)
    oms.receive_fill(fill)
    revision = amendment(fill, qty=80, price=990, fee=80)
    venue.correct_execution(revision)
    oms.receive_correction(revision)
    result = oms.reconcile()
    local = oms.snapshot()
    record("quantity_price_correction_booked", [True, 920720, 80, 79200, 80, "PARTIAL", 20420],
           [result["matched"], local["account"]["cash"], local["orders"][0]["filled"], local["orders"][0]["notional"], local["orders"][0]["fees"], local["orders"][0]["state"], local["reserved_cash"]])
    second = amendment(fill, revision=2, qty=0, price=990, fee=0)
    venue.correct_execution(second)
    # Lost revision delivery recovered from authoritative history on restart.
    oms = OrderManager(oms.path, venue, binding=BINDING)
    result = oms.reconcile(resume=True)
    record("lost_second_revision_recovered", [True, True, 1000000, 0, 100500, 2],
           [result["matched"], result["resumed"], oms.snapshot()["account"]["cash"], oms.snapshot()["orders"][0]["filled"], oms.snapshot()["reserved_cash"], len(oms.snapshot()["corrections"])])
    save("execution-revision", oms, venue)

    # Economic correction beyond risk cap is booked and independently halts.
    oms, venue, raw = setup(folder / "fee-cap")
    oms.submit(raw, NOW)
    fill = venue.fill("control", "overfee", 100, "10.00", 0)
    oms.receive_fill(fill)
    correction = amendment(fill, fee=900)
    venue.correct_execution(correction)
    oms.receive_correction(correction)
    result = oms.reconcile(resume=True)
    record("overcap_late_fee_accounted_then_halted", [True, False, 899100, 900, 1],
           [result["matched"], result["resumed"], oms.snapshot()["account"]["cash"], oms.snapshot()["orders"][0]["fees"], oms.snapshot()["account"]["halted"]])
    save("fee-cap", oms, venue)

    # Sell-side revisions use reversed principal sign and preserve the fee debit.
    oms, venue, raw = setup(folder / "sell-correction", side="SELL",
        config={"initial_positions": {"TEST.CNY": {"qty": 200, "sellable": 200}}})
    oms.submit(raw, NOW)
    fill = venue.fill("control", "sell", 100, "10.00", 100)
    oms.receive_fill(fill)
    correction = amendment(fill, qty=90, price=1010, fee=90)
    venue.correct_execution(correction)
    oms.receive_correction(correction)
    result = oms.reconcile()
    record("sell_revision_principal_fee_sign", [True, 1090810, 110, 10],
           [result["matched"], oms.snapshot()["account"]["cash"], oms.snapshot()["positions"]["TEST.CNY"]["qty"], oms.snapshot()["reserved_quantity"]])
    save("sell-correction", oms, venue)

    # Invalid revision cannot become economic truth by being reported again.
    oms, venue, raw = setup(folder / "bad-correction")
    oms.submit(raw, NOW)
    fill = venue.fill("control", "bad", 100, "10.00", 0)
    oms.receive_fill(fill)
    invalid = oms.receive_correction(amendment(fill, revision=2, fee=50))
    record("revision_gap_rejected_without_booking", [False, "CORRECTION_REVISION_GAP", 900000, 0],
           [invalid["applied"], invalid["reason"], oms.snapshot()["account"]["cash"], len(oms.snapshot()["corrections"])])
    save("bad-correction", oms, venue)

    # A stale rejection must never resurrect a cancelled order with no reserves.
    oms, venue, raw = setup(folder / "stale-cancel-response")
    oms.submit(raw, NOW)
    unbound = oms.cancel_response("control", "CANCEL_REJECTED")
    record("cancel_reject_without_pending_ignored", [False, "NO_PENDING_CANCEL", "ACKNOWLEDGED", 100500],
           [unbound["accepted"], unbound["reason"], oms.snapshot()["orders"][0]["state"], oms.snapshot()["reserved_cash"]])
    oms.request_cancel("control")
    duplicate = oms.request_cancel("control")
    record("pending_cancel_request_not_resent", [None, True, "CANCEL_PENDING", 100500],
           [duplicate["accepted"], duplicate["duplicate"], oms.snapshot()["orders"][0]["state"], oms.snapshot()["reserved_cash"]])
    venue.cancel("control", confirm=True)
    oms.cancel_response("control", "CANCELLED")
    rejected = oms.cancel_response("control", "CANCEL_REJECTED")
    record("stale_cancel_reject_preserves_terminal", [True, "CANCELLED", "CANCELLED", 0],
           [rejected["accepted"], rejected["preserved_terminal"], oms.snapshot()["orders"][0]["state"], oms.snapshot()["reserved_cash"]])
    save("stale-cancel-response", oms, venue)

    # Repeated cancellation attempts need correlation; old reject cannot clear new pending.
    oms, venue, raw = setup(folder / "cancel-correlation")
    oms.submit(raw, NOW)
    first = oms.request_cancel("control")
    venue.cancel("control", reject=True)
    oms.cancel_response("control", "CANCEL_REJECTED", request_id=first["request_id"])
    second = oms.request_cancel("control")
    stale = oms.cancel_response("control", "CANCEL_REJECTED", request_id=first["request_id"])
    missing = oms.cancel_response("control", "CANCEL_REJECTED")
    record("stale_cancel_attempt_cannot_clear_current", ["STALE_CANCEL_REQUEST_ID", "CANCEL_CORRELATION_REQUIRED", "CANCEL_PENDING", 1, 100500],
           [stale["reason"], missing["reason"], oms.snapshot()["orders"][0]["state"], oms.snapshot()["orders"][0]["cancel_pending"], oms.snapshot()["reserved_cash"]])
    venue.cancel("control", reject=True)
    current = oms.cancel_response("control", "CANCEL_REJECTED", request_id=second["request_id"])
    record("current_cancel_attempt_rejection_applied", ["CANCEL_REJECTED", "ACKNOWLEDGED", 0, 100500],
           [current["reason"], oms.snapshot()["orders"][0]["state"], oms.snapshot()["orders"][0]["cancel_pending"], oms.snapshot()["reserved_cash"]])
    save("cancel-correlation", oms, venue)

    # Cancel transport uncertainty retains reservations; no local success shortcut.
    oms, venue, raw = setup(folder / "cancel-reply-lost")
    oms.submit(raw, NOW)
    original_cancel = venue.cancel
    def lost_cancel(intent):
        original_cancel(intent)
        raise TimeoutError("FICTIONAL_CANCEL_REPLY_LOST")
    venue.cancel = lost_cancel
    result = oms.request_cancel("control")
    record("cancel_reply_lost_retains_reserve", [None, False, "CANCEL_PENDING", 100500],
           [result["accepted"], result["cancel_confirmed"], oms.snapshot()["orders"][0]["state"], oms.snapshot()["reserved_cash"]])
    venue.cancel = original_cancel
    venue.cancel("control", confirm=True)
    result = oms.reconcile(resume=True)
    record("cancel_reply_recovered_by_snapshot", [True, True, "CANCELLED", 0],
           [result["matched"], result["resumed"], oms.snapshot()["orders"][0]["state"], oms.snapshot()["reserved_cash"]])
    save("cancel-reply-lost", oms, venue)

    # A reused correction ID with different economics is a conflict, never a fee delta.
    oms, venue, raw = setup(folder / "correction-conflict")
    oms.submit(raw, NOW)
    fill = venue.fill("control", "conflict", 100, "10.00")
    oms.receive_fill(fill)
    correction = amendment(fill, fee=100)
    venue.correct_execution(correction)
    oms.receive_correction(correction)
    conflict = oms.receive_correction({**correction, "fee": 200})
    wrong_account = oms.receive_correction({**amendment(fill, revision=2, fee=100), "account": "UNKNOWN-ACCOUNT"})
    overfill = oms.receive_correction(amendment(fill, revision=2, qty=101))
    record("correction_id_conflict_no_double_debit", [False, "CORRECTION_ID_CONFLICT", 899900, 1],
           [conflict["applied"], conflict["reason"], oms.snapshot()["account"]["cash"], len(oms.snapshot()["corrections"])])
    record("correction_binding_rejected", [False, "CORRECTION_BINDING_REFUSED"],
           [wrong_account["applied"], wrong_account["reason"]])
    record("correction_overfill_rejected", [False, "CORRECTION_OVERFILL", 100],
           [overfill["applied"], overfill["reason"], oms.snapshot()["orders"][0]["filled"]])
    save("correction-conflict", oms, venue)

    # An authoritative negative balance is accounted, rather than hidden by a risk veto.
    oms, venue, raw = setup(folder / "negative-correction")
    oms.submit(raw, NOW)
    fill = venue.fill("control", "negative", 100, "10.00")
    oms.receive_fill(fill)
    correction = amendment(fill, fee=1000000)
    venue.correct_execution(correction)
    oms.receive_correction(correction)
    result = oms.reconcile(resume=True)
    record("negative_authoritative_cash_booked_then_halted", [True, False, -100000, 1],
           [result["matched"], result["resumed"], oms.snapshot()["account"]["cash"], oms.snapshot()["account"]["halted"]])
    save("negative-correction", oms, venue)

    # A non-final or wrongly bound query cannot authorize retrying an unknown intent.
    oms, venue, raw = setup(folder / "untrusted-query")
    venue.submit = lambda *args: (_ for _ in ()).throw(TimeoutError("FICTIONAL_PRE_ACCEPT"))
    oms.submit(raw, NOW)
    venue.query_intent = lambda intent: {"found": False, "authoritative_absence": False, **BINDING.as_dict()}
    result = oms.recover_submission("control", retry_if_absent=True, now=NOW)
    record("nonfinal_absence_never_retries", [False, False, 0],
           [result["resolved"], result["resent"], venue.snapshot()["submit_call_count"]])
    venue.query_intent = lambda intent: {"found": False, "authoritative_absence": True, "environment": "REAL", "account": ACCOUNT}
    result = oms.recover_submission("control", retry_if_absent=True, now=NOW)
    record("wrong_query_binding_never_retries", [False, "QUERY_BINDING_MISMATCH", 0],
           [result["resolved"], result["reason"], venue.snapshot()["submit_call_count"]])
    save("untrusted-query", oms, venue)

    # Real/unknown environments and implicit/empty allowlists cannot bind.
    for id, binding in [("real", SimulationBinding("REAL", ACCOUNT, (ACCOUNT,))),
                        ("unknown", SimulationBinding("UNKNOWN", ACCOUNT, (ACCOUNT,))),
                        ("empty-whitelist", SimulationBinding(ENV, ACCOUNT, ())),
                        ("other-account", SimulationBinding(ENV, "REAL-ACCOUNT", ("REAL-ACCOUNT",)))]:
        try:
            binding.validate()
            actual = False
        except ValueError:
            actual = True
        record("binding_rejects_" + id, True, actual)
    (folder / "reports.json").write_text(json.dumps(rows, indent=2))
    return rows
