"""Fixed-grid full-account friction reruns; no monotonic root assumption.

The caller runs and retains complete continuous S/BR accounts, including their
independent reconciliation. This module validates the resulting net ledgers,
frozen comparison scope and the one permitted dated-term perturbation.
"""
from __future__ import annotations

import copy
from decimal import Decimal, localcontext
from fractions import Fraction

from .evidence import ContractError, canonical_hash
from .performance import report as performance_report
from .trading_terms import TradingTerms

VERSION = "historical-friction/1"
DEFAULT_GRID_BP = ("0", "1", "2", "5", "10", "20", "50", "100")
CONTEXT_FIELDS = {"schema_version", "capital_cny", "frozen_k", "calendar", "comparison_context", "terms", "scenario", "fixed_expenses", "expense_review", "data_kind"}
ZERO_TOLERANCE = Decimal(0)


def _exact(value, name):
    if not isinstance(value, str):
        raise ContractError(name + ": exact decimal string required")
    try:
        number = Decimal(value)
    except Exception:
        raise ContractError(name + ": invalid decimal") from None
    if not number.is_finite():
        raise ContractError(name + ": finite decimal required")
    return number


def _text(value):
    if value == 0:
        return "0"
    return format(value, "f")


def _hash(value, label):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ContractError(label + ": SHA256 required")


def _costs(terms, context):
    return canonical_hash({"terms": terms, "scenario": context["scenario"], "fixed_expenses": context["fixed_expenses"], "expense_review": context["expense_review"]})


def _terms_at(context, bp):
    terms = copy.deepcopy(context["terms"])
    for rule in terms["rules"]:
        rate = _exact(rule["execution"]["friction_rate"], "base per-side friction")
        rule["execution"]["friction_rate"] = _text(rate + bp / Decimal(10000))
    TradingTerms(terms)
    return terms


def _context(context):
    if not isinstance(context, dict) or set(context) != CONTEXT_FIELDS or context["schema_version"] != VERSION:
        raise ContractError("exact historical-friction/1 context fields required")
    if context["scenario"] != "C0":
        raise ContractError("this friction probe requires C0; original C1-C3 matrix remains separate")
    if context["data_kind"] not in {"historical_market", "invented_control"}:
        raise ContractError("explicit historical or invented input classification required")
    if not 0 <= _exact(context["frozen_k"], "frozen k") <= 1 or _exact(context["capital_cny"], "capital") <= 0:
        raise ContractError("positive capital and frozen k in0..1 required")
    calendar = context["calendar"]
    if not isinstance(calendar, list) or len(calendar) < 3 or calendar != sorted(set(calendar)):
        raise ContractError("ordered complete continuous calendar required")
    from datetime import date
    for day in calendar:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ContractError("canonical calendar date required")
    scope = context["comparison_context"]
    if not isinstance(scope, dict) or set(scope) != {"data", "account", "costs", "execution", "fx"}:
        raise ContractError("five fixed comparison identities required")
    for key, value in scope.items():
        _hash(value, "comparison context " + key)
    TradingTerms(context["terms"])
    if scope["costs"] != _costs(context["terms"], context):
        raise ContractError("baseline cost identity is not bound to terms/scenario/expense review")
    if not isinstance(context["fixed_expenses"], list) or not isinstance(context["expense_review"], dict):
        raise ContractError("explicit model2 fixed expenses/review required")
    review = context["expense_review"]
    required_review = {"schema_version", "reviewer", "scope", "reviewed_at", "coverage_start_date", "coverage_end_date", "explicit_zero", "schedule_sha256", "evidence"}
    if set(review) != required_review or review["schema_version"] != "fixed-expense-review/1":
        raise ContractError("explicit versioned fixed-expense review required")
    if type(review["explicit_zero"]) is not bool or review["explicit_zero"] != (not context["fixed_expenses"]):
        raise ContractError("zero fixed expenses require an explicit zero declaration")
    if review["schedule_sha256"] != canonical_hash(context["fixed_expenses"]):
        raise ContractError("fixed expense review does not bind schedule")
    if not review["coverage_start_date"] <= calendar[0] <= calendar[-1] <= review["coverage_end_date"]:
        raise ContractError("fixed expense review does not cover the continuous trial")
    for day in calendar[1:]:
        matches = [r for r in context["terms"]["rules"] if r["from"] <= day <= r["through"]]
        if len(matches) != 1:
            raise ContractError("dated trading terms do not cover full continuous calendar")


