"""Fixed S02 causal features for normalized inputs; no account or orders.

An external complete-session inventory is required. Its declaration and hashes
bind scope/content but do not independently certify an exchange calendar.
"""
import copy
import json
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from .contracts import decimal
from .evidence import ContractError, canonical_hash, digest, load_json, write_json
from . import historical_input as h
from .historical_input import VERSION as INPUT_VERSION, _day, _integer, _stamp

VERSION = "historical-signals/1"
ASSETS = ("SPY", "EFA", "IEF", "GLD")


def _month_number(month):
    try:
        day = date.fromisoformat(month + "-01")
        return day.year * 12 + day.month
    except (ValueError, TypeError) as exc:
        raise ContractError("canonical YYYY-MM month required") from exc


def _calendar(artifact):
    declaration = artifact.get("signal_calendar")
    if not isinstance(declaration, dict) or declaration.get("schema_version") != "signal-calendar/1":
        raise ContractError("external complete-month inventory declaration required")
    inventory = declaration.get("inventory")
    if not isinstance(inventory, dict) or not inventory:
        raise ContractError("complete-month inventory required")
    checksum = canonical_hash(inventory)
    entry = artifact.get("input_files", {}).get(declaration.get("inventory_file"), {})
    if (checksum != declaration.get("inventory_sha256") or entry.get("sha256") != checksum
            or entry.get("role") != "evidence"):
        raise ContractError("external inventory/content hash binding differs")
    review = declaration.get("review")
    if not isinstance(review, dict) or any(not isinstance(review.get(k), str) or not review[k].strip()
                                         for k in ("reviewer", "scope", "source_nature")):
        raise ContractError("calendar completeness review declaration required")
    if review["source_nature"] != artifact["data_kind"] or _stamp(review.get("reviewed_at")) > _stamp(artifact["checked_at"]):
        raise ContractError("calendar review provenance/time differs")
    for month, record in inventory.items():
        _month_number(month)
        if not isinstance(record, dict):
            raise ContractError("complete-month record must be an object")
        expected = record.get("expected_sessions")
        if not isinstance(expected, list) or not expected or expected != sorted(set(expected)):
            raise ContractError("unique ascending expected sessions required")
        if any(_day(day)[:7] != month for day in expected) or record.get("month_end_session") != expected[-1]:
            raise ContractError("month-end/full-month inventory differs")
    return declaration, inventory


def _weights(weights):
    return {s: str(Decimal(v.numerator) / Decimal(v.denominator)) for s, v in weights.items()}


