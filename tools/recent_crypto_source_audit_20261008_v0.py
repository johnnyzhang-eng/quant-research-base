"""Read-only audit of pinned public source disclosures; never executes their code.

This checks evidence completeness and raw card fields. It does not reproduce
returns or determine whether an author's private validation actually passed.
"""
from __future__ import annotations

import ast
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


CRYPTO_REV = "f8e32599c192b823dbe7aea77a2b9341c6114527"
MIRROR_REV = "8ec32ea1ad454edd88abc5118a808c6fa764063b"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def normalized_markdown_text(raw: bytes) -> str:
    return " ".join(raw.decode().replace("**", "").split())


def inventory(tree: dict, revision: str) -> dict[str, dict]:
    if tree.get("sha") != revision or tree.get("truncated") is not False:
        raise ValueError("Incomplete or wrong revision tree")
    blobs = [entry for entry in tree["tree"] if entry["type"] == "blob"]
    paths = [entry["path"] for entry in blobs]
    if len(set(paths)) != len(paths):
        raise ValueError("Duplicate tree paths")
    return dict(zip(paths, blobs))


def decode_rows(raw: bytes) -> list[dict]:
    rows = json.loads(raw, parse_float=Decimal)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Expected index list")
    keys = [row["key"] for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate strategy keys")
    return rows


def card_fields(card: dict) -> dict:
    """Report literal fields without treating 'passed' as a profit assertion."""
    return {
        "key": card["key"],
        "status": card["status"],
        "stages_passed": card["stages_passed"],
        "stage_count_differs_from_seven": card["stages_passed"] != 7,
        "oos_total_return_bps": str(card["oos"]["total_return_bps"]),
        "oos_sharpe": str(card["oos"]["sharpe"]),
        "negative_oos_return": card["oos"]["total_return_bps"] < 0,
        "negative_oos_sharpe": card["oos"]["sharpe"] < 0,
        "total_trades": card["oos"]["total_trades"],
        "independent_validation_pass_determined": False,
    }


def controls() -> list[str]:
    checked = []
    good = {"sha": "known", "truncated": False,
            "tree": [{"type": "blob", "path": "cards/a.json", "sha": "x"}]}
    assert set(inventory(good, "known")) == {"cards/a.json"}
    checked.append("known_complete_tree")
    for name, mutant in [
        ("truncated_tree_rejected", dict(good, truncated=True)),
        ("wrong_revision_rejected", dict(good, sha="other")),
        ("duplicate_tree_path_rejected", dict(good, tree=good["tree"] * 2)),
    ]:
        try:
            inventory(mutant, "known")
        except ValueError:
            checked.append(name)
        else:
            raise AssertionError(name)
    assert decode_rows(b'[{"key":"a","status":"passed"}]')[0]["key"] == "a"
    checked.append("known_index_row")
    try:
        decode_rows(b'[{"key":"a"},{"key":"a"}]')
    except ValueError:
        checked.append("duplicate_index_key_rejected")
    else:
        raise AssertionError("duplicate_index_key_rejected")
    card = {"key": "a", "status": "passed", "stages_passed": 7,
            "oos": {"total_return_bps": Decimal("-1"), "sharpe": Decimal("-0.2"),
                    "total_trades": 3}}
    values = card_fields(card)
    assert values["negative_oos_return"] is True
    assert values["stage_count_differs_from_seven"] is False
    assert values["independent_validation_pass_determined"] is False
    checked.append("negative_return_does_not_infer_failed_validation")
    values = card_fields(dict(card, stages_passed=2,
                            oos=dict(card["oos"], total_return_bps=Decimal("1"))))
    assert values["negative_oos_return"] is False
    assert values["stage_count_differs_from_seven"] is True
    checked.append("stage_gap_separate_from_return_sign")
    assert git_blob(b"test\n") == "9daeafb9864cf43055ae93beb0afd6c7d144bfa4"
    checked.append("known_git_blob_hash")
    assert normalized_markdown_text(b"separate, **private\nresearch repository**") == "separate, private research repository"
    checked.append("wrapped_bold_disclosure_normalization")
    return checked


def audit(first: Path, second: Path) -> dict:
    checks = controls()
    crypto = inventory(json.loads((second / "crypto-strategies-tree.json").read_bytes()), CRYPTO_REV)
    mirror = inventory(json.loads((second / "walk-forward-crypto-tree.json").read_bytes()), MIRROR_REV)
    captures = []

    def verified(path: Path, upstream: str, tree: dict) -> bytes:
        raw = path.read_bytes()
        if git_blob(raw) != tree[upstream]["sha"]:
            raise ValueError("Capture does not match pinned upstream blob: " + upstream)
        captures.append({"upstream_path": upstream, "git_blob_sha": tree[upstream]["sha"],
                         "sha256": sha256(raw), "bytes": len(raw)})
        return raw

    index = decode_rows(verified(first / "index.json", "index.json", crypto))
    verified(second / "crypto-strategies-README.md", "README.md", crypto)
    passed_keys = sorted(Path(path).stem for path in crypto if path.startswith("passed/") and path.endswith(".py"))
    rejected_keys = sorted(Path(path).stem for path in crypto if path.startswith("strategies/") and path.endswith(".py"))
    card_keys = sorted(Path(path).stem for path in crypto if path.startswith("cards/") and path.endswith(".json"))
    index_keys = {row["key"] for row in index}
    cards = []
    for key in passed_keys:
        raw = verified(first / "cards" / (key + ".json"), "cards/" + key + ".json", crypto)
        card = json.loads(raw, parse_float=Decimal)
        if card["key"] != key:
            raise ValueError("Card key does not match filename")
        cards.append(card_fields(card))
    imports = []
    for key in ["asia_drift_btc_v2", "crash_recovery_eth_v3"]:
        raw = verified(first / "passed" / (key + ".py"), "passed/" + key + ".py", crypto)
        modules = sorted({node.module for node in ast.walk(ast.parse(raw))
                          if isinstance(node, ast.ImportFrom) and node.module})
        imports.append({"strategy_key": key, "static_imports": modules, "executed": False})
    verified(second / "walk-forward-crypto-README.md", "README.md", mirror)
    redaction = verified(first / "walk-forward-redaction-report.md", "docs/REDACTION_REPORT.md", mirror)
    assert "separate, private research repository" in normalized_markdown_text(redaction)
    return {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PINNED_PUBLIC_DISCLOSURE_REVIEW_ONLY",
        "auditor_source_sha256": sha256(Path(__file__).read_bytes()),
        "control_groups_passed": len(checks), "controls": checks,
        "external_code_executed": False, "network_requests_in_auditor": 0,
        "crypto_strategies": {
            "repository": "CacheCarti/Crypto-Strategies", "revision": CRYPTO_REV,
            "tree_complete": True, "tree_file_count": len(crypto),
            "strategy_source_file_count": len(passed_keys) + len(rejected_keys),
            "passed_source_files": len(passed_keys), "rejected_source_files": len(rejected_keys),
            "card_files_in_tree": len(card_keys), "index_entries": len(index),
            "index_status_counts": dict(Counter(row["status"] for row in index)),
            "index_matches_rejected_source_keys": index_keys == set(rejected_keys),
            "all_source_keys_have_card_paths": set(card_keys) == set(passed_keys + rejected_keys),
            "passed_source_keys_missing_from_index": sorted(set(passed_keys) - index_keys),
            "passed_cards_actually_read": len(cards), "passed_cards": cards,
            "static_source_reviews": imports,
            "validation_harness_python_files_in_tree": [p for p in crypto if p.endswith(".py") and not p.startswith(("passed/", "strategies/"))],
            "claim_scope": "README, index and released card fields require reconciliation; private later-stage metrics may explain them. No independent profit or failed-validation determination.",
            "next_required_evidence": ["versioned seven-stage logs and card schema", "validator, strategy contract and fill/cost semantics", "exact data windows, gap treatment and sentiment vintages", "whole-account return paths and matched holding benchmark", "parameter-selection history and fresh forward sample"],
        },
        "walk_forward_crypto": {
            "repository": "AKzar1el/walk-forward-crypto", "revision": MIRROR_REV,
            "tree_complete": True, "tree_file_count": len(mirror),
            "public_python_files": sorted(p for p in mirror if p.endswith(".py")),
            "author_discloses_separate_private_strategy_source": True,
            "public_exact_reproduction_possible_from_reviewed_tree_alone": False,
            "private_strategy_failure_inferred": False,
        },
        "verified_raw_captures": captures,
        "decision_check": {
            "P1": "Known-answer controls, complete exact-revision trees and Git blob matching precede counts and literal card metrics.",
            "P2": "Inconsistent public field scope differs from deliberately private source; neither mechanism establishes trading failure.",
            "P3": "Keep strongest alternative: cards can contain earlier-stage metrics while private later stages passed; source logs/schema are needed.",
        },
        "historical_strategy_returns_reproduced": False,
        "new_strategy_admitted_for_trading": False,
        "real_money_order_posts": 0,
        "new_paid_model_calls": 0,
        "original_goal_complete": False,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-v0", required=True, type=Path)
    parser.add_argument("--capture-v1", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.capture_v0, args.capture_v1)
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({"status": report["status"], "control_groups_passed": report["control_groups_passed"],
                      "report_sha256": sha256(args.output.read_bytes())}))