def _windows(context, windows):
    if not isinstance(windows, dict) or set(windows) != {"validation", "confirmation"}:
        raise ContractError("original validation and confirmation boundaries required")
    output = {"full": (0, len(context["calendar"])-1)}
    for name, window in windows.items():
        if not isinstance(window, dict) or set(window) != {"boundary_date", "end"}:
            raise ContractError("exact window boundary_date/end fields required")
        if window["boundary_date"] not in context["calendar"] or window["end"] not in context["calendar"]:
            raise ContractError("window outside continuous source calendar")
        first, last = (context["calendar"].index(window[k]) for k in ("boundary_date", "end"))
        if first >= last:
            raise ContractError("window must include a return after its boundary")
        output[name] = (first, last)
    if output["validation"][1] != output["confirmation"][0]:
        raise ContractError("original adjacent validation/confirmation periods must share one boundary")
    return output


def _reconciliation(record):
    if not isinstance(record, dict) or set(record) != {"passed", "errors", "source_trial_ref", "account_sha256", "replay_sha256"}:
        raise ContractError("explicit full account reconciliation reference required")
    if record["passed"] is not True or record["errors"] != []:
        raise ContractError("caller account did not independently reconcile")
    if not isinstance(record["source_trial_ref"], str) or not record["source_trial_ref"].strip():
        raise ContractError("retained actual trial reference required")
    _hash(record["account_sha256"], "account evidence")
    _hash(record["replay_sha256"], "replay evidence")


def _gain(strategy, benchmark, first, last):
    growths, insolvencies = [], []
    for ledger in (strategy, benchmark):
        loss_day = None
        # Inspect the full prefix, including development. A window boundary
        # cannot resurrect an earlier insolvent continuous account.
        for observation in ledger["observations"][:last+1]:
            if _exact(observation["equity"], "equity") <= 0:
                loss_day = loss_day or observation["date"]
        insolvencies.append(loss_day)
        begin = Fraction(_exact(ledger["observations"][first]["equity"], "boundary equity"))
        end = Fraction(_exact(ledger["observations"][last]["equity"], "end equity"))
        growths.append(end / begin if begin > 0 and end > 0 else None)
    if any(insolvencies):
        return {"status": "UNDEFINED_NONPOSITIVE_EQUITY", "log_gain": None, "sign": None,
                "strategy_first_nonpositive_date": insolvencies[0], "benchmark_first_nonpositive_date": insolvencies[1]}
    relative = growths[0]/growths[1]
    logs = [(Decimal(g.numerator)/Decimal(g.denominator)).ln() for g in growths]
    gain = (Decimal(relative.numerator)/Decimal(relative.denominator)).ln()
    # Sign comes from the exact endpoint ratio, not a numerical zero band.
    # A log below registered Decimal precision can display0 while its economic
    # sign remains explicit and its rational ratio is retained.
    return {"status": "DEFINED", "strategy_log_growth": _text(logs[0]), "benchmark_log_growth": _text(logs[1]),
            "log_gain": _text(gain), "relative_growth_exact_fraction": str(relative),
            "log_gain_rounded_to_zero": gain == 0 and relative != 1,
            "sign": "ZERO" if relative == 1 else "POSITIVE" if relative > 1 else "NEGATIVE"}


