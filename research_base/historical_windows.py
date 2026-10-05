"""Continuous whole-account window diagnostics; no portfolio resets or exits."""
import copy
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, localcontext
from fractions import Fraction

from .evidence import ContractError, canonical_hash
from .historical_account import performance_input
from .historical_contract import validate_account_input
from .historical_replay import independent_replay, _Replay, ReplayContractError
from .performance import report as performance_report, metric

VERSION = "historical-windows/1"


def _f(value):
    if isinstance(value, (bool, float)):
        raise ContractError("Exact monetary string/integer required")
    try:
        return Fraction(value)
    except (ValueError, TypeError, ZeroDivisionError):
        raise ContractError("Invalid exact monetary value") from None


def _decimal(value):
    with localcontext() as context:
        context.prec = 50
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def _components(snapshot):
    usd = _f(snapshot["usd_total_equity"])
    fx = _f(snapshot["fx_mark_cny_per_usd"])
    # Use net whole-account sources, including any expense liabilities. Gross
    # wallet settled/receivable fields must never erase those liabilities.
    domestic = _f(snapshot["marked_equity_cny"]) - fx * usd
    if fx <= 0:
        raise ContractError("Positive valuation FX required")
    return domestic, usd, fx


def _verify_background(data, result):
    """Reuse the independent Fraction oracle on an isolated CNY-only wallet.

    No production CashAccount/rate/rounding helper is used. Main replay's cash
    methods are private but deliberately guarded by actual journal controls.
    """
    probe = _Replay(data, result, "0.00000001")
    events = result["cny_cash_background_events"]
    probe._sequences(events, "CNY benchmark")
    snapshots = [result["opening_cny_snapshot"]] + result["cny_cash_background_snapshots"]
    state = {"settled": {"CNY": Fraction(0), "USD": Fraction(0)},
             "receivables": {"CNY": Fraction(0), "USD": Fraction(0)},
             "unpaid": {"CNY": Fraction(0), "USD": Fraction(0)},
             "last_day": {"CNY": None, "USD": None}, "at": None}
    cursor = 0
    for snapshot in snapshots:
        at = datetime.fromisoformat(snapshot["as_of"])
        while cursor < len(events) and datetime.fromisoformat(events[cursor]["at"]) <= at:
            event = events[cursor]
            if event["type"] not in {"FUNDING", "CASH_INTEREST"} or (event["type"] == "FUNDING" and cursor != 0):
                raise ContractError("Unconverted CNY benchmark admits only initial funding and interest")
            if state["at"] is not None and datetime.fromisoformat(event["at"]) < state["at"]:
                raise ContractError("Background event clock reversed")
            probe.check("benchmark knowledge clock", event.get("knowledge_clock"), probe.clock)
            state["at"] = datetime.fromisoformat(event["at"])
            probe._wallet_event(state, event)
            cursor += 1
        for currency in ("CNY", "USD"):
            for key, source in (("settled", "settled"), ("receivables", "receivables"), ("unpaid_interest", "unpaid")):
                probe.check("benchmark " + snapshot["date"] + currency + key,
                            snapshot["wallets"][currency][key], state[source][currency], money=True)
            if snapshot is not snapshots[0]:
                probe.check("benchmark complete accrual " + currency, str(state["last_day"][currency]), snapshot["date"])
        expected = state["settled"]["CNY"] + state["unpaid"]["CNY"]
        probe.check("benchmark CNY NAV " + snapshot["date"], snapshot["marked_equity_cny"], expected, money=True)
        probe.check("benchmark no USD asset", snapshot["usd_total_equity"], Fraction(0), money=True)
    if cursor != len(events) or probe.errors:
        raise ContractError("CNY risk-free background rejected by Fraction replay: " + str(probe.errors))
    return {"method": "isolated unconverted CNY background using independent Fraction funding/interest oracle",
            "passed": True, "checks": probe.checks, "events_sha256": canonical_hash(events),
            "snapshots_sha256": canonical_hash(snapshots)}


