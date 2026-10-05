"""Finite invented integrated-account controls through actual installed Vibe.

No injected engine or downloaded data. Independent Fraction replay audits each
actual stock/wallet tape, while literal answers freeze the control denominator.
"""
import copy
from decimal import Decimal
from pathlib import Path

from .evidence import canonical_hash, digest, write_json
from .historical_signals import ASSETS, write_control_fixture, generate
from .historical_contract import control_model, prepare
from .historical_account import execute, performance_input
from .historical_replay import independent_replay
from .performance import report as performance_report

VERSION = "integrated-account-controls/1"
CLASSIFICATION = "INVENTED_INTEGRATED_ACCOUNT_CONTROLS_ONLY"
EXPECTED_CONTROL_IDS = (
    "full_cny_target", "settled_cash_constraint", "baseline_final_cny", "baseline_replay",
    "fx_open_sizes", "fx_close_nav", "fx_replay", "c3_execution_rule", "c3_replay",
    "dividend_receivable", "weekend_payment", "monday_dividend_cash", "dividend_replay",
    "bc_no_fills", "bc_metrics", "bc_replay", "usd_credit_known_answer", "credit_replay",
    "weekday_payment", "weekday_cutoff_state", "weekday_replay", "monthend_credit", "monthend_replay",
    "unknown_open_targets", "unknown_open_no_fills", "unknown_100_replay", "unknown_200_replay",
    "native_fill_reconciliation", "offline_execution", "fee_tamper", "fx_nav_tamper", "duplicate_credit_tamper",
)
EXPECTED_ANSWERS = {
    "full_cny_target": {"opening_equity_usd_equivalent": "2000", "deltas": {"SPY": 5, "EFA": 5, "IEF": 5, "GLD": 5}},
    "settled_cash_constraint": [["SPY", 5], ["EFA", 5]],
    "baseline_final_cny": "14000",
    "baseline_replay": True,
    "fx_open_sizes": {"SPY": 5, "EFA": 5, "IEF": 5, "GLD": 5},
    "fx_close_nav": "15000",
    "fx_replay": True,
    "c3_execution_rule": {"date": "2020-10-02", "rule_id": "new", "raw_open": "200", "price": "200.2", "fee": "2", "settlement_date": "2020-10-05"},
    "c3_replay": True,
    "dividend_receivable": {"cash": "0", "receivable": "4"},
    "weekend_payment": {"count": 1, "effective_date": "2020-10-17", "payment_at": "2020-10-17T16:30:00-04:00", "net": "4"},
    "monday_dividend_cash": {"cash": "4", "receivable": "0", "cny_nav": "13993"},
    "dividend_replay": True,
    "bc_no_fills": {"stock_fills": 0, "native_fills": 0},
    "bc_metrics": {"currency": "CNY", "opening": "14000", "final": "14000", "period_net_return": 0.0},
    "bc_replay": True,
    "usd_credit_known_answer": {"credit_amounts": ["1", "1"], "first_cny": "14007", "final_cny": "14014"},
    "credit_replay": True,
    "weekday_payment": {"count": 1, "effective_date": "2020-10-19", "payment_at": "2020-10-19T16:30:00-04:00"},
    "weekday_cutoff_state": {"stock_cash": "4", "stock_receivable": "0", "wallet_receivable": "0", "nav": "13993"},
    "weekday_replay": True,
    "monthend_credit": {"count": 1, "credit": "31", "economic_day": "2020-10-31", "stock_posting_date": "2020-11-02", "final_cash": "1031", "final_unpaid": "3.09", "final_nav": "14238.63"},
    "monthend_replay": True,
    "unknown_open_targets": {"same_attempts": True, "deltas": {"SPY": 5, "EFA": 5, "IEF": 5, "GLD": 5}},
    "unknown_open_no_fills": {"raw100": 0, "raw200": 0},
    "unknown_100_replay": True,
    "unknown_200_replay": True,
    "native_fill_reconciliation": {"baseline": True, "fx_move": True, "c3": True, "dividend": True, "bc": True, "interest": True, "weekday": True, "monthend": True, "unknown_100": True, "unknown_200": True},
    "offline_execution": {"cases": 10, "network_attempts": 0, "data_kinds": ["invented_control"], "ready": False},
    "fee_tamper": {"rejected": True, "has_errors": True},
    "fx_nav_tamper": {"rejected": True, "has_errors": True},
    "duplicate_credit_tamper": {"rejected": True, "has_errors": True},
}
CASE_IDS = ("baseline", "fx_move", "c3", "dividend", "bc", "interest", "weekday", "monthend", "unknown_100", "unknown_200")


