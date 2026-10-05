"""File-bound invented small study instrumentation; never market observations."""
import copy
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path

from research_base import historical_input as history
from research_base.cash_account import CashAccount
from research_base.evidence import canonical_hash, digest, load_json, write_json
from research_base.historical_contract import control_model
from research_base.historical_signals import ASSETS


def write_study_fixture(root):
    """Write a normalized invented Jan2020–Feb2021 source and eight v2 models.

    Only capital, entry principal and scenario differ across the model cohort.
    Returned manifest is accepted only as INVENTED_SMALL_CONTROL instrumentation.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    source = root / "source"
    source_manifest = history.write_control_fixture(source)
    calendar, bars, inventory = [], [], {}
    current = date(2020, 1, 1)
    november_session = 0
    while current <= date(2021, 2, 5):
        if current.weekday() < 5:
            day, month = current.isoformat(), current.isoformat()[:7]
            calendar.append({"trade_date": day, "open_at": day + "T14:30:00Z", "close_at": day + "T21:00:00Z"})
            if day < "2021-02-01":
                record = inventory.setdefault(month, {"expected_sessions": [], "month_end_session": None})
                record["expected_sessions"].append(day)
                record["month_end_session"] = day
            price = "100"
            if month == "2020-11":
                november_session += 1
                price = str(100 + november_session)
            elif day >= "2020-12-01":
                price = "130" if day >= "2021-01-01" else "125"
            for symbol in ASSETS:
                bars.append({"trade_date": day, "symbol": symbol, "source_id": "CONTROL",
                    "open": price, "high": price, "low": price, "close": price, "volume": "1000000",
                    "currency": "USD", "price_basis": "raw_unadjusted", "price_unit": "currency_per_share",
                    "volume_unit": "shares", "availability_basis": "modelled", "available_at": day + "T21:05:00Z",
                    "observed_received_at": "", "timing_evidence": "", "model_rule": "invented close plus five minutes",
                    "revision_status": "unknown"})
        current += timedelta(days=1)
    history._rewrite_control(source, "calendar.csv", calendar, history.CALENDAR)
    history._rewrite_control(source, "bars.csv", bars, history.BARS)
    history._rewrite_control(source, "actions.csv", [], history.ACTIONS)
    # Canonical JSON bytes bind an independently declared full-month inventory.
    inventory_path = source / "complete-months.json"
    inventory_path.write_text(json.dumps(inventory, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    original = load_json(source_manifest)
    original["retrieved_at"] = "2021-02-06T00:00:00Z"
    original["instruments"] = {symbol: copy.deepcopy(original["instruments"]["TEST"]) for symbol in ASSETS}
    original["files"]["complete-months.json"] = {"role": "evidence", "sha256": digest(inventory_path)}
    source_manifest.write_text(json.dumps(original, sort_keys=True, ensure_ascii=False, allow_nan=False))
    artifact = history.normalize(source_manifest, root, checked_at="2021-02-06T00:00:00Z")
    artifact["signal_calendar"] = {"schema_version": "signal-calendar/1", "inventory": inventory,
        "inventory_file": "complete-months.json", "inventory_sha256": digest(inventory_path),
        "review": {"reviewer": "invented control author", "scope": "complete invented weekdays; no actual exchange/session claim",
                   "reviewed_at": "2021-02-06T00:00:00Z", "source_nature": "invented_control"}}
    artifact_path = root / "artifact.json"
    write_json(artifact_path, artifact)
    base, _ = control_model(artifact, start="2020-11-02", end="2021-01-29")
    base["schema_version"] = "historical-account-model/2"
    base["cash_rules"]["knowledge_clock"]["freeze_at"] = "2021-02-06T00:00:00+00:00"
    for rounding in base["cash_rules"]["money_rounding"].values():
        rounding["quantum"] = "0.00000001"
    for schedule in base["cash_rules"]["interest_schedules"]:
        schedule["tiers"][0]["net_annual_rate"] = "0"
    base["fx"]["entry"]["execution_quote"].update(buy_usd_spread_bps="0", sell_usd_spread_bps="0", buy_fixed_fee_cny="0", sell_fixed_fee_usd="0")
    for index, quote in enumerate(base["fx"]["close_marks"].values()):
        quote["cny_per_usd"] = str(Decimal("7") + Decimal(index % 5 - 2) / 100)
    for rule in base["terms"]["rules"]:
        rule["execution"]["friction_rate"] = "0"
        for fee in rule["fees"]:
            fee.update(rate="0", minimum="0")
    for opening in base["execution"]["rows"]:
        opening["open_capacity_shares"] = 10000
    evidence = copy.deepcopy(base["cash_rules"]["funding_evidence"]["evidence"])
    wallet = CashAccount(base["cash_rules"])
    expenses = []
    for month in ("2020-11", "2020-12", "2021-01"):
        for currency, amount, suffix in (("CNY", "70", "data"), ("USD", "5", "server")):
            expenses.append({"schema_version": "fixed-expense/1", "id": month + "-" + suffix,
                "currency": currency, "amount": amount, "book_at": wallet.cutoff_at(month + "-10"),
                "category": "fixed", "evidence": copy.deepcopy(evidence)})
    base["fixed_expenses"] = expenses
    base["expense_review"] = {"schema_version": "fixed-expense-review/1", "reviewer": "invented control author",
        "scope": "fictional nonzero CNY data/USD server expense controls; not actual service bills",
        "reviewed_at": base["cash_rules"]["knowledge_clock"]["freeze_at"],
        "coverage_start_date": base["cash_rules"]["start_at"][:10], "coverage_end_date": "2021-01-29",
        "explicit_zero": False, "schedule_sha256": canonical_hash(expenses), "evidence": copy.deepcopy(evidence)}
    def descriptor(path):
        return {"file": str(path.relative_to(root)), "sha256": digest(path)}
    models = {}
    for capital in ("50000", "200000"):
        for scenario in ("C0", "C1", "C2", "C3"):
            model = copy.deepcopy(base)
            model["scenario"] = scenario
            model["cash_rules"]["initial_cny"] = capital
            model["fx"]["entry"]["principal_cny"] = str(int(capital) - 1000)
            path = root / (capital + "-" + scenario + ".json")
            write_json(path, model)
            models[capital + "-" + scenario] = descriptor(path)
    protocol = root / "protocol.txt"
    protocol.write_text("INVENTED_SMALL_CONTROL: ten complete invented warmup months, Nov2020 development, Dec2020 validation, Jan2021 confirmation.\nFictional controls, not original S02 historical evidence or market observations.\n")
    review = root / "invented-review.txt"
    review.write_text("Fixture authorship/completeness/model review declaration for invented controls only.\nNo external vendor authorization, historical PIT certification, real funding or observed fill claims.\n")
    manifest = root / "study.json"
    write_json(manifest, {"schema_version": "historical-study/1", "data_kind": "invented_control",
        "scope": {"name": "INVENTED_SMALL_CONTROL", "warmup": {"start": "2020-01-01", "end": "2020-10-31"},
            "periods": {"development": {"start": "2020-11-01", "end": "2020-11-30"},
                        "validation": {"start": "2020-12-01", "end": "2020-12-31"},
                        "confirmation": {"start": "2021-01-01", "end": "2021-01-31"}}},
        "artifact": descriptor(artifact_path), "protocol": descriptor(protocol), "models": models,
        "exit_forwards": {case: None for case in models},
        "reviews": {gate: {"status": "reviewed", "reason": "invented instrumentation declaration only; not actual market certification",
                            "evidence": [descriptor(review)]}
                    for gate in ("source_use", "units_calendar", "actions_receipt", "funding_terms", "execution_friction")},
        "revisited_history": True})
    return manifest
