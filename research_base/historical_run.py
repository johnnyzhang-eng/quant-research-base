"""Retained private execution of one explicitly bound historical account model.

Every attempt has immutable input/source copies and a registry seal. Monetary
acceptance requires independent stock/wallet replay; it is not source, funding,
protocol-matrix or profitability acceptance.
"""
import platform
import re
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .evidence import ContractError, append_event, canonical_hash, digest, load_json, now, seal_run, write_json
from .historical_input import _private
from .runner import PACKAGE, copy_file, package_inventory


def run_account(input_path, output, private_root):
    """No installation, market download, broker connection or forced exit."""
    input_path = _private(input_path, private_root)
    output = _private(output, private_root)
    if Path(private_root).resolve().is_relative_to(PACKAGE.parent):
        raise ContractError("private_root must be outside the distributable code checkout")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    run = output / "runs" / run_id
    run.mkdir(parents=True, exist_ok=False)
    registry = output / "registry.jsonl"
    append_event(registry, {"event": "STARTED", "run_id": run_id, "at": now(),
                           "purpose": "conditional_historical_account_model"})
    code, status = 1, "RUN_ERROR"
    sources = package_inventory()
    try:
        for relative in sources:
            copy_file(PACKAGE / relative, run / "source" / relative)
        input_hash = digest(input_path)
        copy_file(input_path, run / "input.json")
        if digest(run / "input.json") != input_hash:
            raise ContractError("input changed before frozen ingest")
        data = load_json(run / "input.json")
        write_json(run / "environment.json", {"python": sys.version, "platform": platform.platform(),
                   "source_files_sha256": sources, "input_sha256": input_hash, "at": now()})
        from .historical_account import execute, performance_input
        from .historical_replay import independent_replay
        from .historical_account_controls import native_fill_reconciliation
        from .performance import report
        account = execute(data)
        write_json(run / "account.json", account)
        replay = independent_replay(data, account)
        write_json(run / "independent_replay.json", replay)
        errors = []
        if replay.get("passed") is not True or replay.get("errors") != [] or not replay.get("replayed"):
            errors.append("independent monetary/availability replay differs")
        native_match = native_fill_reconciliation(account)
        native_sources = account.get("native_engine_source_sha256", {})
        if not native_match or set(native_sources) != {"GlobalEquityEngine", "BaseEngine"} or not all(
                isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in native_sources.values()):
            errors.append("native fill records or source fingerprints do not reconcile")
        if account["data_kind"] != data["data_kind"]:
            errors.append("market/control provenance changed during execution")
        if not errors:
            ledger = performance_input(data, account)
            write_json(run / "performance_input.json", ledger)
            write_json(run / "performance.json", report(ledger))
        if sources != package_inventory():
            errors.append("program source changed during execution")
        if digest(input_path) != input_hash:
            errors.append("caller input changed during execution")
        code = int(bool(errors))
        status = "MODEL_DIAGNOSTICS_RECONCILED" if not errors else "ACCOUNT_EVIDENCE_ERRORS"
        write_json(run / "result.json", {"schema_version": "historical-account-run/1", "run_id": run_id,
            "status": status, "classification": "CONDITIONAL_ACCOUNT_MODEL_NOT_FORMAL_STUDY_ACCEPTANCE",
            "data_kind": data["data_kind"], "errors": errors, "account_sha256": canonical_hash(account),
            "source_use_recheck": account["source_use_recheck"], "independent_replay_passed": replay["passed"],
            "native_fill_records_reconciled": native_match,
            "model_runs": 1, "formal_s02_studies": 0, "official_paper_orders": 0, "real_orders": 0,
            "historical_engine_ready": False, "goal_complete": False,
            "limitations": ["Source reviews are declarations; actual rights and later revocations need separate review",
                "A reconciled model does not certify funding rights, execution friction or opening fills",
                "One explicit window does not replace the complete frozen S02 matrix or development risk calibration",
                "Positions remain marked at end; future liquidation and exit proceeds are not established"]})
    except Exception as exc:
        (run / "traceback.txt").write_text(traceback.format_exc())
        write_json(run / "failure.json", {"type": type(exc).__name__, "reason": str(exc),
                   "classification": "RUN_ERROR_NOT_STRATEGY_OR_BROKER_FAILURE"})
    finally:
        seal = seal_run(run)
        append_event(registry, {"event": "FINISHED", "run_id": run_id, "at": now(), "status": status,
                               "exit_code": code, "seal_sha256": seal})
    return code, run
