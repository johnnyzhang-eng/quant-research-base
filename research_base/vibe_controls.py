"""Bounded synthetic acceptance of a locally installed Vibe US-equity path.

No market downloads, broker methods or automatic dependency installations.
The small aligned subclass is an explicit model adapter, not engine certification.
"""
import copy
import importlib.metadata
import inspect
import socket
from contextlib import contextmanager
from dataclasses import asdict, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

from .evidence import ContractError, digest, write_json
from .reference import low_frequency_engine as reference
from .reference import run_controls as oracle


@contextmanager
def offline_connections():
    attempts = []
    originals = socket.socket.connect, socket.socket.connect_ex, socket.create_connection
    def blocked(*args, **kwargs):
        attempts.append("connection blocked")
        raise ContractError("Network connections prohibited in synthetic acceptance")
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = blocked
    try:
        yield attempts
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = originals


def shared_cases():
    """Literal outcomes chosen before inspecting the target engine's outputs."""
    cases = []
    def base(price="10", cash="100"):
        data = oracle.fixture(cash=cash, prices={"SPY": price})
        data["settlement"].update(cash_sessions=0, share_sessions=0)
        data["settlement"]["evidence"] = "synthetic immediate-settlement control, not broker conditions"
        return data
    def expect(cash, equity, holdings, fills):
        return {"cash": cash, "equity": equity, "holdings": {"SPY": holdings}, "fills": fills}
    def fill(day, qty, price, fee="0"):
        return {"date": day, "qty": qty, "price": price, "fee": fee}
    d = base()
    cases.append(("P01_no_trade", d, expect("100", "100", 0, [])))
    d = base(); oracle.intent(d, "2024-10-07", SPY="0.5")
    for day in ["2024-10-09", "2024-10-10", "2024-10-11"]:
        oracle.price(d, day, "20")
    cases.append(("P02_cash_weight_and_terminal_mark", d,
                  expect("50", "150", 5, [fill("2024-10-08", 5, "10")])))
    d = base(); oracle.intent(d, "2024-10-07", SPY=1)
    for day in ["2024-10-08", "2024-10-09", "2024-10-10", "2024-10-11"]:
        oracle.price(d, day, "20")
    cases.append(("P03_next_open_gap", d, expect("0", "100", 5, [fill("2024-10-08", 5, "20")])))
    d = base("7"); d["fees"].update(commission_rate="0.01", minimum_commission="2")
    oracle.intent(d, "2024-10-07", SPY=1); oracle.intent(d, "2024-10-08", SPY=0)
    for day in ["2024-10-09", "2024-10-10", "2024-10-11"]:
        oracle.price(d, day, "8")
    cases.append(("P04_integer_fee_round_trip", d,
                  expect("110", "110", 0, [fill("2024-10-08", 14, "7", "2"), fill("2024-10-09", -14, "8", "2")])))
    d = base(); d["fees"]["minimum_commission"] = "5"; oracle.intent(d, "2024-10-07", SPY=1)
    cases.append(("P05_minimum_fee_affordability", d, expect("5", "95", 9, [fill("2024-10-08", 9, "10", "5")])))
    d = base(); oracle.intent(d, "2024-10-07", SPY=1)
    oracle.bar(d, "2024-10-08", open_capacity_shares=0, volume="0")
    cases.append(("P06_zero_known_open_capacity", d, expect("100", "100", 0, [])))
    d = base(); oracle.intent(d, "2024-10-07", SPY=1)
    d["actions"] = [oracle.dividend("synthetic-dividend", "2024-10-09", "2024-10-11")]
    for day in ["2024-10-09", "2024-10-10", "2024-10-11"]:
        oracle.price(d, day, "9")
    cases.append(("P07_dividend_receivable_and_payment", d, expect("10", "100", 10, [fill("2024-10-08", 10, "10")])))
    d = base(); d["settlement"]["share_sessions"] = 2
    oracle.intent(d, "2024-10-07", SPY=1); oracle.intent(d, "2024-10-08", SPY=0)
    cases.append(("P08_share_settlement", d, expect("0", "100", 10, [fill("2024-10-08", 10, "10")])))
    d = base(cash="0"); d["initial"]["holdings"]["SPY"] = 10
    d["actions"] = [{"id": "synthetic-split", "type": "split", "symbol": "SPY",
                     "effective_date": "2024-10-08", "known_at": "2024-10-08T12:00:00Z",
                     "numerator": 2, "denominator": 1}]
    for day in ["2024-10-08", "2024-10-09", "2024-10-10", "2024-10-11"]:
        oracle.price(d, day, "5")
    cases.append(("P09_split_units", d, expect("0", "100", 20, [])))
    d = base(); oracle.intent(d, "2024-10-07", SPY=1)
    oracle.bar(d, "2024-10-08", open_capacity_shares=3)
    cases.append(("P10_partial_open_capacity", d, expect("70", "100", 3, [fill("2024-10-08", 3, "10")])))
    return cases


