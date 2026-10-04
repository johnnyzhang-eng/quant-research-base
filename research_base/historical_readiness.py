"""Evidence-bound S02 preflight, not a license or historical-run authorization.

Version 1 reports remaining conditions of this foundation. It cannot grant
readiness from a manifest label or turn synthetic controls into market evidence.
"""
import copy

from .evidence import canonical_hash


def record_passed(row, causal):
    if row.get("passed") is not True:
        return False
    if not causal and row.get("classification") == "EXPECTED_REJECTION_OF_INVALID_OR_UNACCEPTED_INPUT":
        expected, actual = row.get("expected"), row.get("actual")
        return (isinstance(expected, str) and expected.startswith("STOP with ")
                and isinstance(actual, str) and expected[len("STOP with "):] in actual)
    if not causal and row.get("id") == "C19_replay_detects_tampered_fill":
        errors = row.get("actual_errors", [])
        return (row.get("expected") == "independent replay rejects +1 injected share"
                and any(e.get("field", "").endswith(":holdings")
                        and e.get("actual") != e.get("expected") for e in errors))
    if not causal and row.get("id") == "C20_same_session_future_information":
        answer = {"same_open_intents_attempts_fills": True, "post_close_conflicts_flagged": True}
        return row.get("actual") == row.get("expected") == answer
    if not causal and row.get("id") == "M12_development_risk_only_grid":
        # Producer retains its101-account grid in separate sealed artifacts;
        # this row is a summary control, not a second grid recomputation.
        return set(row) == {"id", "passed"}
    return (row.get("differences") == [] and not row.get("independent_replay_errors")
            and row.get("replay", {}).get("passed", True) is True
            and not row.get("replay", {}).get("errors")
            and (not causal or (row.get("replay", {}).get("passed") is True
                                and row.get("replay", {}).get("errors") == [])))


def case_evidence(report, expected, *, causal=False):
    if report is None:
        return {"observed": False, "accepted": False, "passed": None, "total": None}
    rows = report.get("reports", [])
    passed = sum(record_passed(r, causal) for r in rows)
    unique = (len({r.get("id") for r in rows}) == len(rows)
              and all(isinstance(r.get("id"), str) and r["id"] for r in rows))
    accepted = (len(rows) == expected and unique and passed == expected
                and report.get("accepted") is True)
    if causal:
        controls = report.get("causal_controls", [])
        accepted = (accepted and len(controls) == 2
                    and {c.get("mutation") for c in controls} == {"truncate", "future_price_action"}
                    and all(c.get("passed") is True for c in controls))
    if "bad_inputs" in report:
        rejections = report["bad_inputs"]
        accepted = (accepted and len(rejections) == 4
                    and {r.get("mutation") for r in rejections} == {
                        "missing_observation", "misaligned_rf", "missing_flow", "duplicate_fill"}
                    and all(r.get("detected") is True for r in rejections))
    return {"observed": True, "accepted": accepted, "passed": passed, "total": len(rows),
            "scope": "SYNTHETIC_CONTROL_EVIDENCE_ONLY"}