def _usd_ledger(cny, snapshots, background):
    ledger = copy.deepcopy(cny)
    ledger["currency"] = "USD"
    rates = {s["date"]: _f(s["fx_mark_cny_per_usd"]) for s in snapshots}
    for observation in ledger["observations"]:
        observation["equity"] = _decimal(_f(observation["equity"]) / rates[observation["date"]])
    previous = None
    returns = []
    for snapshot in background:
        current = _f(snapshot["marked_equity_cny"]) / rates[snapshot["date"]]
        if previous is not None:
            returns.append({"date": snapshot["date"], "return": _decimal(current / previous - 1)})
        previous = current
    ledger["risk_free"] = {"currency": "USD", "source":
        "same unconverted CNY cash benchmark valued in USD at each bound FX mark; no gifted USD interest rate",
        "period_returns": returns}
    # Disclosure only; NAV already includes costs. Never deduct these again.
    for fill in ledger["fills"]:
        rate = rates[fill["date"]]
        for key in ("price", "commission", "platform", "tax", "fee"):
            fill[key] = _decimal(_f(fill[key]) / rate)
        # Preserve exact component sum after finite decimal reporting conversion.
        with localcontext() as context:
            context.prec = 50
            fill["fee"] = str(sum((Decimal(fill[k]) for k in ("commission", "platform", "tax")), Decimal(0)))
    for cost in ledger["cost_events"]:
        cost["amount"] = _decimal(_f(cost["amount"]) / rates[cost["date"]])
    return ledger


def _slice(ledger, indices):
    output = copy.deepcopy(ledger)
    first, last = indices
    output["calendar"] = ledger["calendar"][first:last + 1]
    output["observations"] = ledger["observations"][first:last + 1]
    endpoints = set(output["calendar"][1:])
    for key in ("fills", "cost_events"):
        output[key] = [row for row in ledger[key] if row["date"] in endpoints]
    output["risk_free"]["period_returns"] = [row for row in ledger["risk_free"]["period_returns"] if row["date"] in endpoints]
    return output


def _currency_record(full, full_metrics, first, last):
    ledger = _slice(full, (first, last))
    boundary = full["calendar"][first]
    history = [row for row in full_metrics["insolvencies"] if row["date"] <= boundary]
    if not history:
        metrics = performance_report(ledger)
        state = "WINDOW_INSOLVENCY" if metrics["insolvencies"] else "NORMAL"
    else:
        # The original positive opening NAV is retained for cost disclosures.
        # The continuous prefix is actual evidence, never a fabricated positive
        # opening value for this bankrupt window. Costs are disclosure only.
        prefix = _slice(full, (0, last))
        prefix["fills"], prefix["cost_events"] = ledger["fills"], ledger["cost_events"]
        metrics = performance_report(prefix)
        equities = [_f(row["equity"]) for row in ledger["observations"]]
        flows = sum((_f(row["external_flow_end"]) for row in ledger["observations"]), Fraction(0))
        metrics.update(classification="ACCOUNT_WINDOW_INHERITED_INSOLVENCY",
                       ledger_sha256=canonical_hash(ledger), calendar=ledger["calendar"],
                       initial_equity=_decimal(equities[0]), final_equity=_decimal(equities[-1]),
                       external_net_flow=_decimal(flows), monetary_profit=_decimal(equities[-1] - equities[0] - flows),
                       return_observations=len(equities) - 1,
                       calendar_days=(date.fromisoformat(ledger["calendar"][-1]) - date.fromisoformat(boundary)).days,
                       timeline=[{"date": day, "net_return": None, "unit_nav": None, "reason": "INHERITED_INSOLVENCY"}
                                 for day in ledger["calendar"][1:]],
                       insolvencies=copy.deepcopy(history), max_drawdown_before_undefined=None,
                       max_drawdown_dates=None, drawdown_episodes=[], longest_drawdown=None,
                       turnover_average_equity=_decimal(sum(equities) / len(equities)))
        for key, value in list(metrics.items()):
            if isinstance(value, dict) and "status" in value and "value" in value:
                metrics[key] = metric(reason="INHERITED_INSOLVENCY")
        metrics["warnings"].append("Earlier continuous insolvency is inherited; later positive NAV never revives return statistics")
        state = "INHERITED_INSOLVENCY"
    undefined = [key for key, value in metrics.items() if isinstance(value, dict) and value.get("status") == "UNDEFINED"]
    return {"ledger": ledger, "metrics": metrics, "state": state,
            "prior_insolvency_history": copy.deepcopy(history),
            "continuous_ledger_sha256": canonical_hash(full),
            "metric_completeness": "INHERITED_INSOLVENCY" if history else "PARTIALLY_DEFINED" if undefined else "DEFINED",
            "undefined_metrics": undefined}


