"""Continuous exact synthetic two-wallet ledger, separately versioned account/2.

Derived from admitted account/1 economic equations, with an explicit new rule:
once ever_margin_breached is true, every further short increase is rejected,
even after flat closure and cash replenishment. A run freezes its event budget;
capital, FIFO, pending cash and global identities never reset at month bounds.

Append-only immutable FIFO lot records use staged cursors; status record overlays
and cached totals avoid copying/scanning historical records per event. Complete
event/journal history is retained and restoration replays its entire hash-bound
chain. Snapshot hash is integrity binding, not authentication against a forger.
Single writer only; no disk transaction, venue, data, account or broker adapter.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import (Decimal, Context, DecimalException, localcontext,
                     ROUND_HALF_EVEN, Inexact, Rounded, Overflow, Underflow,
                     Clamped, Subnormal, InvalidOperation, DivisionByZero, FloatOperation)

SCHEMA = "funding-account/2"
PROTOCOL_ID = "synthetic-two-wallet-carry-continuous/2"
NUMERIC_CONTRACT_SHA256 = "db2956c58bb3bc2317aa3dcae837b829135b3027d5b6c7159ec332d487604ed2"
ADMITTED_V1_SHA256 = "65c216bf02845d8686ebce0492b0f1329809e8f3e62169faf3edbaf3d563a9aa"
ZERO = Decimal("0")
ONE = Decimal("1")
NUMERIC_DOMAIN = {
    "max_input_significant_digits": 60,
    "max_absolute_input": "1E60",
    "max_normalized_fractional_digits": 60,
    "max_numeric_string_characters": 256,
    "max_committed_events": 1000000,
    "money_precision": 512,
    "money_rounding_or_inexact_is_rejected": True,
    "weighted_entry_diagnostic_precision": 64,
    "weighted_entry_diagnostic_may_round": True,
    "weighted_entry_diagnostic_used_for_money": False,
    "proof_bound": "Run-frozen 1<=N<=1000000; abs external input<=B=1E60; finite scale<=240; all monetary intermediates <=22*N*B**2+(6*N+4)*B<1E128, requiring at most368digits. Exact512 and traps; no monetary division. Numeric contract SHA256=" + NUMERIC_CONTRACT_SHA256,
}
EXACT_CONTEXT = Context(prec=512, rounding=ROUND_HALF_EVEN, Emin=-1000, Emax=1000,
                        capitals=1, clamp=0)
for _signal in (Inexact, Rounded, Overflow, Underflow, Clamped, Subnormal,
                InvalidOperation, DivisionByZero, FloatOperation):
    EXACT_CONTEXT.traps[_signal] = True
DIAGNOSTIC_CONTEXT = Context(prec=64, rounding=ROUND_HALF_EVEN, Emin=-1000, Emax=1000,
                             capitals=1, clamp=0)
DIAGNOSTIC_CONTEXT.traps[Inexact] = False
DIAGNOSTIC_CONTEXT.traps[Rounded] = False
DEFAULT_ASSUMPTIONS = {
    "instrument": "GENERIC_BASE_USDT_LINEAR_PERPETUAL",
    "contract_multiplier": "1",
    "initial_margin_rate": "0.20",
    "maintenance_margin_rate": "0.10",
    "margin_breach_comparison": "equity_less_than_or_equal_to_maintenance",
    "funding_recognition": "cash_posting_only_pending_settlement_is_memo",
    "positive_pending_funding_is_available_margin": False,
    "spot_automatically_collateralizes_perpetual": False,
    "usdt_unit_value": "1",
    "decimal_precision": 512,
    "numeric_domain": copy.deepcopy(NUMERIC_DOMAIN),
    "inventory_cost_method": "FIFO_exact_finite_fill_costs_weighted_entry_diagnostic_only",
    "synthetic_liquidation_is_venue_replica": False,
    "exposure_increase_after_ever_margin_breached": "permanently_rejected_v2_explicit_change",
    "single_writer_in_memory_transaction": True,
    "continuous_capital_initialization": "once_per_hash_bound_run_not_monthly",
}


class AccountingError(ValueError):
    """A modeled input or state transition is not accepted."""


@contextmanager
def exact_money_context():
    # Never inherit the caller's precision, rounding mode, flags or traps.
    with localcontext(EXACT_CONTEXT) as context:
        context.clear_flags()
        try:
            yield context
        except DecimalException as exc:
            raise AccountingError("money operation outside exact frozen numeric domain") from exc


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_value(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def decimal(value, label="number", *, nonnegative=False, positive=False):
    # Floats are deliberately rejected: a binary float is not an exact oracle.
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise AccountingError(f"{label}: require decimal string or integer")
    try:
        result = Decimal(value)
    except Exception as exc:
        raise AccountingError(f"{label}: invalid Decimal") from exc
    if not result.is_finite():
        raise AccountingError(f"{label}: require finite value")
    if nonnegative and result < ZERO:
        raise AccountingError(f"{label}: negative value")
    if positive and result <= ZERO:
        raise AccountingError(f"{label}: require positive value")
    return result


def input_decimal(value, label="input", *, nonnegative=False, positive=False):
    """Validate and canonicalize finite inputs without ambient-context math."""
    if isinstance(value, str) and len(value) > NUMERIC_DOMAIN["max_numeric_string_characters"]:
        raise AccountingError(f"{label}: numeric token too long")
    result = decimal(value, label, nonnegative=nonnegative, positive=positive)
    if result == ZERO:
        return ZERO
    sign, digits_tuple, exponent = result.as_tuple()
    digits = list(digits_tuple)
    # Removing representational trailing zeros is integer/tuple manipulation,
    # not Decimal.normalize(), whose answer can depend on ambient precision.
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if len(digits) > NUMERIC_DOMAIN["max_input_significant_digits"]:
        raise AccountingError(f"{label}: too many input significant digits")
    if exponent < -NUMERIC_DOMAIN["max_normalized_fractional_digits"]:
        raise AccountingError(f"{label}: input fractional scale outside frozen domain")
    if result.copy_abs() > Decimal(NUMERIC_DOMAIN["max_absolute_input"]):
        raise AccountingError(f"{label}: input magnitude outside frozen domain")
    return Decimal((sign, tuple(digits), exponent))


def decimal_text(value):
    if value == ZERO:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def utc_time(value, label):
    if not isinstance(value, str):
        raise AccountingError(f"{label}: UTC ISO string required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AccountingError(f"{label}: invalid ISO time") from exc
    if result.tzinfo is None or result.utcoffset() != timezone.utc.utcoffset(result):
        raise AccountingError(f"{label}: explicit UTC required")
    return result


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
        raise AccountingError(f"{label}: invalid identifier")
    return value


COMMON_FIELDS = {"id", "type", "event_time", "applied_at", "known_at", "sequence"}
OPTIONAL_COMMON = {"decision_time", "knowledge_inputs"}
EVENT_FIELDS = {
    "mark": ({"spot_mark", "perp_mark"}, set()),
    "spot_fill": ({"side", "qty", "reference_price", "slippage_rate", "fee_rate"},
                  {"fee_currency"}),
    "perp_fill": ({"side", "qty", "reference_price", "slippage_rate", "fee_rate"},
                  {"fee_currency"}),
    "liquidation_fill": (
        {"side", "qty", "reference_price", "slippage_rate", "fee_rate", "fixed_fee"},
        {"fee_currency"}),
    "funding_settlement": (
        {"settlement_id", "settlement_time", "payment_time", "eligible_perp_qty_signed",
         "settlement_mark", "rate"}, set()),
    "funding_post": ({"settlement_id"}, set()),
    "transfer_send": ({"transfer_id", "source", "target", "amount"}, set()),
    "transfer_receive": ({"transfer_id"}, set()),
}


class _FifoLots:
    """Append-only immutable lot records with cheap transactional cursor forks."""

    def __init__(self):
        self.records = []
        self.pending = []
        self.head = 0
        self.head_remaining = None
        self.total_qty = ZERO
        self.total_cost = ZERO

    def fork(self):
        result = object.__new__(type(self))
        result.records = self.records
        result.pending = []
        result.head = self.head
        result.head_remaining = self.head_remaining
        result.total_qty = self.total_qty
        result.total_cost = self.total_cost
        return result

    def append(self, lot):
        quantity, price = lot["qty"], lot["price"]
        self.pending.append((quantity, price))
        self.total_qty += quantity
        self.total_cost += quantity * price

    def _record(self, index):
        n = len(self.records)
        return self.records[index] if index < n else self.pending[index - n]

    def consume(self, quantity):
        if quantity <= ZERO or quantity > self.total_qty:
            raise AccountingError("inventory lot coverage does not reconcile")
        remaining, cost = quantity, ZERO
        while remaining > ZERO:
            original_qty, price = self._record(self.head)
            available = original_qty if self.head_remaining is None else self.head_remaining
            consumed = min(available, remaining)
            cost += consumed * price
            remaining -= consumed
            available -= consumed
            if available == ZERO:
                self.head += 1
                self.head_remaining = None
            else:
                self.head_remaining = available
        self.total_qty -= quantity
        self.total_cost -= cost
        return cost

    def commit(self):
        self.records.extend(self.pending)
        self.pending = []
        return self

    def cursor(self):
        return {"head": self.head, "head_remaining": None if self.head_remaining is None else decimal_text(self.head_remaining),
                "record_count": len(self.records) + len(self.pending),
                "active_qty": decimal_text(self.total_qty), "active_cost": decimal_text(self.total_cost)}


class _RecordOverlay:
    """Only touched status records are copied; the full identity map is retained."""

    def __init__(self, original):
        self.original = original
        self.changes = {}

    def __contains__(self, key):
        return key in self.changes or key in self.original

    def get(self, key, default=None):
        if key in self.changes:
            return self.changes[key]
        item = self.original.get(key)
        if item is None:
            return default
        self.changes[key] = dict(item)
        return self.changes[key]

    def __setitem__(self, key, value):
        self.changes[key] = value

    def commit(self):
        self.original.update(self.changes)
        return self.original


class CarryAccount:
    """One fully funded spot wallet and one separate synthetic USDT wallet.

    All apply_event calls are transactional in memory. Identical committed
    event IDs are no-ops, while a conflicting ID or an unresolved snapshot is
    rejected. A state is durably resumed only from a hash-bound complete event
    log; this is an accounting model, not broker submission recovery.
    """

    def __init__(self, initial_spot_cash, initial_deriv_wallet_cash, assumptions=None, *, max_events):
        if isinstance(max_events, bool) or not isinstance(max_events, int) or not 1 <= max_events <= NUMERIC_DOMAIN["max_committed_events"]:
            raise AccountingError("run max_events must be an integer in [1,1000000]")
        self._run_max_events = max_events
        assumed = copy.deepcopy(DEFAULT_ASSUMPTIONS)
        if assumptions is not None:
            if not isinstance(assumptions, dict) or set(assumptions) != set(assumed):
                raise AccountingError("all frozen assumption fields must be supplied")
            assumed = copy.deepcopy(assumptions)
        # Reject silent expansion into a new contract, venue, valuation, or model.
        if assumed != DEFAULT_ASSUMPTIONS:
            raise AccountingError("assumptions differ from frozen synthetic protocol")
        self.assumptions = assumed
        self.initial_spot_cash = input_decimal(initial_spot_cash, "initial_spot_cash", nonnegative=True)
        self.initial_deriv_wallet_cash = input_decimal(initial_deriv_wallet_cash,
                                                "initial_deriv_wallet_cash", nonnegative=True)
        with exact_money_context():
            self.initial_equity = self.initial_spot_cash + self.initial_deriv_wallet_cash
        if self.initial_equity <= ZERO:
            raise AccountingError("initial allocated equity must be positive")
        self.spot_cash = self.initial_spot_cash
        self.deriv_wallet_cash = self.initial_deriv_wallet_cash
        self.spot_qty = ZERO
        self.spot_average_entry = ZERO
        self.spot_lots = _FifoLots()
        self.perp_qty_signed = ZERO
        self.perp_entry_price = ZERO
        self.perp_lots = _FifoLots()
        self.spot_mark = None
        self.perp_mark = None
        self.fees_total = ZERO
        self.slippage_diagnostic = ZERO
        self.realized_spot_gross = ZERO
        self.realized_perp_gross = ZERO
        self.funding_cash_posted = ZERO
        self.transfers = {}
        self.settlements = {}
        self.perp_margin_breached = False
        self.ever_margin_breached = False
        self.liquidation_count = 0
        self._last_order = None
        self._event_hashes = {}
        self.events = []
        self.ledger = []
        self._committed_count = 0
        self._inflight_total = ZERO
        self._pending_total = ZERO
        self._inflight_count = 0
        self._pending_count = 0
        self._journal_chain_sha256 = "0" * 64
        self._run_contract = {
            "schema": "funding-run-contract/2", "protocol_id": PROTOCOL_ID,
            "max_events": max_events, "numeric_contract_sha256": NUMERIC_CONTRACT_SHA256,
            "initial_spot_cash": decimal_text(self.initial_spot_cash),
            "initial_deriv_wallet_cash": decimal_text(self.initial_deriv_wallet_cash),
            "assumptions_sha256": sha256_value(self.assumptions),
        }
        self._run_contract_sha256 = sha256_value(self._run_contract)

    @property
    def max_events(self):
        return self._run_max_events

    def _check_frozen_run(self):
        if self._run_max_events != self._run_contract["max_events"] or self.assumptions != DEFAULT_ASSUMPTIONS:
            raise AccountingError("run budget or assumptions changed after initialization")
        if sha256_value(self._run_contract) != self._run_contract_sha256:
            raise AccountingError("frozen run contract differs")

    def _marked(self):
        if self.spot_mark is None or self.perp_mark is None:
            raise AccountingError("explicit spot and perpetual marks required first")

    def _perp_upnl(self):
        if self.perp_qty_signed == ZERO:
            return ZERO
        self._marked()
        return self.perp_lots.total_cost - abs(self.perp_qty_signed) * self.perp_mark

    def _inflight(self):
        return self._inflight_total

    def _pending_funding(self):
        return self._pending_total

    def _margin(self):
        mark = self.perp_mark if self.perp_mark is not None else ZERO
        notional = abs(self.perp_qty_signed) * mark
        equity = self.deriv_wallet_cash + self._perp_upnl()
        im = notional * decimal(self.assumptions["initial_margin_rate"])
        mm = notional * decimal(self.assumptions["maintenance_margin_rate"])
        return equity, im, mm

    def _check_margin(self):
        equity, _, maintenance = self._margin()
        if self.perp_qty_signed < ZERO and equity <= maintenance:
            self.perp_margin_breached = True
            self.ever_margin_breached = True
        if self.perp_qty_signed == ZERO:
            self.perp_margin_breached = False

    def view(self):
        with exact_money_context():
            spot_value = self.spot_qty * (self.spot_mark or ZERO)
            perp_upnl = self._perp_upnl()
            inflight = self._inflight()
            wallet_asset = max(self.deriv_wallet_cash, ZERO)
            wallet_liability = max(-self.deriv_wallet_cash, ZERO)
            # Signed wallet is represented by positive asset minus debit balance
            # exactly once; reserved collateral is never added to this identity.
            nav = self.spot_cash + wallet_asset + spot_value + perp_upnl + inflight - wallet_liability
            margin_equity, reserved, maintenance = self._margin()
            return {
                "spot_cash": decimal_text(self.spot_cash),
                "deriv_wallet_cash_signed": decimal_text(self.deriv_wallet_cash),
                "deriv_wallet_asset": decimal_text(wallet_asset),
                "deriv_debit_liability": decimal_text(wallet_liability),
                "spot_qty": decimal_text(self.spot_qty),
                "spot_average_entry": decimal_text(self.spot_average_entry),
                "perp_qty_signed": decimal_text(self.perp_qty_signed),
                "perp_entry_price": decimal_text(self.perp_entry_price),
                "entry_averages_are_diagnostics_not_money_inputs": True,
                "entry_average_diagnostic_precision": 64,
                "spot_entry_notional_exact": decimal_text(self.spot_lots.total_cost),
                "perp_entry_notional_exact": decimal_text(self.perp_lots.total_cost),
                "spot_mark": None if self.spot_mark is None else decimal_text(self.spot_mark),
                "perp_mark": None if self.perp_mark is None else decimal_text(self.perp_mark),
                "spot_inventory_value": decimal_text(spot_value),
                "perp_unrealized_pnl": decimal_text(perp_upnl),
                "transfer_inflight": decimal_text(inflight),
                "pending_funding_memo_not_in_nav": decimal_text(self._pending_funding()),
                "net_base_delta": decimal_text(self.spot_qty + self.perp_qty_signed),
                "margin_equity": decimal_text(margin_equity),
                "reserved_initial_margin_partition": decimal_text(reserved),
                "maintenance_margin": decimal_text(maintenance),
                "available_margin_diagnostic": decimal_text(margin_equity - reserved),
                "nav": decimal_text(nav),
                "initial_allocated_equity": decimal_text(self.initial_equity),
                "fees_total": decimal_text(self.fees_total),
                "slippage_diagnostic_not_a_second_debit": decimal_text(self.slippage_diagnostic),
                "realized_spot_gross": decimal_text(self.realized_spot_gross),
                "realized_perp_gross": decimal_text(self.realized_perp_gross),
                "funding_cash_posted": decimal_text(self.funding_cash_posted),
                "perp_margin_breached": self.perp_margin_breached,
                "ever_margin_breached": self.ever_margin_breached,
                "derivative_wallet_insolvent": self.deriv_wallet_cash < ZERO,
                "liquidation_count": self.liquidation_count,
                "committed_event_count": self._committed_count,
                "run_max_events": self.max_events,
                "pending_settlement_count": self._pending_count,
                "inflight_transfer_count": self._inflight_count,
            }

    def _validate_event(self, event):
        if not isinstance(event, dict):
            raise AccountingError("event object required")
        kind = event.get("type")
        if kind not in EVENT_FIELDS:
            raise AccountingError("unsupported event type")
        required, optional = EVENT_FIELDS[kind]
        if not COMMON_FIELDS | required <= set(event):
            raise AccountingError("missing required event field")
        if not set(event) <= COMMON_FIELDS | OPTIONAL_COMMON | required | optional:
            raise AccountingError("unknown or unsupported event field")
        identifier(event["id"], "event id")
        event_at = utc_time(event["event_time"], "event_time")
        applied_at = utc_time(event["applied_at"], "applied_at")
        known_at = utc_time(event["known_at"], "known_at")
        # This scope supports delayed cash posting as explicit later events,
        # but does not silently backdate delayed observations into prior states.
        if event_at != applied_at:
            raise AccountingError("late/backdated application outside synthetic scope")
        if known_at > applied_at:
            raise AccountingError("future-known event")
        sequence = event["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise AccountingError("nonnegative integer sequence required")
        order = (event_at, sequence)
        if self._last_order is not None and order <= self._last_order:
            raise AccountingError("event order is not strictly increasing")
        decision_at = utc_time(event.get("decision_time", event["event_time"]), "decision_time")
        if decision_at > event_at:
            raise AccountingError("decision after execution event")
        knowledge = event.get("knowledge_inputs", [])
        if not isinstance(knowledge, list):
            raise AccountingError("knowledge_inputs must be a list")
        for item in knowledge:
            if not isinstance(item, dict) or set(item) != {"id", "known_at"}:
                raise AccountingError("knowledge input must contain id and known_at only")
            identifier(item["id"], "knowledge id")
            if utc_time(item["known_at"], "knowledge known_at") > decision_at:
                raise AccountingError("future-known decision input")
        return order

    def apply_event(self, event):
        self._check_frozen_run()
        event_id = event.get("id") if isinstance(event, dict) else None
        identifier(event_id, "event id")
        try:
            digest = sha256_value(event)
        except (TypeError, ValueError) as exc:
            raise AccountingError("event must be canonical finite JSON") from exc
        if event_id in self._event_hashes:
            if digest != self._event_hashes[event_id]:
                raise AccountingError("conflicting committed event id")
            return {"status": "IDEMPOTENT_COMMITTED_NOOP", "event_id": event_id,
                    "event_sha256": digest, "state": self.view()}
        if self._committed_count >= self.max_events:
            raise AccountingError("run-frozen committed event budget exhausted")
        # Fixed-size scalar dictionary and references only: no deepcopy(self),
        # no full lot/history/identity-map cloning and no historical scans.
        staged = object.__new__(type(self))
        staged.__dict__ = self.__dict__.copy()
        staged.spot_lots = self.spot_lots.fork()
        staged.perp_lots = self.perp_lots.fork()
        staged.transfers = _RecordOverlay(self.transfers)
        staged.settlements = _RecordOverlay(self.settlements)
        with exact_money_context():
            order = staged._validate_event(event)
            before = staged.view()
            postings = staged._apply(event)
            staged._check_margin()
            staged._last_order = order
            staged._committed_count += 1
            after = staged.view()
            changes = {"spot_fifo_before": self.spot_lots.cursor(),
                       "spot_fifo_after": staged.spot_lots.cursor(),
                       "perp_fifo_before": self.perp_lots.cursor(),
                       "perp_fifo_after": staged.perp_lots.cursor(),
                       "new_spot_lots": [[decimal_text(q), decimal_text(p)] for q, p in staged.spot_lots.pending],
                       "new_perp_lots": [[decimal_text(q), decimal_text(p)] for q, p in staged.perp_lots.pending],
                       "transfer_records_changed": copy.deepcopy(staged.transfers.changes),
                       "settlement_records_changed": copy.deepcopy(staged.settlements.changes)}
            entry = {"ledger_sequence": staged._committed_count,
                     "event_id": event_id, "event_sha256": digest,
                     "event": copy.deepcopy(event), "before": before,
                     "postings": postings, "after": after,
                     "economic_changes": changes,
                     "nav_change": decimal_text(decimal(after["nav"]) - decimal(before["nav"])),
                     "prior_journal_chain_sha256": self._journal_chain_sha256}
            entry["journal_chain_sha256"] = sha256_value(entry)
            staged._journal_chain_sha256 = entry["journal_chain_sha256"]
            committed_event = copy.deepcopy(event)
            response = {"status": "COMMITTED", "event_id": event_id,
                        "event_sha256": digest, "state": copy.deepcopy(after)}
        # All modeled validation, arithmetic, serialization and journal preparation
        # finish before the commit. AccountingError/rejected events change nothing.
        # Process crashes/MemoryError and concurrent writers are outside this scope.
        staged.spot_lots.commit()
        staged.perp_lots.commit()
        staged.transfers = staged.transfers.commit()
        staged.settlements = staged.settlements.commit()
        self._event_hashes[event_id] = digest
        self.events.append(committed_event)
        self.ledger.append(entry)
        self.__dict__.clear()
        self.__dict__.update(staged.__dict__)
        return response

    @staticmethod
    def _posting(account, amount, unit="USDT", memo=False):
        return {"account": account, "amount": decimal_text(amount), "unit": unit, "memo_only": memo}

    @staticmethod
    def _consume_lots(lots, qty):
        return lots.consume(qty)

    @staticmethod
    def _average(lots):
        return CarryAccount._diagnostic_average(lots.total_cost, lots.total_qty)

    @staticmethod
    def _diagnostic_average(cost, qty):
        if not qty:
            return ZERO
        # This independently rounded display number never feeds cash, FIFO
        # cost, realized/unrealized PnL, IM/MM, NAV or reconciliation.
        with localcontext(DIAGNOSTIC_CONTEXT):
            return cost / qty

    def _fill_inputs(self, event):
        self._marked()
        side = event["side"]
        if side not in {"buy", "sell"}:
            raise AccountingError("side must be buy or sell")
        qty = input_decimal(event["qty"], "qty", positive=True)
        reference = input_decimal(event["reference_price"], "reference_price", positive=True)
        slip = input_decimal(event["slippage_rate"], "slippage_rate", nonnegative=True)
        fee_rate = input_decimal(event["fee_rate"], "fee_rate", nonnegative=True)
        if slip >= ONE or fee_rate >= ONE:
            raise AccountingError("slippage and fee rates must be less than one")
        if event.get("fee_currency", "USDT") != "USDT":
            raise AccountingError("base or third-asset fees require another protocol")
        fill = reference * (ONE + slip if side == "buy" else ONE - slip)
        fee = qty * fill * fee_rate
        self.slippage_diagnostic += qty * abs(fill - reference)
        return side, qty, fill, fee

    def _apply(self, event):
        kind = event["type"]
        if kind == "mark":
            self.spot_mark = input_decimal(event["spot_mark"], "spot_mark", positive=True)
            self.perp_mark = input_decimal(event["perp_mark"], "perp_mark", positive=True)
            return []
        if kind == "spot_fill":
            side, qty, fill, fee = self._fill_inputs(event)
            value = qty * fill
            if side == "buy":
                if self.spot_cash < value + fee:
                    raise AccountingError("insufficient spot cash including entry fee")
                self.spot_lots.append({"qty": qty, "price": fill})
                self.spot_qty += qty
                self.spot_average_entry = self._average(self.spot_lots)
                self.spot_cash -= value + fee
                cash_change, qty_change = -value - fee, qty
            else:
                if qty > self.spot_qty:
                    raise AccountingError("spot sell exceeds fully funded inventory")
                realized_cost = self._consume_lots(self.spot_lots, qty)
                self.realized_spot_gross += value - realized_cost
                self.spot_qty -= qty
                self.spot_cash += value - fee
                self.spot_average_entry = self._average(self.spot_lots)
                cash_change, qty_change = value - fee, -qty
            self.fees_total += fee
            return [self._posting("spot_cash", cash_change),
                    self._posting("spot_inventory_qty", qty_change, "BASE"),
                    self._posting("commission_expense_diagnostic", fee, memo=True)]
        if kind in {"perp_fill", "liquidation_fill"}:
            side, qty, fill, fee = self._fill_inputs(event)
            fixed_fee = ZERO
            if kind == "liquidation_fill":
                fixed_fee = input_decimal(event["fixed_fee"], "fixed_fee", nonnegative=True)
                if not self.perp_margin_breached or side != "buy" or qty != abs(self.perp_qty_signed):
                    raise AccountingError("synthetic liquidation requires breached full short closure")
                fee += fixed_fee
            if side == "sell":
                if kind == "liquidation_fill" or self.perp_margin_breached or self.ever_margin_breached:
                    raise AccountingError("v2 permanently rejects exposure increase after any margin breach")
                if self.deriv_wallet_cash < fee:
                    raise AccountingError("insufficient derivative cash for entry fee")
                new_qty = self.perp_qty_signed - qty
                if abs(new_qty) > self.spot_qty:
                    raise AccountingError("short opening exceeds confirmed funded spot inventory")
                old_entry_notional = self.perp_lots.total_cost
                new_entry_notional = old_entry_notional + qty * fill
                new_entry = self._diagnostic_average(new_entry_notional, abs(new_qty))
                new_wallet = self.deriv_wallet_cash - fee
                margin_equity = new_wallet + new_entry_notional - abs(new_qty) * self.perp_mark
                required_im = abs(new_qty) * fill * decimal(self.assumptions["initial_margin_rate"])
                if margin_equity < required_im:
                    raise AccountingError("insufficient initial margin including execution and fees")
                self.perp_qty_signed, self.perp_entry_price = new_qty, new_entry
                self.perp_lots.append({"qty": qty, "price": fill})
                self.deriv_wallet_cash = new_wallet
                cash_change, qty_change = -fee, -qty
            else:
                if self.perp_qty_signed >= ZERO or qty > abs(self.perp_qty_signed):
                    raise AccountingError("perpetual buy must reduce existing short only")
                closed_entry_notional = self._consume_lots(self.perp_lots, qty)
                realized = closed_entry_notional - qty * fill
                new_wallet = self.deriv_wallet_cash + realized - fee
                if new_wallet < ZERO and kind != "liquidation_fill":
                    raise AccountingError("negative wallet requires explicit synthetic liquidation")
                self.perp_qty_signed += qty
                self.deriv_wallet_cash = new_wallet
                self.realized_perp_gross += realized
                self.perp_entry_price = self._average(self.perp_lots)
                cash_change, qty_change = realized - fee, qty
                if kind == "liquidation_fill":
                    self.liquidation_count += 1
            self.fees_total += fee
            return [self._posting("deriv_wallet_cash", cash_change),
                    self._posting("perp_signed_qty", qty_change, "BASE"),
                    self._posting("commission_and_forced_close_expense_diagnostic", fee, memo=True)]
        if kind == "funding_settlement":
            self._marked()
            key = identifier(event["settlement_id"], "settlement_id")
            if key in self.settlements:
                raise AccountingError("duplicate settlement identity")
            settlement_time = utc_time(event["settlement_time"], "settlement_time")
            payment_time = utc_time(event["payment_time"], "payment_time")
            if settlement_time != utc_time(event["event_time"], "event_time"):
                raise AccountingError("settlement capture must occur at declared settlement time")
            if payment_time < settlement_time:
                raise AccountingError("funding payment precedes settlement")
            eligible = input_decimal(event["eligible_perp_qty_signed"], "eligible_perp_qty_signed")
            if eligible >= ZERO or eligible != self.perp_qty_signed:
                raise AccountingError("eligible quantity must equal held short at settlement")
            mark = input_decimal(event["settlement_mark"], "settlement_mark", positive=True)
            rate = input_decimal(event["rate"], "funding_rate")
            if abs(rate) > ONE:
                raise AccountingError("funding rate exceeds frozen synthetic range")
            amount = -eligible * mark * rate
            self.settlements[key] = {
                "status": "unposted", "amount": decimal_text(amount),
                "eligible_perp_qty_signed": decimal_text(eligible),
                "settlement_mark": decimal_text(mark), "rate": decimal_text(rate),
                "settlement_time": event["settlement_time"], "payment_time": event["payment_time"],
                "settlement_event_id": event["id"],
            }
            self._pending_total += amount
            self._pending_count += 1
            return [self._posting("pending_funding_memo_not_in_nav", amount, memo=True)]
        if kind == "funding_post":
            key = identifier(event["settlement_id"], "settlement_id")
            item = self.settlements.get(key)
            if item is None or item["status"] != "unposted":
                raise AccountingError("funding settlement absent or already posted")
            if utc_time(item["payment_time"], "payment_time") != utc_time(event["event_time"], "event_time"):
                raise AccountingError("funding posting must match frozen payment time")
            amount = decimal(item["amount"])
            self.deriv_wallet_cash += amount
            self.funding_cash_posted += amount
            item["status"] = "posted"
            item["posting_event_id"] = event["id"]
            self._pending_total -= amount
            self._pending_count -= 1
            return [self._posting("deriv_wallet_cash", amount),
                    self._posting("pending_funding_memo_not_in_nav", -amount, memo=True)]
        if kind == "transfer_send":
            key = identifier(event["transfer_id"], "transfer_id")
            if key in self.transfers:
                raise AccountingError("duplicate internal transfer identity")
            source, target = event["source"], event["target"]
            if source not in {"spot", "deriv"} or target not in {"spot", "deriv"} or source == target:
                raise AccountingError("internal transfer requires different declared wallets")
            amount = input_decimal(event["amount"], "transfer amount", positive=True)
            if source == "spot":
                if amount > self.spot_cash:
                    raise AccountingError("insufficient source spot cash")
                self.spot_cash -= amount
            else:
                if amount > self.deriv_wallet_cash:
                    raise AccountingError("insufficient source derivative cash")
                margin_equity, reserved, maintenance = self._margin()
                if self.perp_qty_signed < ZERO and (self.perp_margin_breached or
                                                    margin_equity - amount < reserved or
                                                    margin_equity - amount <= maintenance):
                    raise AccountingError("transfer would impair required derivative collateral")
                self.deriv_wallet_cash -= amount
            self.transfers[key] = {"source": source, "target": target,
                                   "amount": decimal_text(amount), "status": "inflight"}
            self._inflight_total += amount
            self._inflight_count += 1
            return [self._posting(source + "_cash", -amount), self._posting("transfer_inflight", amount)]
        if kind == "transfer_receive":
            key = identifier(event["transfer_id"], "transfer_id")
            item = self.transfers.get(key)
            if item is None or item["status"] != "inflight":
                raise AccountingError("transfer absent or already received")
            amount = decimal(item["amount"])
            if item["target"] == "spot":
                self.spot_cash += amount
            else:
                self.deriv_wallet_cash += amount
            item["status"] = "received"
            self._inflight_total -= amount
            self._inflight_count -= 1
            return [self._posting("transfer_inflight", -amount),
                    self._posting(item["target"] + "_cash", amount)]
        raise AccountingError("unsupported transition")

    def reconciliation(self):
        with exact_money_context():
            current = self.view()
            spot_upnl = self.spot_qty * (self.spot_mark or ZERO) - self.spot_lots.total_cost
            attribution = (self.realized_spot_gross + spot_upnl + self.realized_perp_gross +
                           self._perp_upnl() + self.funding_cash_posted - self.fees_total)
            nav_change = decimal(current["nav"]) - self.initial_equity
            difference = nav_change - attribution
            flat = self.spot_qty == ZERO and self.perp_qty_signed == ZERO
            no_inflight = self._inflight_count == 0
            no_pending = self._pending_count == 0
            no_liability = self.deriv_wallet_cash >= ZERO
            return {
                "state": current, "nav_minus_initial": decimal_text(nav_change),
                "pnl_attribution": decimal_text(attribution),
                "identity_difference": decimal_text(difference),
                "identity_exact": difference == ZERO,
                "all_legs_flat": flat, "no_transfer_inflight": no_inflight,
                "all_settled_funding_posted": no_pending,
                "no_derivative_debit_liability": no_liability,
                "closed_reconciliation": flat and no_inflight and no_pending and no_liability and difference == ZERO,
                "pending_settlement_is_memo_not_accrual": True,
                "slippage_already_in_fill_not_second_debit": True,
            }

    def hypothetical_exit(self, *, spot_reference_price, perp_reference_price,
                          spot_slippage_rate="0", perp_slippage_rate="0",
                          spot_fee_rate="0", perp_fee_rate="0"):
        """A nonmutating diagnostic, never a confirmed or closed execution."""
        self._marked()
        with exact_money_context():
            spot_reference = input_decimal(spot_reference_price, "spot reference", positive=True)
            perp_reference = input_decimal(perp_reference_price, "perp reference", positive=True)
            spot_slip = input_decimal(spot_slippage_rate, "spot slip", nonnegative=True)
            perp_slip = input_decimal(perp_slippage_rate, "perp slip", nonnegative=True)
            spot_fee = input_decimal(spot_fee_rate, "spot fee", nonnegative=True)
            perp_fee = input_decimal(perp_fee_rate, "perp fee", nonnegative=True)
            if max(spot_slip, perp_slip, spot_fee, perp_fee) >= ONE:
                raise AccountingError("hypothetical rates must be less than one")
            spot_fill = spot_reference * (ONE - spot_slip)
            perp_fill = perp_reference * (ONE + perp_slip)
            fees = self.spot_qty * spot_fill * spot_fee + abs(self.perp_qty_signed) * perp_fill * perp_fee
            price_change = (self.spot_qty * (spot_fill - self.spot_mark) +
                            abs(self.perp_qty_signed) * (self.perp_mark - perp_fill))
            current_nav = decimal(self.view()["nav"])
            estimated = current_nav + price_change - fees
            return {"status": "HYPOTHETICAL_NOT_CONFIRMED_EXECUTION",
                    "mark_nav": decimal_text(current_nav),
                    "hypothetical_exit_nav": decimal_text(estimated),
                    "hypothetical_exit_fees": decimal_text(fees),
                    "price_change_from_marks": decimal_text(price_change),
                    "extra_exit_haircut": decimal_text(current_nav - estimated),
                    "mutates_cash_positions_or_paid_fees": False,
                    "is_closed_reconciliation": False}

    def snapshot(self):
        self._check_frozen_run()
        result = {
            "schema": SCHEMA + "/snapshot", "protocol_id": PROTOCOL_ID,
            "run_contract": copy.deepcopy(self._run_contract),
            "run_contract_sha256": self._run_contract_sha256,
            "initial_spot_cash": decimal_text(self.initial_spot_cash),
            "initial_deriv_wallet_cash": decimal_text(self.initial_deriv_wallet_cash),
            "assumptions": copy.deepcopy(self.assumptions),
            "complete_committed_events": copy.deepcopy(self.events),
            "committed_event_count": self._committed_count,
            "journal_chain_sha256": self._journal_chain_sha256,
            "global_event_identity_sha256": sha256_value(self._event_hashes),
            "complete_status_records_sha256": sha256_value({"transfers": self.transfers, "settlements": self.settlements}),
            "final_state": self.view(), "incomplete_intent": False,
        }
        result["snapshot_sha256"] = sha256_value(result)
        return result

    @classmethod
    def restore(cls, snapshot):
        required = {"schema", "protocol_id", "run_contract", "run_contract_sha256",
                    "initial_spot_cash", "initial_deriv_wallet_cash", "assumptions",
                    "complete_committed_events", "committed_event_count", "journal_chain_sha256",
                    "global_event_identity_sha256", "complete_status_records_sha256",
                    "final_state", "incomplete_intent", "snapshot_sha256"}
        if not isinstance(snapshot, dict) or set(snapshot) != required:
            raise AccountingError("snapshot fields not accepted")
        if snapshot["incomplete_intent"] is not False:
            raise AccountingError("unresolved intent cannot be blindly resumed")
        if snapshot["schema"] != SCHEMA + "/snapshot" or snapshot["protocol_id"] != PROTOCOL_ID:
            raise AccountingError("snapshot scope mismatch; account/1 is not account/2")
        unsigned = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
        try:
            if sha256_value(unsigned) != snapshot["snapshot_sha256"]:
                raise AccountingError("snapshot hash mismatch")
            contract = snapshot["run_contract"]
            if not isinstance(contract, dict) or sha256_value(contract) != snapshot["run_contract_sha256"]:
                raise AccountingError("run contract hash mismatch")
            result = cls(snapshot["initial_spot_cash"], snapshot["initial_deriv_wallet_cash"],
                         snapshot["assumptions"], max_events=contract.get("max_events"))
        except (TypeError, ValueError) as exc:
            if isinstance(exc, AccountingError):
                raise
            raise AccountingError("snapshot canonical structure invalid") from exc
        if result._run_contract != contract or result._run_contract_sha256 != snapshot["run_contract_sha256"]:
            raise AccountingError("snapshot frozen run contract mismatch")
        events = snapshot["complete_committed_events"]
        count = snapshot["committed_event_count"]
        if not isinstance(events, list) or isinstance(count, bool) or not isinstance(count, int) or count != len(events) or count > result.max_events:
            raise AccountingError("snapshot complete event count outside run budget")
        for event in events:
            response = result.apply_event(event)
            if response["status"] != "COMMITTED":
                raise AccountingError("snapshot contains duplicate committed events")
        replay = result.snapshot()
        for key in ("final_state", "committed_event_count", "journal_chain_sha256",
                    "global_event_identity_sha256", "complete_status_records_sha256"):
            if replay[key] != snapshot[key]:
                raise AccountingError("snapshot complete replay mismatch: " + key)
        return result
