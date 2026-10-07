"""Separate, constrained hypothetical exit over the existing native account.

The study terminal is immutable. An exit is a forward continuation, never a
last-bar reset. All future market, cost, cash and FX evidence is supplied.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import inspect

from .cash_account import CashAccount, number, stamp, validate_rules
from .evidence import ContractError, canonical_hash, digest
from .historical_account import historical_class
from .historical_replay import _Replay, _f, _round, _stamp
from .trading_terms import TradingTerms
from .vibe_controls import offline_connections

VERSION = "historical-exit/1"
VERSION2 = "historical-exit/2"
FORWARD_FIELDS = {"schema_version", "through", "calendar", "bars", "actions", "terms", "cash_rules", "fx", "source_binding"}
BAR_FIELDS = {"date", "symbol", "open", "high", "low", "close", "volume", "open_at", "close_at", "available_at", "open_quote_known_at", "open_status", "open_capacity_shares"}


def _day(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise ContractError("canonical date required") from None


def _decimal_fraction(value):
    return Decimal(value.numerator) / Decimal(value.denominator)


def _binding(forward, data):
    v2 = data["historical_model"].get("schema_version") == "historical-account-model/2"
    fields = FORWARD_FIELDS | {"expense_review", "fixed_expenses"} if v2 else FORWARD_FIELDS
    if not isinstance(forward, dict) or set(forward) != fields or forward["schema_version"] != (VERSION2 if v2 else VERSION):
        raise ContractError("exact versioned historical-exit forward fields required")
    if v2:
        for key in ("expense_review", "fixed_expenses"):
            if forward[key] != data["historical_model"][key]:
                raise ContractError("exit must retain exact reviewed expense schedule")
    binding = forward["source_binding"]
    if not isinstance(binding, dict) or set(binding) != {"data_kind", "evidence", "payload_sha256"}:
        raise ContractError("explicit forward source binding required")
    evidence = binding["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {"reference", "sha256", "known_at", "basis"}:
        raise ContractError("explicit forward evidence required")
    if not evidence["reference"] or evidence["basis"] not in {"invented_control", "reviewed_historical"}:
        raise ContractError("explicit forward evidence classification required")
    if binding["data_kind"] != data["data_kind"]:
        raise ContractError("forward source classification differs from study")
    for checksum in (evidence["sha256"], binding["payload_sha256"]):
        if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ContractError("forward SHA256 required")
    payload = {k: forward[k] for k in fields - {"source_binding"}}
    if canonical_hash(payload) != binding["payload_sha256"]:
        raise ContractError("forward payload differs from frozen binding")
    if stamp(evidence["known_at"]) > stamp(forward["terms"]["frozen_at"]):
        raise ContractError("forward evidence received after model freeze")
    _day(forward["through"])
    TradingTerms(forward["terms"])
    validate_rules(forward["cash_rules"])
    original = data["historical_model"]
    # An extension can append future rules. It cannot revise the study's costs,
    # funding route, clock, tier definitions, or already accrued schedules.
    if forward["terms"]["frozen_at"] != original["terms"]["frozen_at"]:
        raise ContractError("exit must retain the frozen model clock")
    old_rules = original["terms"]["rules"]
    if forward["terms"]["rules"][:len(old_rules)] != old_rules:
        raise ContractError("exit revised existing trading terms")
    old_cash = original["cash_rules"]
    for key in old_cash:
        if key != "interest_schedules" and forward["cash_rules"][key] != old_cash[key]:
            raise ContractError("exit revised existing cash/funding assumptions")
    old_schedules = old_cash["interest_schedules"]
    if forward["cash_rules"]["interest_schedules"][:len(old_schedules)] != old_schedules:
        raise ContractError("exit revised existing cash schedules")
    for row in forward["cash_rules"]["interest_schedules"][len(old_schedules):]:
        if row["effective_date"] <= data["end"]:
            raise ContractError("exit backfilled a study cash schedule")
    if set(forward["fx"]) != {"close_marks", "execution_quotes"}:
        raise ContractError("explicit forward mark and conversion quote maps required")


def _extended(data, forward):
    _binding(forward, data)
    if forward["schema_version"] == VERSION2 and forward["through"] > forward["expense_review"]["coverage_end_date"]:
        return None, [], "EXPENSE_REVIEW_COVERAGE_ENDED"
    calendars = {}
    for row in forward["calendar"]:
        if set(row) != {"trade_date", "open_at", "close_at"}:
            raise ContractError("forward calendar row fields required")
        day = row["trade_date"]
        _day(day)
        if day in calendars or stamp(row["open_at"]) >= stamp(row["close_at"]):
            raise ContractError("duplicate/invalid forward calendar")
        calendars[day] = copy.deepcopy(row)
    if list(calendars) != sorted(calendars):
        raise ContractError("forward calendar must ascend")
    original_calendar = {r["trade_date"]: r for r in data["historical_artifact"]["calendar"]}
    for day, row in calendars.items():
        if day <= data["end"] and (day not in original_calendar or any(row[k] != original_calendar[day][k] for k in row)):
            raise ContractError("forward calendar changed study history")
    combined_calendar = {d: {k: r[k] for k in ("trade_date", "open_at", "close_at")} for d, r in original_calendar.items()}
    combined_calendar.update(calendars)
    sessions = sorted(combined_calendar)
    future = [d for d in sessions if data["end"] < d <= forward["through"]]
    if future and forward["through"] != future[-1]:
        return None, future, "HORIZON_NOT_AN_ACTUAL_STOCK_SESSION"
    bars = {}
    for row in forward["bars"]:
        if set(row) != BAR_FIELDS:
            raise ContractError("exact explicit forward bar/quote/capacity fields required")
        day, symbol = row["date"], row["symbol"]
        if day not in calendars or symbol not in data["symbols"] or (day, symbol) in bars:
            raise ContractError("forward bar calendar/symbol/duplicate mismatch")
        if row["open_at"] != calendars[day]["open_at"] or row["close_at"] != calendars[day]["close_at"]:
            raise ContractError("forward bar differs from declared session")
        if stamp(row["available_at"]) < stamp(row["close_at"]):
            raise ContractError("daily forward bar available before its close")
        if row["open_status"] not in {"tradable", "halted", "unknown"}:
            raise ContractError("unknown forward opening status semantics")
        if row["open_quote_known_at"] is not None:
            stamp(row["open_quote_known_at"])
        cap = row["open_capacity_shares"]
        if cap is not None and (type(cap) is not int or cap < 0):
            raise ContractError("forward capacity must be nonnegative integer or unknown")
        for key in ("open", "high", "low", "close", "volume"):
            if row[key] is not None and (number(row[key]) < 0 or (key != "volume" and number(row[key]) == 0)):
                raise ContractError("nonpositive forward price or negative volume")
        bars[day, symbol] = copy.deepcopy(row)
    original = {(b["date"], b["symbol"]): b for b in data["bars"]}
    for key, row in bars.items():
        if key[0] <= data["end"]:
            source = original.get(key)
            if source is None:
                source = next((b for b in data["historical_artifact"]["bars"] if b["trade_date"] == key[0] and b["symbol"] == key[1]), None)
            if source is None or any(row[k] != source[k] for k in ("open", "high", "low", "close", "volume", "open_at", "close_at", "available_at")):
                raise ContractError("forward volume screen changed historical raw evidence")
    missing = [(d, s) for d in future for s in data["symbols"] if (d, s) not in bars]
    if missing:
        return None, future, "MISSING_FORWARD_BARS"
    for day in future:
        if day not in forward["fx"]["close_marks"]:
            return None, future, "MISSING_FORWARD_FX_MARK"
        TradingTerms(forward["terms"]).rule(day, use_at=calendars[day]["open_at"])
    combined = copy.deepcopy(data)
    combined["end"] = forward["through"]
    combined["calendar"]["sessions"] = sessions
    combined["bars"] = list(original.values()) + [bars[d, s] for d in future for s in data["symbols"]]
    combined["historical_artifact"]["calendar"] = list(combined_calendar.values())
    combined["historical_model"]["terms"] = copy.deepcopy(forward["terms"])
    combined["historical_model"]["cash_rules"] = copy.deepcopy(forward["cash_rules"])
    combined["historical_model"]["fx"]["close_marks"].update(copy.deepcopy(forward["fx"]["close_marks"]))
    combined["historical_model"]["fx"]["exit_quotes"].update({d: None for d in future})
    for action in forward["actions"]:
        if action["effective_date"] <= data["end"] or any(a["id"] == action["id"] for a in combined["actions"]):
            raise ContractError("exit action is duplicate or backfilled")
        if action["type"] not in {"split", "dividend"} or action["symbol"] not in data["symbols"]:
            raise ContractError("unsupported explicit future action")
        if action["type"] == "dividend" and not all(k in action for k in ("withholding_rate", "pay_date", "payment_at", "gross_per_share")):
            raise ContractError("future dividend net/payment assumptions required")
        combined["actions"].append(copy.deepcopy(action))
    return combined, future, None


def _restore(engine, data, result, replay, forward):
    from backtest.models import Position
    import pandas as pd
    state = replay._stock(len(result["events"]))
    terminal = result["snapshots"][-1]
    if forward["schema_version"] == VERSION2:
        from .historical_expenses import ExpenseWallet
        wallet = ExpenseWallet(forward["cash_rules"], expense_review=forward["expense_review"], fixed_expenses=forward["fixed_expenses"])
    else:
        wallet = CashAccount(forward["cash_rules"])
    wallet.events = copy.deepcopy(result["wallet_events"])
    wallet.last_at = stamp(result["cny_snapshots"][-1]["as_of"])
    wallet.last_accrued_day = _day(data["end"])
    wallet.used_ids = {e["event_id"] for e in wallet.events if "event_id" in e}
    for currency in ("CNY", "USD"):
        row = result["cny_snapshots"][-1]["wallets"][currency]
        wallet.settled[currency] = number(row["settled"])
        wallet.receivables[currency] = number(row["receivables"])
        wallet.unpaid_interest[currency] = number(row["unpaid_interest"])
    if forward["schema_version"] == VERSION2:
        wallet.restore_expenses_from_events()
    engine.wallet, engine.wallet_cursor = wallet, len(wallet.events)
    engine.wallet_journal = copy.deepcopy(wallet.events)
    engine.account_events = copy.deepcopy(result["events"])
    engine.account_snapshots = copy.deepcopy(result["snapshots"])
    engine.cny_snapshots = copy.deepcopy(result["cny_snapshots"])
    engine.capital = float(terminal["settled_cash"])
    engine.positions = {s: Position(s, 1, float(terminal["valuation_marks"][s]), pd.Timestamp(data["end"]), qty, 1.0, -1, 0.0)
                        for s, qty in terminal["holdings"].items() if qty}
    engine.account_marks = {s: number(v) for s, v in terminal["valuation_marks"].items()}
    engine.pending_cash = [{"sequence": seq, "amount": _decimal_fraction(p[0]), "due": p[1]} for seq, p in state["pending_cash"].items()]
    engine.pending_shares = [{"sequence": seq, "symbol": p[0], "qty": p[1], "due": p[2]} for seq, p in state["locked"].items()]
    engine.receivables = {aid: {"amount": _decimal_fraction(p[0]), "due": p[1]} for aid, p in state["receivables"].items()}
    engine.seeded, engine.close_frame = True, None


def execute_exit(data, reconciled_result, forward, *, native=None):
    """Continue a verified terminal account; return pending when evidence ends."""
    replay = _Replay(data, reconciled_result, "0.00000001")
    check = replay.run()
    if not check["passed"]:
        raise ContractError("study terminal is not independently reconciled")
    combined, future, missing = _extended(data, forward)
    terminal = {"stock": copy.deepcopy(reconciled_result["snapshots"][-1]), "cny": copy.deepcopy(reconciled_result["cny_snapshots"][-1])}
    identity = {"schema_version": forward["schema_version"], "classification": "SEPARATE_CONDITIONAL_HYPOTHETICAL_EXIT", "data_kind": data["data_kind"],
                "study_terminal": terminal, "study_result_sha256": canonical_hash(reconciled_result), "forward_binding": copy.deepcopy(forward["source_binding"]),
                "historical_engine_ready": False, "broker_orders": 0,
                "model_version": data["historical_model"]["schema_version"], "scenario": data["historical_model"]["scenario"],
                "study_lineage": copy.deepcopy(reconciled_result.get("lineage", {})),
                "native_engine_source_sha256": copy.deepcopy(reconciled_result["native_engine_source_sha256"]),
                "original_reconciliation": check}
    if combined is None or not future:
        return {**identity, "realizability": "PENDING", "pending_reasons": [missing or "NO_FORWARD_SESSION"], "events": [], "wallet_events": [], "conversion": None}
    if native is None:
        from backtest.engines.global_equity import GlobalEquityEngine
        native = GlobalEquityEngine
    from backtest.engines.base import BaseEngine
    fingerprints = {"GlobalEquityEngine": digest(inspect.getfile(native)), "BaseEngine": digest(inspect.getfile(BaseEngine))}
    if fingerprints != reconciled_result["native_engine_source_sha256"]:
        raise ContractError("exit native source differs from reconciled study")
    base = historical_class(native)

    class Exit(base):
        def _background_through(self, until):
            pass  # the study's opportunity-cost series is preserved separately

        def _execute_target_rebalance(self, weights, data_map, ts, equity, codes):
            for symbol in codes:
                held = self.held(symbol)
                if held == 0:
                    continue
                bar = self.bar_lookup[self.account_day, symbol]
                self.emit("EXIT_ATTEMPT", symbol=symbol, side="SELL", requested_qty=held, observed_at=bar["open_at"])
                locked = held - self.sellable(symbol)
                prior = self.prior_volumes[symbol]
                reason = ("SCENARIO_DELAY" if self.scenario == "C3" and self.account_day == future[0] else
                    "MISSING_OPEN" if bar["open"] is None else
                    "LATE_OPEN_QUOTE" if bar["open_quote_known_at"] is None or stamp(bar["open_quote_known_at"]) > stamp(bar["open_at"]) else
                    "UNKNOWN_OPEN_TRADABILITY" if bar["open_status"] == "unknown" else
                    "HALTED_AT_OPEN" if bar["open_status"] == "halted" else
                    "UNKNOWN_OPEN_LIQUIDITY" if bar["open_capacity_shares"] is None else
                    "ZERO_OPEN_LIQUIDITY" if bar["open_capacity_shares"] == 0 else
                    "VOLUME_WARMUP_INCOMPLETE" if len(prior) < 20 else
                    "UNSETTLED_SHARES" if self.sellable(symbol) == 0 else None)
                if reason:
                    self.emit("EXIT_CANCEL", symbol=symbol, side="SELL", requested_qty=held, reason=reason)
                    continue
                capacity = min(int(sum(prior[-20:]) / Decimal(20) * Decimal("0.001")), bar["open_capacity_shares"])
                qty = min(held - locked, capacity)
                if qty == 0:
                    self.emit("EXIT_CANCEL", symbol=symbol, side="SELL", requested_qty=held, reason="CAPACITY")
                    continue
                # Existing native close/reduction and dated fee functions own
                # all stock cash effects and settlement journal entries.
                price = self.apply_slippage(float(bar["open"]), -1)
                fee = self.calc_commission(qty, price, 1, False)
                if qty * number(str(price)) - number(str(fee)) <= 0:
                    self.emit("EXIT_CANCEL", symbol=symbol, side="SELL", requested_qty=held, reason="NONPOSITIVE_NET_PROCEEDS")
                    continue
                self.order_metadata = {"requested_qty": held, "decision_date": data["end"], "intent_source": VERSION,
                                       "reduction_reasons": ["CAPACITY_OR_UNSETTLED_SHARES"] if qty < held else []}
                self.phase = "sell"
                if qty == held:
                    self._close_position(symbol, price, ts, "hypothetical_exit")
                else:
                    self._execute_partial_reduction(self._plan_reduction(self.positions[symbol], held - qty, price), ts)
                self.phase = None
                if qty < held:
                    self.emit("EXIT_REMAINDER_CANCEL", symbol=symbol, unfilled_qty=held-qty, reason="CAPACITY_OR_UNSETTLED_SHARES")

    with offline_connections() as attempts:
        import pandas as pd
        config = {"initial_cash": float(data["initial"]["settled_cash"]), "position_adjustment": "rebalance", "leverage": 1.0, "rebalance_mask": future}
        engine = Exit(config, data)
        _restore(engine, data, reconciled_result, replay, forward)
        engine.model, engine.account_data = combined["historical_model"], combined
        engine.terms = TradingTerms(forward["terms"])
        engine.calendar_rows = {r["trade_date"]: r for r in combined["historical_artifact"]["calendar"]}
        engine.bar_lookup = {(r["date"], r["symbol"]): r for r in combined["bars"]}
        raw = {(r["trade_date"], r["symbol"]): r for r in data["historical_artifact"]["bars"]}
        raw.update({(r["date"], r["symbol"]): r for r in forward["bars"]})
        engine.source_bars = raw
        engine.actions_by_day = {}
        for action in combined["actions"]:
            engine.actions_by_day.setdefault(action["effective_date"], []).append(action)
        frames = {}
        for symbol in data["symbols"]:
            frame = pd.DataFrame([engine.bar_lookup[d, symbol] for d in future])
            frame.index = pd.DatetimeIndex(pd.to_datetime(frame["date"])).as_unit("ns")
            for key in ("open", "high", "low", "close", "volume"):
                frame[key] = pd.to_numeric(frame[key])
            frames[symbol] = frame
        dates = frames[data["symbols"][0]].index
        close = pd.DataFrame({s: frames[s]["close"] for s in data["symbols"]}, index=dates)
        targets = pd.DataFrame(0.0, index=dates, columns=data["symbols"])
        engine._execute_bars(dates, frames, close, targets, data["symbols"], close_val_df=close)
        if attempts:
            raise ContractError("exit attempted a network connection")
    nstock, nwallet = len(reconciled_result["events"]), len(reconciled_result["wallet_events"])
    combined_result = {**copy.deepcopy(reconciled_result), "events": engine.account_events, "wallet_events": engine.wallet_journal,
                       "snapshots": engine.account_snapshots, "cny_snapshots": engine.cny_snapshots}
    stock_terminal = engine.account_snapshots[-1]
    wallet_terminal = engine.cny_snapshots[-1]
    reasons = []
    if any(stock_terminal["holdings"].values()): reasons.append("UNSOLD_SHARES")
    if engine.pending_cash: reasons.append("UNSETTLED_PROCEEDS")
    if engine.pending_shares: reasons.append("UNSETTLED_SHARES")
    if engine.receivables: reasons.append("UNPAID_DIVIDENDS")
    if any(engine.wallet.unpaid_interest.values()): reasons.append("UNPAID_INTEREST")
    if hasattr(engine.wallet, "owed") and any(engine.wallet.owed.values()): reasons.append("UNPAID_FIXED_EXPENSES")
    conversion = None
    quotes = forward["fx"]["execution_quotes"]
    at = wallet_terminal["as_of"]
    quote = quotes.get(future[-1])
    # Cash-only conversion may execute while other rights remain. Its result
    # stays partial; it never turns receivables/securities into available USD.
    if quote is None:
        reasons.append("MISSING_EXIT_FX_QUOTE")
    else:
        available = number(engine.wallet.balances()["USD"]["available"])
        _, _, _, fixed_fee = engine.wallet._fx(quote, stamp(at), execution=True, extra_spread_rate=engine.fx_stress())
        if available > fixed_fee:
            conversion = engine.wallet.exchange("USD_TO_CNY", str(available - fixed_fee), execution_quote=quote,
                as_of=at, event_id="hypothetical-exit-fx", extra_spread_rate=engine.fx_stress())
        elif available:
            reasons.append("FX_FEE_EXCEEDS_AVAILABLE_CASH")
        wallet_terminal = engine.wallet.snapshot(as_of=at, fx_mark=forward["fx"]["close_marks"][future[-1]],
            usd_assets=str(sum(engine.held(s)*engine.account_marks[s] for s in data["symbols"])))
    result = {**identity, "realizability": "COMPLETE_CASH_EXIT" if not reasons else "PENDING", "pending_reasons": reasons,
              "events": copy.deepcopy(engine.account_events[nstock:]), "wallet_events": copy.deepcopy(engine.wallet_journal[nwallet:] + (conversion["events"] if conversion is not None else [])),
              "snapshots": copy.deepcopy(engine.account_snapshots[len(reconciled_result["snapshots"]):]),
              "cny_snapshots": copy.deepcopy(engine.cny_snapshots[len(reconciled_result["cny_snapshots"]):]),
              "conversion": copy.deepcopy(conversion), "exit_wallet_snapshot": wallet_terminal,
              "reconciliation_data": combined, "reconciliation_result": combined_result,
              "native_fill_records": [{**asdict(f), "timestamp": str(f.timestamp)} for f in engine.fill_records],
              "native_engine_source_sha256": fingerprints, "network_attempts": 0}
    result["independent_reconciliation"] = reconcile_exit(data, reconciled_result, forward, result)
    if not result["independent_reconciliation"]["passed"]:
        raise ContractError("hypothetical exit failed independent reconciliation: " + str(result["independent_reconciliation"]["errors"][:3]))
    return result


class _ExitReplay(_Replay):
    def _decision(self, state, day, symbol):
        from fractions import Fraction
        held = state["holdings"][symbol]
        locked = sum(p[1] for p in state["locked"].values() if p[0] == symbol)
        bar = self.quotes[day, symbol]
        at = _stamp(bar["open_at"])
        first_future = min(d for d in self.days if d > self.study_end)
        position = self.sessions.index(day)
        prior = [self.forward_bars.get((d, symbol)) for d in self.sessions[max(0, position-20):position]]
        valid_prior = len(prior) == 20 and all(b is not None and b["volume"] is not None and _stamp(b["available_at"]) < at for b in prior)
        reason = ("SCENARIO_DELAY" if self.scenario == "C3" and day == first_future else
            "MISSING_OPEN" if bar["open"] is None else
            "LATE_OPEN_QUOTE" if bar["open_quote_known_at"] is None or _stamp(bar["open_quote_known_at"]) > at else
            "UNKNOWN_OPEN_TRADABILITY" if bar["open_status"] == "unknown" else
            "HALTED_AT_OPEN" if bar["open_status"] == "halted" else
            "UNKNOWN_OPEN_LIQUIDITY" if bar["open_capacity_shares"] is None else
            "ZERO_OPEN_LIQUIDITY" if bar["open_capacity_shares"] == 0 else
            "VOLUME_WARMUP_INCOMPLETE" if not valid_prior else
            "UNSETTLED_SHARES" if held-locked == 0 else None)
        if reason:
            return 0, reason
        capacity = min(int(sum((_f(b["volume"]) for b in prior), Fraction(0)) / 20 / 1000), bar["open_capacity_shares"])
        qty = min(held-locked, capacity)
        if qty == 0:
            return 0, "CAPACITY"
        _, price, components, _ = self._order(day, symbol, "SELL", qty)
        if qty*price-sum((c["amount"] for c in components), Fraction(0)) <= 0:
            return 0, "NONPOSITIVE_NET_PROCEEDS"
        return qty, None

    def _stock_event(self, state, event):
        day, kind = event["date"], event["type"]
        if kind.startswith("EXIT_"):
            if kind not in {"EXIT_ATTEMPT", "EXIT_CANCEL", "EXIT_REMAINDER_CANCEL"}:
                raise ContractError("unknown exit stock event")
            symbol = event["symbol"]
            held = state["holdings"][symbol]
            if kind == "EXIT_ATTEMPT":
                self.check("exit requested inventory:"+str(event["sequence"]), event["requested_qty"], held)
                self.check("exit side", event["side"], "SELL")
                self.check("exit observed at actual open", event["observed_at"], self.quotes[day, symbol]["open_at"])
                qty, reason = self._decision(state, day, symbol)
                self.exit_decisions[day, symbol] = (held, qty, reason)
            elif kind == "EXIT_CANCEL":
                held, qty, reason = self.exit_decisions[day, symbol]
                self.check("exit cancellation quantity", event["requested_qty"], held)
                self.check("exit cancellation eligible zero", qty, 0)
                self.check("exit cancellation reason", event["reason"], reason)
            else:
                held, qty, reason = self.exit_decisions[day, symbol]
                self.check("exit cancelled remainder", event["unfilled_qty"], held-qty)
            return
        if kind == "FILL" and day > self.study_end:
            _, qty, reason = self.exit_decisions[day, event["symbol"]]
            self.check("exit fill independently eligible", reason, None)
            self.check("exit eligible quantity", event["qty"], qty)
            self.check("exit sell only", event["side"], "SELL")
        super()._stock_event(state, event)


def reconcile_exit(data, study, forward, result):
    """Fraction full-prefix replay plus separate post-settlement FX replay."""
    try:
        extended, future, missing = _extended(data, forward)
        if missing or extended is None:
            raise ContractError("no runnable forward evidence for reconciliation")
        if result["study_result_sha256"] != canonical_hash(study) or result["study_terminal"] != {"stock": study["snapshots"][-1], "cny": study["cny_snapshots"][-1]}:
            raise ContractError("study result or terminal mark changed")
        ledger = result["reconciliation_result"]
        for name in ("events", "wallet_events", "snapshots", "cny_snapshots"):
            if ledger[name][:len(study[name])] != study[name]:
                raise ContractError("exit changed historical journal prefix")
        for name in ("events", "snapshots", "cny_snapshots"):
            if result[name] != ledger[name][len(study[name]):]:
                raise ContractError("public exit journal differs from reconciled suffix")
        conversion_rows = result["conversion"]["events"] if result["conversion"] is not None else []
        if result["wallet_events"] != ledger["wallet_events"][len(study["wallet_events"]):] + conversion_rows:
            raise ContractError("public exit wallet journal differs from reconciled suffix")
        oracle = _ExitReplay(extended, ledger, "0.00000001")
        oracle.study_end, oracle.exit_decisions = data["end"], {}
        oracle.forward_bars = {(b["trade_date"], b["symbol"]): b for b in data["historical_artifact"]["bars"]}
        oracle.forward_bars.update({(b["date"], b["symbol"]): b for b in forward["bars"]})
        check = oracle.run()
        for day in future:
            prefix = max((i+1 for i,e in enumerate(ledger["events"]) if e["date"] < day), default=0)
            opening_held = oracle._stock(prefix)["holdings"]
            for action in extended["actions"]:
                if action["effective_date"] == day and action["type"] == "split":
                    opening_held[action["symbol"]] = int(opening_held[action["symbol"]]*_f(action["numerator"])/_f(action["denominator"]))
            expected_symbols = sorted(s for s,q in opening_held.items() if q)
            actual_symbols = sorted(e["symbol"] for e in ledger["events"] if e["date"] == day and e["type"] == "EXIT_ATTEMPT")
            oracle.check("daily exit attempts exactly once:"+day, actual_symbols, expected_symbols)
        conversion = result["conversion"]
        state = oracle._wallet(len(ledger["wallet_events"]))
        final = result["exit_wallet_snapshot"]
        at = _stamp(final["as_of"])
        stock_state = oracle._stock(len(ledger["events"]))
        reasons = []
        if any(stock_state["holdings"].values()): reasons.append("UNSOLD_SHARES")
        if stock_state["pending_cash"]: reasons.append("UNSETTLED_PROCEEDS")
        if stock_state["locked"]: reasons.append("UNSETTLED_SHARES")
        if stock_state["receivables"]: reasons.append("UNPAID_DIVIDENDS")
        if any(state["unpaid"].values()): reasons.append("UNPAID_INTEREST")
        if any(oracle._owed(state, c) for c in ("CNY", "USD")): reasons.append("UNPAID_FIXED_EXPENSES")
        quote = forward["fx"]["execution_quotes"].get(future[-1])
        if quote is None:
            reasons.append("MISSING_EXIT_FX_QUOTE")
        else:
            extra = _f("0.0025") if data["historical_model"]["scenario"] in {"C2", "C3"} else _f("0")
            _, _, _, fee = oracle._fx_quote(quote, at, extra)
            available = oracle._available(state, "USD")
            oracle.check("exit conversion required iff affordable", conversion is not None, available > fee)
            if available and available <= fee:
                reasons.append("FX_FEE_EXCEEDS_AVAILABLE_CASH")
        oracle.check("exit pending reasons", sorted(result["pending_reasons"]), sorted(reasons))
        oracle.check("exit realizability", result["realizability"], "PENDING" if reasons else "COMPLETE_CASH_EXIT")
        if conversion is not None:
            quote = forward["fx"]["execution_quotes"][future[-1]]
            extra = _f("0.0025") if data["historical_model"]["scenario"] in {"C2", "C3"} else _f("0")
            _, bid, _, fee = oracle._fx_quote(quote, at, extra)
            available = oracle._available(state, "USD")
            row = forward["cash_rules"]["money_rounding"]["CNY"]
            amount = _round((available-fee)*bid, _f(row["quantum"]), row["mode"])
            event = conversion["events"][0]
            for key, value in (("principal", available-fee), ("debit", available), ("credit", amount), ("source_fixed_fee", fee), ("execution_cny_per_usd", bid), ("extra_spread_rate", extra)):
                oracle.check("exit FX:"+key, event[key], value)
            oracle.check("exit FX direction", event["direction"], "USD_TO_CNY")
            oracle.check("exit FX quote", event["quote"], quote)
            state["settled"]["USD"] -= available
            state["settled"]["CNY"] += amount
        holdings = oracle._stock(len(ledger["events"]))["holdings"]
        marks = oracle._marks(future[-1])
        assets = sum((holdings[s]*marks[s] for s in holdings), _f("0"))
        oracle._wallet_snapshot(final, state, assets, forward["fx"]["close_marks"][future[-1]], opening=True)
        return {"passed": not oracle.errors, "checks": oracle.checks, "errors": oracle.errors, "replayed": check["replayed"]}
    except (ContractError, ValueError, KeyError, TypeError, IndexError) as error:
        return {"passed": False, "errors": [{"field": "exit contract", "actual": str(error)}]}