def _decomposition(snapshots):
    rows = []
    totals = {key: Fraction(0) for key in ("domestic_cny_change", "usd_asset_change_at_prior_fx", "fx_change_on_prior_usd", "cross_term", "cny_change", "reported_nav_rounding_residual")}
    for previous, current in zip(snapshots, snapshots[1:]):
        c0, u0, x0 = _components(previous)
        c1, u1, x1 = _components(current)
        values = {"domestic_cny_change": c1 - c0, "usd_asset_change_at_prior_fx": x0 * (u1 - u0),
                  "fx_change_on_prior_usd": u0 * (x1 - x0), "cross_term": (u1 - u0) * (x1 - x0)}
        values["cny_change"] = c1 + x1 * u1 - c0 - x0 * u0
        if sum(values[key] for key in list(values)[:4]) != values["cny_change"]:
            raise ContractError("Exact FX identity failed")
        values["reported_nav_rounding_residual"] = _f(current["marked_equity_cny"]) - _f(previous["marked_equity_cny"]) - values["cny_change"]
        for key, value in values.items():
            totals[key] += value
        rows.append({"from_date": previous["date"], "date": current["date"],
                     "exact_fractions": {key: str(value) for key, value in values.items()}})
    return {"basis": "domestic net CNY = reconciled CNY NAV - FX * net USD total; both currency components include liabilities",
            "rows": rows, "totals_exact_fractions": {key: str(value) for key, value in totals.items()},
            "rounding_residual_policy": "retained explicitly; input replay tolerance still applies"}


def _diagnostics(data, result, start, end):
    events = [e for e in result["events"] if start <= e["date"] <= end]
    count = Counter(e["type"] for e in events)
    cancellations = Counter()
    for event in events:
        if event["type"] == "ORDER_CANCEL":
            reasons = event["reason"].split("|")
        elif event["type"] == "ORDER_REMAINDER_CANCEL":
            reasons = event["reasons"]
        else:
            continue
        if not isinstance(reasons, list) or not reasons or any(not isinstance(reason, str) or not reason for reason in reasons):
            raise ContractError("Cancellation must retain explicit reason components")
        cancellations.update(reasons)
    exposure = []
    intents = sorted(data["control_intents"], key=lambda p: p["execution_date"])
    for snap in result["cny_snapshots"]:
        if not start <= snap["date"] <= end:
            continue
        active = [intent for intent in intents if intent["execution_date"] <= snap["date"]]
        target = sum((_f(w) for w in active[-1]["weights"].values()), Fraction(0)) if active else Fraction(0)
        nav = _f(snap["marked_equity_cny"])
        actual = _f(snap["usd_external_assets"]) * _f(snap["fx_mark_cny_per_usd"]) / nav if nav > 0 else None
        exposure.append({"date": snap["date"], "target_risky_weight": str(target),
                         "actual_risky_weight": str(actual) if actual is not None else None,
                         "positive_underallocation": str(max(Fraction(0), target - actual)) if actual is not None else None})
    means = {}
    for key in ("actual_risky_weight", "positive_underallocation"):
        values = [_f(row[key]) for row in exposure if row[key] is not None]
        mean = sum(values, Fraction(0)) / len(values) if len(values) == len(exposure) and values else None
        means[key] = {"value": _decimal(mean) if mean is not None else None,
                      "value_exact_fraction": str(mean) if mean is not None else None,
                      "status": "DEFINED" if len(values) == len(exposure) and values else "UNDEFINED_NONPOSITIVE_NAV"}
    friction, withholding = Fraction(0), Fraction(0)
    marks = data["historical_model"]["fx"]["close_marks"]
    for event in events:
        rate = _f(marks[event["date"]]["cny_per_usd"])
        if event["type"] == "FILL":
            friction += abs(_f(event["price"]) - _f(event["raw_open"])) * event["qty"] * rate
        if event["type"] == "DIV_EX":
            withholding += _f(event["gross_per_share"]) * event["entitled_qty"] * _f(event["withholding_rate"]) * rate
    return {"event_counts": dict(sorted(count.items())), "cancellations_by_reason": dict(sorted(cancellations.items())),
            "requested_shares": sum(e["requested_qty"] for e in events if e["type"] == "ORDER_ATTEMPT"),
            "filled_shares": sum(e["qty"] for e in events if e["type"] == "FILL"),
            "exposure": exposure, "session_mean_exposure": means,
            "underallocation_basis": "positive target minus actual close stock weight; includes integer/cash/capacity constraints and market drift",
            "additional_cost_disclosures_cny_exact": {"friction": str(friction), "dividend_withholding": str(withholding)},
            "cost_policy": "metrics fees/FX plus separate friction/withholding disclosures; all already in NAV, never rededuct"}


