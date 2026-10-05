"""Sealed acceptance for historical conversion and offline paper-order controls.

This entry point neither runs a historical strategy nor connects to a broker.
Every attempt is retained, including exceptions and failed assertions.
"""
import platform
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .evidence import append_event, canonical_hash, now, seal_run, write_json
from .runner import PACKAGE, copy_file, package_inventory


def assess_controls(report, expected_ids):
    """Do not infer acceptance from an exit code or producer summary alone."""
    rows = report.get("reports", [])
    ids = [r.get("id") for r in rows]
    def correct(row):
        if row.get("passed") is not True or "expected" not in row or "actual" not in row:
            return False
        try:
            return canonical_hash(row["actual"]) == canonical_hash(row["expected"])
        except (TypeError, ValueError):
            return False
    passed = sum(correct(r) for r in rows)
    valid_ids = all(isinstance(i, str) and i for i in ids)
    valid_expected = all(isinstance(i, str) and i for i in expected_ids)
    inventory_matches = (valid_ids and valid_expected
                         and len(expected_ids) == len(set(expected_ids))
                         and len(ids) == len(set(ids)) == len(expected_ids)
                         and set(ids) == set(expected_ids))
    return {"accepted": bool(rows) and inventory_matches
            and passed == len(rows) and report.get("accepted") is True,
            "passed": passed, "total": len(rows), "expected_total": len(expected_ids),
            "classification": report.get("classification")}


def run_phase2_validation(output, *, historical_model_controls=False, integrated_account_controls=False):
    output = Path(output).resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    run = output / "runs" / run_id
    run.mkdir(parents=True, exist_ok=False)
    registry = output / "registry.jsonl"
    append_event(registry, {"event": "STARTED", "run_id": run_id,
                           "purpose": "phase2_offline_controls", "at": now()})
    status, code = "RUN_FAILED", 1
    sources = package_inventory()
    try:
        for relative in sources:
            copy_file(PACKAGE / relative, run / "source" / relative)
        write_json(run / "environment.json", {"python": sys.version,
                   "platform": platform.platform(), "source_files_sha256": sources})
        from .historical_input import (run_controls, control_report_accepted,
                                       EXPECTED_CONTROL_IDS as HISTORY_IDS)
        from .execution import run_offline_controls, EXPECTED_CONTROL_IDS as ORDER_IDS
        from .execution.reporting import validate_control_report
        history = run_controls(run / "historical_input")
        write_json(run / "historical_input_controls.json", history)
        orders = run_offline_controls(run / "offline_orders")
        write_json(run / "offline_order_controls.json", orders)
        summaries = {"historical_input": assess_controls(history, HISTORY_IDS),
                     "offline_orders": assess_controls(orders, ORDER_IDS)}
        # Component-specific validators also check frozen fixture answers,
        # scenario counts and classification, beyond generic row consistency.
        summaries["historical_input"]["accepted"] &= control_report_accepted(history)
        order_validation = validate_control_report(orders)
        write_json(run / "offline_order_independent_validation.json", order_validation)
        summaries["offline_orders"]["accepted"] &= order_validation["accepted"]
        model_reports = {}
        if historical_model_controls or integrated_account_controls:
            from . import historical_signals, trading_terms, cash_account
            for name, component in [("historical_signals", historical_signals),
                                    ("trading_terms", trading_terms), ("cash_account", cash_account)]:
                report = component.run_controls(run / name)
                write_json(run / (name + "_controls.json"), report)
                model_reports[name] = report
                summaries[name] = assess_controls(report, component.EXPECTED_CONTROL_IDS)
                summaries[name]["accepted"] &= component.control_report_accepted(report)
        if integrated_account_controls:
            from . import historical_account_controls
            report = historical_account_controls.run_controls(run / "historical_account")
            write_json(run / "historical_account_controls.json", report)
            model_reports["historical_account"] = report
            summaries["historical_account"] = assess_controls(report, historical_account_controls.EXPECTED_CONTROL_IDS)
            summaries["historical_account"]["accepted"] &= historical_account_controls.control_report_accepted(report)
        errors = [f"{name}: known-answer/rejection rows differ"
                  for name, summary in summaries.items() if not summary["accepted"]]
        if sources != package_inventory():
            errors.append("Program source changed during acceptance")
        code = int(bool(errors))
        status = "CONTROL_ERRORS" if errors else "OFFLINE_MILESTONE_CONTROLS_COMPLETED"
        write_json(run / "result.json", {
            "schema_version": "phase2_controls/1", "run_id": run_id,
            "status": status, "classification": "SYNTHETIC_INPUT_AND_OFFLINE_EXECUTION_ONLY",
            "controls": summaries, "errors": errors,
            "scientific_result_sha256": canonical_hash({"history": history, "orders": orders,
                                                        "historical_model_components": model_reports}),
            "historical_model_components_requested": historical_model_controls,
            "integrated_account_controls_requested": integrated_account_controls,
            "formal_history_trials": 0, "official_paper_orders": 0, "real_orders": 0,
            "goal_complete": False,
            "limitations": ["Conversion controls do not certify vendor data or use rights",
                            "Offline order controls do not establish official simulation readiness",
                            "Optional integrated account controls use invented data; no full S02 matrix or source/funding acceptance inferred",
                            "Original historical protocol and synthetic-only engine gates remain unchanged"]})
    except Exception as exc:
        (run / "traceback.txt").write_text(traceback.format_exc())
        write_json(run / "failure.json", {"type": type(exc).__name__, "reason": str(exc),
                   "classification": "RUN_ERROR_NOT_STRATEGY_OR_BROKER_FAILURE"})
    finally:
        seal = seal_run(run)
        append_event(registry, {"event": "FINISHED", "run_id": run_id, "at": now(),
                               "status": status, "exit_code": code, "seal_sha256": seal})
    return code, run
