"""Invented monthly accounts and literal diagnostics; no historical returns."""
import calendar
import copy
import math
from decimal import Decimal, localcontext

from .evidence import ContractError, write_json
from .paired_inference import paired_monthly
from .performance_controls import fixture


def months(n):
    # Civil month ends are invented valuation boundaries, not exchange sessions.
    out = ["2018-12-31"]
    for i in range(n):
        y, m = 2019+i//12, i%12+1
        out.append(f"{y:04d}-{m:02d}-{calendar.monthrange(y,m)[1]:02d}")
    return out


def monthly(returns):
    dates = months(len(returns))
    with localcontext() as c:
        c.prec = 250
        values, nav = ["100"], Decimal(100)
        for r in returns:
            nav *= 1+Decimal(str(r)); values.append(str(nav))
    d = fixture(values, dates=dates)
    d.update(sampling="monthly_close", periods_per_year=12)
    return d


def declared_sessions(ledger):
    ends = ledger["calendar"]
    y, m = int(ends[-1][:4]), int(ends[-1][5:7])
    y, m = (y+1, 1) if m == 12 else (y, m+1)
    return sorted(set(ends+[d[:7]+"-15" for d in ends]+[f"{y:04d}-{m:02d}-01"]))


def calculate(s, b, n=None):
    ends = s["calendar"][1:1+n] if n is not None else s["calendar"][1:]
    return paired_monthly(s, b, boundary_date=s["calendar"][0], end_date=ends[-1], month_ends=ends,
                          session_calendar=declared_sessions(s))


def cases():
    positive, flat = monthly([".01"]*60), monthly(["0"]*60)
    mixed = monthly([".02", "-.02"]*30)
    out = [
        ("I01_identical_paired_zero", mixed, copy.deepcopy(mixed),
         {"paired_months": 60, "valid_log_months": 60, "annual_log_growth_difference": 0,
          "interval.lower": 0, "interval.upper": 0, "interval.direction": "ZERO_DEGENERATE",
          "point_conditions.growth_positive": False}),
        ("I02_constant_positive_log_growth", positive, flat,
         {"annual_log_growth_difference": 12*math.log(1.01), "interval.lower": 12*math.log(1.01),
          "interval.upper": 12*math.log(1.01), "interval.direction": "LOWER_POSITIVE",
          "same_window_drawdown.strategy": 0, "point_conditions.drawdown_not_deeper": True}),
        ("I03_constant_negative_retained", flat, positive,
         {"annual_log_growth_difference": -12*math.log(1.01), "interval.direction": "UPPER_NONPOSITIVE",
          "point_conditions.growth_positive": False}),
        ("I04_fewer_than60_no_bootstrap", monthly([".01"]*59), monthly(["0"]*59),
         {"paired_months": 59, "sample_sufficient": False, "interval.status": "NOT_RUN",
          "interval.reason": "FEWER_THAN_60_MONTHS"}),
        ("I05_bankruptcy_not_deleted", monthly(["0"]*10+["-1"]+["0"]*49), flat,
         {"paired_months": 60, "valid_log_months": 10, "annual_log_growth_difference": None,
          "interval.reason": "INSOLVENCY_OR_UNDEFINED_MONTH", "sample_sufficient": False}),
    ]
    # Positive monthly point, but a deeper intermediate drawdown than cash.
    with localcontext() as c:
        c.prec = 250
        svalues, bvalues, dates = ["100"], ["100"], ["2018-12-31"]
        nav = Decimal(100)
        for end in months(60)[1:]:
            dates.extend([end[:7]+"-15", end]); svalues.extend([str(nav*Decimal(".5")),str(nav*Decimal("1.01"))])
            nav *= Decimal("1.01"); bvalues.extend(["100","100"])
    s, b = fixture(svalues, dates=dates), fixture(bvalues, dates=dates)
    out.append(("I06_daily_compound_and_drawdown", s, b,
                {"annual_log_growth_difference": 12*math.log(1.01), "same_window_drawdown.strategy": .5,
                 "point_conditions.drawdown_not_deeper": False}))
    return out


def run_controls(run):
    reports = []
    for name, s, b, expected in cases():
        ends = months(59 if name.startswith("I04") else 60)[1:]
        sessions = declared_sessions(s)
        write_json(run / "inference_inputs" / f"{name}.json", {"strategy": s, "benchmark": b,
                   "boundary_date": s["calendar"][0], "end_date": ends[-1], "month_ends": ends,
                   "session_calendar": sessions, "expected": expected})
        result = paired_monthly(s, b, boundary_date=s["calendar"][0], end_date=ends[-1], month_ends=ends,
                                session_calendar=sessions)
        problems = []
        for key, wanted in expected.items():
            value = result
            for part in key.split("."):
                value = value[part]
            if isinstance(wanted, (int, float)) and not isinstance(wanted, bool):
                good = value is not None and math.isclose(value, wanted, rel_tol=1e-12, abs_tol=1e-12)
            else:
                good = value == wanted
            if not good:
                problems.append({"field": key, "expected": wanted, "actual": value})
        if name.startswith("I05") and len(result["monthly_pairs"]) != 60:
            problems.append({"field": "retained_months", "expected": 60, "actual": len(result["monthly_pairs"])})
        write_json(run / "inference_actual" / f"{name}.json", result)
        reports.append({"id": name, "passed": not problems, "differences": problems})
    rejections = []
    for name in ("missing_month", "context_mismatch", "external_flow", "partial_month"):
        s, b = monthly(["0"]*60), monthly(["0"]*60)
        sessions = declared_sessions(s)
        ends = s["calendar"][1:]
        if name == "missing_month":
            ends = ends[:5]+ends[6:]
        elif name == "context_mismatch":
            b["comparison_context"]["costs"] = "f"*64
        elif name == "external_flow":
            b["observations"][1]["external_flow_end"] = "1"
        else:
            ends[-1] = "2023-12-15"
        write_json(run / "inference_inputs" / f"reject_{name}.json", {"strategy": s,"benchmark": b,
                   "month_ends": ends,"session_calendar": sessions})
        try:
            paired_monthly(s,b,boundary_date=s["calendar"][0],end_date=s["calendar"][-1],month_ends=ends,
                           session_calendar=sessions)
            detected = False
        except ContractError:
            detected = True
        rejections.append({"mutation": name, "detected": detected})
    return {"classification": "SYNTHETIC_PAIRED_INFERENCE_CONTROLS_NOT_HISTORY", "version": "inference-controls-v1",
            "reports": reports,"cases_total": len(reports),"cases_passed": sum(r["passed"] for r in reports),
            "bad_inputs": rejections,"accepted": all(r["passed"] for r in reports) and all(r["detected"] for r in rejections),
            "limitations": ["Invented account paths and civil month boundaries, not market records",
                            "These six controls cannot establish coverage on arbitrary dependent or nonstationary returns",
                            "No risk-calibrated historical BR or complete historical study matrix has run"]}
