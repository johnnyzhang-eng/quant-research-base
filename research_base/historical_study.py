"""Retained S02 account matrix and development-only risk calibration.

Market inputs use the unchanged original scope. A smaller scope is available
only for explicitly invented instrumentation controls, never market research.
No prices, accounts or broker connections are acquired by this module.
"""
import copy
import csv
from decimal import Decimal
from datetime import date, datetime, timezone
from pathlib import Path
import platform
import sys
import traceback
import uuid

from .evidence import ContractError, append_event, canonical_hash, digest, inside, load_json, now, seal_run, write_json
from .historical_input import _private, recheck_use_rights
from .historical_signals import ASSETS, _calendar, generate
from .historical_contract import prepare
from .historical_account import execute, performance_input
from .historical_account_controls import native_fill_reconciliation
from .historical_replay import independent_replay
from .historical_windows import report_windows, compare_windows
from .performance import calibrate_risk
from .paired_inference import paired_monthly
from .runner import PACKAGE, copy_file, package_inventory

VERSION = "historical-study/1"
ORIGINAL_PROTOCOL_SHA256 = "44f2ccda145f834a94ee822233339abfe7a9a05907cbc5d40afc49b8058b7f99"
CAPITALS = ("50000", "200000")
SCENARIOS = ("C0", "C1", "C2", "C3")
VARIANTS = ("S", "B0", "BR", "BC")
PERIODS = ("development", "validation", "confirmation")
FULL_SCOPE = {"name": "S02-E01-v0.1", "warmup": {"start": "2005-01-01", "end": "2005-12-31"},
    "periods": {"development": {"start": "2006-01-01", "end": "2014-12-31"},
                "validation": {"start": "2015-01-01", "end": "2019-12-31"},
                "confirmation": {"start": "2020-01-01", "end": "2026-09-30"}}}
GATES = ("source_use", "units_calendar", "actions_receipt", "funding_terms", "execution_friction")


def _keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ContractError(label + ": exact versioned fields required")


def _iso(day):
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ContractError("canonical study date required")
    return day


def resolve_scope(artifact, scope):
    _keys(scope, {"name", "warmup", "periods"}, "study scope")
    if scope != FULL_SCOPE and (artifact.get("data_kind") != "invented_control" or scope["name"] != "INVENTED_SMALL_CONTROL"):
        raise ContractError("market study may not shorten, shift or relabel original S02 scope")
    _keys(scope["periods"], PERIODS, "fixed diagnostic periods")
    _keys(scope["warmup"], {"start", "end"}, "warmup")
    _, inventory = _calendar(artifact)
    sessions = [r["trade_date"] for r in artifact["calendar"]]
    if sessions != sorted(set(sessions)):
        raise ContractError("unique complete session inventory required")
    nominal = []
    for name in PERIODS:
        window = scope["periods"][name]
        _keys(window, {"start", "end"}, "nominal period")
        start, end = _iso(window["start"]), _iso(window["end"])
        if start > end or start[-2:] != "01":
            raise ContractError("period starts must be explicit first calendar day of a month")
        nominal.append((start, end))
    if any(date.fromisoformat(nominal[i][1]).toordinal()+1 != date.fromisoformat(nominal[i+1][0]).toordinal() for i in (0, 1)):
        raise ContractError("diagnostic periods must be contiguous without overlap or omitted days")
    warm_start, warm_end = _iso(scope["warmup"]["start"]), _iso(scope["warmup"]["end"])
    if warm_start[-2:] != "01" or date.fromisoformat(warm_end).toordinal()+1 != date.fromisoformat(nominal[0][0]).toordinal():
        raise ContractError("warmup must end immediately before development")
    required_months = []
    year, month = int(warm_start[:4]), int(warm_start[5:7])
    while f"{year:04d}-{month:02d}" <= nominal[-1][1][:7]:
        required_months.append(f"{year:04d}-{month:02d}")
        year, month = (year+1, 1) if month == 12 else (year, month+1)
    if sum(m <= warm_end[:7] for m in required_months) < 10:
        raise ContractError("at least ten complete past warmup months required")
    for month in required_months:
        record = inventory.get(month)
        actual = [d for d in sessions if d[:7] == month]
        if not record or actual != record["expected_sessions"]:
            raise ContractError("scope lacks declared complete ETF session month: " + month)
    windows = {}
    for name, (start, end) in zip(PERIODS, nominal):
        included = [d for d in sessions if start <= d <= end]
        if not included or sessions.index(included[0]) == 0:
            raise ContractError("period lacks preceding actual session NAV")
        windows[name] = {"start": included[0], "end": included[-1]}
    start, end = windows["development"]["start"], windows["confirmation"]["end"]
    if not any(d[:7] > end[:7] for d in sessions):
        raise ContractError("calendar must include actual next-month sessions for paired inference and settlement")
    windows["full"] = {"start": start, "end": end}
    for year in range(int(start[:4]), int(end[:4])+1):
        included = [d for d in sessions if start <= d <= end and int(d[:4]) == year]
        if included:
            windows[f"year-{year}"] = {"start": included[0], "end": included[-1]}
    return {"scope": copy.deepcopy(scope), "windows": windows, "nominal_periods": copy.deepcopy(scope["periods"]),
        "start": start, "end": end, "opening_boundary": sessions[sessions.index(start)-1],
        "calendar_sha256": canonical_hash(sessions), "original_scope": scope == FULL_SCOPE,
        "boundary_policy": "split starts advance to first included session; each end is last session before next split; preceding NAV retained"}


