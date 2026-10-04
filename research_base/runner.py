"""Single offline entry point. Runs are immutable and all attempts are logged."""
import os
import platform
import shutil
import subprocess
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .contracts import validate_spec
from .data_audit import audit_snapshot
from .evidence import (ContractError, append_event, canonical_hash, digest, inside,
                       load_json, now, seal_run, write_json)

PACKAGE = Path(__file__).resolve().parent


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ContractError("Refuse to overwrite evidence")
    if source.is_symlink():
        raise ContractError("Refuse to ingest a symlink")
    shutil.copyfile(source, target)
    if digest(source) != digest(target):
        raise ContractError("Input changed during snapshot copy")


def freeze_snapshot(source, target):
    manifest = load_json(source)
    before = digest(source)
    copy_file(source, target / "manifest.json")
    inventory = []
    for section, prefix in (("raw_files", "raw"), ("normalized_files", "normalized"),
                            ("supplemental_files", ""), ("scripts_sha256", "")):
        for name in manifest.get(section, {}):
            relative = f"{prefix}/{name}" if prefix else name
            src, dst = inside(source.parent, relative), inside(target, relative)
            copy_file(src, dst)
            inventory.append({"file": relative, "sha256": digest(dst)})
    if digest(source) != before:
        raise ContractError("Manifest changed during ingest")
    return before, inventory


def run_reference(run, protocol):
    root = run / "reference"
    folder = root / "round2"
    folder.mkdir(parents=True)
    for name in ("low_frequency_engine.py", "run_controls.py"):
        copy_file(PACKAGE / "reference" / name, folder / name)
    copy_file(protocol, root / "候选策略与首个实验协议.md")
    (root / "试验登记.csv").write_text("trial_id,classification\n")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.run([sys.executable, "-B", str(folder / "run_controls.py")],
                             cwd=folder, env=env, capture_output=True, text=True, timeout=90)
    (root / "stdout.txt").write_text(process.stdout)
    (root / "stderr.txt").write_text(process.stderr)
    if not (folder / "controls_actual.json").exists():
        raise ContractError(f"Reference control process did not produce a report: {process.returncode}")
    report = load_json(folder / "controls_actual.json")
    # Derive acceptance from each result, not just a summary label or exit code.
    reports = report["reports"]
    correct = (process.returncode == 0 and len(reports) == report["controls_total"]
               and all(r["passed"] is True for r in reports)
               and report["controls_passed"] == len(reports)
               and report["frozen_files_unchanged"] is True
               and report["formal_history_trials"] == 0)
    return {"scope": report["classification"], "accepted": correct,
            "passed": sum(r["passed"] is True for r in reports), "total": len(reports),
            "full_signal_path_cases": 4, "reports": reports,
            "engine_version": report["engine_version"]}


def package_inventory():
    return {str(p.relative_to(PACKAGE)): digest(p) for p in sorted(PACKAGE.rglob("*.py"))}


