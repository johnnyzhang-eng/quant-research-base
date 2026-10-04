"""Frozen S02 paired monthly diagnostics over continuous net account ledgers.

This API does not run a strategy, certify input rights or assert profitability.
Calendar/units/account acceptance and complete study coverage are upstream gates.
"""
from datetime import date
from decimal import Decimal, localcontext
from random import Random

from .contracts import decimal
from .evidence import ContractError, canonical_hash
from .performance import finite_float, report

VERSION = "paired-months-v1"
BLOCK_LENGTH, REPLICATES, SEED, MIN_MONTHS = 12, 2000, 20261003, 60


def quantile(values, probability):
    """Registered linear interpolation at zero-based (N-1)*p (type7)."""
    if not values or not Decimal(0) <= probability <= Decimal(1):
        raise ContractError("Nonempty finite quantile sample and probability in[0,1] required")
    values = sorted(values)
    position = (len(values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (position - lower) * (values[upper] - values[lower])


def block_indices(starts, n):
    """Paired circular blocks, wrapping at n and truncating to exactly n."""
    if n < 1 or len(starts) != (n + BLOCK_LENGTH - 1) // BLOCK_LENGTH or any(
            type(s) is not int or not 0 <= s < n for s in starts):
        raise ContractError("One in-range start per required circular block")
    return [(s + j) % n for s in starts for j in range(BLOCK_LENGTH)][:n]


def bootstrap(differences):
    """Only the fixed protocol draws; no seed/interval search parameters."""
    with localcontext() as context:
        context.prec = 50
        return _bootstrap([decimal(v, "paired log difference") for v in differences])


def _bootstrap(differences):
    n = len(differences)
    if n < MIN_MONTHS:
        raise ContractError("At least60 paired months before bootstrap")
    rng = Random(SEED)
    full, tail = divmod(n, BLOCK_LENGTH)
    lengths = [BLOCK_LENGTH] * full + ([tail] if tail else [])
    sums = {size: [sum((differences[(i+j) % n] for j in range(size)), Decimal(0))
                   for i in range(n)] for size in set(lengths)}
    starts, estimates = [], []
    for _ in range(REPLICATES):
        row = [rng.randrange(n) for _ in lengths]
        starts.append(row)
        estimates.append(Decimal(12) * sum((sums[size][i] for size, i in zip(lengths, row)), Decimal(0)) / n)
    lower, upper = quantile(estimates, Decimal(".05")), quantile(estimates, Decimal(".95"))
    direction = ("ZERO_DEGENERATE" if lower == upper == 0 else
                 "LOWER_POSITIVE" if lower > 0 else
                 "UPPER_NONPOSITIVE" if upper <= 0 else "INCLUDES_ZERO")
    return {"status": "COMPUTED", "method": "PAIRED_CIRCULAR_BLOCK_PERCENTILE",
            "block_length": BLOCK_LENGTH, "replicates": REPLICATES, "seed": SEED,
            "rng": "Python random.Random integer seed; runtime version bound in run evidence",
            "interval_level": ".90", "quantile": "linear_type7_(N-1)*p",
            "lower": finite_float(lower), "upper": finite_float(upper),
            "lower_decimal": str(lower), "upper_decimal": str(upper), "direction": direction,
            "block_starts": starts, "block_starts_sha256": canonical_hash(starts),
            "annual_log_growth_draws_decimal": [str(v) for v in estimates]}


def month_number(day):
    try:
        d = date.fromisoformat(day)
        if d.isoformat() != day:
            raise ValueError("noncanonical date")
    except (ValueError, TypeError) as exc:
        raise ContractError("Canonical ISO month endpoint required") from exc
    return d.year * 12 + d.month


def aggregate(timeline, boundaries):
    returns = {r["date"]: r["net_return"] for r in timeline}
    dates = sorted(returns)
    out = []
    for previous, end in zip(boundaries, boundaries[1:]):
        dates_in_month = [d for d in dates if previous < d <= end]
        nav, bad = Decimal(1), []
        for d in dates_in_month:
            value = returns[d]
            if value is None:
                bad.append({"date": d, "reason": "UNDEFINED_PERIOD_RETURN"})
            else:
                r = Decimal(value)
                nav *= 1 + r
                if r <= -1:
                    bad.append({"date": d, "reason": "PERIOD_RETURN_AT_OR_BELOW_MINUS_ONE", "return": value})
        out.append({"date": end, "month": end[:7], "return": str(nav-1) if not any(
            b["reason"] == "UNDEFINED_PERIOD_RETURN" for b in bad) else None,
            "source_periods": len(dates_in_month), "issues": bad})
    return out


def window_drawdown(timeline, boundary, end):
    nav, peak, maximum = Decimal(1), Decimal(1), Decimal(0)
    for row in timeline:
        if boundary < row["date"] <= end:
            if row["net_return"] is None:
                return None
            nav *= 1 + Decimal(row["net_return"])
            peak = max(peak, nav)
            maximum = max(maximum, 1 - nav/peak)
    return maximum


def paired_monthly(strategy, benchmark, *, boundary_date, end_date, month_ends, session_calendar):
    with localcontext() as context:
        context.prec = 50
        return _paired(strategy, benchmark, boundary_date, end_date, month_ends, session_calendar)


def _paired(strategy, benchmark, boundary, end, month_ends, sessions):
    s, b = report(strategy), report(benchmark)
    compatible = ("currency", "sampling", "calendar", "calendar_source", "periods_per_year",
                  "year_day_basis", "flow_policy", "initial_equity", "comparison_context")
    if any(s[k] != b[k] for k in compatible):
        raise ContractError("Paired ledgers differ in currency, capital, source/model context or full sampling calendar")
    if any(decimal(o["external_flow_end"], "flow") != 0 for ledger in (strategy, benchmark)
           for o in ledger["observations"]):
        raise ContractError("Frozen paired study admits no contributions or withdrawals")
    calendar = s["calendar"]
    if boundary not in calendar or end not in calendar or boundary >= end:
        raise ContractError("Explicit resolved window boundaries must belong to full account calendar")
    if not isinstance(month_ends, list) or not month_ends or month_ends != sorted(set(month_ends)):
        raise ContractError("Ordered unique declared month endpoints required")
    if not isinstance(sessions, list) or not sessions or sessions != sorted(set(sessions)):
        raise ContractError("Separate complete ordered session calendar required")
    for day in sessions:
        month_number(day)
    if sessions[0] > boundary or month_number(sessions[-1]) <= month_number(end):
        raise ContractError("Session calendar must cover opening boundary and extend beyond evaluation's final month")
    if any(d not in sessions for d in calendar):
        raise ContractError("Account valuations outside declared session calendar")
    if s["sampling"] == "daily_session_close" and calendar != [d for d in sessions if calendar[0] <= d <= calendar[-1]]:
        raise ContractError("Missing or extra daily account valuation against separate session calendar")
    last_by_month = {}
    for d in sessions:
        last_by_month[d[:7]] = d
    if boundary != last_by_month[boundary[:7]] or month_ends[-1] != end:
        raise ContractError("Boundary must precede complete first month; end must be last declared month endpoint")
    expected_months = list(range(month_number(boundary)+1, month_number(end)+1))
    if [month_number(d) for d in month_ends] != expected_months or any(
            d not in calendar or last_by_month[d[:7]] != d for d in month_ends):
        raise ContractError("No missing, repeated or partial declared calendar month")
    if s["sampling"] == "monthly_close" and [d for d in calendar if boundary < d <= end] != month_ends:
        raise ContractError("Monthly-close ledger has extra intramonth valuations")
    boundaries = [boundary, *month_ends]
    sm, bm = aggregate(s["timeline"], boundaries), aggregate(b["timeline"], boundaries)
    pairs, differences = [], []
    for left, right in zip(sm, bm):
        sr = Decimal(left["return"]) if left["return"] is not None else None
        br = Decimal(right["return"]) if right["return"] is not None else None
        valid = sr is not None and br is not None and sr > -1 and br > -1 and not left["issues"] and not right["issues"]
        difference = (1 + sr).ln() - (1 + br).ln() if valid else None
        if difference is not None:
            differences.append(difference)
        pairs.append({"date": left["date"], "month": left["month"],
                      "strategy_net_return": left["return"], "benchmark_net_return": right["return"],
                      "strategy_source_periods": left["source_periods"], "benchmark_source_periods": right["source_periods"],
                      "paired_log_growth_difference": str(difference) if valid else None,
                      "issues": {"strategy": left["issues"], "benchmark": right["issues"]}})
    insolvencies = {name: [i for i in r["insolvencies"] if i["date"] <= end] for name, r in (("strategy", s), ("benchmark", b))}
    complete = len(differences) == len(pairs) and not any(insolvencies.values())
    point = Decimal(12) * sum(differences, Decimal(0)) / len(pairs) if complete else None
    sd = window_drawdown(s["timeline"], boundary, end) if complete else None
    bd = window_drawdown(b["timeline"], boundary, end) if complete else None
    enough = len(pairs) >= MIN_MONTHS
    interval = bootstrap(differences) if complete and enough else {
        "status": "NOT_RUN", "reason": "INSOLVENCY_OR_UNDEFINED_MONTH" if not complete else "FEWER_THAN_60_MONTHS",
        "block_length": BLOCK_LENGTH, "replicates": REPLICATES, "seed": SEED,
        "lower": None, "upper": None, "direction": "NOT_RUN"}
    return {"version": VERSION, "classification": "PAIRED_ACCOUNT_WINDOW_DIAGNOSTICS_NOT_STRATEGY_ACCEPTANCE",
            "source_ledger_sha256": {"strategy": s["ledger_sha256"], "benchmark": b["ledger_sha256"]},
            "comparison_context": dict(s["comparison_context"]), "currency": s["currency"],
            "boundary_date": boundary, "end_date": end, "declared_month_ends": list(month_ends),
            "full_sampling_calendar_sha256": canonical_hash(calendar),
            "declared_session_calendar_sha256": canonical_hash(sessions),
            "paired_months": len(pairs), "valid_log_months": len(differences), "minimum_months": MIN_MONTHS,
            "sample_sufficient": enough and complete, "monthly_pairs": pairs, "insolvencies": insolvencies,
            "annual_log_growth_difference": finite_float(point) if point is not None else None,
            "annual_log_growth_difference_decimal": str(point) if point is not None else None,
            "same_window_drawdown": {"strategy": finite_float(sd) if sd is not None else None,
                                     "benchmark": finite_float(bd) if bd is not None else None,
                                     "sampling": s["sampling"]},
            "point_conditions": {"growth_positive": point > 0 if point is not None else None,
                                 "drawdown_not_deeper": sd <= bd if complete else None,
                                 "sample_sufficient": enough and complete},
            "interval": interval,
            "limitations": ["No source/account acceptance or historical risk calibration inferred",
                            "Declared month endpoints and calendar require separate market acceptance",
                            "Daily interval returns compound; costs already belong in net account equity",
                            "Log-growth difference is not the difference between two CAGRs",
                            "Fixed-block percentile interval is conditional on suitable time-series assumptions",
                            "Revisited history, multiple comparisons and nonstationarity are not corrected",
                            "A single window cannot establish full frozen study support"]}