def differences(expected, actual, prefix=""):
    problems = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            return [{"field": prefix, "reason": "keys/type differ", "expected": expected, "actual": actual}]
        for key in expected:
            problems.extend(differences(expected[key], actual[key], f"{prefix}.{key}"))
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return [{"field": prefix, "reason": "list length/type differ", "expected": expected, "actual": actual}]
        for i, value in enumerate(expected):
            problems.extend(differences(value, actual[i], f"{prefix}[{i}]"))
    else:
        try:
            a, b = Decimal(str(expected)), Decimal(str(actual))
            equal = a.is_finite() and b.is_finite() and abs(a-b) <= Decimal("0.00000001")
        except Exception:
            equal = expected == actual
        if not equal:
            problems.append({"field": prefix, "expected": expected, "actual": actual})
    return problems


def reference_projection(data, result):
    final = result["snapshots"][-1]
    fills = [{"date": e["date"], "qty": e["qty"] * (1 if e["side"] == "BUY" else -1),
              "price": e["price"], "fee": e["fee"]} for e in result["events"] if e["type"] == "FILL"]
    return {"cash": final["settled_cash"], "equity": final["equity"],
            "holdings": final["holdings"], "fills": fills}


def aligned_class(native):
    class AlignedUS(native):
        def __init__(self, config, fees):
            super().__init__(config, market="us")
            self.contract_fees = fees
            self.terminal_mark = None
            self.terminal_positions = None

        def round_size(self, raw_size, price):
            return float(Decimal(str(max(raw_size, 0))).to_integral_value(rounding=ROUND_FLOOR))

        def calc_commission(self, size, price, direction, is_open):
            f = self.contract_fees
            notional = Decimal(str(size)) * Decimal(str(price))
            buy = direction > 0 if is_open else direction < 0
            q = Decimal(f["fee_quantum"])
            comm = max(notional * Decimal(f["commission_rate"]), Decimal(f["minimum_commission"]))
            platform = Decimal(f["platform_per_order"])
            tax = notional * Decimal(f["buy_tax_rate" if buy else "sell_tax_rate"])
            return float(sum(x.quantize(q, rounding=ROUND_HALF_UP) for x in (comm, platform, tax)))

        def apply_slippage(self, price, direction):
            value = Decimal(str(price)) * (1 + Decimal(direction) * Decimal(self.contract_fees["slippage_rate"]))
            return float((value / Decimal("0.01")).to_integral_value(
                rounding=ROUND_CEILING if direction > 0 else ROUND_FLOOR) * Decimal("0.01"))

        def can_execute(self, symbol, direction, bar):
            known, opening = bar.get("open_quote_known_at"), bar.get("open_at")
            capacity = bar.get("open_capacity_shares")
            if bar.get("open_status") != "tradable" or known is None or opening is None or capacity is None:
                return False
            if reference.stamp(known) > reference.stamp(opening) or capacity <= 0:
                return False
            return super().can_execute(symbol, direction, bar)

        def _plan_open_order(self, symbol, target_weight, df, ts, equity, **kwargs):
            order = super()._plan_open_order(symbol, target_weight, df, ts, equity, **kwargs)
            if order is None:
                return None
            size = min(order.size, int(df.loc[ts, "open_capacity_shares"]))
            if size != order.size:
                return replace(order, size=size, margin=size * order.price,
                               commission=self.calc_commission(size, order.price, order.direction, True))
            return order

        def _close_position(self, symbol, price, ts, reason):
            if reason == "end_of_backtest":
                if self.terminal_mark is None:
                    self.terminal_mark = self.equity_snapshots[-1]
                    self.terminal_positions = self.actual_position_snapshots[-1]
                return  # explicitly value open positions instead of forcing a sale
            return super()._close_position(symbol, price, ts, reason)
    return AlignedUS


