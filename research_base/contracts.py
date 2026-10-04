from decimal import Decimal, InvalidOperation
from .evidence import ContractError

SCHEMA_VERSION = "1"


def decimal(value, label, minimum=None):
    if isinstance(value, bool) or value is None:
        raise ContractError(f"{label}: explicit finite number required")
    try:
        out = Decimal(str(value))
    except InvalidOperation as exc:
        raise ContractError(f"{label}: invalid number") from exc
    if not out.is_finite() or (minimum is not None and out < Decimal(str(minimum))):
        raise ContractError(f"{label}: invalid range/non-finite")
    return out


def validate_spec(spec):
    required = {"schema_version", "experiment_id", "purpose", "dataset_manifest",
                "protocol_file", "assumptions", "run_reference_controls", "run_vibe_controls"}
    if set(spec) != required:
        raise ContractError(f"Spec fields differ: missing={sorted(required-set(spec))}; "
                            f"unknown={sorted(set(spec)-required)}")
    if spec["schema_version"] != SCHEMA_VERSION:
        raise ContractError("Unsupported schema version")
    if spec["purpose"] != "validation_only":
        raise ContractError("This version accepts validation_only, not historical returns or orders")
    if not isinstance(spec["experiment_id"], str) or not spec["experiment_id"].strip():
        raise ContractError("experiment_id required")
    for key in ("dataset_manifest", "protocol_file"):
        if key == "dataset_manifest" and spec[key] is None:
            continue
        if not isinstance(spec[key], str) or not spec[key]:
            raise ContractError(f"{key}: relative path required")
    for key in ("run_reference_controls", "run_vibe_controls"):
        if type(spec[key]) is not bool:
            raise ContractError(f"{key}: boolean required")
    assumptions = spec["assumptions"]
    needed = {"costs", "information_time", "execution", "account", "data_usage"}
    if not isinstance(assumptions, dict) or set(assumptions) != needed:
        raise ContractError("All five assumption domains must be explicit")
    for key, item in assumptions.items():
        if not isinstance(item, dict) or set(item) != {"status", "detail"}:
            raise ContractError(f"{key}: status/detail required")
        if item["status"] not in {"unknown", "model_assumption", "verified"}:
            raise ContractError(f"{key}: invalid status")
        if not isinstance(item["detail"], str) or not item["detail"].strip():
            raise ContractError(f"{key}: evidence/limitation required")
        # A 'verified' label cannot unlock a different purpose or formal results.
    return spec
