"""Account-wide performance with declared sampling and end-boundary flows.

Prices, distributions, fees and FX belong in the upstream net equity ledger.
This module never reconstructs a portfolio by discarding its cash sleeve.
"""
import math
from datetime import date
from decimal import Decimal, localcontext

from .contracts import decimal
from .evidence import ContractError, canonical_hash


VERSION = "performance-v1"


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ContractError("Metric exceeds finite reporting range")
    return number


def sample_std(values):
    if len(values) < 2:
        return None
    mean = sum(values, Decimal(0)) / len(values)
    return (sum((x - mean) ** 2 for x in values) / (len(values) - 1)).sqrt()


def metric(value=None, reason=None):
    return {"value": finite_float(value) if value is not None else None,
            "status": "DEFINED" if value is not None else "UNDEFINED", "reason": reason}


def report(ledger):
    with localcontext() as context:
        context.prec = 50
        return _report(ledger)


def _report(ledger):
    required = {"schema_version", "currency", "sampling", "calendar", "calendar_source",
                "periods_per_year", "year_day_basis", "flow_policy", "observations",
                "risk_free", "fills", "cost_events", "comparison_context"}
    if set(ledger) != required or ledger["schema_version"] != "1":
        raise ContractError("Performance ledger fields/version differ")
    for key in ("currency", "calendar_source"):
        if not isinstance(ledger[key], str) or not ledger[key].strip():
            raise ContractError(f"Explicit {key} required")
    comparison = ledger["comparison_context"]
    if not isinstance(comparison, dict) or set(comparison) != {"data", "account", "costs", "execution", "fx"} or any(
            not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v) for v in comparison.values()):
        raise ContractError("Five frozen comparison-context SHA256 identifiers required")
    frequency = {"daily_session_close": 252, "monthly_close": 12}
    if ledger["sampling"] not in frequency or ledger["periods_per_year"] != frequency[ledger["sampling"]]:
        raise ContractError("Explicit supported sampling/annualization pair required")
    if ledger["year_day_basis"] != 365 or ledger["flow_policy"] != "end_boundary_only":
        raise ContractError("ACT/365 and explicitly timed end-boundary flows required")
    observations = ledger["observations"]
    calendar = ledger["calendar"]
    if not isinstance(observations, list) or len(observations) < 2:
        raise ContractError("Opening valuation and at least one subsequent valuation required")
    if not isinstance(calendar, list) or calendar != sorted(set(calendar)):
        raise ContractError("Ordered unique explicit calendar required")
    if [o.get("date") for o in observations] != calendar:
        raise ContractError("No missing/extra observation or silently intersected calendar permitted")
    for day in calendar:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ContractError("Canonical ISO date required")
    equities, flows = [], []
    for o in observations:
        if set(o) != {"date", "equity", "external_flow_end"}:
            raise ContractError("Each valuation requires equity and explicit boundary flow")
        equities.append(decimal(o["equity"], "equity"))
        flows.append(decimal(o["external_flow_end"], "end flow"))
    if equities[0] <= 0 or flows[0] != 0:
        raise ContractError("Positive whole-account opening value; initial contribution already included")
    periods = ledger["periods_per_year"]
    rf = ledger["risk_free"]
    if rf is not None:
        if not isinstance(rf, dict) or set(rf) != {"source", "currency", "period_returns"}:
            raise ContractError("Explicit risk-free source/currency/period returns required")
        if not isinstance(rf["source"], str) or not rf["source"].strip() or rf["currency"] != ledger["currency"]:
            raise ContractError("Risk-free source and reporting currency must match")
        if not isinstance(rf["period_returns"], list) or [r.get("date") for r in rf["period_returns"]] != calendar[1:]:
            raise ContractError("Risk-free observations must exactly match return endpoints")
        for r in rf["period_returns"]:
            if set(r) != {"date", "return"} or decimal(r["return"], "risk-free return") <= -1:
                raise ContractError("Invalid risk-free period return")

    nav, returns, timeline, insolvent = Decimal(1), [], [], False
    navs = [nav]
    insolvencies = []
    for i in range(1, len(equities)):
        end_before_flow = equities[i] - flows[i]
        reason = None
        if insolvent or equities[i-1] <= 0:
            r = None
            reason = "UNDEFINED_AFTER_INSOLVENCY" if insolvent else "NONPOSITIVE_PREVIOUS_EQUITY"
        else:
            r = end_before_flow / equities[i-1] - 1
            nav *= 1 + r
            if end_before_flow <= 0:
                insolvent = True
                insolvencies.append({"date": calendar[i], "equity_before_flow": str(end_before_flow),
                                     "equity_after_flow": str(equities[i]), "return": str(r)})
            elif equities[i] < 0:
                raise ContractError("External withdrawal exceeds positive account assets")
        returns.append(r)
        navs.append(nav if r is not None else None)
        timeline.append({"date": calendar[i], "net_return": str(r) if r is not None else None,
                         "unit_nav": str(navs[-1]) if navs[-1] is not None else None, "reason": reason})
    complete = all(r is not None for r in returns)
    days = (date.fromisoformat(calendar[-1]) - date.fromisoformat(calendar[0])).days
    total = metric(navs[-1] - 1) if complete else metric(reason="UNDEFINED_AFTER_INSOLVENCY")
    cagr = metric(reason="INSOLVENCY" if insolvent else "LESS_THAN_ONE_ACT_365_YEAR")
    if days >= 365 and not insolvent and complete:
        cagr = metric((navs[-1].ln() * Decimal(365) / days).exp() - 1)
    std = sample_std(returns) if complete else None
    vol = metric(std * Decimal(periods).sqrt()) if std is not None else metric(reason="INSOLVENCY_OR_FEWER_THAN_TWO_RETURNS")
    sharpe = metric(reason="RISK_FREE_NOT_SUPPLIED")
    if rf is not None:
        excess = [r - decimal(f["return"], "risk-free") for r, f in zip(returns, rf["period_returns"])] if complete else []
        excess_std = sample_std(excess)
        if excess_std is None:
            sharpe = metric(reason="INSOLVENCY_OR_FEWER_THAN_TWO_RETURNS")
        elif excess_std == 0:
            sharpe = metric(reason="ZERO_EXCESS_RETURN_VOLATILITY")
        else:
            sharpe = metric(sum(excess) / len(excess) / excess_std * Decimal(periods).sqrt())

    peak, peak_i, maximum, max_info = Decimal(1), 0, Decimal(0), None
    episodes, active = [], None
    for i, value in enumerate(navs):
        if value is None:
            continue  # dates remain present; no ratios below claim a complete path
        if value >= peak:
            if active is not None:
                active.update(end_date=calendar[i], observations=i-active["peak_index"],
                              calendar_days=(date.fromisoformat(calendar[i])-date.fromisoformat(active["peak_date"])).days,
                              recovered=True)
                episodes.append(active); active = None
            peak, peak_i = value, i
            dd = Decimal(0)
        else:
            dd = 1 - value / peak
            if active is None:
                active = {"peak_date": calendar[peak_i], "peak_index": peak_i}
        if dd > maximum:
            maximum = dd
            max_info = {"peak_date": calendar[peak_i], "trough_date": calendar[i]}
    if active is not None:
        active.update(end_date=calendar[-1], observations=len(calendar)-1-active["peak_index"],
                      calendar_days=(date.fromisoformat(calendar[-1])-date.fromisoformat(active["peak_date"])).days,
                      recovered=False)
        episodes.append(active)
    longest = max(episodes, key=lambda e: (e["calendar_days"], e["observations"])) if episodes else None
    calmar = metric(reason="CAGR_UNDEFINED")
    if cagr["status"] == "DEFINED":
        calmar = metric(Decimal(str(cagr["value"])) / maximum) if maximum else metric(reason="ZERO_MAX_DRAWDOWN")

    fills, costs = ledger["fills"], ledger["cost_events"]
    if not isinstance(fills, list) or not isinstance(costs, list):
        raise ContractError("Explicit fills/cost event arrays required")
    notional = Decimal(0)
    fee_totals = {k: Decimal(0) for k in ("commission", "platform", "tax", "other", "fx", "fixed")}
    ids = set()
    for fill in fills:
        if set(fill) != {"id", "date", "symbol", "signed_quantity", "price", "commission", "platform", "tax", "fee"}:
            raise ContractError("Fill fields differ")
        if fill["id"] in ids or fill["date"] not in calendar[1:] or not isinstance(fill["symbol"], str) or not fill["symbol"]:
            raise ContractError("Duplicate fill or fill outside measured interval")
        ids.add(fill["id"])
        qty, price = decimal(fill["signed_quantity"], "quantity"), decimal(fill["price"], "price", 0)
        if qty == 0 or price == 0:
            raise ContractError("Nonzero quantity and positive price required")
        notional += abs(qty) * price
        amounts = {k: decimal(fill[k], k, 0) for k in ("commission", "platform", "tax")}
        if sum(amounts.values()) != decimal(fill["fee"], "fee", 0):
            raise ContractError("Fill fee does not equal its components")
        for k, amount in amounts.items():
            fee_totals[k] += amount
    for cost in costs:
        if set(cost) != {"id", "date", "category", "amount"} or cost["id"] in ids or cost["date"] not in calendar[1:]:
            raise ContractError("Duplicate or out-of-window cost event")
        if cost["category"] not in {"other", "fx", "fixed", "tax"}:
            raise ContractError("Unrecognized cost category or duplicate fill-fee category")
        ids.add(cost["id"])
        fee_totals[cost["category"]] += decimal(cost["amount"], "cost", 0)
    average_equity = sum(equities) / len(equities)
    turnover = metric(notional / average_equity) if average_equity > 0 and not insolvent else metric(reason="NONPOSITIVE_OR_INSOLVENT_ACCOUNT")
    turnover_annual = metric(Decimal(str(turnover["value"])) * 365 / days) if days >= 365 and turnover["value"] is not None else metric(reason="SHORT_WINDOW_OR_UNDEFINED_TURNOVER")
    warnings = ["Square-root annualization is a descriptive convention; serial dependence is not corrected",
                "Calendar/source correctness and upstream net-equity accounting require separate acceptance",
                "Explicit costs are disclosed, never subtracted from net equity a second time"]
    if len(returns) < periods:
        warnings.append("Fewer than one nominal year's return observations; annualized risk ratios are limited diagnostics")
    return {"version": VERSION, "classification": "ACCOUNT_LEDGER_DIAGNOSTICS_NOT_STRATEGY_ACCEPTANCE",
            "ledger_sha256": canonical_hash(ledger), "currency": ledger["currency"],
            "comparison_context": comparison,
            "sampling": ledger["sampling"], "periods_per_year": periods, "year_day_basis": 365,
            "flow_policy": ledger["flow_policy"], "calendar": calendar, "calendar_source": ledger["calendar_source"],
            "initial_equity": str(equities[0]), "final_equity": str(equities[-1]),
            "external_net_flow": str(sum(flows)), "monetary_profit": str(equities[-1]-equities[0]-sum(flows)),
            "return_observations": len(returns), "calendar_days": days, "timeline": timeline,
            "insolvencies": insolvencies, "period_net_return": total, "cagr_net": cagr,
            "volatility_annual": vol, "sharpe_annual": sharpe, "risk_free_source": rf["source"] if rf else None,
            "max_drawdown": metric(maximum) if complete else metric(reason="INCOMPLETE_AFTER_INSOLVENCY"),
            "max_drawdown_before_undefined": finite_float(maximum), "max_drawdown_dates": max_info,
            "drawdown_episodes": episodes, "longest_drawdown": longest,
            "calmar_full_window": calmar, "turnover_two_sided": turnover,
            "turnover_annual": turnover_annual, "turnover_notional": str(notional),
            "turnover_average_equity": str(average_equity), "fill_count": len(fills),
            "cost_totals": {k: str(v) for k, v in fee_totals.items()},
            "cost_total": str(sum(fee_totals.values())), "warnings": warnings}