def unsupported(data):
    problems = []
    if data["actions"]:
        problems.append("Corporate-action application/payment not implemented in this adapter")
    if data["settlement"]["cash_sessions"] or data["settlement"]["share_sessions"]:
        problems.append("Delayed settlement not implemented in this adapter")
    if any(data["initial"]["holdings"].values()):
        problems.append("Initial holdings transfer not implemented in this adapter")
    if data["fees"]["annual_cash_rate"] != "0":
        problems.append("Cash interest not implemented in this adapter")
    return problems


def execute_vibe(data, native, aligned=False):
    import pandas as pd
    from backtest.engines.base import _align
    frames, signals = {}, {}
    for symbol in data["symbols"]:
        source = [b for b in data["bars"] if b["symbol"] == symbol]
        frame = pd.DataFrame(source)
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame["date"])).as_unit("ns")
        for key in ("open", "high", "low", "close", "volume"):
            frame[key] = pd.to_numeric(frame[key])
        frames[symbol] = frame
        signal = pd.Series(0.0, index=frame.index)
        state = 0.0
        for ts in frame.index:
            intents = [p for p in data["control_intents"] if p["decision_date"] == str(ts.date())]
            if intents:
                state = float(intents[0]["weights"][symbol])
            signal.loc[ts] = state
        signals[symbol] = signal
    session = data["calendar"]["sessions"]
    execute_dates = {session[session.index(p["decision_date"])+1] for p in data["control_intents"]}
    config = {"initial_cash": float(data["initial"]["settled_cash"]),
              "slippage_us": float(data["fees"]["slippage_rate"]),
              "commission_rate": float(data["fees"]["commission_rate"]),
              "commission_min": float(data["fees"]["minimum_commission"]),
              "position_adjustment": "rebalance", "leverage": 1.0,
              "rebalance_mask": sorted(execute_dates) if execute_dates else [data["start"]]}
    engine = aligned_class(native)(config, data["fees"]) if aligned else native(config, market="us")
    dates, close, valuation, target, _ = _align(frames, signals, data["symbols"])
    # Both paths get the same explicit one-shot decision calendar; a missing
    # intent must not become an implicit daily retry or rebalance.
    engine._execute_bars(dates, frames, close, target, data["symbols"], close_val_df=valuation)
    if aligned and engine.terminal_mark is not None:
        engine.equity_snapshots[-1] = engine.terminal_mark
        engine.actual_position_snapshots[-1] = engine.terminal_positions
    cash = Decimal(data["initial"]["settled_cash"])
    holdings = {s: Decimal(0) for s in data["symbols"]}
    fills, replay, errors = [], [], []
    for snapshot in engine.equity_snapshots:
        ts = snapshot.timestamp
        for f in engine.fill_records:
            if f.timestamp == ts:
                quantity, price, fee = (Decimal(str(v)) for v in (f.signed_quantity, f.execution_price, f.fee))
                cash -= quantity * price + fee
                holdings[f.symbol] += quantity
                fills.append({"date": str(ts.date()), "qty": str(quantity), "price": str(price), "fee": str(fee)})
        equity = cash + sum(holdings[s] * Decimal(str(frames[s].loc[ts, "close"])) for s in holdings)
        error = abs(equity - Decimal(str(snapshot.equity)))
        if error > Decimal("0.00000001"):
            errors.append({"date": str(ts.date()), "equity_difference": str(error)})
        replay.append({"date": str(ts.date()), "cash": str(cash),
                       "holdings": {s: str(q) for s, q in holdings.items()}, "equity": str(equity)})
    actual = {"cash": str(cash), "equity": str(engine.equity_snapshots[-1].equity),
              "holdings": {s: str(q) for s, q in holdings.items()}, "fills": fills}
    details = {"projection": actual, "independent_replay": replay, "replay_errors": errors,
               "native_fill_records": [{**asdict(f), "timestamp": str(f.timestamp)} for f in engine.fill_records],
               "decision_calendar": sorted(execute_dates), "adapter": "aligned-v1" if aligned else "native"}
    return actual, details


