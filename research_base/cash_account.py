"""Versioned Decimal cash/FX model; supplied evidence is a gate, not certification.

No network, broker, stock engine or implicit rates. Daily balances are settled
cash observed at the declared calendar-day cutoff. Interest receivables and
external USD receivables belong to equity but cannot be spent.
"""
from __future__ import annotations

import calendar
import copy
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, localcontext, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP
import json
import re
from zoneinfo import ZoneInfo


class CashContractError(ValueError):
    pass


ROUNDING = {"DOWN": ROUND_DOWN, "HALF_EVEN": ROUND_HALF_EVEN, "HALF_UP": ROUND_HALF_UP}
CURRENCIES = {"CNY", "USD"}


def _keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise CashContractError(label + ": missing or unknown fields")


def number(value, label="number"):
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (str, int, Decimal)):
        raise CashContractError(label + ": exact decimal string/integer required")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise CashContractError(label + ": invalid decimal") from None
    if not result.is_finite():
        raise CashContractError(label + ": nonfinite")
    return result


def stamp(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise CashContractError("timezone-aware ISO timestamp required") from None
    if result.tzinfo is None:
        raise CashContractError("timezone-aware ISO timestamp required")
    return result


def _day(value):
    if not isinstance(value, str):
        raise CashContractError("canonical YYYY-MM-DD date required")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise CashContractError("canonical YYYY-MM-DD date required") from None
    if parsed.isoformat() != value:
        raise CashContractError("noncanonical date alias refused")
    return parsed


def serial(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    return value


def loads_rules(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CashContractError("duplicate JSON field: " + key)
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(CashContractError("nonfinite JSON")))
    except json.JSONDecodeError as error:
        raise CashContractError("invalid JSON rules") from error


def _evidence(evidence, use_at, classification):
    _keys(evidence, {"reference", "sha256", "known_at", "classification"}, "evidence")
    if not isinstance(evidence["reference"], str) or not evidence["reference"].strip():
        raise CashContractError("evidence reference required")
    if not isinstance(evidence["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", evidence["sha256"]):
        raise CashContractError("frozen evidence SHA256 required")
    if evidence["classification"] != classification:
        raise CashContractError("evidence classification mismatch")
    if stamp(evidence["known_at"]) > use_at:
        raise CashContractError("future evidence")


def _rounding(quantum, mode):
    q = number(quantum, "rounding quantum")
    if q <= 0 or q.normalize().as_tuple().digits != (1,) or mode not in ROUNDING:
        raise CashContractError("power-of-ten quantum and explicit rounding mode required")
    return q.normalize(), ROUNDING[mode]


def validate_rules(raw):
    spec = copy.deepcopy(raw)
    _keys(spec, {"schema_version", "classification", "start_at", "timezone", "accrual_cutoff",
                 "initial_cny", "funding_evidence", "money_rounding", "interest_schedules", "knowledge_clock"}, "cash rules")
    if spec["schema_version"] != "cash-account.v1":
        raise CashContractError("unsupported cash schema version")
    if spec["classification"] not in {"synthetic_control", "conditional_historical_model"}:
        raise CashContractError("explicit model classification required")
    _keys(spec["knowledge_clock"], {"basis", "freeze_at"}, "knowledge clock")
    clock = spec["knowledge_clock"]
    if clock["basis"] not in {"historical_point_in_time", "frozen_current_counterfactual"}:
        raise CashContractError("unknown knowledge clock")
    freeze = stamp(clock["freeze_at"])
    try:
        timezone = ZoneInfo(spec["timezone"])
        cutoff = time.fromisoformat(spec["accrual_cutoff"])
    except (TypeError, ValueError, KeyError):
        raise CashContractError("explicit timezone and cutoff required") from None
    if cutoff.tzinfo is not None:
        raise CashContractError("cutoff must use the declared timezone")
    start = stamp(spec["start_at"])
    if start.astimezone(timezone).time() != time(0, 0):
        raise CashContractError("funded start must be calendar-day midnight")
    if number(spec["initial_cny"]) < 0:
        raise CashContractError("negative initial wallet")
    funding = spec["funding_evidence"]
    _keys(funding, {"status", "lawful_and_usable", "route_description", "effective_at", "allowed_currencies", "evidence"}, "funding")
    status = "synthetic_control" if spec["classification"] == "synthetic_control" else "accepted_for_frozen_model"
    if funding["status"] != status or funding["lawful_and_usable"] is not True:
        raise CashContractError("funding path not explicitly accepted")
    if not isinstance(funding["route_description"], str) or not funding["route_description"].strip():
        raise CashContractError("funding route description required")
    if funding["allowed_currencies"] != ["CNY", "USD"]:
        raise CashContractError("explicit CNY/USD funding scope required")
    knowledge_start = start if clock["basis"] == "historical_point_in_time" else freeze
    if stamp(funding["effective_at"]) > knowledge_start:
        raise CashContractError("funding not effective at start")
    _evidence(funding["evidence"], min(knowledge_start, freeze), spec["classification"])
    _keys(spec["money_rounding"], CURRENCIES, "money rounding")
    for row in spec["money_rounding"].values():
        _keys(row, {"quantum", "mode"}, "money rounding row")
        _rounding(row["quantum"], row["mode"])
    if not isinstance(spec["interest_schedules"], list) or not spec["interest_schedules"]:
        raise CashContractError("cash interest cannot default to unknown zero")
    seen = set()
    required = {"schema_version", "currency", "effective_date", "known_at", "balance_observation",
                "source_effective_at", "net_rate_description", "eligibility_threshold", "eligibility_mode", "tier_mode", "tiers",
                "day_count", "rounding_quantum", "rounding_mode", "rounding_timing", "crediting", "evidence"}
    for row in spec["interest_schedules"]:
        _keys(row, required, "interest schedule")
        if row["schema_version"] != "cash-interest.v1" or row["currency"] not in CURRENCIES:
            raise CashContractError("interest version/currency refused")
        effective = _day(row["effective_date"])
        key = row["currency"], effective
        if key in seen:
            raise CashContractError("duplicate currency/effective date")
        seen.add(key)
        at = datetime.combine(effective, time(0, 0), timezone)
        use_clock = min(at, freeze) if clock["basis"] == "historical_point_in_time" else freeze
        if stamp(row["source_effective_at"]) > use_clock or stamp(row["known_at"]) > use_clock:
            raise CashContractError("late effective interest schedule")
        _evidence(row["evidence"], use_clock, spec["classification"])
        if row["balance_observation"] != "settled_at_calendar_cutoff":
            raise CashContractError("unsupported balance observation")
        if not isinstance(row["net_rate_description"], str) or not row["net_rate_description"].strip():
            raise CashContractError("net-rate/tax basis description required")
        if (row["eligibility_mode"] not in {"gate_all", "above_threshold"} or
            row["tier_mode"] not in {"marginal", "whole_balance"} or
            row["day_count"] not in {"ACT365", "ACT360"} or
            row["rounding_timing"] not in {"daily_accrual", "on_credit"} or
            row["crediting"] not in {"daily", "calendar_month_end"}):
            raise CashContractError("unknown interest semantics")
        if number(row["eligibility_threshold"]) < 0:
            raise CashContractError("negative eligibility threshold")
        _rounding(row["rounding_quantum"], row["rounding_mode"])
        if not isinstance(row["tiers"], list) or not row["tiers"]:
            raise CashContractError("explicit tiers required")
        lower = Decimal(0)
        for index, tier in enumerate(row["tiers"]):
            _keys(tier, {"lower", "upper", "net_annual_rate", "known_at"}, "interest tier")
            if number(tier["lower"]) != lower or stamp(tier["known_at"]) > use_clock:
                raise CashContractError("tier gap/order or future tier")
            number(tier["net_annual_rate"])
            if tier["upper"] is None:
                if index != len(row["tiers"]) - 1:
                    raise CashContractError("only final tier can be unbounded")
            else:
                upper = number(tier["upper"])
                if upper <= lower:
                    raise CashContractError("empty or overlapping tier")
                lower = upper
        if row["tiers"][-1]["upper"] is not None:
            raise CashContractError("final tier must be unbounded")
    for currency in CURRENCIES:
        if not any(r["currency"] == currency and _day(r["effective_date"]) <= start.astimezone(timezone).date()
                   for r in spec["interest_schedules"]):
            raise CashContractError("initial interest schedule missing for " + currency)
    return spec


class CashAccount:
    def __init__(self, rules):
        self.rules = validate_rules(rules)
        self.timezone = ZoneInfo(self.rules["timezone"])
        self.start_at = stamp(self.rules["start_at"])
        self.last_at = self.start_at
        self.last_accrued_day = self.start_at.astimezone(self.timezone).date() - timedelta(days=1)
        self.settled = {"CNY": number(self.rules["initial_cny"]), "USD": Decimal(0)}
        self.receivables = {"CNY": Decimal(0), "USD": Decimal(0)}
        self.unpaid_interest = {"CNY": Decimal(0), "USD": Decimal(0)}
        self.events = []
        self.used_ids = set()
        self._emit("FUNDING", self.start_at, initial_cny=self.settled["CNY"], funding_evidence=self.rules["funding_evidence"])

    def _emit(self, kind, at, **fields):
        event = serial({"sequence": len(self.events) + 1, "type": kind, "at": at, "knowledge_clock": self.rules["knowledge_clock"], **fields})
        self.events.append(event)
        return copy.deepcopy(event)

    def _time(self, at):
        at = stamp(at)
        if at < self.last_at:
            raise CashContractError("reverse account timestamp")
        return at

    def _id(self, event_id):
        if not isinstance(event_id, str) or not event_id or event_id in self.used_ids:
            raise CashContractError("missing or replayed event ID")

    def _money(self, amount, currency):
        row = self.rules["money_rounding"][currency]
        quantum, mode = _rounding(row["quantum"], row["mode"])
        return amount.quantize(quantum, rounding=mode)

    def cutoff_at(self, day):
        return datetime.combine(_day(day), time.fromisoformat(self.rules["accrual_cutoff"]), self.timezone).isoformat()

    def _rate(self, currency, day):
        available = [r for r in self.rules["interest_schedules"] if r["currency"] == currency and _day(r["effective_date"]) <= day]
        if not available:
            raise CashContractError("unknown cash interest")
        return max(available, key=lambda r: _day(r["effective_date"]))

    @staticmethod
    def _annual_interest(balance, rule):
        threshold = number(rule["eligibility_threshold"])
        if balance < threshold:
            return Decimal(0), Decimal(0)
        eligible = balance if rule["eligibility_mode"] == "gate_all" else max(Decimal(0), balance - threshold)
        total = Decimal(0)
        for tier in rule["tiers"]:
            lower = number(tier["lower"])
            upper = number(tier["upper"]) if tier["upper"] is not None else None
            rate = number(tier["net_annual_rate"])
            if rule["tier_mode"] == "whole_balance":
                if eligible >= lower and (upper is None or eligible < upper):
                    total = eligible * rate
                    break
            else:
                principal = max(Decimal(0), (min(eligible, upper) if upper is not None else eligible) - lower)
                total += principal * rate
        return eligible, total

    def accrue_day(self, day, *, as_of):
        day = _day(day)
        at = self._time(as_of)
        if day != self.last_accrued_day + timedelta(days=1):
            raise CashContractError("interest day replay, reversal or missing calendar day")
        if at != stamp(self.cutoff_at(day.isoformat())):
            raise CashContractError("interest use time must equal declared calendar cutoff")
        staged = []
        with localcontext() as context:
            context.prec = 38
            for currency in sorted(CURRENCIES):
                rule = self._rate(currency, day)
                eligible, annual = self._annual_interest(self.settled[currency], rule)
                raw = annual / Decimal(365 if rule["day_count"] == "ACT365" else 360)
                quantum, mode = _rounding(rule["rounding_quantum"], rule["rounding_mode"])
                accrued = raw.quantize(quantum, rounding=mode) if rule["rounding_timing"] == "daily_accrual" else raw
                unpaid = self.unpaid_interest[currency] + accrued
                due = rule["crediting"] == "daily" or day.day == calendar.monthrange(day.year, day.month)[1]
                credited = unpaid.quantize(quantum, rounding=mode) if due else Decimal(0)
                if self.settled[currency] + credited < 0:
                    raise CashContractError("interest would create negative wallet")
                staged.append((currency, accrued, credited, Decimal(0) if due else unpaid, rule, eligible, raw, unpaid - credited if due else Decimal(0)))
            emitted = []
            for currency, accrued, credited, unpaid, rule, eligible, raw, rounding_delta in staged:
                self.settled[currency] += credited
                self.unpaid_interest[currency] = unpaid
                emitted.append(self._emit("CASH_INTEREST", at, currency=currency, day=day, eligible_balance=eligible,
                    raw_accrual=raw, accrued=accrued, credited=credited, unpaid_interest=unpaid,
                    credit_rounding_delta=rounding_delta, effective_date=rule["effective_date"],
                    day_count=rule["day_count"], crediting=rule["crediting"], evidence=rule["evidence"]))
        self.last_accrued_day, self.last_at = day, at
        return {"events": emitted, "credited": {r[0]: str(r[2]) for r in staged}, "snapshot": self.balances()}

    def sync_usd(self, *, settled_usd, receivables_usd, as_of, event_id):
        """Caller supplies settled stock-engine cash; receivables stay unspendable.

        Apply previously returned interest credits to the external engine before
        the next sync; otherwise an overwrite would intentionally omit that cash.
        """
        at = self._time(as_of)
        self._id(event_id)
        cash, receivable = number(settled_usd), number(receivables_usd)
        if cash < 0 or receivable < 0:
            raise CashContractError("negative wallet/receivable")
        previous = self.settled["USD"]
        self.settled["USD"], self.receivables["USD"] = cash, receivable
        self.last_at = at
        self.used_ids.add(event_id)
        event = self._emit("EXTERNAL_USD_SYNC", at, event_id=event_id, previous_settled=previous,
                           settled=cash, receivables=receivable)
        return {"events": [event], "snapshot": self.balances()}

    def _knowledge_at(self, at):
        clock = self.rules["knowledge_clock"]
        frozen = stamp(clock["freeze_at"])
        return min(at, frozen) if clock["basis"] == "historical_point_in_time" else frozen

    def _fx(self, quote, at, *, execution=False, extra_spread_rate="0"):
        required = {"schema_version", "quoted_at", "known_at", "cny_per_usd", "evidence"}
        if execution:
            required |= {"buy_usd_spread_bps", "sell_usd_spread_bps", "buy_fixed_fee_cny", "sell_fixed_fee_usd"}
        _keys(quote, required, "FX execution" if execution else "FX mark")
        version = "fx-execution.v1" if execution else "fx-mark.v1"
        if quote["schema_version"] != version:
            raise CashContractError("unsupported FX quote version")
        quoted, known = stamp(quote["quoted_at"]), stamp(quote["known_at"])
        knowledge_at = self._knowledge_at(at)
        if quoted > at or quoted > known or known > knowledge_at:
            raise CashContractError("future or invalid FX quote/receipt")
        _evidence(quote["evidence"], knowledge_at, self.rules["classification"])
        mid = number(quote["cny_per_usd"])
        if mid <= 0:
            raise CashContractError("nonpositive CNY per USD quote")
        if not execution:
            return mid
        buy, sell = number(quote["buy_usd_spread_bps"]), number(quote["sell_usd_spread_bps"])
        buy_fee, sell_fee = number(quote["buy_fixed_fee_cny"]), number(quote["sell_fixed_fee_usd"])
        if buy < 0 or sell < 0 or sell >= 10000 or buy_fee < 0 or sell_fee < 0:
            raise CashContractError("invalid explicit FX costs")
        extra = number(extra_spread_rate, "extra FX spread rate")
        if extra < 0 or extra >= 1:
            raise CashContractError("invalid extra FX stress spread")
        return mid * (1 + buy / 10000) * (1 + extra), mid * (1 - sell / 10000) * (1 - extra), buy_fee, sell_fee

    def exchange(self, direction, amount, *, execution_quote, as_of, event_id, extra_spread_rate="0"):
        at = self._time(as_of)
        self._id(event_id)
        amount = number(amount)
        if amount <= 0 or direction not in {"CNY_TO_USD", "USD_TO_CNY"}:
            raise CashContractError("positive FX amount and explicit direction required")
        with localcontext() as context:
            context.prec = 38
            ask, bid, buy_fee, sell_fee = self._fx(execution_quote, at, execution=True, extra_spread_rate=extra_spread_rate)
            source, target = ("CNY", "USD") if direction == "CNY_TO_USD" else ("USD", "CNY")
            fee = buy_fee if source == "CNY" else sell_fee
            debit = amount + fee
            credit = self._money(amount / ask if source == "CNY" else amount * bid, target)
            available = max(Decimal(0), self.settled[source] + min(Decimal(0), self.unpaid_interest[source]))
            if debit > available or credit <= 0:
                raise CashContractError("insufficient settled wallet; receivables cannot fund FX")
            self.settled[source] -= debit
            self.settled[target] += credit
            event = self._emit("FX_EXECUTION", at, event_id=event_id, direction=direction,
                source=source, target=target, principal=amount, source_fixed_fee=fee, debit=debit, credit=credit,
                execution_cny_per_usd=ask if source == "CNY" else bid, quote=execution_quote, extra_spread_rate=number(extra_spread_rate))
        self.used_ids.add(event_id)
        self.last_at = at
        return {"events": [event], "snapshot": self.balances()}

    def balances(self):
        return serial({currency: {"settled": self.settled[currency], "available": max(Decimal(0), self.settled[currency] + min(Decimal(0), self.unpaid_interest[currency])),
            "receivables": self.receivables[currency], "unpaid_interest": self.unpaid_interest[currency]}
            for currency in sorted(CURRENCIES)})

    def snapshot(self, *, as_of, fx_mark, usd_assets, exit_quote=None, extra_spread_rate="0"):
        at = self._time(as_of)
        assets = number(usd_assets, "USD external asset value")
        if assets < 0:
            raise CashContractError("negative external asset value")
        with localcontext() as context:
            context.prec = 38
            mid = self._fx(fx_mark, at)
            cny_equity = self.settled["CNY"] + self.receivables["CNY"] + self.unpaid_interest["CNY"]
            usd_equity = self.settled["USD"] + self.receivables["USD"] + self.unpaid_interest["USD"] + assets
            marked = cny_equity + usd_equity * mid
            result = {"as_of": at, "knowledge_clock": self.rules["knowledge_clock"], "classification": self.rules["classification"], "wallets": self.balances(),
                "usd_external_assets": assets, "usd_total_equity": usd_equity, "fx_mark_cny_per_usd": mid,
                "marked_equity_cny": marked, "funding_certified_by_module": False,
                "exit_equity_cny": None, "settled_cash_exit_cny": None,
                "exit_quote": copy.deepcopy(exit_quote), "extra_exit_spread_rate": number(extra_spread_rate),
                "exit_limit": "FX-only exit is unavailable without explicit costs; assets/receivables are not settled cash"}
            if exit_quote is not None:
                _, bid, _, sell_fee = self._fx(exit_quote, at, execution=True, extra_spread_rate=extra_spread_rate)
                spendable = max(Decimal(0), self.settled["USD"] + min(Decimal(0), self.unpaid_interest["USD"]))
                cash_exit = Decimal(0) if spendable == 0 else (self._money((spendable - sell_fee) * bid, "CNY") if spendable >= sell_fee else None)
                cny_available = max(Decimal(0), self.settled["CNY"] + min(Decimal(0), self.unpaid_interest["CNY"]))
                result["settled_cash_exit_cny"] = cny_available + cash_exit if cash_exit is not None else None
                if assets == 0 and self.receivables["USD"] == 0 and self.unpaid_interest["USD"] == 0 and self.receivables["CNY"] == 0 and self.unpaid_interest["CNY"] == 0:
                    result["exit_equity_cny"] = result["settled_cash_exit_cny"]
                    result["exit_limit"] = "all equity settled cash; explicit FX spread/fee applied without executing"
                else:
                    result["exit_limit"] = "only settled cash is immediately executable; asset liquidation and unpaid amounts need separate acceptance"
            return serial(result)

# Frozen, synthetic instrument answers; these are not funding/fee certification.
EXPECTED_ANSWERS = {
    "fx_entry": ["0", "1000", "7.07"],
    "fx_mark_and_cash_exit": ["7100", "6951.04", "7100"],
    "fx_sell": ["3479", "499"],
    "extra_fx_stress_explicit": ["997.5", "7.087675", "0.0025"],
    "calendar_unpaid_not_spendable": ["36500", "3.65", "36503.65"],
    "saturday_month_end_credit": ["36507.3", "0", "7.3"],
    "sunday_accrual": ["36507.3", "3.65"],
    "receivables_and_assets_not_interest_base": ["1001", "9999", "100000", None, "6860"],
    "marginal_above_threshold": "0.7",
    "whole_above_threshold": "0.45",
    "marginal_gate_all": "0.75",
    "act360": "3.7",
    "on_credit_rounding": "0.01",
    "daily_accrual_rounding": "0",
    "effective_rate_change": "10.95",
    "second_day_new_daily_rate": "7.3",
    "noncanonical_effective_date_rejected": True,
    "missing_rate_rejected": True,
    "duplicate_rate_date_rejected": True,
    "late_rate_rejected": True,
    "future_tier_rejected": True,
    "pending_funding_rejected": True,
    "future_fx_rejected": True,
    "receivable_cannot_fund_fx": True,
    "replayed_interest_rejected": True,
    "missing_calendar_day_rejected": True,
    "reverse_sync_rejected": True,
    "negative_wallet_rejected": True,
    "unknown_semantics_rejected": True,
    "duplicate_json_field_rejected": True,
    "float_money_rejected": True,
    "counterfactual_current_quote_explicit": ["8000", "frozen_current_counterfactual", False],
    "counterfactual_post_freeze_rate_rejected": True,
    "counterfactual_post_freeze_quote_rejected": True,
    "counterfactual_future_price_time_rejected": True,
    "event_replay_rejected": True,
    "zero_usd_exit_does_not_charge_fx": "100",
    "negative_monthend_reserve": ["100", "-3", "97"],
    "negative_unpaid_cannot_spend_full": True,
    "negative_monthend_posting": ["96.9", "0", "96.9"],
}
EXPECTED_CONTROL_IDS = (
    "fx_entry", "fx_mark_and_cash_exit", "fx_sell", "extra_fx_stress_explicit",
    "calendar_unpaid_not_spendable", "saturday_month_end_credit", "sunday_accrual",
    "receivables_and_assets_not_interest_base", "marginal_above_threshold",
    "whole_above_threshold", "marginal_gate_all", "act360", "on_credit_rounding",
    "daily_accrual_rounding", "effective_rate_change", "second_day_new_daily_rate",
    "noncanonical_effective_date_rejected", "missing_rate_rejected",
    "duplicate_rate_date_rejected", "late_rate_rejected", "future_tier_rejected",
    "pending_funding_rejected", "future_fx_rejected", "receivable_cannot_fund_fx",
    "replayed_interest_rejected", "missing_calendar_day_rejected", "reverse_sync_rejected",
    "negative_wallet_rejected", "unknown_semantics_rejected", "duplicate_json_field_rejected",
    "float_money_rejected", "counterfactual_current_quote_explicit",
    "counterfactual_post_freeze_rate_rejected", "counterfactual_post_freeze_quote_rejected",
    "counterfactual_future_price_time_rejected",
    "event_replay_rejected", "zero_usd_exit_does_not_charge_fx",
    "negative_monthend_reserve", "negative_unpaid_cannot_spend_full", "negative_monthend_posting",
)


def _control_rules(initial="36500", *, start="2026-01-30", crediting="calendar_month_end"):
    at = start + "T00:00:00+00:00"
    evidence = {"reference": "synthetic instrument only", "sha256": "0" * 64,
                "known_at": at, "classification": "synthetic_control"}
    def rate(currency):
        return {"schema_version": "cash-interest.v1", "currency": currency,
            "effective_date": start, "source_effective_at": at, "known_at": at,
            "balance_observation": "settled_at_calendar_cutoff", "net_rate_description": "synthetic net APR, no vendor rate",
            "eligibility_threshold": "0", "eligibility_mode": "gate_all", "tier_mode": "marginal",
            "tiers": [{"lower": "0", "upper": None, "net_annual_rate": "0.0365", "known_at": at}],
            "day_count": "ACT365", "rounding_quantum": "0.01", "rounding_mode": "HALF_UP",
            "rounding_timing": "daily_accrual", "crediting": crediting, "evidence": copy.deepcopy(evidence)}
    return {"schema_version": "cash-account.v1", "classification": "synthetic_control",
        "start_at": at, "timezone": "UTC", "accrual_cutoff": "23:59:59", "initial_cny": initial,
        "knowledge_clock": {"basis": "historical_point_in_time", "freeze_at": "2026-10-05T00:00:00+00:00"},
        "funding_evidence": {"status": "synthetic_control", "lawful_and_usable": True,
            "route_description": "fictional CNY funding/FX control, no account acceptance",
            "effective_at": at, "allowed_currencies": ["CNY", "USD"], "evidence": copy.deepcopy(evidence)},
        "money_rounding": {c: {"quantum": "0.01", "mode": "DOWN"} for c in CURRENCIES},
        "interest_schedules": [rate("CNY"), rate("USD")]}


def _control_quote(at, *, execution=False, mid="7"):
    result = {"schema_version": "fx-execution.v1" if execution else "fx-mark.v1",
        "quoted_at": at, "known_at": at, "cny_per_usd": mid,
        "evidence": {"reference": "synthetic FX instrument only", "sha256": "0" * 64,
                     "known_at": at, "classification": "synthetic_control"}}
    if execution:
        result.update(buy_usd_spread_bps="100", sell_usd_spread_bps="200", buy_fixed_fee_cny="7", sell_fixed_fee_usd="1")
    return result


def _control_serial(value):
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dict):
        return {k: _control_serial(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_control_serial(v) for v in value]
    return value


def validate_control_report(report):
    from collections import Counter
    from .execution.reporting import same_typed_value
    rows = report.get("reports", [])
    ids = Counter(r.get("id") for r in rows)
    missing = sorted(set(EXPECTED_CONTROL_IDS) - set(ids))
    extra = sorted(set(ids) - set(EXPECTED_CONTROL_IDS), key=str)
    duplicates = sorted((k for k, v in ids.items() if v != 1), key=str)
    wrong = [r.get("id") for r in rows if r.get("id") in EXPECTED_ANSWERS and
        (r.get("passed") is not True or not same_typed_value(r.get("expected"), EXPECTED_ANSWERS[r["id"]]) or
         not same_typed_value(r.get("actual"), EXPECTED_ANSWERS[r["id"]]))]
    return {"accepted": not (missing or extra or duplicates or wrong) and len(rows) == len(EXPECTED_CONTROL_IDS)
                and report.get("classification") == "SYNTHETIC_CASH_FX_CONTROLS_ONLY",
            "expected_report_count": len(EXPECTED_CONTROL_IDS), "observed_report_count": len(rows),
            "missing_ids": missing, "extra_ids": extra, "duplicate_ids": duplicates, "wrong_answers": wrong}


def run_controls(output_dir):
    """Finite synthetic known-answer controls; preserves every observed answer."""
    from pathlib import Path
    import hashlib
    folder = Path(output_dir) / datetime.now().strftime("cash-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    rows, accounts = [], []
    def record(id, actual):
        actual = _control_serial(actual)
        expected = EXPECTED_ANSWERS[id]
        rows.append({"id": id, "expected": expected, "actual": actual, "passed": actual == expected})
    def reject(id, function):
        try:
            function()
        except (CashContractError, ValueError):
            record(id, True)
        else:
            record(id, False)
    def build(rules):
        account = CashAccount(rules)
        accounts.append(account)
        return account
    def accrue(account, day):
        return account.accrue_day(day, as_of=account.cutoff_at(day))
    at = "2026-01-30T12:00:00+00:00"
    execution, mark = _control_quote(at, execution=True), _control_quote(at, mid="7.1")
    account = build(_control_rules("7077"))
    event = account.exchange("CNY_TO_USD", "7070", execution_quote=execution, as_of=at, event_id="entry")["events"][0]
    record("fx_entry", [account.settled["CNY"], account.settled["USD"], number(event["execution_cny_per_usd"])])
    snap = account.snapshot(as_of=at, fx_mark=mark, usd_assets="0", exit_quote={**execution, "cny_per_usd": "7.1"})
    record("fx_mark_and_cash_exit", [number(snap["marked_equity_cny"]), number(snap["exit_equity_cny"]), account.settled["USD"] * Decimal("7.1")])
    account.exchange("USD_TO_CNY", "500", execution_quote={**execution, "cny_per_usd": "7.1"}, as_of=at, event_id="exit")
    record("fx_sell", [account.settled["CNY"], account.settled["USD"]])
    reject("event_replay_rejected", lambda: account.exchange("USD_TO_CNY", "1", execution_quote=execution, as_of=at, event_id="exit"))
    stressed = build(_control_rules("7077"))
    event = stressed.exchange("CNY_TO_USD", "7070", execution_quote=execution, as_of=at, event_id="entry", extra_spread_rate="0.0025")["events"][0]
    record("extra_fx_stress_explicit", [stressed.settled["USD"], number(event["execution_cny_per_usd"]), number(event["extra_spread_rate"])])
    account = build(_control_rules())
    accrue(account, "2026-01-30")
    snap = account.snapshot(as_of=account.cutoff_at("2026-01-30"), fx_mark=_control_quote(at), usd_assets="0")
    record("calendar_unpaid_not_spendable", [account.settled["CNY"], account.unpaid_interest["CNY"], number(snap["marked_equity_cny"])])
    result = accrue(account, "2026-01-31")
    record("saturday_month_end_credit", [account.settled["CNY"], account.unpaid_interest["CNY"], number(result["credited"]["CNY"])])
    accrue(account, "2026-02-01")
    record("sunday_accrual", [account.settled["CNY"], account.unpaid_interest["CNY"]])
    reject("replayed_interest_rejected", lambda: accrue(account, "2026-02-01"))
    reject("missing_calendar_day_rejected", lambda: accrue(account, "2026-02-03"))
    reject("reverse_sync_rejected", lambda: account.sync_usd(settled_usd="0", receivables_usd="0", as_of=at, event_id="reverse"))
    rules = _control_rules("0", crediting="daily")
    rules["interest_schedules"][1]["tiers"][0]["net_annual_rate"] = "0.365"
    account = build(rules)
    account.sync_usd(settled_usd="1000", receivables_usd="9999", as_of=at, event_id="outer")
    accrue(account, "2026-01-30")
    snap = account.snapshot(as_of=account.cutoff_at("2026-01-30"), fx_mark=_control_quote(at), usd_assets="100000", exit_quote=execution)
    record("receivables_and_assets_not_interest_base", [account.settled["USD"], account.receivables["USD"], number(snap["usd_external_assets"]), snap["exit_equity_cny"], number(snap["settled_cash_exit_cny"])])
    # Exit cost: (1001-1)*6.86=6860; expected below is independently frozen.
    for id, tier_mode, eligibility in [("marginal_above_threshold", "marginal", "above_threshold"),
        ("whole_above_threshold", "whole_balance", "above_threshold"), ("marginal_gate_all", "marginal", "gate_all")]:
        rules = _control_rules("1000", crediting="daily")
        rate = rules["interest_schedules"][0]
        rate.update(eligibility_threshold="100", eligibility_mode=eligibility, tier_mode=tier_mode, day_count="ACT360")
        rate["tiers"] = [{"lower": "0", "upper": "500", "net_annual_rate": "0.36", "known_at": rules["start_at"]},
                         {"lower": "500", "upper": None, "net_annual_rate": "0.18", "known_at": rules["start_at"]}]
        account = build(rules)
        record(id, number(accrue(account, "2026-01-30")["credited"]["CNY"]))
    rules = _control_rules(crediting="daily")
    rules["interest_schedules"][0]["day_count"] = "ACT360"
    record("act360", number(accrue(build(rules), "2026-01-30")["credited"]["CNY"]))
    for id, timing in [("on_credit_rounding", "on_credit"), ("daily_accrual_rounding", "daily_accrual")]:
        rules = _control_rules("100")
        rate = rules["interest_schedules"][0]
        rate.update(rounding_timing=timing)
        rate["tiers"][0]["net_annual_rate"] = "0.012775"
        account = build(rules)
        accrue(account, "2026-01-30")
        record(id, number(accrue(account, "2026-01-31")["credited"]["CNY"]))
    rules = _control_rules()
    rate = copy.deepcopy(rules["interest_schedules"][0])
    rate["effective_date"] = "2026-01-31"
    rate["tiers"][0]["net_annual_rate"] = "0.073"
    rules["interest_schedules"].append(rate)
    account = build(rules)
    accrue(account, "2026-01-30")
    record("effective_rate_change", number(accrue(account, "2026-01-31")["credited"]["CNY"]))
    daily_rules = copy.deepcopy(rules)
    for rate in daily_rules["interest_schedules"]:
        rate["crediting"] = "daily"
    daily_account = build(daily_rules)
    accrue(daily_account, "2026-01-30")
    record("second_day_new_daily_rate", number(accrue(daily_account, "2026-01-31")["credited"]["CNY"]))
    aliased = _control_rules()
    aliased["interest_schedules"][0]["effective_date"] = "20260130"
    reject("noncanonical_effective_date_rejected", lambda: CashAccount(aliased))
    malformed = []
    r = _control_rules(); r["interest_schedules"] = r["interest_schedules"][:1]; malformed.append(("missing_rate_rejected", r))
    r = _control_rules(); r["interest_schedules"].append(copy.deepcopy(r["interest_schedules"][0])); malformed.append(("duplicate_rate_date_rejected", r))
    r = _control_rules(); r["interest_schedules"][0]["known_at"] = at; malformed.append(("late_rate_rejected", r))
    r = _control_rules(); r["interest_schedules"][0]["tiers"][0]["known_at"] = at; malformed.append(("future_tier_rejected", r))
    r = _control_rules(); r["funding_evidence"]["status"] = "unknown"; malformed.append(("pending_funding_rejected", r))
    r = _control_rules(); r["interest_schedules"][0]["crediting"] = "unknown"; malformed.append(("unknown_semantics_rejected", r))
    r = _control_rules(); r["initial_cny"] = 1.0; malformed.append(("float_money_rejected", r))
    for id, invalid in malformed:
        reject(id, lambda invalid=invalid: CashAccount(invalid))
    reject("duplicate_json_field_rejected", lambda: loads_rules('{"x":1,"x":2}'))
    account = build(_control_rules("100"))
    reject("future_fx_rejected", lambda: account.snapshot(as_of=at, fx_mark=_control_quote("2026-02-01T00:00:00+00:00"), usd_assets="0"))
    reject("negative_wallet_rejected", lambda: account.sync_usd(settled_usd="-1", receivables_usd="0", as_of=at, event_id="negative"))
    account.sync_usd(settled_usd="0", receivables_usd="1000", as_of=at, event_id="receivable")
    reject("receivable_cannot_fund_fx", lambda: account.exchange("USD_TO_CNY", "100", execution_quote=execution, as_of=at, event_id="invalid-spend"))
    record("zero_usd_exit_does_not_charge_fx", number(account.snapshot(as_of=at, fx_mark=_control_quote(at), usd_assets="0", exit_quote=execution)["settled_cash_exit_cny"]))
    rules = _control_rules("1000", start="2006-01-01")
    rules["knowledge_clock"]["basis"] = "frozen_current_counterfactual"
    source_at = "2026-10-01T00:00:00+00:00"
    rules["funding_evidence"]["effective_at"] = source_at
    rules["funding_evidence"]["evidence"]["known_at"] = source_at
    for rate in rules["interest_schedules"]:
        rate["known_at"] = rate["source_effective_at"] = rate["evidence"]["known_at"] = source_at
        rate["tiers"][0]["known_at"] = source_at
    account = build(rules)
    past_quote = _control_quote(source_at)
    past_quote["quoted_at"] = "2006-01-01T00:00:00+00:00"
    snap = account.snapshot(as_of="2006-01-01T12:00:00+00:00", fx_mark=past_quote, usd_assets="1000")
    reject("counterfactual_future_price_time_rejected", lambda: account.snapshot(as_of="2006-01-01T12:00:00+00:00", fx_mark=_control_quote(source_at), usd_assets="0"))
    record("counterfactual_current_quote_explicit", [number(snap["marked_equity_cny"]), snap["knowledge_clock"]["basis"], snap["funding_certified_by_module"]])
    bad = copy.deepcopy(rules); bad["interest_schedules"][0]["known_at"] = "2026-10-06T00:00:00+00:00"
    reject("counterfactual_post_freeze_rate_rejected", lambda: CashAccount(bad))
    reject("counterfactual_post_freeze_quote_rejected", lambda: account.snapshot(as_of="2006-01-01T12:00:00+00:00", fx_mark=_control_quote("2026-10-06T00:00:00+00:00"), usd_assets="0"))
    negative_rules = _control_rules("100", start="2026-01-01")
    negative_rules["interest_schedules"][0]["tiers"][0]["net_annual_rate"] = "-0.365"
    negative_account = build(negative_rules)
    for n in range(1, 31):
        accrue(negative_account, "2026-01-" + str(n).zfill(2))
    record("negative_monthend_reserve", [negative_account.settled["CNY"], negative_account.unpaid_interest["CNY"], number(negative_account.balances()["CNY"]["available"])])
    zero_cost_quote = _control_quote(negative_account.cutoff_at("2026-01-30"), execution=True)
    zero_cost_quote.update(buy_usd_spread_bps="0", sell_usd_spread_bps="0", buy_fixed_fee_cny="0", sell_fixed_fee_usd="0")
    reject("negative_unpaid_cannot_spend_full", lambda: negative_account.exchange("CNY_TO_USD", "100", execution_quote=zero_cost_quote, as_of=negative_account.cutoff_at("2026-01-30"), event_id="negative-reserve-spend"))
    accrue(negative_account, "2026-01-31")
    record("negative_monthend_posting", [negative_account.settled["CNY"], negative_account.unpaid_interest["CNY"], number(negative_account.balances()["CNY"]["available"])])
    report = {"classification": "SYNTHETIC_CASH_FX_CONTROLS_ONLY", "reports": rows,
              "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "expected_control_ids": list(EXPECTED_CONTROL_IDS), "funding_certified": False,
              "historical_return_experiment": False, "run_directory": str(folder)}
    report["completeness"] = validate_control_report(report)
    report["accepted"] = report["completeness"]["accepted"]
    (folder / "report.json").write_text(json.dumps(report, indent=2))
    (folder / "events.json").write_text(json.dumps([a.events for a in accounts], indent=2))
    return report


def control_report_accepted(report):
    return validate_control_report(report)["accepted"]