def _money(value):
    return format(Decimal(str(value)).normalize(), "f")


def control_report_accepted(report):
    if (not isinstance(report, dict) or report.get("schema_version") != VERSION
            or report.get("classification") != CLASSIFICATION or report.get("data_kind") != "invented_control"
            or report.get("historical_engine_ready") is not False or report.get("goal_complete") is not False
            or type(report.get("formal_history_runs")) is not int or report["formal_history_runs"] != 0):
        return False
    rows = report.get("reports")
    cases = report.get("actual_execution_cases")
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or not isinstance(cases, list) or cases != list(CASE_IDS)):
        return False
    ids = [row.get("id") for row in rows]
    if (any(not isinstance(identifier, str) for identifier in ids)
            or len(ids) != len(EXPECTED_CONTROL_IDS) or set(ids) != set(EXPECTED_CONTROL_IDS)
            or set(EXPECTED_ANSWERS) != set(EXPECTED_CONTROL_IDS)):
        return False
    try:
        return all(row.get("passed") is True
                   and canonical_hash(row["expected"]) == canonical_hash(EXPECTED_ANSWERS[row["id"]])
                   and canonical_hash(row["actual"]) == canonical_hash(EXPECTED_ANSWERS[row["id"]]) for row in rows)
    except (KeyError, TypeError, ValueError):
        return False


def _baseline(artifact):
    artifact = copy.deepcopy(artifact)
    for bar in artifact["bars"]:
        bar["volume"] = "1000000"
    model, _ = control_model(artifact)
    model["cash_rules"]["initial_cny"] = "14000"
    for schedule in model["cash_rules"]["interest_schedules"]:
        schedule["tiers"][0]["net_annual_rate"] = "0"
    model["fx"]["entry"]["principal_cny"] = "7000"
    model["fx"]["entry"]["execution_quote"].update(
        buy_usd_spread_bps="0", sell_usd_spread_bps="0", buy_fixed_fee_cny="0", sell_fixed_fee_usd="0")
    for rule in model["terms"]["rules"]:
        rule["execution"]["friction_rate"] = "0"
        for fee in rule["fees"]:
            fee.update(rate="0", minimum="0")
    for opening in model["execution"]["rows"]:
        opening["open_capacity_shares"] = 10000
    return artifact, model


def native_fill_reconciliation(result):
    """Compare native fills with stock FILL rows; empty tapes agree vacuously.

    This checks recorded amounts and directions, not independent native replay.
    Callers must separately require installed engine source fingerprints.
    """
    fills = [event for event in result["events"] if event["type"] == "FILL"]
    native = result["native_fill_records"]
    if len(fills) != len(native):
        return False
    # Native field mapping is inspected directly below, with no generated tape.
    for actual, source in zip(fills, native):
        quantity = source["signed_quantity"]
        if (source["symbol"] != actual["symbol"] or abs(quantity) != actual["qty"]
                or source["timestamp"].split(" ")[0] != actual["date"]
                or (quantity > 0) != (actual["side"] == "BUY")
                or _money(source["execution_price"]) != _money(actual["price"])
                or _money(source["fee"]) != _money(actual["fee"])):
            return False
    return True


