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
from .vibe_accounting import accounting_class


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
    d = base(cash="0"); d["initial"]["holdings"]["SPY"] = 10
    d["actions"] = [oracle.dividend("after-sale", "2024-10-08", "2024-10-11")]
    oracle.intent(d, "2024-10-07", SPY=0)
    for day in d["calendar"]["sessions"][1:5]:
        oracle.price(d, day, "9")
    cases.append(("P11_sell_on_ex_keeps_entitlement", d,
                  expect("100", "100", 0, [fill("2024-10-08", -10, "9")])))
    d = base(); oracle.intent(d, "2024-10-07", SPY=1)
    d["actions"] = [oracle.dividend("buy-on-ex", "2024-10-08", "2024-10-11")]
    for day in d["calendar"]["sessions"][1:5]:
        oracle.price(d, day, "9")
    cases.append(("P12_buy_on_ex_has_no_entitlement", d,
                  expect("1", "100", 11, [fill("2024-10-08", 11, "9")])))
    d = base(cash="0"); d["initial"]["holdings"]["SPY"] = 10
    d["actions"] = [oracle.dividend("taxed", "2024-10-08", "2024-10-11", tax="0.2")]
    for day in d["calendar"]["sessions"][1:5]:
        oracle.price(d, day, "9")
    cases.append(("P13_net_withheld_dividend", d, expect("8", "98", 10, [])))
    for retry, cid in ((False, "P14_unsettled_sale_not_reused"), (True, "P15_new_intent_after_cash_settles")):
        d = oracle.fixture(symbols=["SPY", "EFA"], cash="0")
        d["initial"]["holdings"]["SPY"] = 10
        d["settlement"].update(cash_sessions=2, share_sessions=0)
        oracle.intent(d, "2024-10-07", EFA=1)
        fills = [fill("2024-10-08", -10, "10")]
        if retry:
            oracle.intent(d, "2024-10-09", EFA=1)
            fills.append(fill("2024-10-10", 10, "10"))
        cases.append((cid, d, {"cash": "0" if retry else "100", "equity": "100",
                              "holdings": {"SPY": 0, "EFA": 10 if retry else 0}, "fills": fills}))
    d = base(); d["settlement"]["share_sessions"] = 2
    oracle.intent(d, "2024-10-07", SPY=1); oracle.intent(d, "2024-10-08", SPY=0)
    oracle.intent(d, "2024-10-09", SPY=0)
    d["actions"] = [{"id": "locked-split", "type": "split", "symbol": "SPY",
                     "effective_date": "2024-10-09", "known_at": "2024-10-09T12:00:00Z",
                     "numerator": 2, "denominator": 1}]
    for day in d["calendar"]["sessions"][2:5]:
        oracle.price(d, day, "5")
    cases.append(("P16_split_locked_shares_and_release", d,
                  expect("100", "100", 0, [fill("2024-10-08", 10, "10"), fill("2024-10-10", -20, "5")])))
    d = base(cash="0"); d["initial"]["holdings"]["SPY"] = 10
    d["actions"] = [oracle.dividend("beyond-end", "2024-10-08", "2024-10-14")]
    oracle.intent(d, "2024-10-07", SPY=0)
    for day in d["calendar"]["sessions"][1:5]:
        oracle.price(d, day, "9")
    cases.append(("P17_final_receivable_not_cash", d,
                  expect("90", "100", 0, [fill("2024-10-08", -10, "9")])))
    d = base(cash="0"); d["initial"]["holdings"]["SPY"] = 10
    d["actions"] = [oracle.dividend("post-split-div", "2024-10-08", "2024-10-11", amount="0.5"),
                    {"id": "same-day-split", "type": "split", "symbol": "SPY", "effective_date": "2024-10-08",
                     "known_at": "2024-10-08T12:00:00Z", "numerator": 2, "denominator": 1}]
    for day in d["calendar"]["sessions"][1:5]:
        oracle.price(d, day, "4.5")
    cases.append(("P18_split_then_post_split_dividend", d, expect("10", "100", 20, [])))
    d = base(cash="0"); d["initial"]["holdings"]["SPY"] = 10
    oracle.intent(d, "2024-10-07", SPY=0); oracle.bar(d, "2024-10-08", open_capacity_shares=3)
    cases.append(("P19_partial_sale_capacity", d,
                  expect("30", "100", 7, [fill("2024-10-08", -3, "10")])))
    d = base(cash="50"); d["initial"]["holdings"]["SPY"] = 5
    oracle.intent(d, "2024-10-07", SPY=1); oracle.bar(d, "2024-10-08", open_capacity_shares=3)
    cases.append(("P20_partial_increase_capacity", d,
                  expect("20", "100", 8, [fill("2024-10-08", 3, "10")])))
    d = oracle.fixture(symbols=["SPY", "EFA"], cash="0")
    d["settlement"].update(cash_sessions=0, share_sessions=0)
    d["initial"]["holdings"]["SPY"] = 10
    d["actions"] = [oracle.dividend("not-spendable", "2024-10-07", "2024-10-10")]
    for day in d["calendar"]["sessions"][:5]:
        oracle.price(d, day, "9")
    oracle.intent(d, "2024-10-07", SPY="0.9", EFA="0.1")
    cases.append(("P21_receivable_not_spendable", d,
                  {"cash": "10", "equity": "100", "holdings": {"SPY": 10, "EFA": 0}, "fills": []}))
    for cid, data, expected in cases:
        data["performance_boundary_date"] = "2024-10-04"  # invented settled opening marks, not exchange evidence
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