def _trim(model, artifact, end):
    value = copy.deepcopy(model)
    value["end"] = end
    value["fx"]["close_marks"] = {d: q for d, q in value["fx"]["close_marks"].items() if d <= end}
    value["fx"]["exit_quotes"] = {d: q for d, q in value["fx"]["exit_quotes"].items() if d <= end}
    value["execution"]["rows"] = [r for r in value["execution"]["rows"] if r["date"] <= end]
    active = {a["id"] for a in artifact["actions"] if a["type"] == "dividend" and value["start"] <= a["effective_date"] <= end}
    value["withholding"] = {k: v for k, v in value["withholding"].items() if k in active}
    return value


def _cohort(model):
    value = copy.deepcopy(model)
    value.pop("scenario")
    value["cash_rules"].pop("initial_cny")
    value["fx"]["entry"].pop("principal_cny")
    return canonical_hash(value)


def _freeze_descriptor(descriptor, source_root, target):
    _keys(descriptor, {"file", "sha256"}, "frozen private file")
    source = inside(source_root, descriptor["file"])
    if digest(source) != descriptor["sha256"]:
        raise ContractError("input content differs from frozen descriptor")
    copy_file(source, target)
    if digest(target) != descriptor["sha256"]:
        raise ContractError("input changed during frozen copy")
    return target


def _load_inputs(manifest_path, run):
    manifest = load_json(manifest_path)
    _keys(manifest, {"schema_version", "data_kind", "scope", "artifact", "protocol", "models", "exit_forwards", "reviews", "revisited_history"}, "study manifest")
    if manifest["schema_version"] != VERSION or manifest["data_kind"] not in {"historical_market", "invented_control"} or manifest["revisited_history"] is not True:
        raise ContractError("explicit study provenance and revisited-history declaration required")
    source = manifest_path.parent
    artifact_file = _freeze_descriptor(manifest["artifact"], source, run / "inputs" / "artifact.json")
    _freeze_descriptor(manifest["protocol"], source, run / "inputs" / "protocol.txt")
    artifact = load_json(artifact_file)
    if artifact["data_kind"] != manifest["data_kind"]:
        raise ContractError("study/source provenance differs")
    if artifact["data_kind"] == "historical_market" and manifest["protocol"]["sha256"] != ORIGINAL_PROTOCOL_SHA256:
        raise ContractError("market study protocol differs from frozen original S02 text")
    recheck_use_rights(artifact, checked_at=now())
    resolved = resolve_scope(artifact, manifest["scope"])
    case_ids = {capital + "-" + scenario for capital in CAPITALS for scenario in SCENARIOS}
    _keys(manifest["models"], case_ids, "eight capital/scenario models")
    _keys(manifest["exit_forwards"], case_ids, "explicit eight exit branches")
    models, forwards, cohort = {}, {}, None
    for case in sorted(case_ids):
        capital, scenario = case.split("-")
        path = _freeze_descriptor(manifest["models"][case], source, run / "inputs" / (case + ".json"))
        model = load_json(path)
        if (model["schema_version"] != "historical-account-model/2" or model["scenario"] != scenario
                or model["cash_rules"]["initial_cny"] != capital or model["start"] != resolved["start"] or model["end"] != resolved["end"]):
            raise ContractError("case lacks full original net-CNY model, explicit fixed costs or correct capital/scenario")
        current = _cohort(model)
        if cohort is not None and current != cohort:
            raise ContractError("case conditions changed beyond capital, entry principal and frozen scenario stress")
        cohort = current
        models[case] = model
        exit_input = manifest["exit_forwards"][case]
        if exit_input is None:
            forwards[case] = None
        else:
            forwards[case] = load_json(_freeze_descriptor(exit_input, source, run / "inputs" / (case + "-exit.json")))
    _keys(manifest["reviews"], GATES, "external review gates")
    reviews = {}
    for name in GATES:
        entry = manifest["reviews"][name]
        _keys(entry, {"status", "reason", "evidence"}, "review declaration")
        if entry["status"] not in {"reviewed", "unresolved"} or not isinstance(entry["reason"], str) or not entry["reason"].strip() or not isinstance(entry["evidence"], list):
            raise ContractError("review status/reason/evidence unknown")
        if entry["status"] == "reviewed" and not entry["evidence"]:
            raise ContractError("reviewed declaration lacks frozen evidence")
        for i, item in enumerate(entry["evidence"]):
            _freeze_descriptor(item, source, run / "inputs" / "reviews" / f"{name}-{i}.txt")
        reviews[name] = copy.deepcopy(entry)
    if artifact["data_kind"] == "historical_market" and any(reviews[k]["status"] != "reviewed" for k in GATES[:4]):
        raise ContractError("historical model execution waits for source, unit/calendar, action/receipt and funding/terms reviews")
    return manifest, artifact, resolved, models, forwards, reviews