def assess(audit, reference, engine, metrics, paths, live_paths, assumptions):
    """Derive observations from this run; never edit source conditions in place."""
    inputs = {"data_audit": audit, "reference": reference, "vibe": engine,
              "metrics": metrics, "protocol_reference": paths, "protocol_vibe": live_paths,
              "assumptions": assumptions}
    fingerprints = {k: canonical_hash(v) if v is not None else None for k, v in inputs.items()}
    controls = {"reference": case_evidence(reference, 39),
                "metrics": case_evidence(metrics, 12),
                "protocol_reference": case_evidence(paths, 11, causal=True),
                "protocol_vibe": case_evidence(live_paths, 11, causal=True)}
    aligned_rows = engine.get("reports", []) if engine else []
    aligned_passed = sum(r.get("aligned", {}).get("status") == "MATCH"
                         and r.get("aligned", {}).get("differences") == []
                         and r.get("aligned", {}).get("replay_errors") == [] for r in aligned_rows)
    controls["vibe_accounting"] = {
        "observed": engine is not None, "total": len(aligned_rows) if engine else None,
        "passed": aligned_passed if engine else None,
        "accepted": bool(engine and len(aligned_rows) == 21 and aligned_passed == 21
                         and len({r.get("id") for r in aligned_rows}) == 21
                         and engine.get("instrument_calibrated") is True
                         and engine.get("aligned_supported_cases_accepted") is True),
        "scope": "EXPLICIT_ADAPTER_SYNTHETIC_CONTROL_EVIDENCE_ONLY"}
    observed = audit is not None
    usable = bool(observed and audit.get("integrity_passed") is True)
    daily = audit.get("daily", {}) if usable else {}
    cross = audit.get("cross_source", {}) if usable else {}
    source_conditions = copy.deepcopy(audit.get("gaps", [])) if observed else []
    declared = {c.get("id"): c for c in source_conditions}
    metadata = copy.deepcopy(audit.get("source_metadata", {})) if usable else {}
    gates = []

    def add(code, title, evidence, remaining, resolve, assumptions_policy):
        gates.append({"id": code, "requirement": title,
                      "status": "PARTIAL_SYNTHETIC_ACCEPTANCE" if code == "D06" and all(
                          c["accepted"] for c in controls.values()) else "UNRESOLVED",
                      "snapshot_declaration": declared.get(code), "current_evidence": evidence,
                      "remaining": remaining, "resolution": resolve,
                      "conditional_model_policy": assumptions_policy})

    add("D01", "Source use rights",
        {"snapshot_attached": observed, "integrity_passed": usable,
         "license_grant_verified_by_this_run": False},
        "No provider-specific acquisition, analysis, storage and redistribution grant verified.",
        "Preserve applicable terms or written grants for each source and intended use; select an authorized source.",
        "Permission cannot be replaced by a zero-cost or personal-use assumption.")
    add("D02", "Primary price and volume definition",
        {"cross_source": copy.deepcopy(cross), "structure": {k: {
            a: v.get(a) for a in ("rows", "error_count", "missing_volume", "missing_sessions_full_window")}
            for k, v in daily.items()}, "source_metadata": metadata},
        "No accepted primary source, historical quote/share unit mapping or opening execution definition.",
        "Explain disagreements and revisions; freeze one authorized definition without averaging or silent fill.",
        "A registered daily execution assumption can be conditional; unexplained unit conflicts cannot be ignored.")
    add("D03", "Calendar and information timing",
        {"calendar_rows": audit.get("calendar_rows") if usable else None,
         "missing_received_timestamps": {k: v.get("missing_received_timestamps") for k, v in daily.items()}},
        "Session-date integrity does not establish intraday hours or historical receipt/revision times.",
        "Freeze normal/half-day sessions and supported event timing; identify unavailable original receipts.",
        "A conservative availability/delay model must be explicit and stress-tested; never relabel receipts as observed.")
    add("D04", "Corporate-action and share units",
        {"issuer_distributions": copy.deepcopy(audit.get("issuer_distributions", {})) if usable else {},
         "EFA_source_metadata": metadata.get("EFA")},
        "Issuer events and byte checks do not validate normalized prices against raw share, volume and dividend units.",
        "Freeze raw-versus-signal mappings; verify split and gross/net distribution units and cutoff receivables.",
        "No double counting split-adjusted prices; missing action/unit evidence requires a stated model restriction.")
    add("D05", "Account, costs, cash yield and currency",
        {"registered_assumptions": copy.deepcopy(assumptions),
         "historical_account_conditions_verified": False,
         "nonzero_interest_and_fx_implemented": False},
        "No accepted effective-dated fees/taxes, cash yield, settlement or USD/CNY account/FX model.",
        "Freeze separate fictional capital cases, executable units, net costs, FX and cash returns plus adverse scenarios.",
        "Account assumptions may support a conditional study; unknown fees, cash yield and FX cannot silently become zero.")
    add("D06", "Accepted historical strategy/benchmark path",
        {"synthetic_controls": controls, "history_input_adapter_accepted": False,
         "development_101_accounts_run_on_history": False, "historical_paired_monthly_inference_run": False},
        "Synthetic full paths do not validate historical conversion, currency/cash support or registered historical scenarios.",
        "After D01-D05, accept history conversion; run all development risk accounts, frozen scenarios/windows and paired inference.",
        "Keep development/revisited history distinct from genuinely new forward evidence; no return claim from controls.")
    return {"schema_version": "1", "classification": "HISTORICAL_STUDY_PREFLIGHT_NOT_RETURN_TRIAL",
            "gate_decision": "NOT_ELIGIBLE", "historical_run_permitted": False,
            "source_conditions_preserved": source_conditions, "gates": gates,
            "input_report_fingerprints": fingerprints, "controls": controls,
            "formal_history_trials": 0,
            "limitations": ["This foundation version has no accepted historical input path",
                            "Declarations/hashes do not certify data accuracy or provider permission",
                            "Unresolved conditions are not evidence of strategy failure",
                            "A missing report differs from zero failures; optional Vibe controls may be absent"]}