def report_windows(data, result, windows):
    """Report resolved {name:{start,end}} session intervals on one continuous run."""
    validate_account_input(data)
    if result.get("data_kind") != data["data_kind"] or result.get("historical_engine_ready") is not False:
        raise ContractError("Source label or readiness differs")
    replay = independent_replay(data, result)
    if replay.get("passed") is not True:
        raise ContractError("Continuous account rejected by independent replay: " + str(replay.get("errors")))
    try:
        background_replay = _verify_background(data, result)
    except (ReplayContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError("Incomplete/invalid CNY background: " + str(error)) from None
    if not isinstance(windows, dict) or not windows:
        raise ContractError("Explicit nonempty resolved windows required")
    cny = performance_input(data, result)
    snapshots = [result["opening_cny_snapshot"]] + result["cny_snapshots"]
    background = [result["opening_cny_snapshot"]] + result["cny_cash_background_snapshots"]
    calendar = cny["calendar"]
    if [s["date"] for s in background] != calendar:
        raise ContractError("Complete aligned CNY benchmark calendar required")
    usd = _usd_ledger(cny, snapshots, background)
    continuous_ledgers = {"CNY": cny, "USD": usd}
    continuous_metrics = {currency: performance_report(ledger) for currency, ledger in continuous_ledgers.items()}
    outputs = {}
    for name, window in windows.items():
        if not isinstance(name, str) or not name or not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise ContractError("Window name and exact start/end fields required")
        start, end = window["start"], window["end"]
        for day in (start, end):
            try:
                canonical = isinstance(day, str) and date.fromisoformat(day).isoformat() == day
            except ValueError:
                canonical = False
            if not canonical or day not in calendar[1:]:
                raise ContractError("Resolved window endpoints must be included execution sessions")
        first, last = calendar.index(start) - 1, calendar.index(end)
        if first >= last:
            raise ContractError("Window start follows end")
        currencies = {}
        for currency, full in (("CNY", cny), ("USD", usd)):
            currencies[currency] = _currency_record(full, continuous_metrics[currency], first, last)
        outputs[name] = {"start": start, "end": end, "boundary_date": calendar[first],
                         "boundary_snapshot_sha256": canonical_hash(snapshots[first]),
                         "end_snapshot_sha256": canonical_hash(snapshots[last]), "currencies": currencies,
                         "fx_decomposition": _decomposition(snapshots[first:last + 1]),
                         "execution_diagnostics": _diagnostics(data, result, start, end)}
    return {"schema_version": VERSION, "classification": "CONTINUOUS_ACCOUNT_WINDOW_DIAGNOSTICS",
            "data_kind": data["data_kind"], "historical_engine_ready": False, "goal_complete": False,
            "input_sha256": canonical_hash(data), "result_sha256": canonical_hash(result),
            "windows_sha256": canonical_hash(windows), "continuous_calendar_sha256": canonical_hash(calendar),
            "lineage": copy.deepcopy(data["lineage"]), "independent_replay": replay, "windows": outputs,
            "background_replay": background_replay,
            "continuous_ledgers": continuous_ledgers,
            "continuous_ledger_sha256": {currency: canonical_hash(ledger) for currency, ledger in continuous_ledgers.items()},
            "continuous_insolvency_history": {currency: copy.deepcopy(metrics["insolvencies"]) for currency, metrics in continuous_metrics.items()},
            "policy": "preceding session boundary; no reset, free liquidation, sleeve-only USD, or second cost deduction",
            "usd_reporting_precision": "50 decimal significant digits; exact decomposition fractions retained"}


def compare_windows(strategy, benchmark):
    """S versus independently executed BR: exact calendars, no intersection."""
    if (strategy.get("schema_version") != VERSION or benchmark.get("schema_version") != VERSION
            or strategy.get("data_kind") != benchmark.get("data_kind")
            or strategy.get("continuous_calendar_sha256") != benchmark.get("continuous_calendar_sha256")
            or set(strategy["windows"]) != set(benchmark["windows"])):
        raise ContractError("Window report sources/calendars/inventory differ")
    bound_metrics = {}
    for source in (strategy, benchmark):
        metrics_by_currency = {}
        for currency in ("CNY", "USD"):
            full = source["continuous_ledgers"][currency]
            if (canonical_hash(full) != source["continuous_ledger_sha256"][currency]
                    or canonical_hash(full["calendar"]) != source["continuous_calendar_sha256"]):
                raise ContractError("Continuous window ledger/hash binding differs")
            metrics_by_currency[currency] = performance_report(full)
            if canonical_hash(metrics_by_currency[currency]["insolvencies"]) != canonical_hash(source["continuous_insolvency_history"][currency]):
                raise ContractError("Continuous bankruptcy history differs")
        bound_metrics[id(source)] = metrics_by_currency
    output = {}
    for name, window in strategy["windows"].items():
        comparisons = {}
        for currency in ("CNY", "USD"):
            s = window["currencies"][currency]["metrics"]
            b = benchmark["windows"][name]["currencies"][currency]["metrics"]
            # Different actual boundary NAVs after development are allowed.
            if s["calendar"] != b["calendar"] or s["comparison_context"] != b["comparison_context"]:
                raise ContractError("Comparison requires aligned complete calendars and frozen contexts")
            for source, record in ((strategy, window["currencies"][currency]),
                                   (benchmark, benchmark["windows"][name]["currencies"][currency])):
                full = source["continuous_ledgers"][currency]
                measured = source["windows"][name]
                try:
                    first, last = full["calendar"].index(measured["boundary_date"]), full["calendar"].index(measured["end"])
                except ValueError:
                    raise ContractError("Window boundaries differ from bound continuous ledger") from None
                if (first >= last or full["calendar"][first + 1] != measured["start"] or
                        canonical_hash(_currency_record(full, bound_metrics[id(source)][currency], first, last)) != canonical_hash(record)):
                    raise ContractError("Window state/metrics differ from bound continuous ledger")
            sr, br = [row["net_return"] for row in s["timeline"]], [row["net_return"] for row in b["timeline"]]
            beta, gap, matched = None, None, False
            reason = "INCOMPLETE_OR_FEWER_THAN_TWO_RETURNS"
            inherited = any(record["state"] == "INHERITED_INSOLVENCY" for record in
                            (window["currencies"][currency], benchmark["windows"][name]["currencies"][currency]))
            if inherited:
                reason = "INHERITED_INSOLVENCY"
            if not inherited and len(sr) >= 2 and all(value is not None for value in sr + br):
                xs, ys = list(map(_f, sr)), list(map(_f, br))
                xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
                denominator = sum((y - ym) ** 2 for y in ys)
                strategy_variance = sum((x - xm) ** 2 for x in xs)
                if strategy_variance:
                    variance_ratio = denominator / strategy_variance
                    # Identical annualization cancels. Compare exact variances
                    # to squared 0.9/1.1 bounds; rounded float sigma cannot move
                    # the inclusive 10% gate.
                    matched = Fraction(81, 100) <= variance_ratio <= Fraction(121, 100)
                    with localcontext() as context:
                        context.prec = 50
                        gap = abs((Decimal(variance_ratio.numerator) / Decimal(variance_ratio.denominator)).sqrt() - 1)
                if denominator:
                    beta = _decimal(sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / denominator)
                    reason = None
                else:
                    reason = "ZERO_BR_RETURN_VARIANCE"
            comparisons[currency] = {"relative_volatility_gap": str(gap) if gap is not None else None,
                                     "risk_matched": matched,
                                     "risk_status": "INHERITED_INSOLVENCY" if inherited else "DEFINED" if gap is not None else "UNDEFINED_STRATEGY_RISK_OR_OBSERVATIONS",
                                     "beta_s_vs_br": {"value": beta, "status": "DEFINED" if beta is not None else "UNDEFINED", "reason": reason}}
        output[name] = comparisons
    return {"schema_version": VERSION, "strategy_report_sha256": canonical_hash(strategy),
            "br_report_sha256": canonical_hash(benchmark), "threshold": "0.10", "windows": output,
            "historical_engine_ready": False, "classification": "ALIGNED_WINDOW_COMPARISON_DIAGNOSTICS"}
