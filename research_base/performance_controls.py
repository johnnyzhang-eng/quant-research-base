"""Predeclared synthetic metric controls, not historical strategy results."""
import copy
import math
from decimal import Decimal
from fractions import Fraction

from .evidence import ContractError, canonical_hash, write_json
from .performance import calibrate_risk, report


def fixture(values, dates=None, flows=None, rf=None):
    dates = dates or [f"2024-10-{7+i:02d}" for i in range(len(values))]
    flows = flows or ["0"] * len(values)
    return {"schema_version": "1", "currency": "USD", "sampling": "daily_session_close",
            "calendar": dates, "calendar_source": "invented synthetic metric calendar, not real market dates",
            "periods_per_year": 252, "year_day_basis": 365, "flow_policy": "end_boundary_only",
            "observations": [{"date": d, "equity": str(v), "external_flow_end": str(f)} for d, v, f in zip(dates, values, flows)],
            "risk_free": {"source": "declared synthetic period returns", "currency": "USD",
                          "period_returns": [{"date": d, "return": str(v)} for d, v in zip(dates[1:], rf)]} if rf is not None else None,
            "fills": [], "cost_events": [],
            "comparison_context": {k: canonical_hash({"synthetic_fixture": k})
                                   for k in ("data", "account", "costs", "execution", "fx")}}


def cases():
    out = []
    def add(cid, data, expected):
        out.append((cid, data, expected))
    add("M01_cash_in_denominator", fixture([100, 150]), {"period_net_return.value": .5, "cagr_net.value": None})
    d = fixture([100, 98]); d["fills"] = [{"id": "fill-1", "date": "2024-10-08", "symbol": "SPY",
        "signed_quantity": "5", "price": "10", "commission": "2", "platform": "0", "tax": "0", "fee": "2"}]
    add("M02_net_equity_no_double_fee", d, {"period_net_return.value": -.02, "cost_total": "2", "monetary_profit": "-2",
                                          "turnover_two_sided.value": float(Fraction(50, 99))})
    add("M03_deposit_is_not_profit", fixture([100, 250, 275], flows=[0, 150, 0]),
        {"period_net_return.value": .1, "monetary_profit": "25", "external_net_flow": "150"})
    add("M04_withdrawal_is_not_loss", fixture([100, 50, 45], flows=[0, -50, 0]),
        {"period_net_return.value": -.1, "monetary_profit": "-5"})
    add("M05_flat_zero_risk", fixture([100, 100, 100], rf=[0, 0]),
        {"sharpe_annual.value": None, "sharpe_annual.reason": "ZERO_EXCESS_RETURN_VOLATILITY", "max_drawdown.value": 0})
    dates = ["2023-01-01", "2023-01-02", "2023-01-03", "2024-01-01"]
    add("M06_cagr_drawdown_and_excess_std", fixture([100, 120, 90, 108], dates=dates, rf=[.1, 0, -.1]),
        {"period_net_return.value": .08, "cagr_net.value": .08, "max_drawdown.value": .25,
         "calmar_full_window.value": .32, "volatility_annual.value": math.sqrt(17.01),
         # Excess returns .1,-.25,.3: mean .05, sample variance .0775.
         "sharpe_annual.value": .05 / math.sqrt(.0775) * math.sqrt(252),
         "longest_drawdown.recovered": False, "longest_drawdown.calendar_days": 364})
    add("M07_zero_drawdown_not_infinite_calmar", fixture([100, 110, 121], dates=["2023-01-01", "2023-07-01", "2024-01-01"]),
        {"cagr_net.value": .21, "calmar_full_window.value": None, "calmar_full_window.reason": "ZERO_MAX_DRAWDOWN"})
    add("M08_missing_risk_free_not_zero", fixture([100, 120, 90]),
        {"sharpe_annual.value": None, "sharpe_annual.reason": "RISK_FREE_NOT_SUPPLIED"})
    add("M09_recovered_duration", fixture([100, 120, 90, 120]),
        {"max_drawdown.value": .25, "longest_drawdown.calendar_days": 2, "longest_drawdown.observations": 2,
         "longest_drawdown.recovered": True})
    add("M10_bankruptcy_not_dropped", fixture([100, 0, 20], flows=[0, 0, 20]),
        {"period_net_return.value": None, "cagr_net.reason": "INSOLVENCY", "max_drawdown_before_undefined": 1})
    add("M11_full_redemption_not_bankruptcy", fixture([100, 0], flows=[0, -100]),
        {"period_net_return.value": 0, "monetary_profit": "0", "insolvencies": []})
    return out


def risk_fixture(scale):
    # Synthetic independently supplied account curves only. Never used as a
    # substitute for actual BR monthly account simulations with fees/rounding.
    scale = Decimal(str(scale))
    return fixture(["100", str(100 + 10 * scale), str((100 + 10 * scale) * (1 - Decimal(".1") * scale))])


def run_controls(run):
    reports = []
    for cid, data, expected in cases():
        write_json(run / "metric_inputs" / f"{cid}.json", {"ledger": data, "expected": expected})
        actual = report(data)
        problems = []
        for path, wanted in expected.items():
            value = actual
            for part in path.split("."):
                value = value[part]
            if isinstance(wanted, float):
                good = value is not None and math.isclose(value, wanted, rel_tol=1e-12, abs_tol=1e-12)
            else:
                good = value == wanted
            if not good:
                problems.append({"field": path, "expected": wanted, "actual": value})
        write_json(run / "metric_actual" / f"{cid}.json", actual)
        reports.append({"id": cid, "passed": not problems, "differences": problems})
    candidates = {f"{i/100:.2f}": risk_fixture(i / 100) for i in range(101)}
    strategy = risk_fixture(.25)
    result = calibrate_risk(strategy, candidates, boundary_date="2024-10-07", end_date="2024-10-09")
    write_json(run / "metric_inputs" / "risk_calibration.json", {"strategy": strategy, "candidates": candidates})
    write_json(run / "metric_actual" / "risk_calibration.json", result)
    reports.append({"id": "M12_development_risk_only_grid", "passed": result["selected_k"] == "0.25" and result["risk_matched"] and
                    len(result["candidates"]) == 101 and result["return_optimization"] is False})
    malformed = []
    for name in ("missing_observation", "misaligned_rf", "missing_flow", "duplicate_fill"):
        data = copy.deepcopy(cases()[1 if name == "duplicate_fill" else 5][1])
        if name == "missing_observation":
            data["observations"].pop(1)
        elif name == "misaligned_rf":
            data["risk_free"]["period_returns"][0]["date"] = "2024-01-01"
        elif name == "missing_flow":
            del data["observations"][1]["external_flow_end"]
        else:
            data["fills"].append(copy.deepcopy(data["fills"][0]))
        write_json(run / "metric_inputs" / f"reject_{name}.json", data)
        try:
            report(data)
            detected = False
        except ContractError:
            detected = True
        malformed.append({"mutation": name, "detected": detected})
    return {"classification": "SYNTHETIC_METRIC_ACCEPTANCE_NOT_HISTORY", "version": "metric-controls-v1",
            "reports": reports, "cases_total": len(reports), "cases_passed": sum(r["passed"] for r in reports),
            "bad_inputs": malformed, "accepted": all(r["passed"] for r in reports) and all(r["detected"] for r in malformed),
            "limitations": ["Invented accounts/calendar/risk-free returns; no historical performance",
                            "Synthetic calibration curves do not model actual monthly BR execution",
                            "ACT/365 CAGR, sample volatility, declared frequency; square-root scaling assumes no correction for serial dependence"]}