def unsupported(data, aligned=False):
    problems = []
    if data["actions"] and not aligned:
        problems.append("Corporate-action application/payment not implemented in this adapter")
    if not aligned and (data["settlement"]["cash_sessions"] or data["settlement"]["share_sessions"]):
        problems.append("Delayed settlement not implemented in this adapter")
    if any(data["initial"]["holdings"].values()) and not aligned:
        problems.append("Initial holdings transfer not implemented in this adapter")
    if data["fees"]["annual_cash_rate"] != "0":
        problems.append("Cash interest not implemented in this adapter")
    if aligned and any(m["lot_size"] != 1 or Decimal(m["price_tick"]) != Decimal("0.01")
                       for m in data["instruments"].values()):
        problems.append("Aligned model accepts only one-share lots and USD 0.01 price ticks")
    if aligned and Decimal(data["fees"]["fee_quantum"]) != Decimal("0.01"):
        problems.append("Aligned fee rounding accepts only USD 0.01 quantum")
    return problems


def execute_vibe(data, native, aligned=False):
    unavailable = unsupported(data, aligned)
    if unavailable:
        raise ContractError("; ".join(unavailable))
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
    engine = accounting_class(aligned_class(native))(config, data["fees"], data) if aligned else native(config, market="us")
    dates, close, valuation, target, _ = _align(frames, signals, data["symbols"])
    # Both paths get the same explicit one-shot decision calendar; a missing
    # intent must not become an implicit daily retry or rebalance.
    engine._execute_bars(dates, frames, close, target, data["symbols"], close_val_df=valuation)
    if aligned and engine.terminal_mark is not None:
        engine.actual_position_snapshots[-1] = engine.terminal_positions
    if aligned:
        ledger = {"events": engine.account_events, "snapshots": engine.account_snapshots}
        checked = oracle.independent_replay(data, ledger)
        snapshot_errors = []
        for actual_snapshot, native_snapshot in zip(engine.account_snapshots, engine.equity_snapshots):
            for field, value in (("equity", native_snapshot.equity), ("settled_cash", native_snapshot.capital)):
                snapshot_errors.extend(differences(actual_snapshot[field], value,
                                                  f"{actual_snapshot['date']}.engine.{field}"))
        actual = reference_projection(data, ledger)
        from .performance_bridge import from_vibe
        metric_input, performance = from_vibe(data, ledger, opening_date=data["performance_boundary_date"])
        details = {"projection": actual, "independent_replay": checked["replayed"],
                   "replay_errors": checked["errors"] + snapshot_errors, "account_ledger": ledger,
                   "native_equity_snapshots": [{**asdict(s), "timestamp": str(s.timestamp)} for s in engine.equity_snapshots],
                   "performance_input": metric_input, "performance": performance,
                   "native_fill_records": [{**asdict(f), "timestamp": str(f.timestamp)} for f in engine.fill_records],
                   "decision_calendar": sorted(execute_dates), "adapter": "aligned-v2-accounting"}
        return actual, details
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
        import backtest.models as models
        import backtest.rebalance_mask as rebalance_mask
        reports, oracle_ok = [], True
        aligned_ledgers = {}
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
            for label, aligned in (("native", False), ("aligned", True)):
                unavailable = unsupported(data, aligned)
                if unavailable:
                    item[label] = {"status": "UNSUPPORTED", "reasons": unavailable}
                    continue
                actual, details = execute_vibe(data, GlobalEquityEngine, aligned)
                if aligned:
                    aligned_ledgers[cid] = (data, details["account_ledger"])
                write_json(run / "vibe_actual" / f"{cid}.{label}.json", details)
                errors = differences(expected, actual)
                if aligned:
                    initial = Decimal(data["initial"]["settled_cash"]) + sum(
                        Decimal(q) * Decimal(data["initial"]["marks"][s]) for s, q in data["initial"]["holdings"].items())
                    expected_return = Decimal(expected["equity"]) / initial - 1
                    errors.extend(differences(expected_return, details["performance"]["period_net_return"]["value"], "performance.net_return"))
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
        injections = []
        for cid, mutation in (("P11_sell_on_ex_keeps_entitlement", "entitlement"),
                              ("P13_net_withheld_dividend", "tax_net"),
                              ("P14_unsettled_sale_not_reused", "settlement"),
                              ("P16_split_locked_shares_and_release", "locked_quantity"),
                              ("P17_final_receivable_not_cash", "noncash_equity")):
            data, ledger = aligned_ledgers[cid]
            wrong = copy.deepcopy(ledger)
            if mutation in ("entitlement", "tax_net"):
                event = next(e for e in wrong["events"] if e["type"] == "DIV_EX")
                event["entitled_qty" if mutation == "entitlement" else "net_amount"] = "999"
            elif mutation == "settlement":
                next(e for e in wrong["events"] if e["type"] == "FILL")["settlement_date"] = "2024-10-08"
            elif mutation == "locked_quantity":
                next(s for s in wrong["snapshots"] if s["date"] == "2024-10-09")["sellable"]["SPY"] = 20
            else:
                wrong["snapshots"][-1]["equity"] = "90"
            replay = oracle.independent_replay(data, wrong)
            write_json(run / "calibration" / f"{mutation}.json", replay)
            injections.append({"mutation": mutation, "detected": not replay["passed"]})
        summary = {"cases_total": len(reports), "reference_matches": sum(r["reference"]["accepted"] for r in reports)}
        for label in ("native", "aligned"):
            summary[label] = {status: sum(r[label]["status"] == status for r in reports)
                              for status in ("MATCH", "DIFFERS", "UNSUPPORTED")}
        source = {name: digest(Path(inspect.getfile(cls))) for name, cls in
                  (("GlobalEquityEngine", GlobalEquityEngine), ("BaseEngine", BaseEngine),
                   ("models", models), ("rebalance_mask", rebalance_mask))}
        deps = {n: importlib.metadata.version(n) for n in ("numpy", "pandas")}
        return {"classification": "SYNTHETIC_FIXED_TARGET_ACCEPTANCE_NOT_HISTORY",
                "instrument_calibrated": bool(oracle_ok and flat_ok and not bad["passed"] and
                                              all(x["detected"] for x in injections) and not network_attempts),
                "aligned_supported_cases_accepted": summary["aligned"]["DIFFERS"] == 0,
                "summary": summary, "reports": reports, "engine_source_sha256": source,
                "dependencies": deps, "network_attempts_blocked": len(network_attempts),
                "injected_fee_error_detected": not bad["passed"],
                "accounting_error_injections": injections,
                "limitations": ["Fixed targets; no Vibe full SMA strategy path tested",
                                "Aligned model: long-only USD, integer shares, explicit fees/ticks, dividend receivables, splits and settlement",
                                "No FX, cash interest, fractional splits/cash-in-lieu or general corporate-action acceptance",
                                "Native basket fitting remains a separate model choice; fixed cases do not certify all allocations",
                                "Both paths use the declared one-shot decision calendar and private engine hooks",
                                "Installed source hashes matter; no inference about latest upstream versions"]}