def _trial(context, windows, bp, supplied):
    if not isinstance(supplied, dict) or set(supplied) != {"extra_per_side_bp", "frozen_k", "terms", "accounts"}:
        raise ContractError("exact full continuous trial fields required")
    if _exact(supplied["extra_per_side_bp"], "trial bp") != bp or supplied["frozen_k"] != context["frozen_k"]:
        raise ContractError("trial changed requested friction or frozen k")
    expected_terms = _terms_at(context, bp)
    # Numeric equality permits formatting of the sole perturbed rate. Every
    # remaining field, including fee versions/knowledge timestamps, is frozen.
    actual_terms = copy.deepcopy(supplied["terms"])
    for actual, expected in zip(actual_terms["rules"], expected_terms["rules"]):
        if _exact(actual["execution"]["friction_rate"], "trial friction") != _exact(expected["execution"]["friction_rate"], "expected friction"):
            raise ContractError("trial per-side rate differs from frozen base plus bp")
        actual["execution"]["friction_rate"] = expected["execution"]["friction_rate"]
    if actual_terms != expected_terms:
        raise ContractError("trial changed fields outside per-side execution friction")
    if set(supplied["accounts"]) != {"S", "BR"}:
        raise ContractError("complete S and frozen-BR account pair required")
    expected_scope = {**context["comparison_context"], "costs": _costs(supplied["terms"], context)}
    ledgers, evidence = {}, {}
    for variant, account in supplied["accounts"].items():
        if not isinstance(account, dict) or set(account) != {"ledger", "reconciliation"}:
            raise ContractError("full ledger and independent account evidence required")
        _reconciliation(account["reconciliation"])
        ledger = account["ledger"]
        performance_report(ledger)  # validates exact fields, fills, returns and insolvency clocks
        if ledger["calendar"] != context["calendar"] or ledger["currency"] != "CNY" or ledger["sampling"] != "daily_session_close":
            raise ContractError("trial omitted/changed continuous CNY session calendar")
        if _exact(ledger["observations"][0]["equity"], "opening capital") != _exact(context["capital_cny"], "frozen capital"):
            raise ContractError("trial changed whole-account opening capital")
        if any(_exact(o["external_flow_end"], "flow") != 0 for o in ledger["observations"]):
            raise ContractError("friction rerun cannot add capital, reset or withdraw at a window boundary")
        if ledger["comparison_context"] != expected_scope:
            raise ContractError("trial changed context beyond bound execution-cost rate")
        ledgers[variant] = ledger
        evidence[variant] = {**account["reconciliation"], "ledger_sha256": canonical_hash(ledger),
                             "fill_count": len(ledger["fills"]), "cost_event_count": len(ledger["cost_events"]),
                             "fill_quantities_sha256": canonical_hash([{k:f[k] for k in ("date", "symbol", "signed_quantity")} for f in ledger["fills"]])}
    # Equal accounting contexts alone do not allow changing calendar-time cash
    # rates; the net risk-free cash background must also be shared/frozen.
    if ledgers["S"]["risk_free"] != ledgers["BR"]["risk_free"]:
        raise ContractError("S/BR cash background differs within trial")
    return {"extra_per_side_bp": _text(bp), "status": "VALIDATED_FULL_ACCOUNT_PAIR", "evidence": evidence,
            "terms_sha256": canonical_hash(supplied["terms"]), "comparison_context": expected_scope,
            "gains": {name: _gain(ledgers["S"], ledgers["BR"], first, last) for name,(first,last) in windows.items()}}, ledgers