def _trial(run, registry, artifact, model, variant, name, *, k=None):
    folder = run / "trials" / name
    folder.mkdir(parents=True, exist_ok=False)
    append_event(registry, {"event": "TRIAL_STARTED", "trial": name, "variant": variant, "k": k, "at": now()})
    status, output = "RUN_ERROR", None
    try:
        write_json(folder / "model.json", model)
        features = generate(artifact, variant, trade_start=model["start"], end_date=model["end"], k=k,
                            delay_sessions=1 if model["scenario"] == "C3" else 0)
        write_json(folder / "features.json", features)
        data = prepare(artifact, features, model)
        write_json(folder / "reconstruction.json", {"artifact": "../../inputs/artifact.json", "artifact_sha256": canonical_hash(artifact),
            "model": "model.json", "features": "features.json", "prepared_sha256": canonical_hash(data),
            "method": "prepare the frozen artifact, features and model using this run's source snapshot"})
        account = execute(data)
        write_json(folder / "account.json", account)
        replay = independent_replay(data, account)
        write_json(folder / "replay.json", replay)
        if replay["passed"] is not True or replay["errors"] or not native_fill_reconciliation(account):
            raise ContractError("account/native monetary or availability evidence differs")
        ledger = performance_input(data, account)
        write_json(folder / "performance_input.json", ledger)
        status = "MODEL_RECONCILED"
        output = (data, account, ledger, folder)
    except Exception as error:
        write_json(folder / "failure.json", {"type": type(error).__name__, "reason": str(error),
            "classification": "TRIAL_ERROR_RETAINED_NOT_ECONOMIC_REJECTION"})
        (folder / "traceback.txt").write_text(traceback.format_exc())
    finally:
        append_event(registry, {"event": "TRIAL_FINISHED", "trial": name, "status": status, "at": now(), "output": str(folder.relative_to(run))})
    return output


