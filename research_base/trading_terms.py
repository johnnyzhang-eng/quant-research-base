"""Frozen, dated trading rules and S02 cost scenarios; no provider defaults.

Evidence references describe a reviewed model. They do not establish a person's
account eligibility or certify that a historical opening order could fill.
"""
import copy
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from .evidence import ContractError, canonical_hash, write_json
from .reference.low_frequency_engine import stamp as _contract_stamp

VERSION = "trading-terms/1"
SCENARIOS = {"C0", "C1", "C2", "C3"}
ROUNDING = {"half_up": ROUND_HALF_UP, "ceiling": ROUND_CEILING}


def stamp(value):
    try:
        return _contract_stamp(value)
    except ValueError as exc:
        raise ContractError("timezone-aware rule/use/freeze timestamp required") from exc


def _keys(value, expected, name):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ContractError(f"{name}: exact versioned keys required")


def _number(value, name, minimum=Decimal(0)):
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractError(f"{name}: decimal string required")
    try:
        result = Decimal(value)
    except Exception as exc:
        raise ContractError(f"{name}: invalid number") from exc
    if not result.is_finite() or result < minimum:
        raise ContractError(f"{name}: nonfinite or out of range")
    return result


def _day(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("noncanonical date")
        return parsed
    except (ValueError, TypeError) as exc:
        raise ContractError("ISO rule date required") from exc


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ContractError(f"{name}: nonnegative integer required")
    return value


def _evidence(value):
    _keys(value, {"reference", "sha256", "basis"}, "evidence")
    checksum = value["sha256"]
    if (not isinstance(value["reference"], str) or not value["reference"].strip()
            or not isinstance(checksum, str) or len(checksum) != 64
            or any(c not in "0123456789abcdef" for c in checksum)
            or value["basis"] not in {"historically_effective", "current_counterfactual", "invented_control"}):
        raise ContractError("explicit evidence identity and historical/model basis required")


class TradingTerms:
    """An immutable snapshot of explicit rules, selected by trading date.

    Historical rules require knowledge before use. Current counterfactual rules
    require knowledge before the explicit model freeze, preserving today's
    observation clock rather than claiming historically received evidence.
    """

    def __init__(self, document):
        _keys(document, {"schema_version", "data_kind", "currency", "frozen_at", "rules"}, "terms")
        if (document["schema_version"] != VERSION or document["currency"] != "USD"
                or document["data_kind"] not in {"reviewed_model", "invented_control"}):
            raise ContractError("explicit USD model version and classification required")
        frozen = stamp(document["frozen_at"])
        rules = document["rules"]
        if not isinstance(rules, list) or not rules:
            raise ContractError("nonempty dated rules required")
        previous, ids = None, set()
        for rule in rules:
            _keys(rule, {"id", "from", "through", "known_at", "evidence", "fees",
                         "execution", "settlement"}, "dated rule")
            if not isinstance(rule["id"], str) or not rule["id"] or rule["id"] in ids:
                raise ContractError("unique rule id required")
            ids.add(rule["id"])
            start, end = _day(rule["from"]), _day(rule["through"])
            if start > end or (previous is not None and start <= previous):
                raise ContractError("rules must ascend without overlap")
            previous = end
            if stamp(rule["known_at"]) > frozen:
                raise ContractError("rule knowledge postdates model freeze")
            _evidence(rule["evidence"])
            if document["data_kind"] == "invented_control" and rule["evidence"]["basis"] != "invented_control":
                raise ContractError("invented input cannot impersonate reviewed rules")
            fees = rule["fees"]
            if not isinstance(fees, list) or not fees:
                raise ContractError("all fees require explicit components, including explicit zero")
            fee_ids = set()
            for fee in fees:
                _keys(fee, {"id", "category", "sides", "basis", "rate", "minimum",
                            "maximum", "quantum", "rounding"}, "fee component")
                if not isinstance(fee["id"], str) or not fee["id"] or fee["id"] in fee_ids:
                    raise ContractError("unique fee component id required")
                fee_ids.add(fee["id"])
                if (fee["category"] not in {"broker", "tax"}
                        or fee["sides"] not in [["BUY"], ["SELL"], ["BUY", "SELL"]]
                        or fee["basis"] not in {"notional", "shares", "order"}
                        or fee["rounding"] not in ROUNDING):
                    raise ContractError("explicit fee category, side, basis and rounding required")
                _number(fee["rate"], "fee rate")
                minimum = _number(fee["minimum"], "minimum")
                maximum = _number(fee["maximum"], "maximum") if fee["maximum"] is not None else None
                if maximum is not None and maximum < minimum:
                    raise ContractError("maximum below minimum")
                quantum = _number(fee["quantum"], "fee quantum", Decimal("0.000000001"))
                if any((value / quantum) != (value / quantum).to_integral_value()
                       for value in [minimum, maximum] if value is not None):
                    raise ContractError("minimum/maximum must lie on the fee rounding grid")
            execution = rule["execution"]
            _keys(execution, {"friction_rate", "price_tick", "evidence"}, "execution model")
            rate = _number(execution["friction_rate"], "execution friction")
            if rate >= 1:
                raise ContractError("execution friction must be below one")
            _number(execution["price_tick"], "tick", Decimal("0.000000001"))
            _evidence(execution["evidence"])
            settlement = rule["settlement"]
            _keys(settlement, {"cash_sessions", "share_sessions", "reuse_unsettled_proceeds",
                               "resell_unsettled_shares", "evidence"}, "settlement")
            _integer(settlement["cash_sessions"], "cash settlement")
            _integer(settlement["share_sessions"], "share settlement")
            if (settlement["reuse_unsettled_proceeds"] is not False
                    or settlement["resell_unsettled_shares"] is not False):
                raise ContractError("v1 requires no unsettled reuse or resale")
            _evidence(settlement["evidence"])
            if any(value["basis"] != rule["evidence"]["basis"] for value in
                   [execution["evidence"], settlement["evidence"]]):
                raise ContractError("one dated rule cannot silently mix evidence clocks")
            if document["data_kind"] == "reviewed_model" and any(
                    value["basis"] == "invented_control" for value in
                    [rule["evidence"], execution["evidence"], settlement["evidence"]]):
                raise ContractError("reviewed model cannot contain invented rule provenance")
            if document["data_kind"] == "invented_control" and any(
                    value["basis"] != "invented_control" for value in
                    [execution["evidence"], settlement["evidence"]]):
                raise ContractError("control provenance must remain invented")
        self._document = copy.deepcopy(document)
        self.sha256 = canonical_hash(self._document)

    def rule(self, day, *, use_at):
        query = _day(day)
        current = stamp(use_at)
        matches = [r for r in self._document["rules"] if _day(r["from"]) <= query <= _day(r["through"])]
        if len(matches) != 1:
            raise ContractError("date not covered by exactly one explicit rule")
        rule = matches[0]
        if (rule["evidence"]["basis"] != "current_counterfactual"
                and stamp(rule["known_at"]) > current):
            raise ContractError("rule was unavailable at use time")
        return copy.deepcopy(rule)

    def order(self, *, day, use_at, side, shares, raw_open, scenario):
        if side not in {"BUY", "SELL"} or scenario not in SCENARIOS:
            raise ContractError("explicit side and frozen S02 scenario required")
        qty = _integer(shares, "order shares")
        if qty == 0:
            raise ContractError("zero shares is no order; minimum fee must not be charged")
        anchor = _number(raw_open, "raw opening price", Decimal("0.000000001"))
        rule = self.rule(day, use_at=use_at)
        friction = _number(rule["execution"]["friction_rate"], "friction")
        if scenario == "C1":
            friction *= 2
        elif scenario in {"C2", "C3"}:
            friction += Decimal("0.001")
        if friction >= 1:
            raise ContractError("scenario friction produces invalid selling price")
        tick = Decimal(rule["execution"]["price_tick"])
        unrounded = anchor * (1 + friction if side == "BUY" else 1 - friction)
        price = (unrounded / tick).to_integral_value(
            rounding=ROUND_CEILING if side == "BUY" else ROUND_FLOOR) * tick
        if price <= 0:
            raise ContractError("rounded execution price must be positive")
        notional = price * qty
        components = self._fee_components(rule, side, qty, notional, scenario)
        total = sum((Decimal(c["amount"]) for c in components), Decimal(0))
        return {"schema_version": VERSION, "terms_sha256": self.sha256, "rule_id": rule["id"],
                "model_frozen_at": self._document["frozen_at"],
                "scenario": scenario, "date": day, "side": side, "shares": qty,
                "raw_open": str(anchor), "execution_price": str(price), "notional": str(notional),
                "fees": components, "fee_total": str(total),
                "cash_delta": str(-notional-total if side == "BUY" else notional-total),
                "extra_delay_sessions": 1 if scenario == "C3" else 0,
                "extra_entry_exit_fx_rate": "0.0025" if scenario in {"C2", "C3"} else "0",
                "settlement": rule["settlement"], "evidence": rule["evidence"],
                "classification": "MODEL_ORDER_COST_ONLY"}

    @staticmethod
    def _fee_components(rule, side, qty, notional, scenario):
        components = []
        for fee in rule["fees"]:
            if side not in fee["sides"]:
                continue
            basis = {"notional": notional, "shares": Decimal(qty), "order": Decimal(1)}[fee["basis"]]
            amount = max(basis * Decimal(fee["rate"]), Decimal(fee["minimum"]))
            if fee["maximum"] is not None:
                amount = min(amount, Decimal(fee["maximum"]))
            quantum = Decimal(fee["quantum"])
            amount = (amount / quantum).to_integral_value(rounding=ROUNDING[fee["rounding"]]) * quantum
            if scenario == "C1" and fee["category"] == "broker":
                amount *= 2
            components.append({"id": fee["id"], "category": fee["category"], "amount": str(amount)})
        return components

    def fees_for(self, *, day, use_at, side, shares, execution_price, scenario):
        """Charge an already modeled fill price without applying friction again."""
        if side not in {"BUY", "SELL"} or scenario not in SCENARIOS:
            raise ContractError("explicit side and frozen scenario required")
        qty = _integer(shares, "fill shares")
        if not qty:
            raise ContractError("zero shares is no fill")
        price = _number(execution_price, "execution price", Decimal("0.000000001"))
        rule = self.rule(day, use_at=use_at)
        components = self._fee_components(rule, side, qty, qty * price, scenario)
        return {"fees": components, "fee_total": str(sum((Decimal(f["amount"]) for f in components), Decimal(0))),
                "rule_id": rule["id"], "terms_sha256": self.sha256}

    def settlement_date(self, *, trade_date, side, sessions, use_at):
        if (not isinstance(sessions, list) or not sessions or any(not isinstance(s, str) for s in sessions)
                or sessions != sorted(set(sessions))
                or side not in {"BUY", "SELL"} or trade_date not in sessions):
            raise ContractError("ordered explicit settlement calendar and side required")
        for item in sessions:
            _day(item)
        rule = self.rule(trade_date, use_at=use_at)
        lag = rule["settlement"]["share_sessions" if side == "BUY" else "cash_sessions"]
        offset = sessions.index(trade_date) + lag
        if offset >= len(sessions):
            raise ContractError("calendar does not cover the explicit settlement lag")
        return sessions[offset]


_CONTROL_ANSWERS = {
    "c0_sell": ["9.98", "4.00", "95.80", "1.00"],
    "c1_sell": ["9.96", "7.00", "92.60", "1.00"],
    "c2_sell": ["9.97", "4.00", "95.70", "0.0025", 0],
    "c3_sell": ["9.97", "4.00", "95.70", "0.0025", 1],
    "buy_anchor_rounding": ["10.03", "3"],
    "sell_anchor_rounding": "9.98",
    "settlement_before_change": "2024-05-29",
    "settlement_after_change": "2024-05-29",
    "unknown_rule_date": "REJECTED",
    "late_rule": "REJECTED",
    "counterfactual_clock": "2024-09-01T00:00:00Z",
    "after_freeze": "REJECTED",
    "zero_order": "REJECTED",
    "overlapping_dates": "REJECTED",
    "invented_as_reviewed": "REJECTED",
    "source_mutation": "0.001",
    "rounded_charge_doubled": ["0.01", "0.02"],
    "canonical_date_alias": "REJECTED",
    "off_grid_cap": "REJECTED",
}
EXPECTED_CONTROL_IDS = tuple(_CONTROL_ANSWERS)


def control_document():
    """A fictional dated fee schedule, never a broker quotation."""
    evidence = {"reference": "invented rule control", "sha256": "a" * 64, "basis": "invented_control"}
    def fee(name, category, basis, rate, minimum="0", sides=None):
        return {"id": name, "category": category, "sides": sides or ["BUY", "SELL"],
                "basis": basis, "rate": rate, "minimum": minimum, "maximum": None,
                "quantum": "0.01", "rounding": "half_up"}
    early = {"id": "before", "from": "2024-01-01", "through": "2024-05-27",
             "known_at": "2023-12-01T00:00:00Z", "evidence": evidence,
             "fees": [fee("commission", "broker", "notional", "0.001", "2"),
                      fee("platform", "broker", "order", "1"),
                      fee("sell_tax", "tax", "notional", "0.01", sides=["SELL"])],
             "execution": {"friction_rate": "0.002", "price_tick": "0.01", "evidence": evidence},
             "settlement": {"cash_sessions": 2, "share_sessions": 2,
                            "reuse_unsettled_proceeds": False, "resell_unsettled_shares": False,
                            "evidence": evidence}}
    later = copy.deepcopy(early)
    later.update(id="after", **{"from": "2024-05-28", "through": "2024-12-31"})
    later["settlement"].update(cash_sessions=1, share_sessions=1)
    return {"schema_version": VERSION, "data_kind": "invented_control", "currency": "USD",
            "frozen_at": "2024-10-01T00:00:00Z", "rules": [early, later]}


def control_report_accepted(report):
    if (report.get("schema_version") != VERSION or report.get("accepted") is not True
            or report.get("classification") != "SYNTHETIC_TRADING_TERMS_CONTROLS_ONLY"):
        return False
    rows = report.get("reports", [])
    if len(rows) != len(EXPECTED_CONTROL_IDS) or {r.get("id") for r in rows} != set(EXPECTED_CONTROL_IDS):
        return False
    try:
        return all(r.get("passed") is True
                   and canonical_hash(r["expected"]) == canonical_hash(_CONTROL_ANSWERS[r["id"]])
                   and canonical_hash(r["actual"]) == canonical_hash(_CONTROL_ANSWERS[r["id"]]) for r in rows)
    except (KeyError, TypeError, ValueError):
        return False


def run_controls(output_dir):
    from pathlib import Path
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    document = control_document()
    write_json(output / "invented_terms.json", document)
    rows = []
    def record(identifier, actual):
        expected = _CONTROL_ANSWERS[identifier]
        rows.append({"id": identifier, "expected": copy.deepcopy(expected), "actual": actual,
                     "passed": canonical_hash(actual) == canonical_hash(expected)})
    def cost(scenario, side="SELL", raw_open="10"):
        return TradingTerms(document).order(day="2024-05-24", use_at="2024-05-24T13:30:00Z",
                                            side=side, shares=10, raw_open=raw_open, scenario=scenario)
    for scenario in ("C0", "C1", "C2", "C3"):
        item = cost(scenario)
        actual = [item["execution_price"], item["fee_total"], item["cash_delta"]]
        actual += [item["fees"][-1]["amount"]] if scenario in {"C0", "C1"} else [
            item["extra_entry_exit_fx_rate"], item["extra_delay_sessions"]]
        record(scenario.lower() + "_sell", actual)
    item = cost("C0", "BUY", "10.003")
    record("buy_anchor_rounding", [item["execution_price"], item["fee_total"]])
    record("sell_anchor_rounding", cost("C0", raw_open="10.003")["execution_price"])
    sessions = ["2024-05-24", "2024-05-28", "2024-05-29", "2024-05-30"]
    for identifier, day in [("settlement_before_change", sessions[0]), ("settlement_after_change", sessions[1])]:
        record(identifier, TradingTerms(document).settlement_date(trade_date=day, side="SELL",
                                                                 sessions=sessions, use_at=day+"T13:30:00Z"))
    def reject(identifier, operation):
        try:
            operation()
            actual = "ACCEPTED"
        except ContractError:
            actual = "REJECTED"
        record(identifier, actual)
    reject("unknown_rule_date", lambda: TradingTerms(document).rule("2025-01-01", use_at="2025-01-01T00:00:00Z"))
    late = copy.deepcopy(document)
    late["rules"][0]["known_at"] = "2024-09-01T00:00:00Z"
    reject("late_rule", lambda: TradingTerms(late).rule("2024-05-24", use_at="2024-05-24T13:30:00Z"))
    current = copy.deepcopy(late)
    current["data_kind"] = "reviewed_model"
    for rule in current["rules"]:
        for evidence in [rule["evidence"], rule["execution"]["evidence"], rule["settlement"]["evidence"]]:
            evidence["basis"] = "current_counterfactual"
    record("counterfactual_clock", TradingTerms(current).rule("2024-05-24", use_at="2024-05-24T13:30:00Z")["known_at"])
    current["frozen_at"] = "2024-08-01T00:00:00Z"
    reject("after_freeze", lambda: TradingTerms(current))
    reject("zero_order", lambda: TradingTerms(document).order(day="2024-05-24", use_at="2024-05-24T13:30:00Z",
                                                             side="BUY", shares=0, raw_open="10", scenario="C0"))
    overlap = copy.deepcopy(document)
    overlap["rules"][1]["from"] = "2024-05-27"
    reject("overlapping_dates", lambda: TradingTerms(overlap))
    relabelled = copy.deepcopy(document)
    relabelled["data_kind"] = "reviewed_model"
    reject("invented_as_reviewed", lambda: TradingTerms(relabelled))
    model = TradingTerms(document)
    document["rules"][0]["fees"][0]["rate"] = "0.9"
    record("source_mutation", model.rule("2024-05-24", use_at="2024-05-24T13:30:00Z")["fees"][0]["rate"])
    fixed = control_document()
    fixed["rules"][0]["fees"] = [{"id": "fixed", "category": "broker", "sides": ["BUY", "SELL"],
                                  "basis": "order", "rate": "0.006", "minimum": "0", "maximum": None,
                                  "quantum": "0.01", "rounding": "half_up"}]
    args = {"day": "2024-05-24", "use_at": "2024-05-24T13:30:00Z", "side": "BUY", "shares": 1, "raw_open": "10"}
    record("rounded_charge_doubled", [TradingTerms(fixed).order(scenario=s, **args)["fee_total"] for s in ("C0", "C1")])
    reject("canonical_date_alias", lambda: model.settlement_date(trade_date="2024-05-28", side="BUY",
           sessions=["2024-05-28", "20240528", "20240529"], use_at="2024-05-28T13:30:00Z"))
    cap = control_document()
    cap["rules"][0]["fees"][0].update(maximum="3.11", quantum="0.05", rounding="ceiling")
    reject("off_grid_cap", lambda: TradingTerms(cap))
    report = {"schema_version": VERSION, "classification": "SYNTHETIC_TRADING_TERMS_CONTROLS_ONLY",
              "reports": rows, "accepted": True}
    report["accepted"] = control_report_accepted(report)
    write_json(output / "report.json", report)
    return report