def generate(artifact, variant, *, trade_start, end_date, k=None, delay_sessions=0):
    """Return auditable features and monthly targets, never a prepared engine input.

    Timing is tested at the originally scheduled open, including explicit C3
    delay. A late component causes a skip; the due date is never auto-shifted.
    """
    if (artifact.get("schema_version") != INPUT_VERSION
            or artifact.get("classification") != "NORMALIZED_CONVERSION_ONLY"
            or artifact.get("data_kind") not in {"historical_market", "invented_control"}):
        raise ContractError("explicit normalized historical/invented provenance required")
    instruments = artifact.get("instruments", {})
    if set(instruments) != set(ASSETS) or any(instruments[s].get("currency") != "USD" for s in ASSETS):
        raise ContractError("fixed four USD ETFs required")
    if variant not in {"S", "B0", "BR", "BC"}:
        raise ContractError("unknown frozen variant")
    if type(delay_sessions) is not int or delay_sessions not in (0, 1):
        raise ContractError("frozen execution delay must explicitly be 0 or 1 session")
    if variant == "BR":
        coefficient = Fraction(decimal(k, "BR k", 0))
        if coefficient > 1 or (coefficient * 100).denominator != 1:
            raise ContractError("BR k must be predeclared on the cent grid [0,1]")
    else:
        if k is not None:
            raise ContractError("only BR accepts k")
        coefficient = Fraction(0 if variant == "BC" else 1)
    _day(trade_start)
    _day(end_date)
    calendar = artifact.get("calendar")
    if not isinstance(calendar, list) or not calendar:
        raise ContractError("normalized calendar rows required")
    sessions = [row["trade_date"] for row in calendar]
    if sessions != sorted(set(sessions)) or trade_start not in sessions or end_date not in sessions or trade_start > end_date:
        raise ContractError("unique sessions and resolved trade_start/end_date required")
    declaration, inventory = _calendar(artifact)
    calendar_by_day = {row["trade_date"]: row for row in calendar}
    month_sessions = {}
    for day in sessions:
        _day(day)
        row = calendar_by_day[day]
        if _stamp(row["open_at"]) >= _stamp(row["close_at"]):
            raise ContractError("invalid session hours")
        month_sessions.setdefault(day[:7], []).append(day)
    bars = {}
    for bar in artifact.get("bars", []):
        key = bar["trade_date"], bar["symbol"]
        if key in bars or key[0] not in calendar_by_day or key[1] not in ASSETS:
            raise ContractError("missing/duplicate/out-of-calendar signal bar")
        if (bar.get("price_basis") != "raw_unadjusted" or bar.get("price_unit") != "currency_per_share"
                or bar.get("volume_unit") != "shares" or bar.get("currency") != "USD"):
            raise ContractError("signal requires explicit raw prices and share units")
        decimal(bar["close"], "close", "0.000000001")
        if _stamp(bar["available_at"]) < _stamp(calendar_by_day[key[0]]["close_at"]):
            raise ContractError("full close availability before close")
        bars[key] = bar
    if set(bars) != {(d, s) for d in sessions for s in ASSETS}:
        raise ContractError("signal bar/session coverage differs")
    actions_by_day, ids = {}, set()
    for action in artifact.get("actions", []):
        if action["id"] in ids or action["symbol"] not in ASSETS or action["effective_date"] not in calendar_by_day:
            raise ContractError("action identity/symbol/session differs")
        ids.add(action["id"])
        _stamp(action["available_at"])
        if action["type"] == "split":
            _integer(action["numerator"], "split numerator", 1)
            _integer(action["denominator"], "split denominator", 1)
        elif action["type"] == "dividend":
            if action.get("share_basis") != "post_split":
                raise ContractError("gross dividend must be per post-split share")
            decimal(action["gross_per_share"], "gross dividend", 0)
        else:
            raise ContractError("unsupported signal action")
        actions_by_day.setdefault(action["effective_date"], []).append(action)
    decisions, skipped, intents, unexecuted = [], [], [], []
    indices = {s: Fraction(100) for s in ASSETS}
    previous = {s: None for s in ASSETS}
    monthly = {s: [] for s in ASSETS}
    prior_month = None
    causal_rows = []
    chain_broken = False
    month_valid = {}
    for month, actual_sessions in month_sessions.items():
        if month not in inventory:
            month_valid[month] = False
        else:
            month_valid[month] = actual_sessions == inventory[month]["expected_sessions"]
    for position, day in enumerate(sessions):
        if day > end_date:
            break
        month = day[:7]
        if month != prior_month:
            if prior_month is not None and _month_number(month) != _month_number(prior_month) + 1:
                skipped.append({"date": day, "reason": "CALENDAR_MONTH_GAP", "previous_month": prior_month, "month": month})
                chain_broken = True
            if not month_valid[month]:
                skipped.append({"date": day, "reason": "INCOMPLETE_MONTH_CALENDAR", "month": month})
                # Trailing partial months never invalidate already emitted decisions.
                chain_broken = True
            prior_month = month
        ratios, dividends = {s: Fraction(1) for s in ASSETS}, {s: Fraction(0) for s in ASSETS}
        for action in actions_by_day.get(day, []):
            symbol = action["symbol"]
            if action["type"] == "split":
                ratios[symbol] *= Fraction(action["numerator"], action["denominator"])
            else:
                dividends[symbol] += Fraction(str(action["gross_per_share"]))
            causal_rows.append({"kind": "action", "id": action["id"], "symbol": symbol, "date": day,
                                "available_at": action["available_at"], "availability_basis": action["availability_basis"],
                                "revision_status": action["revision_status"], "record_hash": canonical_hash(action)})
        for symbol in ASSETS:
            bar = bars[day, symbol]
            close = Fraction(str(bar["close"]))
            if previous[symbol] is not None:
                indices[symbol] *= ratios[symbol] * (close + dividends[symbol]) / previous[symbol]
            previous[symbol] = close
            causal_rows.append({"kind": "bar", "symbol": symbol, "date": day,
                                "available_at": bar["available_at"], "availability_basis": bar["availability_basis"],
                                "revision_status": bar["revision_status"], "record_hash": canonical_hash(bar)})
        if not month_valid[month] or day != inventory[month]["month_end_session"]:
            continue
        for symbol in ASSETS:
            monthly[symbol].append((month, indices[symbol]))
        due_position = position + 1 + delay_sessions
        due = sessions[due_position] if due_position < len(sessions) else None
        base_record = {"date": day, "variant": variant, "execution_date": due, "delay_sessions": delay_sessions,
                       "data_kind": artifact["data_kind"]}
        if variant == "S" and chain_broken:
            skipped.append({**base_record, "reason": "BROKEN_COMPLETE_TOTAL_RETURN_CHAIN"})
            continue
        if variant == "S" and len(monthly[ASSETS[0]]) < 10:
            skipped.append({**base_record, "reason": "TEN_COMPLETE_MONTH_WARMUP", "complete_months": len(monthly[ASSETS[0]])})
            continue
        # Only S depends on price/action features. Static comparators have no SMA dependency.
        components = causal_rows if variant == "S" else []
        available_at = max((r["available_at"] for r in components), key=_stamp) if components else calendar_by_day[day]["close_at"]
        if due is not None:
            late = [r for r in components if _stamp(r["available_at"]) >= _stamp(calendar_by_day[due]["open_at"])]
            if late:
                skipped.append({**base_record, "reason": "COMPONENT_NOT_AVAILABLE_BEFORE_PLANNED_OPEN",
                                "planned_open_at": calendar_by_day[due]["open_at"], "late_components": copy.deepcopy(late)})
                continue
        averages = {s: sum(x for _, x in monthly[s][-10:]) / 10 for s in ASSETS} if variant == "S" else None
        weights = {s: Fraction(1, 4) if indices[s] > averages[s] else Fraction(0) for s in ASSETS} if variant == "S" else {s: coefficient / 4 for s in ASSETS}
        record = {**base_record, "available_at": available_at,
                  "index_fraction": {s: str(indices[s]) for s in ASSETS} if variant == "S" else None,
                  "sma_fraction": {s: str(averages[s]) for s in ASSETS} if variant == "S" else None,
                  "sma_months": [m for m, _ in monthly[ASSETS[0]][-10:]] if variant == "S" else [],
                  "weights": _weights(weights), "component_hash": canonical_hash(components),
                  "component_count": len(components),
                  "availability_modes": sorted({r["availability_basis"] for r in components}),
                  "revision_statuses": sorted({r["revision_status"] for r in components}),
                  "status": "NO_NEXT_SESSION" if due is None else ("OUTSIDE_EXECUTION_WINDOW" if not trade_start <= due <= end_date else "INTENT"),
                  "calendar_inventory_sha256": declaration["inventory_sha256"]}
        decisions.append(record)
        intent = {"decision_date": day, "execution_date": due, "available_at": available_at,
                  "weights": copy.deepcopy(record["weights"]), "variant": variant, "data_kind": artifact["data_kind"],
                  "decision_hash": canonical_hash(record), "status": record["status"]}
        if record["status"] == "INTENT":
            intents.append(intent)
        elif record["status"] == "NO_NEXT_SESSION" or (due and due > end_date):
            unexecuted.append(intent)
    return {"schema_version": VERSION, "classification": "CAUSAL_FEATURES_AND_TARGETS_ONLY",
            "data_kind": artifact["data_kind"], "variant": variant, "k": str(k) if k is not None else None,
            "trade_start": trade_start, "end_date": end_date, "delay_sessions": delay_sessions,
            "input_manifest_sha256": artifact.get("input_manifest_sha256"),
            "normalized_artifact_sha256": canonical_hash(artifact),
            "calendar_declaration": copy.deepcopy(declaration), "provenance_records": causal_rows, "decisions": decisions,
            "intents": intents, "skipped": skipped, "unexecuted_intents": unexecuted,
            "historical_engine_ready": False,
            "limitations": ["Calendar completeness is an evidence-bound reviewed declaration, not independent certification",
                            "Observed/modelled timing and unknown revisions retain input limitations",
                            "Gross signal dividends are not account net cash; no costs, FX, interest or fills computed"]}


