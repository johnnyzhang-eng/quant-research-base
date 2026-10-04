"""Causal synthetic S02 features and S/B0/BR/BC target generation.

Signal arithmetic is independent Fraction arithmetic; accounting stays in Vibe.
This version explicitly refuses historical inputs and nonzero cash interest.
"""
import copy
from fractions import Fraction

from .evidence import ContractError
from .reference import low_frequency_engine as contract

ASSETS = ["SPY", "EFA", "IEF", "GLD"]


def generate(data, variant, *, trade_start, k=None):
    contract.validate(data, "synthetic")
    if set(data["symbols"]) != set(ASSETS) or data["control_intents"]:
        raise ContractError("Full path requires exactly four fixed ETFs and no prebuilt intents")
    if variant not in {"S", "B0", "BR", "BC"}:
        raise ContractError("Unrecognized frozen study variant")
    if variant == "BR":
        if k is None or Fraction(str(k)) < 0 or Fraction(str(k)) > 1 or (Fraction(str(k))*100).denominator != 1:
            raise ContractError("BR requires a predeclared k in[0,1]")
    elif k is not None:
        raise ContractError("Only BR accepts a risk coefficient")
    sessions = data["calendar"]["sessions"]
    if trade_start not in sessions:
        raise ContractError("Explicit resolved first trade session required")
    prices = {(b["date"], b["symbol"]): b for b in data["bars"]}
    previous = {s: Fraction(data["initial"]["marks"][s]) for s in ASSETS}
    index = {s: Fraction(100) for s in ASSETS}
    months = {s: [] for s in ASSETS}
    decisions, skipped, intents = [], [], []
    for day in sessions:
        if not data["start"] <= day <= data["end"]:
            continue
        ratios, dividends = {s: Fraction(1) for s in ASSETS}, {s: Fraction(0) for s in ASSETS}
        for action in data["actions"]:
            if action["effective_date"] != day:
                continue
            s = action["symbol"]
            if contract.stamp(action["known_at"]) > contract.stamp(prices[day, s]["open_at"]):
                raise ContractError("Late effective action cannot be backfilled")
            if action["type"] == "split":
                ratios[s] *= Fraction(action["numerator"], action["denominator"])
            else:
                dividends[s] += Fraction(action["gross_per_share"])
        for s in ASSETS:
            close = prices[day, s]["close"]
            if close is None:
                index[s], previous[s] = None, None
            else:
                close = Fraction(close)
                if previous[s] is not None and index[s] is not None:
                    index[s] *= ratios[s] * (close + dividends[s]) / previous[s]
                previous[s] = close
        if day not in data["calendar"]["month_end_sessions"]:
            continue
        available = max((prices[day, s]["available_at"] for s in ASSETS), key=contract.stamp)
        close_at = max((prices[day, s]["close_at"] for s in ASSETS), key=contract.stamp)
        if contract.stamp(available) != contract.stamp(close_at):
            raise ContractError("Late close cannot be relabeled timely next-open information")
        if variant == "S" and any(index[s] is None for s in ASSETS):
            skipped.append({"date": day, "reason": "BROKEN_TOTAL_RETURN_CHAIN"})
            continue
        for s in ASSETS:
            if index[s] is not None:
                months[s].append(index[s])
        if variant == "S" and len(months[ASSETS[0]]) < 10:
            skipped.append({"date": day, "reason": "TEN_MONTH_WARMUP"})
            continue
        average = {s: sum(months[s][-10:]) / len(months[s][-10:]) if months[s] and index[s] is not None else None for s in ASSETS}
        if variant == "S":
            weights = {s: Fraction(1, 4) if index[s] > average[s] else Fraction(0) for s in ASSETS}
        else:
            coefficient = Fraction(str(k)) if variant == "BR" else Fraction(0 if variant == "BC" else 1)
            weights = {s: coefficient / 4 for s in ASSETS}
        position = sessions.index(day)
        if position+1 >= len(sessions):
            raise ContractError("Calendar must cover next-session decision timing")
        due = sessions[position+1]
        for s in ASSETS:
            if (due, s) in prices and contract.stamp(available) >= contract.stamp(prices[due, s]["open_at"]):
                raise ContractError("Decision data not available before next opening")
        # Quarter weights and predeclared finite-decimal k convert exactly;
        # gross indices remain rational strings for independent comparison.
        from decimal import Decimal
        output_weights = {s: str(Decimal(weights[s].numerator) / Decimal(weights[s].denominator)) for s in ASSETS}
        record = {"date": day, "available_at": available, "execution_date": due,
                  "index_fraction": {s: str(index[s]) if index[s] is not None else None for s in ASSETS},
                  "sma_fraction": {s: str(average[s]) if average[s] is not None else None for s in ASSETS}, "weights": output_weights,
                  "variant": variant, "eligible": due >= trade_start}
        decisions.append(record)
        if record["eligible"]:
            intents.append({"decision_date": day, "weights": output_weights, "source": f"S02_{variant}_CAUSAL_PIPELINE"})
    prepared = copy.deepcopy(data)
    prepared["control_intents"] = intents
    # Features above are the sole decision source. Reference automatic signals
    # must not generate a second intent in the accounting comparison.
    prepared["calendar"]["month_end_sessions"] = []
    return prepared, {"classification": "SYNTHETIC_FULL_PROTOCOL_PATH_NOT_HISTORY", "variant": variant,
                      "k": str(k) if k is not None else None, "trade_start": trade_start,
                      "decisions": decisions, "skipped": skipped, "intents": intents,
                      "original_month_ends": data["calendar"]["month_end_sessions"]}