def run_validation(spec_path, workspace, output, vibe=False):
    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    run = output / "runs" / run_id
    run.mkdir(parents=True, exist_ok=False)
    registry = output / "registry.jsonl"
    append_event(registry, {"event": "STARTED", "run_id": run_id, "at": now()})
    status, returncode = "RUN_FAILED", 1
    sources = package_inventory()
    try:
        for relative in sources:
            copy_file(PACKAGE / relative, run / "source" / relative)
        copy_file(Path(spec_path), run / "inputs" / "spec.json")
        spec = validate_spec(load_json(run / "inputs" / "spec.json"))
        protocol = inside(workspace, spec["protocol_file"])
        copy_file(protocol, run / "inputs" / "protocol")
        protocol = run / "inputs" / "protocol"
        write_json(run / "environment.json", {"python": sys.version,
                   "platform": platform.platform(), "package_version": __version__,
                   "source_files_sha256": sources})
        write_json(run / "assumptions.json", spec["assumptions"])
        data = None
        inventory = []
        manifest_sha = None
        if spec["dataset_manifest"] is not None:
            source = inside(workspace, spec["dataset_manifest"])
            manifest_sha, inventory = freeze_snapshot(source, run / "dataset")
            data = audit_snapshot(run / "dataset" / "manifest.json")
            write_json(run / "data_audit.json", data)
        reference = run_reference(run, protocol) if spec["run_reference_controls"] else None
        if reference is not None:
            write_json(run / "reference_summary.json", reference)
        engine = None
        if vibe or spec["run_vibe_controls"]:
            from .vibe_controls import run_controls
            engine = run_controls(run)
            write_json(run / "vibe_controls.json", engine)
        from .performance_controls import run_controls as metric_controls
        metrics = metric_controls(run)
        write_json(run / "metric_controls.json", metrics)
        from .path_controls import run_controls as path_controls
        paths = path_controls(run / "protocol_reference", use_vibe=False)
        write_json(run / "protocol_path_reference.json", paths)
        live_paths = path_controls(run / "protocol_vibe", use_vibe=True) if engine is not None else None
        if live_paths is not None:
            write_json(run / "protocol_path_vibe.json", live_paths)
        from .historical_readiness import assess
        preflight = assess(data, reference, engine, metrics, paths, live_paths, spec["assumptions"])
        write_json(run / "historical_preflight.json", preflight)
        sources_unchanged = sources == package_inventory()
        errors = []
        if reference is not None and not reference["accepted"]:
            errors.append("Reference known-answer controls differ")
        if data is not None and (not data["integrity_passed"] or
                any(v["error_count"] for v in data.get("daily", {}).values()) or
                any(v["error_count"] for v in data.get("issuer_distributions", {}).values())):
            errors.append("Snapshot integrity or structure errors")
        if not sources_unchanged:
            errors.append("Program source changed during run")
        if engine is not None and not engine["instrument_calibrated"]:
            errors.append("Vibe acceptance instrument calibration failed")
        if engine is not None and not engine["aligned_supported_cases_accepted"]:
            errors.append("Aligned Vibe supported cases differ from fixed contract")
        if not metrics["accepted"]:
            errors.append("Metric/calibration instrument controls differ")
        if not paths["accepted"] or (live_paths is not None and not live_paths["accepted"]):
            errors.append("Full protocol/allocation path or causal-prefix controls differ")
        fingerprint = {"source": sources, "spec": spec, "protocol_sha256": digest(protocol),
                       "dataset_manifest_sha256": manifest_sha, "dataset_inventory": inventory,
                       "python": platform.python_version(), "vibe": bool(vibe or spec["run_vibe_controls"]),
                       "engine_environment": {k: engine[k] for k in ("engine_source_sha256", "dependencies")} if engine else None}
        scientific = {"reference": reference, "data": data, "engine": engine, "metrics": metrics,
                      "protocol_reference": paths, "protocol_vibe": live_paths,
                      "historical_preflight": preflight}
        status = "VALIDATION_ERRORS" if errors else "VALIDATION_COMPLETED_WITH_GAPS"
        result = {"schema_version": "1", "run_id": run_id, "at": now(), "status": status,
                  "experiment_id": spec["experiment_id"], "purpose": spec["purpose"],
                  "reproduction_key": canonical_hash(fingerprint),
                  "scientific_result_sha256": canonical_hash(scientific), "errors": errors,
                  "data_attached": data is not None, "data_historical_ready": False,
                  "reference_controls": {k: reference[k] for k in ("passed", "total", "scope")} if reference else None,
                  "vibe_controls": engine["summary"] if engine else None,
                  "metric_controls": {"passed": metrics["cases_passed"], "total": metrics["cases_total"],
                                      "scope": metrics["classification"]},
                  "protocol_path_controls": {"reference": paths["cases_passed"],
                                             "vibe": live_paths["cases_passed"] if live_paths is not None else None,
                                             "total": paths["cases_total"], "classification": paths["classification"]},
                  "formal_history_trials": 0, "broker_orders": 0,
                  "historical_preflight": {"gate_decision": preflight["gate_decision"],
                                           "unresolved_conditions": len(preflight["gates"]),
                                           "report": "historical_preflight.json"},
                  "gaps": data["gaps"] if data else [{"id": "DATA", "detail": "No historical dataset attached"}],
                  "limitations": ["Validation-only, not a strategy-return or broker result",
                                  "Control acceptance is bounded by its cases and declared model"]}
        write_json(run / "result.json", result)
        returncode = int(bool(errors))
    except Exception as exc:
        (run / "traceback.txt").write_text(traceback.format_exc())
        write_json(run / "failure.json", {"type": type(exc).__name__, "reason": str(exc),
                   "classification": "RUN_ERROR_NOT_STRATEGY_FAILURE"})
    finally:
        seal = seal_run(run)
        append_event(registry, {"event": "FINISHED", "run_id": run_id, "at": now(),
                     "status": status, "exit_code": returncode, "seal_sha256": seal})
    return returncode, run