def _diagnostic_status(comparison, pairs, calibration, reviews, original, kind):
    reasons = []
    if not original or kind != "historical_market": reasons.append("INVENTED_INSTRUMENTATION_NOT_HISTORY")
    if not calibration.get("risk_matched"): reasons.append("DEVELOPMENT_RISK_UNMATCHED")
    reasons += ["UNRESOLVED_" + k.upper() for k, r in reviews.items() if r["status"] != "reviewed"]
    for name in ("validation", "confirmation"):
        if not comparison["windows"][name]["CNY"]["risk_matched"]: reasons.append(name.upper() + "_RISK_NOT_EQUIVALENT")
        if not pairs[name]["sample_sufficient"]: reasons.append(name.upper() + "_SAMPLE_INSUFFICIENT")
    point = all(pairs[n]["point_conditions"]["growth_positive"] is True and pairs[n]["point_conditions"]["drawdown_not_deeper"] is True for n in ("validation", "confirmation"))
    intervals = all(pairs[n]["interval"]["direction"] == "LOWER_POSITIVE" for n in ("validation", "confirmation"))
    return {"status": "EVIDENCE_INSUFFICIENT" if reasons else "POINT_SCREEN_NOT_PASSED" if not point else "CONDITIONAL_INTERVAL_SUPPORT" if intervals else "POINT_SUPPORT_INTERVAL_UNCERTAIN",
            "reasons": sorted(set(reasons)), "point_conditions_met": point, "interval_conditions_met": intervals,
            "source_review_limit": "frozen review records are external declarations, not automatic legal/PIT/fill certification"}


def _write_table(path, matrix, comparisons, exits):
    """One row per cell/window/numeraire; missing evidence stays blank."""
    fields = ("cell", "variant", "capital_cny", "scenario", "period", "currency", "status", "boundary_date", "start", "end",
              "initial_equity", "final_equity", "external_net_flow", "monetary_profit", "period_net_return", "cagr_net",
              "volatility_annual", "sharpe_annual", "risk_free_source", "max_drawdown", "turnover_two_sided", "turnover_annual", "fill_count", "cost_total",
              "cost_components", "undefined_metrics", "insolvencies", "drawdown_episodes", "longest_drawdown", "cancellations",
              "requested_shares", "filled_shares", "mean_actual_risky_weight", "mean_positive_underallocation", "friction_cny_exact", "dividend_withholding_cny_exact",
              "fx_decomposition_exact", "beta_s_vs_br", "relative_volatility_gap", "risk_matched", "exit_status")
    import json
    def serialize(value):
        if value is None: return ""
        return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for cell, entry in matrix.items():
            case = entry["capital"] + "-" + entry["scenario"]
            for name, window in entry["windows"].items():
                for currency in ("CNY", "USD"):
                    row = {"cell": cell, "variant": entry["variant"], "capital_cny": entry["capital"], "scenario": entry["scenario"],
                           "period": name, "currency": currency, "status": entry["status"],
                           "exit_status": exits.get(cell, {}).get("realizability", exits.get(cell, {}).get("status", "NOT_COMPUTED"))}
                    if window is not None:
                        reported = window["currencies"][currency]
                        metrics, diagnostics = reported["metrics"], window["execution_diagnostics"]
                        row.update({k: window[k] for k in ("start", "end", "boundary_date")})
                        for key in ("initial_equity", "final_equity", "external_net_flow", "monetary_profit", "fill_count", "cost_total", "risk_free_source",
                                    "insolvencies", "drawdown_episodes", "longest_drawdown"):
                            row[key] = metrics[key]
                        for key in ("period_net_return", "cagr_net", "volatility_annual", "sharpe_annual", "max_drawdown", "turnover_two_sided", "turnover_annual"):
                            row[key] = metrics[key]["value"]
                        row.update(cost_components=metrics["cost_totals"], undefined_metrics=reported["undefined_metrics"],
                                   cancellations=diagnostics["cancellations_by_reason"],
                                   requested_shares=diagnostics["requested_shares"], filled_shares=diagnostics["filled_shares"],
                                   mean_actual_risky_weight=diagnostics["session_mean_exposure"]["actual_risky_weight"]["value"],
                                   mean_positive_underallocation=diagnostics["session_mean_exposure"]["positive_underallocation"]["value"],
                                   friction_cny_exact=diagnostics["additional_cost_disclosures_cny_exact"]["friction"],
                                   dividend_withholding_cny_exact=diagnostics["additional_cost_disclosures_cny_exact"]["dividend_withholding"],
                                   fx_decomposition_exact=window["fx_decomposition"]["totals_exact_fractions"])
                        comparison = comparisons.get(case, {}).get("risk_beta", {}).get("windows", {}).get(name, {}).get(currency)
                        if comparison is not None and entry["variant"] in {"S", "BR"}:
                            row.update(beta_s_vs_br=comparison["beta_s_vs_br"]["value"],
                                       relative_volatility_gap=comparison["relative_volatility_gap"], risk_matched=comparison["risk_matched"])
                    writer.writerow({key: serialize(value) for key, value in row.items()})