def run_controls(run):
    with offline_connections() as network_attempts:
        from backtest.engines.global_equity import GlobalEquityEngine
        from backtest.engines.base import BaseEngine
        reports, oracle_ok = [], True
        first_result = None
        for cid, data, expected in shared_cases():
            write_json(run / "shared_inputs" / f"{cid}.json", {"input": data, "expected": expected})
            result = reference.run(data, "synthetic")
            ref_actual = reference_projection(data, result)
            replay = oracle.independent_replay(data, result)
            ref_errors = differences(expected, ref_actual)
            oracle_ok &= not ref_errors and replay["passed"]
            if first_result is None and cid == "P04_integer_fee_round_trip":
                first_result = data, result
            item = {"id": cid, "expected": expected,
                    "reference": {"accepted": not ref_errors and replay["passed"], "differences": ref_errors}}
            unavailable = unsupported(data)
            for label, aligned in (("native", False), ("aligned", True)):
                if unavailable:
                    item[label] = {"status": "UNSUPPORTED", "reasons": unavailable}
                    continue
                actual, details = execute_vibe(data, GlobalEquityEngine, aligned)
                write_json(run / "vibe_actual" / f"{cid}.{label}.json", details)
                errors = differences(expected, actual)
                item[label] = {"status": "MATCH" if not errors and not details["replay_errors"] else "DIFFERS",
                               "differences": errors, "replay_errors": details["replay_errors"]}
            reports.append(item)
        original, valid_result = first_result
        attacked = copy.deepcopy(valid_result)
        next(e for e in attacked["events"] if e["type"] == "FILL")["fee"] = "3"
        bad = oracle.independent_replay(original, attacked)
        write_json(run / "calibration" / "injected_wrong_fee.json", bad)
        # Known flat control is exercised through the same target path.
        flat_ok = reports[0]["native"]["status"] == "MATCH"
        summary = {"cases_total": len(reports), "reference_matches": sum(r["reference"]["accepted"] for r in reports)}
        for label in ("native", "aligned"):
            summary[label] = {status: sum(r[label]["status"] == status for r in reports)
                              for status in ("MATCH", "DIFFERS", "UNSUPPORTED")}
        source = {name: digest(Path(inspect.getfile(cls))) for name, cls in
                  (("GlobalEquityEngine", GlobalEquityEngine), ("BaseEngine", BaseEngine))}
        deps = {n: importlib.metadata.version(n) for n in ("numpy", "pandas")}
        return {"classification": "SYNTHETIC_FIXED_TARGET_ACCEPTANCE_NOT_HISTORY",
                "instrument_calibrated": bool(oracle_ok and flat_ok and not bad["passed"] and not network_attempts),
                "aligned_supported_cases_accepted": summary["aligned"]["DIFFERS"] == 0,
                "summary": summary, "reports": reports, "engine_source_sha256": source,
                "dependencies": deps, "network_attempts_blocked": len(network_attempts),
                "injected_fee_error_detected": not bad["passed"],
                "limitations": ["Fixed targets; no Vibe full SMA strategy path tested",
                                "Aligned adapter: integer USD cash, explicit fees/ticks, opening capacity, terminal mark only",
                                "No corporate-action, delayed settlement, initial-holdings or FX acceptance",
                                "Both paths use the declared one-shot decision calendar and private engine hooks",
                                "Installed source hashes matter; no inference about latest upstream versions"]}