def calibrate_risk(strategy, candidates, *, boundary_date, end_date):
    """Select only by development risk over 101 independently generated accounts.

    Never manufacture BR by scaling a completed strategy/benchmark return path.
    Callers supply ledgers from actual monthly target/fee/account simulations.
    """
    with localcontext() as context:
        context.prec = 50
        return _calibrate(strategy, candidates, boundary_date, end_date)


def _calibrate(strategy, candidates, boundary_date, end_date):
    if len(candidates) != 101 or set(candidates) != {f"{i/100:.2f}" for i in range(101)}:
        raise ContractError("All 101 predeclared k=0.00..1.00 accounts required")
    s = report(strategy)
    if s["calendar"][0] != boundary_date or s["calendar"][-1] != end_date:
        raise ContractError("Calibration must use exact resolved development boundaries")
    target_vol = s["volatility_annual"]["value"]
    rows = []
    compatible = ("currency", "sampling", "periods_per_year", "year_day_basis", "flow_policy", "calendar", "initial_equity", "comparison_context")
    for i in range(101):
        k = f"{i/100:.2f}"
        r = report(candidates[k])
        if any(r[key] != s[key] for key in compatible):
            raise ContractError("Calibration account units, capital, timing or calendar differ")
        if decimal(r["external_net_flow"], "flow") != 0 or decimal(s["external_net_flow"], "flow") != 0 or any(
                decimal(o["external_flow_end"], "flow") != 0 for o in candidates[k]["observations"] + strategy["observations"]):
            raise ContractError("Frozen calibration admits no contributions or withdrawals")
        vol = r["volatility_annual"]["value"]
        if vol is None:
            raise ContractError("A calibration account has undefined risk; retain as blocked experiment")
        gap = abs(Decimal(str(vol))-Decimal(str(target_vol))) if target_vol is not None else None
        rows.append({"k": k, "volatility_annual": vol, "absolute_risk_error": finite_float(gap) if gap is not None else None,
                     "account_sha256": r["ledger_sha256"]})
    output = {"version": VERSION, "classification": "DEVELOPMENT_ONLY_RISK_CALIBRATION",
              "boundary_date": boundary_date, "end_date": end_date, "currency": s["currency"],
              "strategy_sha256": s["ledger_sha256"], "strategy_volatility": target_vol,
              "candidates": rows, "selected_k": None, "risk_matched": False,
              "tie_policy": "risk error rounded to 1e-12 absolute annual volatility; lower k wins",
              "return_optimization": False}
    if target_vol is None or target_vol <= 0:
        return {**output, "reason": "STRATEGY_RISK_UNDEFINED_OR_ZERO"}
    best = min(rows, key=lambda r: (Decimal(str(r["absolute_risk_error"])).quantize(Decimal("1e-12")), Decimal(r["k"])))
    relative = Decimal(str(best["absolute_risk_error"])) / Decimal(str(target_vol))
    return {**output, "selected_k": best["k"], "relative_risk_error": finite_float(relative),
            "risk_matched": relative <= Decimal("0.10"),
            "reason": None if relative <= Decimal("0.10") else "RISK_ERROR_EXCEEDS_TEN_PERCENT"}