def _approximation(ledgers, first, last, gain):
    variants = {}
    if gain["status"] != "DEFINED":
        return {"status": "UNDEFINED_BASELINE", "formal_threshold": False}
    for variant, ledger in ledgers.items():
        days = set(ledger["calendar"][first+1:last+1])
        fills = [f for f in ledger["fills"] if f["date"] in days]
        notional = sum((abs(_exact(f["signed_quantity"], "fill quantity"))*_exact(f["price"], "fill price") for f in fills), Decimal(0))
        terminal = _exact(ledger["observations"][last]["equity"], "terminal NAV")
        variants[variant] = {"fill_count": len(fills), "one_side_notional_cny": _text(notional),
                            "one_bp_frozen_path_cash_cost_cny": _text(notional/Decimal(10000)),
                            "one_bp_average_cost_per_fill_cny": _text(notional/Decimal(10000)/len(fills)) if fills else None,
                            "terminal_cny": _text(terminal)}
    derivative = Decimal(variants["S"]["one_side_notional_cny"])/Decimal(variants["S"]["terminal_cny"])-Decimal(variants["BR"]["one_side_notional_cny"])/Decimal(variants["BR"]["terminal_cny"])
    candidate = _exact(gain["log_gain"], "baseline gain") * Decimal(10000)/derivative if derivative > 0 and gain["sign"] == "POSITIVE" and not gain["log_gain_rounded_to_zero"] else None
    return {"status": "DISCLOSURE_ONLY", "formal_threshold": False, "variants": variants,
            "linear_approximation_bp": _text(candidate) if candidate is not None else None,
            "reason": None if candidate is not None else "NO_POSITIVE_BASELINE_OR_NO_ADDITIONAL_RELATIVE_TURNOVER_COST",
            "method": "baseline log gain / (S notional / S terminal NAV - BR notional / BR terminal NAV) * 10000",
            "limitations": "Frozen baseline fills and terminal wealth only; ignores integer-share changes, future compounding, missed orders and rerun path changes. Never a formal zero-gain threshold."}


def _threshold(rows, name):
    samples = [{"extra_per_side_bp":r["extra_per_side_bp"], **r["gains"][name]} if r["status"] == "VALIDATED_FULL_ACCOUNT_PAIR" else
               {"extra_per_side_bp":r["extra_per_side_bp"], "status":r["status"], "log_gain":None, "sign":None} for r in rows]
    crossings, reentries = [], []
    undefined = [s["extra_per_side_bp"] for s in samples if s["status"] != "DEFINED"]
    for left,right in zip(samples,samples[1:]):
        if left["status"] == right["status"] == "DEFINED":
            interval = {"lower_bp":left["extra_per_side_bp"], "upper_bp":right["extra_per_side_bp"],
                        "lower_log_gain":left["log_gain"], "upper_log_gain":right["log_gain"],
                        "lower_relative_growth_exact_fraction":left["relative_growth_exact_fraction"],
                        "upper_relative_growth_exact_fraction":right["relative_growth_exact_fraction"]}
            if left["sign"] == "POSITIVE" and right["sign"] in {"ZERO", "NEGATIVE"}: crossings.append(interval)
            if left["sign"] in {"ZERO", "NEGATIVE"} and right["sign"] == "POSITIVE": reentries.append(interval)
    baseline_positive = samples[0]["status"] == "DEFINED" and samples[0]["sign"] == "POSITIVE"
    first_crossing = crossings[0] if baseline_positive and crossings else None
    if first_crossing is not None:
        upper_index = next(i for i,s in enumerate(samples) if s["extra_per_side_bp"] == first_crossing["upper_bp"])
        if any(s["status"] != "DEFINED" for s in samples[:upper_index+1]):
            first_crossing = None
    valid = [Fraction(s["relative_growth_exact_fraction"]) for s in samples if s["status"] == "DEFINED"]
    monotone = all(b <= a for a,b in zip(valid,valid[1:]))
    return {"baseline_positive":baseline_positive, "first_observed_nonpositive_interval":first_crossing,
            "all_observed_positive_to_nonpositive_intervals":crossings, "reentry_positive_intervals":reentries,
            "zero_candidates_bp":[s["extra_per_side_bp"] for s in samples if s["sign"] == "ZERO"],
            "undefined_candidates_bp":undefined, "observed_nonincreasing":monotone,
            "status":"BASELINE_NOT_POSITIVE" if not baseline_positive else "INCOMPLETE_OR_INSOLVENT_SCAN" if undefined else
                     "OBSERVED_BRACKET_WITH_REENTRY" if crossings and reentries else "OBSERVED_GRID_BRACKET" if crossings else "NOT_FOUND_ON_SCANNED_GRID",
            "complete_defined_scan":not undefined,
            "global_unique_root_claim":False, "samples":samples}