def run_controls(output_dir):
    """Execute ten finite invented models; retain every tape and mutant."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    original = write_control_fixture(output / "invented-source-fixture")
    artifact, base_model = _baseline(original)
    rows, cases, saved = [], [], {}
    def record(identifier, actual):
        expected = copy.deepcopy(EXPECTED_ANSWERS[identifier])
        passed = canonical_hash(actual) == canonical_hash(expected)
        rows.append({"id": identifier, "expected": expected, "actual": actual, "passed": passed})
    def run_case(name, value, model, variant="B0"):
        directory = output / name
        directory.mkdir(exist_ok=False)
        write_json(directory / "artifact.json", value)
        write_json(directory / "model.json", model)
        features = generate(value, variant, trade_start=model["start"], end_date=model["end"],
                            delay_sessions=1 if model["scenario"] == "C3" else 0)
        write_json(directory / "features.json", features)
        data = prepare(value, features, model)
        write_json(directory / "account-input.json", data)
        result = execute(data)  # installed GlobalEquityEngine, never an injected stub
        write_json(directory / "native-result.json", result)
        replay = independent_replay(data, result)
        write_json(directory / "fraction-replay.json", replay)
        metric_input = performance_input(data, result)
        write_json(directory / "performance-input.json", metric_input)
        metrics = performance_report(metric_input)
        write_json(directory / "metrics.json", metrics)
        cases.append(name)
        saved[name] = (data, result, replay, metric_input, metrics)
        return result, replay, metric_input, metrics
    baseline, replay, _, _ = run_case("baseline", artifact, base_model)
    plan = next(e for e in baseline["events"] if e["type"] == "TARGET_PLAN")
    record("full_cny_target", {"opening_equity_usd_equivalent": _money(plan["opening_equity"]), "deltas": plan["deltas"]})
    record("settled_cash_constraint", [[e["symbol"], e["qty"]] for e in baseline["events"] if e["type"] == "FILL" and e["date"] == "2020-10-01"])
    record("baseline_final_cny", _money(baseline["cny_snapshots"][-1]["marked_equity_cny"]))
    record("baseline_replay", replay["passed"])
    fx_model = copy.deepcopy(base_model)
    for quote in fx_model["fx"]["close_marks"].values(): quote["cny_per_usd"] = "8"
    result, replay, _, _ = run_case("fx_move", artifact, fx_model)
    record("fx_open_sizes", next(e for e in result["events"] if e["type"] == "TARGET_PLAN")["deltas"])
    record("fx_close_nav", _money(result["cny_snapshots"][0]["marked_equity_cny"]))
    record("fx_replay", replay["passed"])
    c3_artifact, c3_model = copy.deepcopy(artifact), copy.deepcopy(base_model)
    c3_model["scenario"] = "C3"
    first = c3_model["terms"]["rules"][0]
    first["through"] = "2020-10-01"
    later = copy.deepcopy(first)
    later.update(id="new", **{"from": "2020-10-02", "through": "2021-01-31"})
    later["fees"][0]["minimum"] = "2"
    later["settlement"].update(cash_sessions=1, share_sessions=1)
    c3_model["terms"]["rules"].append(later)
    for bar in c3_artifact["bars"]:
        if bar["trade_date"] >= "2020-10-02": bar.update(open="200", high="200", low="200", close="200")
    result, replay, _, _ = run_case("c3", c3_artifact, c3_model)
    fill = next(e for e in result["events"] if e["type"] == "FILL")
    record("c3_execution_rule", {**{k: fill[k] for k in ("date", "rule_id", "settlement_date")},
                                  **{k: _money(fill[k]) for k in ("raw_open", "price", "fee")}})
    record("c3_replay", replay["passed"])
    dividend_artifact, dividend_model = copy.deepcopy(artifact), copy.deepcopy(base_model)
    dividend_artifact["actions"] = [{"id": "bankday", "type": "dividend", "symbol": "SPY", "currency": "USD", "source_id": "CONTROL",
            "effective_date": "2020-10-16", "pay_date": "2020-10-17", "gross_per_share": "1", "share_basis": "post_split",
            "available_at": "2020-10-16T00:00:00Z", "availability_basis": "modelled", "revision_status": "unknown"}]
    dividend_model["withholding"] = {"bankday": "0.2"}
    for bar in dividend_artifact["bars"]:
        if bar["symbol"] == "SPY" and bar["trade_date"] >= "2020-10-16": bar.update(open="99", high="99", low="99", close="99")
    result, replay, _, _ = run_case("dividend", dividend_artifact, dividend_model)
    ex = next(s for s in result["snapshots"] if s["date"] == "2020-10-16")
    record("dividend_receivable", {"cash": _money(ex["settled_cash"]), "receivable": _money(ex["dividend_receivable"])})
    paid = [e for e in result["events"] if e["type"] == "DIV_PAY"]
    record("weekend_payment", {"count": len(paid), "effective_date": paid[0]["effective_pay_date"],
                               "payment_at": paid[0]["payment_at"], "net": _money(paid[0]["amount"])})
    monday = next(s for s in result["snapshots"] if s["date"] == "2020-10-19")
    monday_cny = next(s for s in result["cny_snapshots"] if s["date"] == "2020-10-19")
    record("monday_dividend_cash", {"cash": _money(monday["settled_cash"]), "receivable": _money(monday["dividend_receivable"]),
                                    "cny_nav": _money(monday_cny["marked_equity_cny"])})
    record("dividend_replay", replay["passed"])
    result, replay, metric_input, metrics = run_case("bc", artifact, base_model, "BC")
    record("bc_no_fills", {"stock_fills": sum(e["type"] == "FILL" for e in result["events"]), "native_fills": len(result["native_fill_records"])})
    record("bc_metrics", {"currency": metric_input["currency"], "opening": _money(metric_input["observations"][0]["equity"]),
                          "final": _money(metrics["final_equity"]), "period_net_return": metrics["period_net_return"]["value"]})
    record("bc_replay", replay["passed"])
    interest_model = copy.deepcopy(base_model)
    interest_model["end"] = "2020-10-02"
    run_days = {"2020-10-01", "2020-10-02"}
    interest_model["execution"]["rows"] = [r for r in interest_model["execution"]["rows"] if r["date"] in run_days]
    for key in ("close_marks", "exit_quotes"):
        interest_model["fx"][key] = {d: q for d, q in interest_model["fx"][key].items() if d in run_days}
    for schedule in interest_model["cash_rules"]["interest_schedules"]:
        if schedule["currency"] == "USD": schedule["tiers"][0]["net_annual_rate"] = "0.365"
    result, replay, _, _ = run_case("interest", artifact, interest_model, "BC")
    credits = [e for e in result["events"] if e["type"] == "CASH_INTEREST_CREDIT"]
    record("usd_credit_known_answer", {"credit_amounts": [_money(e["amount"]) for e in credits],
             "first_cny": _money(result["cny_snapshots"][0]["marked_equity_cny"]), "final_cny": _money(result["cny_snapshots"][-1]["marked_equity_cny"])})
    record("credit_replay", replay["passed"])
    weekday_artifact = copy.deepcopy(dividend_artifact)
    weekday_artifact["actions"][0]["pay_date"] = "2020-10-19"
    result, replay, _, _ = run_case("weekday", weekday_artifact, dividend_model)
    paid = [e for e in result["events"] if e["type"] == "DIV_PAY"]
    record("weekday_payment", {"count": len(paid), "effective_date": paid[0]["effective_pay_date"], "payment_at": paid[0]["payment_at"]})
    stock = next(s for s in result["snapshots"] if s["date"] == "2020-10-19")
    cny = next(s for s in result["cny_snapshots"] if s["date"] == "2020-10-19")
    record("weekday_cutoff_state", {"stock_cash": _money(stock["settled_cash"]), "stock_receivable": _money(stock["dividend_receivable"]),
                                   "wallet_receivable": _money(cny["wallets"]["USD"]["receivables"]), "nav": _money(cny["marked_equity_cny"])})
    record("weekday_replay", replay["passed"])
    monthend_model = copy.deepcopy(base_model)
    for schedule in monthend_model["cash_rules"]["interest_schedules"]:
        schedule["crediting"] = "calendar_month_end"
        if schedule["currency"] == "USD": schedule["tiers"][0]["net_annual_rate"] = "0.365"
    result, replay, _, _ = run_case("monthend", artifact, monthend_model, "BC")
    credits = [e for e in result["events"] if e["type"] == "CASH_INTEREST_CREDIT"]
    record("monthend_credit", {"count": len(credits), "credit": _money(credits[0]["amount"]), "economic_day": credits[0]["source_wallet_day"],
                               "stock_posting_date": credits[0]["date"], "final_cash": _money(result["snapshots"][-1]["settled_cash"]),
                               "final_unpaid": _money(result["snapshots"][-1]["unpaid_usd_interest"]), "final_nav": _money(result["cny_snapshots"][-1]["marked_equity_cny"])})
    record("monthend_replay", replay["passed"])
    unknown_results = []
    unknown_attempts = []
    for raw_price in ("100", "200"):
        candidate, candidate_model = copy.deepcopy(artifact), copy.deepcopy(base_model)
        for bar in candidate["bars"]:
            if bar["trade_date"] == "2020-10-01": bar.update(open=raw_price, high=raw_price, low="100", close="100")
        for opening in candidate_model["execution"]["rows"]:
            if opening["date"] == "2020-10-01": opening.update(open_status="unknown", open_quote_known_at="2020-10-01T15:00:00Z")
        result, replay, _, _ = run_case("unknown_" + raw_price, candidate, candidate_model)
        unknown_results.append(result)
        unknown_attempts.append([(e["symbol"], e["requested_qty"]) for e in result["events"] if e["type"] == "ORDER_ATTEMPT" and e["date"] == "2020-10-01"])
        record("unknown_" + raw_price + "_replay", replay["passed"])
    target = next(e for e in unknown_results[1]["events"] if e["type"] == "TARGET_PLAN")
    record("unknown_open_targets", {"same_attempts": unknown_attempts[0] == unknown_attempts[1], "deltas": target["deltas"]})
    record("unknown_open_no_fills", {"raw" + price: sum(e["type"] == "FILL" and e["date"] == "2020-10-01" for e in result["events"]) for price, result in zip(("100", "200"), unknown_results)})
    record("native_fill_reconciliation", {name: native_fill_reconciliation(saved[name][1]) for name in CASE_IDS})
    record("offline_execution", {"cases": len(cases), "network_attempts": sum(saved[name][1]["network_attempts"] for name in CASE_IDS),
                                 "data_kinds": sorted({saved[name][1]["data_kind"] for name in CASE_IDS}),
                                 "ready": any(saved[name][1]["historical_engine_ready"] for name in CASE_IDS)})
    def reject_tape(identifier, source_case, mutate):
        data, original, _, _, _ = saved[source_case]
        changed = copy.deepcopy(original)
        mutate(changed)
        tested = independent_replay(data, changed)
        folder = output / identifier
        folder.mkdir(exist_ok=False)
        write_json(folder / "mutated-result.json", changed)
        write_json(folder / "fraction-replay.json", tested)
        write_json(folder / "source.json", {"case": source_case, "input_sha256": canonical_hash(data), "actual_result_sha256": canonical_hash(original)})
        record(identifier, {"rejected": tested["passed"] is False, "has_errors": bool(tested["errors"])})
    def wrong_fee(tape):
        next(e for e in tape["events"] if e["type"] == "FILL")["fee"] = "999"
    reject_tape("fee_tamper", "baseline", wrong_fee)
    reject_tape("fx_nav_tamper", "fx_move", lambda tape: tape["cny_snapshots"][0].update(marked_equity_cny="15001"))
    def duplicate_credit(tape):
        source = next(e for e in tape["events"] if e["type"] == "CASH_INTEREST_CREDIT")
        duplicate = copy.deepcopy(source)
        duplicate["sequence"] = len(tape["events"]) + 1
        duplicate["date"] = tape["events"][-1]["date"]
        tape["events"].append(duplicate)
    reject_tape("duplicate_credit_tamper", "interest", duplicate_credit)
    report = {"schema_version": VERSION, "classification": CLASSIFICATION, "data_kind": "invented_control",
        "historical_engine_ready": False, "goal_complete": False, "formal_history_runs": 0,
        "actual_execution_cases": cases, "reports": rows, "accepted": False,
        "source_sha256": digest(Path(__file__)),
        "limitations": ["Ten invented cases through installed Vibe are not a historical S02 matrix or broker simulation",
                        "Source fixture mutations retain origin file references; each actual case artifact hash binds the controlling invented bytes",
                        "Calendar, fees, interest, funding and opening fills remain explicit fictional controls"]}
    report["accepted"] = control_report_accepted(report)
    write_json(output / "report.json", report)
    write_json(output / "manifest.json", {"schema_version": VERSION, "classification": CLASSIFICATION,
        "files": {str(path.relative_to(output)): digest(path) for path in sorted(output.rglob("*")) if path.is_file()}})
    return report