def _friction_scan(run, trials, artifact, resolved, models, accounts, selected):
    from .historical_friction import scan_friction
    outputs = {}
    for capital in CAPITALS:
        case = capital + "-C0"
        names = {variant: case + "-" + variant for variant in ("S", "BR")}
        if selected is None or any(name not in accounts for name in names.values()):
            outputs[case] = {"status": "NOT_EXECUTED_BASELINE_UNAVAILABLE"}
            continue
        baseline = models[case]
        base_ledger = accounts[names["S"]][2]
        windows = {name: {"boundary_date": base_ledger["calendar"][base_ledger["calendar"].index(resolved["windows"][name]["start"])-1],
                          "end": resolved["windows"][name]["end"]} for name in ("validation", "confirmation")}
        context = {"schema_version": "historical-friction/1", "capital_cny": capital, "frozen_k": selected,
                   "calendar": base_ledger["calendar"], "comparison_context": base_ledger["comparison_context"],
                   "terms": baseline["terms"], "scenario": "C0", "fixed_expenses": baseline["fixed_expenses"],
                   "expense_review": baseline["expense_review"], "data_kind": artifact["data_kind"]}
        def trial(bp):
            model = copy.deepcopy(baseline)
            for rule in model["terms"]["rules"]:
                rule["execution"]["friction_rate"] = str(Decimal(rule["execution"]["friction_rate"]) + Decimal(bp)/10000)
            values = {}
            for variant in ("S", "BR"):
                values[variant] = accounts[names[variant]] if Decimal(bp) == 0 else _trial(run, trials, artifact, model,
                    variant, f"friction-{capital}-{bp}-{variant}", k=selected if variant == "BR" else None)
            if any(value is None for value in values.values()):
                raise ContractError("friction account attempt failed; both attempts retained")
            return {"extra_per_side_bp": bp, "frozen_k": selected, "terms": model["terms"], "accounts": {
                variant: {"ledger": value[2], "reconciliation": {"passed": True, "errors": [],
                    "source_trial_ref": str(value[3].relative_to(run)), "account_sha256": canonical_hash(value[1]),
                    "replay_sha256": digest(value[3] / "replay.json")}} for variant, value in values.items()}}
        outputs[case] = scan_friction(trial, context=context, windows=windows)
    return outputs