def scan_friction(run_trial, *, context, windows, grid_bp=DEFAULT_GRID_BP):
    """Call every predeclared candidate once; retain invalid attempts as errors.

    Reconciliation references are checked for structure. Their hashes do not by
    themselves prove the caller ran the native account; source artifacts and
    actual independent replay are retained/verified by the owning study runner.
    """
    frozen = copy.deepcopy(context)
    _context(frozen)
    indices = _windows(frozen, windows)
    if not isinstance(grid_bp, (list,tuple)) or not grid_bp:
        raise ContractError("predeclared nonempty fixed grid required")
    grid = [_exact(v,"grid bp") for v in grid_bp]
    if grid != sorted(set(grid)) or grid[0] != 0 or any(v < 0 for v in grid):
        raise ContractError("grid must ascend uniquely from baseline0bp")
    rows, baseline_ledgers, background = [], None, None
    with localcontext() as precision:
        precision.prec = 50
        for bp in grid:
            try:
                supplied = run_trial(_text(bp))
                row, ledgers = _trial(frozen, indices, bp, supplied)
                if background is not None and ledgers["S"]["risk_free"] != background:
                    raise ContractError("friction trial changed frozen cash background returns")
                background = copy.deepcopy(ledgers["S"]["risk_free"])
                if bp == 0: baseline_ledgers = copy.deepcopy(ledgers)
                row["fill_quantity_path_changed_vs_baseline"] = {variant:row["evidence"][variant]["fill_quantities_sha256"] !=
                    rows[0]["evidence"][variant]["fill_quantities_sha256"] for variant in ("S", "BR")} if bp != 0 and rows[0]["status"] == "VALIDATED_FULL_ACCOUNT_PAIR" else {"S":False,"BR":False} if bp == 0 else None
            except Exception as error:
                row = {"extra_per_side_bp":_text(bp), "status":"TRIAL_ERROR", "error":{"type":type(error).__name__, "reason":str(error)},
                       "economic_failure":False}
            rows.append(row)
        baseline = rows[0]
        approximations = {name:_approximation(baseline_ledgers, first,last,baseline["gains"][name]) if baseline_ledgers is not None else
                          {"status":"BASELINE_UNAVAILABLE", "formal_threshold":False} for name,(first,last) in indices.items()}
        result = {"schema_version":VERSION, "classification":"FINITE_NONMONOTONE_FULL_ACCOUNT_FRICTION_SCAN", "data_kind":frozen["data_kind"],
                  "context_sha256":canonical_hash(frozen), "frozen_k":frozen["frozen_k"], "capital_cny":frozen["capital_cny"], "scenario":"C0",
                  "grid_bp":[_text(v) for v in grid], "intended_trial_count":len(grid), "attempted_trial_count":len(rows),
                  "validated_trial_count":sum(r["status"] == "VALIDATED_FULL_ACCOUNT_PAIR" for r in rows),
                  "all_trials_validated":all(r["status"] == "VALIDATED_FULL_ACCOUNT_PAIR" for r in rows),
                  "decimal_precision":50, "log_zero_tolerance":_text(ZERO_TOLERANCE),
                  "minimum_scanned_spacing_bp":_text(min(b-a for a,b in zip(grid,grid[1:]))) if len(grid)>1 else None,
                  "trials":rows, "thresholds":{name:_threshold(rows,name) for name in indices}, "baseline_linear_approximation":approximations,
                  "retuned_k":False, "global_unique_root_claim":False, "module_network_calls":0,
                  "limitations":["Finite fixed grid only; intervals can contain share/path jumps and multiple crossings",
                                 "No interpolation, monotonic bisection, strategy selection or out-of-grid tolerance claim",
                                 "Caller must retain every attempted actual full account and independent replay",
                                 "Format-valid reconciliation hashes are evidence references, not authentication of native execution"]}
    return result