def write_control_fixture(root):
    """Full weekday-only invented calendar, explicitly not an exchange calendar."""
    root = Path(root)
    manifest = h.write_control_fixture(root)
    calendar, bars, inventory = [], [], {}
    day = date(2020, 1, 1)
    while day <= date(2021, 1, 4):
        if day.weekday() < 5:
            value = day.isoformat()
            calendar.append({"trade_date": value, "open_at": value + "T14:30:00Z", "close_at": value + "T21:00:00Z"})
            if value[:4] == "2020":
                month = value[:7]
                inventory.setdefault(month, {"expected_sessions": [], "month_end_session": None})["expected_sessions"].append(value)
                inventory[month]["month_end_session"] = value
            for symbol in ASSETS:
                bars.append({"trade_date": value, "symbol": symbol, "source_id": "CONTROL",
                             "open": "100", "high": "100", "low": "100", "close": "100", "volume": "1000",
                             "currency": "USD", "price_basis": "raw_unadjusted", "price_unit": "currency_per_share",
                             "volume_unit": "shares", "availability_basis": "modelled", "available_at": value + "T21:05:00Z",
                             "observed_received_at": "", "timing_evidence": "", "model_rule": "invented close + 5 minutes",
                             "revision_status": "unknown"})
        day += timedelta(days=1)
    h._rewrite_control(root, "calendar.csv", calendar, h.CALENDAR)
    h._rewrite_control(root, "bars.csv", bars, h.BARS)
    h._rewrite_control(root, "actions.csv", [], h.ACTIONS)
    inventory_file = root / "complete-months.json"
    inventory_file.write_text(json.dumps(inventory, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    contract = load_json(manifest)
    contract["retrieved_at"] = "2021-02-01T00:00:00Z"
    contract["instruments"] = {symbol: copy.deepcopy(contract["instruments"]["TEST"]) for symbol in ASSETS}
    contract["files"]["complete-months.json"] = {"role": "evidence", "sha256": digest(inventory_file)}
    manifest.write_text(json.dumps(contract))
    artifact = h.normalize(manifest, root.parent, checked_at="2021-02-01T00:00:00Z")
    artifact["signal_calendar"] = {"schema_version": "signal-calendar/1", "inventory": inventory,
          "inventory_file": "complete-months.json", "inventory_sha256": digest(inventory_file),
          "review": {"reviewer": "control author", "scope": "invented weekday schedule, no actual exchange claims",
                     "reviewed_at": "2021-02-01T00:00:00Z", "source_nature": "invented_control"}}
    return artifact


# Literal independent known-answer inventory; producer flags cannot set the gate.
EXPECTED_CONTROLS = {
    "strict_equal": {"index": "100", "sma": "100", "weight": "0", "months": 10},
    "split_dividend": {"index": "100", "sma": "100", "weight": "0"},
    "positive_change": {"index": "200", "sma": "110", "weight": "0.25", "changed": True},
    "close_to_open": {"due": "2020-11-02", "available_at": "2020-11-02T14:29:59Z"},
    "late_old": {"due": "2020-11-02", "old_date": "2020-01-01", "no_backfill": True},
    "explicit_c3": {"due": "2020-11-03"},
    "future_perturbation": {"prefix_unchanged": True, "future_changed": True},
    "future_truncation": {"prefix_unchanged": True},
    "missing_session": {"intents": 0, "gap_reported": True},
    "missing_month": {"intents": 0, "gap_reported": True},
    "sparse_month_ends": {"intents": 0, "incomplete_months": 12},
    "terminal_no_next": {"prior_unchanged": True, "terminal_status": "NO_NEXT_SESSION"},
    "benchmarks": {"B0": "0.25", "BR": "0.0875", "BC": "0"},
    "cent_grid": {"bad_rejected": 5, "non_br_rejected": True},
    "declaration_mutations": {"rejected": 3},
    "late_action": {"due": "2020-11-02", "late_kind": "action", "no_backfill": True},
    "future_effective_action": {"first_index": "100", "first_weight": "0", "second_index": "101", "second_weight": "0.25"},
    "provenance": {"modes": ["modelled"], "revisions": ["unknown"], "components": 872},
    "historical_label": {"kind": "historical_market", "first_date": "2020-09-30", "clock": "2020-09-30T21:00:00Z"},
}
EXPECTED_CONTROL_IDS = (
    "strict_equal", "split_dividend", "positive_change", "close_to_open", "late_old", "explicit_c3",
    "future_perturbation", "future_truncation", "missing_session", "missing_month", "sparse_month_ends",
    "terminal_no_next", "benchmarks", "cent_grid", "declaration_mutations", "late_action",
    "future_effective_action", "provenance", "historical_label",
)


def control_report_accepted(report):
    if (report.get("schema_version") != VERSION
            or report.get("classification") != "INVENTED_CAUSAL_SIGNAL_CONTROLS_ONLY"):
        return False
    reports = report.get("reports")
    if not isinstance(reports, list) or any(not isinstance(r, dict) for r in reports):
        return False
    ids = [r.get("id") for r in reports]
    if (any(not isinstance(i, str) for i in ids) or len(ids) != len(EXPECTED_CONTROL_IDS)
            or set(ids) != set(EXPECTED_CONTROL_IDS) or set(EXPECTED_CONTROLS) != set(EXPECTED_CONTROL_IDS)):
        return False
    return all(r.get("expected") == EXPECTED_CONTROLS[r["id"]]
               and r.get("actual") == EXPECTED_CONTROLS[r["id"]] and r.get("passed") is True for r in reports)


def run_controls(output_dir):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    artifact = write_control_fixture(output / "fixture")
    write_json(output / "normalized-control.json", artifact)
    def run(value=None, variant="S", **kwargs):
        return generate(value or artifact, variant, trade_start="2020-10-01", end_date="2020-12-31", **kwargs)
    baseline = run()
    write_json(output / "baseline-features.json", baseline)
    first = baseline["decisions"][0]
    reports = []
    def record(name, actual):
        expected = copy.deepcopy(EXPECTED_CONTROLS[name])
        reports.append({"id": name, "expected": expected, "actual": actual, "passed": actual == expected})
    def summary(row):
        return {"index": row["index_fraction"]["SPY"], "sma": row["sma_fraction"]["SPY"], "weight": row["weights"]["SPY"]}
    def mutate_prices(value, predicate, price):
        for bar in value["bars"]:
            if predicate(bar): bar.update(open=price, high=price, low=price, close=price)
    def without_days(value, predicate):
        value["calendar"] = [r for r in value["calendar"] if not predicate(r["trade_date"])]
        value["bars"] = [r for r in value["bars"] if not predicate(r["trade_date"])]
        return value
    record("strict_equal", {**summary(first), "months": len(first["sma_months"])})
    changed = copy.deepcopy(artifact)
    mutate_prices(changed, lambda b: b["symbol"] == "SPY" and b["trade_date"] >= "2020-10-01", "49")
    common = {"symbol": "SPY", "effective_date": "2020-10-01", "available_at": "2020-10-01T21:05:00Z",
              "availability_basis": "modelled", "revision_status": "unknown"}
    changed["actions"] = [{**common, "id": "split", "type": "split", "numerator": 2, "denominator": 1},
                          {**common, "id": "dividend", "type": "dividend", "gross_per_share": "1", "share_basis": "post_split"}]
    write_json(output / "split-dividend-input.json", changed)
    combo = run(changed)
    record("split_dividend", summary(combo["decisions"][0]))
    write_json(output / "split-dividend-features.json", combo)
    changed = copy.deepcopy(artifact)
    mutate_prices(changed, lambda b: b["symbol"] == "SPY" and b["trade_date"] == "2020-10-30", "200")
    write_json(output / "positive-control-input.json", changed)
    positive = run(changed)
    record("positive_change", {**summary(positive["decisions"][0]), "changed": first != positive["decisions"][0]})
    write_json(output / "positive-control-features.json", positive)
    changed = copy.deepcopy(artifact)
    for bar in changed["bars"]:
        if bar["trade_date"] == "2020-10-30": bar["available_at"] = "2020-11-02T14:29:59Z"
    row = run(changed)["decisions"][0]
    record("close_to_open", {"due": row["execution_date"], "available_at": row["available_at"]})
    changed = copy.deepcopy(artifact)
    changed["bars"][0]["available_at"] = "2020-11-02T14:30:00Z"
    write_json(output / "late-component-input.json", changed)
    late = run(changed)
    skip = next(r for r in late["skipped"] if r["reason"] == "COMPONENT_NOT_AVAILABLE_BEFORE_PLANNED_OPEN")
    record("late_old", {"due": skip["execution_date"], "old_date": skip["late_components"][0]["date"],
                        "no_backfill": not any(i["decision_date"] == "2020-10-30" for i in late["intents"])})
    record("explicit_c3", {"due": run(changed, delay_sessions=1)["decisions"][0]["execution_date"]})
    write_json(output / "late-component-features.json", late)
    changed = copy.deepcopy(artifact)
    mutate_prices(changed, lambda b: b["trade_date"] > "2020-11-02", "999")
    future = run(changed)
    record("future_perturbation", {"prefix_unchanged": first == future["decisions"][0],
                                   "future_changed": baseline["decisions"][1] != future["decisions"][1]})
    changed = without_days(copy.deepcopy(artifact), lambda d: d > "2020-11-02")
    truncated = generate(changed, "S", trade_start="2020-10-01", end_date="2020-11-02")
    record("future_truncation", {"prefix_unchanged": first == truncated["decisions"][0]})
    changed = without_days(copy.deepcopy(artifact), lambda d: d == "2020-05-06")
    missing = run(changed)
    record("missing_session", {"intents": len(missing["intents"]), "gap_reported": any(r["reason"] == "INCOMPLETE_MONTH_CALENDAR" and r["month"] == "2020-05" for r in missing["skipped"])})
    changed = without_days(copy.deepcopy(artifact), lambda d: d[:7] == "2020-05")
    missing = run(changed)
    record("missing_month", {"intents": len(missing["intents"]), "gap_reported": any(r["reason"] == "CALENDAR_MONTH_GAP" for r in missing["skipped"])})
    ends = {r["month_end_session"] for r in artifact["signal_calendar"]["inventory"].values()} | {"2021-01-04"}
    changed = without_days(copy.deepcopy(artifact), lambda d: d not in ends)
    sparse = generate(changed, "S", trade_start="2020-10-30", end_date="2020-12-31")
    record("sparse_month_ends", {"intents": len(sparse["intents"]), "incomplete_months": sum(r["reason"] == "INCOMPLETE_MONTH_CALENDAR" for r in sparse["skipped"])})
    terminal = run(without_days(copy.deepcopy(artifact), lambda d: d > "2020-12-31"))
    record("terminal_no_next", {"prior_unchanged": baseline["decisions"][:2] == terminal["decisions"][:2],
                                "terminal_status": terminal["unexecuted_intents"][-1]["status"]})
    record("benchmarks", {v: run(variant=v, **({"k": "0.35"} if v == "BR" else {}))["intents"][0]["weights"]["SPY"] for v in ("B0", "BR", "BC")})
    rejected = 0
    for k in ("0.351", "1.01", "-0.01", True, None):
        try: run(variant="BR", k=k)
        except ContractError: rejected += 1
    try: run(k="0.2")
    except ContractError: non_br_rejected = True
    else: non_br_rejected = False
    record("cent_grid", {"bad_rejected": rejected, "non_br_rejected": non_br_rejected})
    rejected = 0
    for mode in ("absent", "inventory", "provenance"):
        changed = copy.deepcopy(artifact)
        if mode == "absent": changed.pop("signal_calendar")
        elif mode == "inventory": changed["signal_calendar"]["inventory"]["2020-01"]["expected_sessions"].pop()
        else: changed["signal_calendar"]["review"]["source_nature"] = "historical_market"
        try: run(changed)
        except ContractError: rejected += 1
    record("declaration_mutations", {"rejected": rejected})
    changed = copy.deepcopy(artifact)
    action = {"id": "late-dividend", "symbol": "SPY", "effective_date": "2020-01-02", "type": "dividend",
              "gross_per_share": "1", "share_basis": "post_split", "available_at": "2020-11-02T14:30:00Z",
              "availability_basis": "observed", "revision_status": "as_of_archive"}
    changed["actions"] = [action]
    late = run(changed)
    skip = next(r for r in late["skipped"] if r["reason"] == "COMPONENT_NOT_AVAILABLE_BEFORE_PLANNED_OPEN")
    record("late_action", {"due": skip["execution_date"], "late_kind": skip["late_components"][0]["kind"],
                           "no_backfill": not any(i["decision_date"] == "2020-10-30" for i in late["intents"])})
    changed = copy.deepcopy(artifact)
    action.update(id="future-dividend", effective_date="2020-11-02", available_at="2020-01-01T21:05:00Z")
    changed["actions"] = [action]
    future = run(changed)
    record("future_effective_action", {"first_index": future["decisions"][0]["index_fraction"]["SPY"],
           "first_weight": future["decisions"][0]["weights"]["SPY"], "second_index": future["decisions"][1]["index_fraction"]["SPY"],
           "second_weight": future["decisions"][1]["weights"]["SPY"]})
    record("provenance", {"modes": first["availability_modes"], "revisions": first["revision_statuses"], "components": first["component_count"]})
    # Exercise preservation using only relabelled invented bytes as a type-control,
    # not as purported historical evidence; do not save this candidate as market data.
    changed = copy.deepcopy(artifact)
    changed["data_kind"] = "historical_market"
    changed["signal_calendar"]["review"]["source_nature"] = "historical_market"
    changed["bars"][0]["available_at"] = "2021-02-01T00:00:00Z"
    label = run(changed, variant="B0")
    record("historical_label", {"kind": label["data_kind"], "first_date": label["intents"][0]["decision_date"], "clock": label["intents"][0]["available_at"]})
    report = {"schema_version": VERSION, "classification": "INVENTED_CAUSAL_SIGNAL_CONTROLS_ONLY",
              "data_kind": "invented_control", "reports": reports, "accepted": False, "historical_engine_ready": False,
              "limitations": ["Invented weekday calendar, not actual exchange sessions or historical returns",
                              "Type-label negative/positive controls contain invented bytes and are not market-data evidence"]}
    report["accepted"] = control_report_accepted(report)
    write_json(output / "report.json", report)
    write_json(output / "manifest.json", {"schema_version": VERSION, "files": {
        str(path.relative_to(output)): digest(path) for path in sorted(output.rglob("*")) if path.is_file()}})
    return report