def run_study(manifest_path, output, private_root):
    manifest_path, output = _private(manifest_path, private_root), _private(output, private_root)
    if Path(private_root).resolve().is_relative_to(PACKAGE.parent):
        raise ContractError("study data/evidence must remain outside distributable checkout")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    run = output / "runs" / run_id
    run.mkdir(parents=True, exist_ok=False)
    registry, trials = output / "registry.jsonl", run / "trials.jsonl"
    append_event(registry, {"event": "STARTED", "run_id": run_id, "purpose": "frozen_continuous_study_matrix", "at": now()})
    sources = package_inventory()
    code, status = 1, "RUN_ERROR"
    try:
        for relative in sources:
            copy_file(PACKAGE / relative, run / "source" / relative)
        copy_file(manifest_path, run / "inputs" / "manifest.json")
        write_json(run / "environment.json", {"python": sys.version, "platform": platform.platform(), "source_files_sha256": sources, "at": now()})
        manifest, artifact, resolved, models, forwards, reviews = _load_inputs(manifest_path, run)
        write_json(run / "resolved_scope.json", resolved)
        base = _trim(models["50000-C0"], artifact, resolved["windows"]["development"]["end"])
        strategy = _trial(run, trials, artifact, base, "S", "development-S-50000-C0")
        candidates = {}
        for i in range(101):
            k = f"{i/100:.2f}"
            result = _trial(run, trials, artifact, base, "BR", "development-BR-" + k, k=k)
            if result is not None: candidates[k] = result[2]
        if strategy is None or len(candidates) != 101:
            calibration = {"selected_k": None, "risk_matched": False, "reason": "DEVELOPMENT_ACCOUNT_ATTEMPTS_INCOMPLETE", "available_candidates": sorted(candidates)}
        else:
            try:
                calibration = calibrate_risk(strategy[2], candidates, boundary_date=base["performance_boundary"]["date"], end_date=base["end"])
            except ContractError as error:
                calibration = {"selected_k": None, "risk_matched": False, "reason": str(error), "available_candidates": sorted(candidates)}
        write_json(run / "calibration.json", calibration)
        selected = calibration["selected_k"]
        append_event(trials, {"event": "K_FROZEN", "selected_k": selected, "calibration_sha256": canonical_hash(calibration), "at": now(), "uses_validation_returns": False, "uses_confirmation_returns": False})
        accounts, window_reports, matrix, exits = {}, {}, {}, {}
        for capital in CAPITALS:
            for scenario in SCENARIOS:
                case = capital + "-" + scenario
                for variant in VARIANTS:
                    cell = case + "-" + variant
                    if variant == "BR" and selected is None:
                        matrix[cell] = {"status": "NOT_EXECUTED_CALIBRATION_UNAVAILABLE", "variant": variant, "capital": capital, "scenario": scenario, "windows": {n: None for n in resolved["windows"]}}
                        append_event(trials, {"event": "TRIAL_NOT_EXECUTED", "trial": cell, "reason": "CALIBRATION_UNAVAILABLE", "at": now()})
                        continue
                    value = _trial(run, trials, artifact, models[case], variant, cell, k=selected if variant == "BR" else None)
                    if value is None:
                        matrix[cell] = {"status": "ACCOUNT_ERROR", "variant": variant, "capital": capital, "scenario": scenario, "windows": {n: None for n in resolved["windows"]}}
                        continue
                    data, account, ledger, folder = value
                    matrix[cell] = {"status": "REPORT_PENDING", "variant": variant, "capital": capital,
                                    "scenario": scenario, "windows": {n: None for n in resolved["windows"]}}
                    try:
                        windows = report_windows(data, account, resolved["windows"])
                        write_json(folder / "windows.json", windows)
                        accounts[cell], window_reports[cell] = value, windows
                        matrix[cell] = {"status": "CONTINUOUS_MODEL_RECONCILED", "variant": variant, "capital": capital, "scenario": scenario,
                                        "frozen_k": selected if variant == "BR" else None, "windows": windows["windows"], "trial": str(folder.relative_to(run))}
                        if forwards[case] is None:
                            exits[cell] = {"status": "PENDING", "reason": "EXPLICIT_FORWARD_EXIT_INPUT_NOT_SUPPLIED", "study_terminal": account["cny_snapshots"][-1]}
                        else:
                            from .historical_liquidation import execute_exit
                            exits[cell] = execute_exit(data, account, forwards[case])
                        write_json(folder / "exit.json", exits[cell])
                    except Exception as error:
                        write_json(folder / "report_failure.json", {"type": type(error).__name__, "reason": str(error)})
                        (folder / "report_traceback.txt").write_text(traceback.format_exc())
                        matrix[cell]["status"] = "REPORT_OR_EXIT_ERROR"
        comparisons = {}
        sessions = [r["trade_date"] for r in artifact["calendar"]]
        for capital in CAPITALS:
            for scenario in SCENARIOS:
                case = capital + "-" + scenario
                s, b = case + "-S", case + "-BR"
                if s not in window_reports or b not in window_reports: continue
                try:
                    comparison = compare_windows(window_reports[s], window_reports[b])
                    pairs = {}
                    for name in PERIODS:
                        window = resolved["windows"][name]
                        boundary = sessions[sessions.index(window["start"])-1]
                        ends = [r["month_end_session"] for m, r in artifact["signal_calendar"]["inventory"].items() if boundary < r["month_end_session"] <= window["end"]]
                        pairs[name] = paired_monthly(accounts[s][2], accounts[b][2], boundary_date=boundary, end_date=window["end"], month_ends=sorted(ends), session_calendar=sessions)
                    comparisons[case] = {"risk_beta": comparison, "paired_monthly": pairs,
                        "diagnostic": _diagnostic_status(comparison, pairs, calibration, reviews, resolved["original_scope"], artifact["data_kind"])}
                except Exception as error:
                    comparisons[case] = {"status": "COMPARISON_ERROR", "type": type(error).__name__, "reason": str(error)}
        write_json(run / "matrix.json", matrix)
        write_json(run / "comparisons.json", comparisons)
        write_json(run / "exit_matrix.json", exits)
        friction = _friction_scan(run, trials, artifact, resolved, models, accounts, selected)
        write_json(run / "friction.json", friction)
        _write_table(run / "matrix.csv", matrix, comparisons, exits)
        errors = [k + ":" + v["status"] for k, v in matrix.items() if v["status"] != "CONTINUOUS_MODEL_RECONCILED"]
        errors += [k + ":COMPARISON_ERROR" for k, v in comparisons.items() if v.get("status") == "COMPARISON_ERROR"]
        errors += [k + ":FRICTION_TRIAL_EVIDENCE_INCOMPLETE" for k, v in friction.items() if v.get("all_trials_validated") is not True]
        if strategy is None or len(candidates) != 101: errors.append("development calibration trial inventory incomplete")
        if sources != package_inventory(): errors.append("program source changed during study")
        code = int(bool(errors))
        status = "MATRIX_DIAGNOSTICS_COMPUTED" if not errors else "STUDY_EVIDENCE_ERRORS"
        screens = [value.get("diagnostic", {}).get("status") for value in comparisons.values()]
        if len(screens) != 8 or any(s in {None, "EVIDENCE_INSUFFICIENT"} for s in screens):
            hypothesis = "EVIDENCE_INSUFFICIENT"
        elif "POINT_SCREEN_NOT_PASSED" in screens:
            hypothesis = "VERSION_DID_NOT_PASS_ECONOMIC_SCREEN"
        elif all(s == "CONDITIONAL_INTERVAL_SUPPORT" for s in screens):
            hypothesis = "CONDITIONAL_INTERVAL_SUPPORT_ACROSS_ALL_EIGHT_CASES"
        else:
            hypothesis = "POINT_SUPPORT_INTERVAL_UNCERTAIN"
        write_json(run / "result.json", {"schema_version": VERSION, "run_id": run_id, "status": status, "data_kind": artifact["data_kind"],
            "classification": "REVISITED_CONDITIONAL_HISTORY_DIAGNOSTICS" if artifact["data_kind"] == "historical_market" else "INVENTED_MATRIX_INSTRUMENTATION_ONLY",
            "scope_full_original": resolved["original_scope"], "errors": errors, "matrix_cells": len(matrix),
            "complete_cells": sum(v["status"] == "CONTINUOUS_MODEL_RECONCILED" for v in matrix.values()), "calibration_candidates": len(candidates),
            "frozen_k": selected, "calibration_risk_matched": calibration["risk_matched"], "comparison_cells": len(comparisons),
            "hypothesis_diagnostic": hypothesis, "hypothesis_policy": "requires validation and confirmation in every capital/scenario case; never select the cheapest surviving case",
            "exit_inputs_complete": all(forwards[k] is not None for k in forwards), "exit_matrix_sha256": canonical_hash(exits),
            "complete_cash_exit_cells": sum(v.get("realizability") == "COMPLETE_CASH_EXIT" for v in exits.values()),
            "terminal_realizability_complete": len(exits) == 32 and all(v.get("realizability") == "COMPLETE_CASH_EXIT" for v in exits.values()),
            "formal_history_account_trials": len(accounts) if artifact["data_kind"] == "historical_market" else 0,
            "official_paper_orders": 0, "real_orders": 0, "historical_engine_ready": False, "goal_complete": False,
            "reviews": reviews, "scientific_result_sha256": canonical_hash({"calibration": calibration, "matrix": matrix, "comparisons": comparisons, "exits": exits}),
            "friction_sha256": canonical_hash(friction), "friction_policy": "fixed C0 additional-per-side grid; full S and frozen-BR account reruns in both capitals; nonmonotonicity retained",
            "limitations": ["Review records bind external declarations, not automatic source/legal/PIT/fill certification",
                "Calibration executes development only; all other cells reuse one frozen k and continuous accounts",
                "Previously seen history and fixed surviving products are not pristine OOS evidence",
                "Pending or failed exit branch is not a realizable terminal-value result",
                "Friction sensitivity uses a finite fixed grid; no unique exact global break-even or actual slippage is inferred"]})
    except Exception as error:
        (run / "traceback.txt").write_text(traceback.format_exc())
        write_json(run / "failure.json", {"type": type(error).__name__, "reason": str(error), "classification": "STUDY_RUN_ERROR_NOT_STRATEGY_REJECTION"})
    finally:
        seal = seal_run(run)
        append_event(registry, {"event": "FINISHED", "run_id": run_id, "at": now(), "status": status, "exit_code": code, "seal_sha256": seal})
    return code, run
