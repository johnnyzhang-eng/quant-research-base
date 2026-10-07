"""Conditional historical CNY account over the existing local Vibe stock engine.

No market downloads or broker calls. Original synthetic entry points retain
their guards. The new path consumes fully bound, explicitly classified inputs.
"""
import copy
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
import inspect
from zoneinfo import ZoneInfo

from .cash_account import CashAccount, number, stamp
from .evidence import ContractError, canonical_hash, digest, now
from .historical_contract import validate_account_input
from .historical_input import recheck_use_rights
from .historical_signals import ASSETS
from .trading_terms import TradingTerms
from .vibe_accounting import accounting_class
from .vibe_controls import aligned_class, offline_connections

VERSION = "historical-account/1"


def historical_class(native):
    class HistoricalAccount(accounting_class(aligned_class(native), input_mode="historical_model")):
        def __init__(self, config, data):
            self.model = data["historical_model"]
            self.terms = TradingTerms(self.model["terms"])
            self.scenario = self.model["scenario"]
            self.expenses_enabled = self.model["schema_version"] == "historical-account-model/2"
            if self.expenses_enabled:
                from .historical_expenses import ExpenseWallet
                self.wallet = ExpenseWallet(self.model["cash_rules"], expense_review=self.model["expense_review"],
                                            fixed_expenses=self.model["fixed_expenses"])
            else:
                self.wallet = CashAccount(self.model["cash_rules"])
            self.background = CashAccount(self.model["cash_rules"])
            self.cny_snapshots, self.background_snapshots, self.wallet_journal = [], [], []
            self.wallet_cursor = 0
            boundary = self.model["performance_boundary"]
            calendars = {r["trade_date"]: r for r in data["historical_artifact"]["calendar"]}
            self.calendar_rows = calendars
            self.source_bars = {(r["trade_date"], r["symbol"]): r for r in data["historical_artifact"]["bars"]}
            self.opening_cny_snapshot = self.wallet.snapshot(as_of=calendars[boundary["date"]]["close_at"],
                fx_mark=boundary["fx_mark"], usd_assets="0")
            self.opening_cny_snapshot["date"] = boundary["date"]
            entry = self.model["fx"]["entry"]
            day = self.wallet.last_accrued_day + timedelta(days=1)
            while stamp(self.wallet.cutoff_at(day.isoformat())) < stamp(entry["at"]):
                if self.expenses_enabled:
                    self.wallet.book_expenses(as_of=self.wallet.cutoff_at(day.isoformat()))
                self.wallet.accrue_day(day.isoformat(), as_of=self.wallet.cutoff_at(day.isoformat()))
                if self.expenses_enabled:
                    self.wallet.book_expenses(as_of=self.wallet.cutoff_at(day.isoformat()))
                day += timedelta(days=1)
            self.wallet.exchange("CNY_TO_USD", entry["principal_cny"], execution_quote=entry["execution_quote"],
                as_of=entry["at"], event_id="entry", extra_spread_rate=self.fx_stress())
            if number(data["initial"]["settled_cash"]) != self.wallet.settled["USD"]:
                raise ContractError("prepared entry cash differs from reconstructed wallet")
            self._capture_wallet_events(0, None)
            super().__init__(config, {}, data)

        def fx_stress(self):
            return "0.0025" if self.scenario in {"C2", "C3"} else "0"

        def opening_at(self):
            return self.calendar_rows[self.account_day]["open_at"]

        def closing_at(self):
            return self.calendar_rows[self.account_day]["close_at"]

        def _capture_wallet_events(self, stock_cursor, stock_date):
            events = copy.deepcopy(self.wallet.events[self.wallet_cursor:])
            for event in events:
                event["source_stock_event_sequence"] = stock_cursor
                event["stock_snapshot_date"] = stock_date
            self.wallet_journal.extend(events)
            self.wallet_cursor = len(self.wallet.events)

        def _sync_wallet(self, at):
            self.wallet.sync_usd(settled_usd=self.cash_for_native_state(),
                receivables_usd=self.assets_not_cash(), as_of=at,
                event_id=f"stock-sync-{len(self.wallet.events)+1}")
            self._capture_wallet_events(len(self.account_events), self.account_day)

        def cash_for_native_state(self):
            return Decimal(str(self.capital)).quantize(Decimal("0.00000001"))

        def cash_for_planning(self):
            return max(Decimal(0), self.cash_for_native_state() + min(Decimal(0), self.wallet.unpaid_interest["USD"]) - self.owed("USD"))

        def owed(self, currency):
            return self.wallet.owed[currency] if self.expenses_enabled else Decimal(0)

        def _post_expenses(self, at):
            if not self.expenses_enabled:
                return
            posted = self.wallet.book_expenses(as_of=at)
            self._capture_wallet_events(len(self.account_events), self.account_day)
            for event in posted["usd_payment_events"]:
                amount = number(event["amount"])
                self.capital -= float(amount)
                self.emit("CASH_EXPENSE_DEBIT", amount=amount, source_wallet_sequence=event["sequence"],
                          source_expense_id=event["expense_id"], debited_at=event["at"])

        def _accrue_through(self, until, *, inclusive):
            while True:
                day = self.wallet.last_accrued_day + timedelta(days=1)
                at = self.wallet.cutoff_at(day.isoformat())
                if stamp(at) > stamp(until) or (not inclusive and stamp(at) == stamp(until)):
                    return
                # Payment belongs to its actual calendar day, including a bank
                # day without a stock session. The stock journal records when
                # its engine hook books it, with the effective time retained.
                paid = False
                for action_id, receivable in list(self.receivables.items()):
                    action = next(a for a in self.account_data["actions"] if a["id"] == action_id)
                    if stamp(action["payment_at"]) <= stamp(at):
                        self.capital += float(receivable["amount"])
                        self.emit("DIV_PAY", action_id=action_id, amount=receivable["amount"],
                                  effective_pay_date=receivable["due"], payment_at=action["payment_at"],
                                  posting_basis="modelled_at_declared_cutoff")
                        del self.receivables[action_id]
                        paid = True
                if paid:
                    self._sync_wallet(at)
                self._post_expenses(at)
                accrued = self.wallet.accrue_day(day.isoformat(), as_of=at)
                self._capture_wallet_events(len(self.account_events), self.account_day)
                for event in accrued["events"]:
                    if event["currency"] == "USD" and number(event["credited"]) != 0:
                        amount = number(event["credited"])
                        self.capital += float(amount)
                        self.emit("CASH_INTEREST_CREDIT", amount=amount, source_wallet_sequence=event["sequence"],
                                  source_wallet_day=event["day"], credited_at=event["at"])
                self._post_expenses(at)

        def _background_through(self, until):
            while True:
                day = self.background.last_accrued_day + timedelta(days=1)
                at = self.background.cutoff_at(day.isoformat())
                if stamp(at) > stamp(until):
                    return
                self.background.accrue_day(day.isoformat(), as_of=at)

        def before_rebalance_bar(self, timestamp, data_map, codes):
            self.account_day = str(timestamp.date())
            self._accrue_through(self.opening_at(), inclusive=False)
            stopped = super().before_rebalance_bar(timestamp, data_map, codes)
            # The capacity screen uses exactly the preceding twenty sessions,
            # never a substitute older set or yet-unreceived full-day volume.
            sessions = self.account_data["calendar"]["sessions"]
            index = sessions.index(self.account_day)
            for symbol in codes:
                prior = [self.source_bars[d, symbol] for d in sessions[max(0, index-20):index]]
                self.prior_volumes[symbol] = ([number(b["volume"]) for b in prior]
                    if len(prior) == 20 and all(stamp(b["available_at"]) < stamp(self.opening_at()) for b in prior) else [])
            self._sync_wallet(self.opening_at())
            return stopped

        def dividend_payment_due(self, receivable):
            return False  # declared calendar-day payment hook above owns posting

        def _fx_at_open(self):
            quotes = [self.model["performance_boundary"]["fx_mark"]]
            quotes += [q for day, q in self.model["fx"]["close_marks"].items() if day < self.account_day]
            timely = [q for q in quotes if stamp(q["quoted_at"]) <= stamp(self.opening_at())]
            if not timely:
                raise ContractError("no nonfuture FX price available for full-account allocation")
            quote = max(timely, key=lambda q: stamp(q["quoted_at"]))
            # snapshot validates both source/model clocks without changing cash.
            snapshot = self.wallet.snapshot(as_of=self.opening_at(), fx_mark=quote, usd_assets="0")
            return number(snapshot["fx_mark_cny_per_usd"]), quote

        def _calc_open_equity(self, data_map, close_df, ts):
            value = self.cash_for_native_state() + self.assets_not_cash() + self.wallet.unpaid_interest["USD"] - self.owed("USD")
            for symbol in self.account_data["symbols"]:
                value += self.held(symbol) * self.planning_mark(symbol)
            rate, quote = self._fx_at_open()
            cny_assets = self.wallet.settled["CNY"] + self.wallet.receivables["CNY"] + self.wallet.unpaid_interest["CNY"] - self.owed("CNY")
            value += cny_assets / rate
            self.opening_allocation_fx = copy.deepcopy(quote)
            return float(value)

        def planning_mark(self, symbol):
            bar = self.bar_lookup[self.account_day, symbol]
            timely = (bar["open_quote_known_at"] is not None
                      and stamp(bar["open_quote_known_at"]) <= stamp(bar["open_at"]))
            return number(bar["open"]) if bar["open"] is not None and timely else self.account_marks[symbol]

        def _calc_equity(self, close_df, ts):
            # Native USD snapshots remain a USD sleeve, while allocation and
            # performance use the complete CNY account in separate fields.
            return super()._calc_equity(close_df, ts) + float(self.wallet.unpaid_interest["USD"] - self.owed("USD"))

        def closing_information_known(self, bar):
            return stamp(bar["available_at"]) <= stamp(self.wallet.cutoff_at(self.account_day))

        def _safe_price(self, close_df, ts, symbol, fallback, **kwargs):
            bar = self.bar_lookup[str(ts.date()), symbol]
            if not self.closing_information_known(bar):
                return float(self.account_marks[symbol])
            return super()._safe_price(close_df, ts, symbol, fallback, **kwargs)

        def apply_slippage(self, price, direction):
            row = self.terms.order(day=self.account_day, use_at=self.opening_at(),
                side="BUY" if direction > 0 else "SELL", shares=1, raw_open=str(price), scenario=self.scenario)
            return float(row["execution_price"])

        def calc_commission(self, size, price, direction, is_open):
            qty = number(str(size))
            if qty != qty.to_integral_value():
                raise ContractError("integer native fill size required")
            side = "BUY" if (direction > 0 if is_open else direction < 0) else "SELL"
            fees = self.terms.fees_for(day=self.account_day, use_at=self.opening_at(), side=side,
                shares=int(qty), execution_price=str(price), scenario=self.scenario)
            return float(fees["fee_total"])

        def settlement_lag(self, side):
            rule = self.terms.rule(self.account_day, use_at=self.opening_at())
            return rule["settlement"]["share_sessions" if side == "BUY" else "cash_sessions"]

        def fill_fee_details(self, qty, price, side):
            costs = self.terms.fees_for(day=self.account_day, use_at=self.opening_at(), side=side,
                shares=qty, execution_price=str(price), scenario=self.scenario)
            return {"commission": sum((number(f["amount"]) for f in costs["fees"] if f["category"] == "broker"), Decimal(0)),
                    "platform": Decimal(0), "tax": sum((number(f["amount"]) for f in costs["fees"] if f["category"] == "tax"), Decimal(0)),
                    "fees": costs["fees"], "rule_id": costs["rule_id"], "terms_sha256": costs["terms_sha256"]}

        def _record_fill(self, **fields):
            super()._record_fill(**fields)
            event = self.account_events[-1]
            expected = number(event["commission"]) + number(event["platform"]) + number(event["tax"])
            if abs(number(event["fee"]) - expected) > Decimal("0.00000001"):
                raise ContractError("native fee differs from dated component charges")

        def _execute_target_rebalance(self, weights, data_map, ts, equity, codes):
            if number(str(equity)).quantize(Decimal("0.00000001")) <= 0:
                intents = [p for p in self.account_data["control_intents"] if self.execution_date(p) == self.account_day]
                if intents:
                    self.emit("TARGET_PLAN", decision_date=intents[0]["decision_date"],
                        opening_equity=number(str(equity)).quantize(Decimal("0.00000001")),
                        allocation_policy="TRADE_HALTED_NONPOSITIVE_NAV", deltas={s: 0 for s in codes},
                        allocation_basis="FULL_CNY_ACCOUNT_CONVERTED_AT_NONFUTURE_FX",
                        allocation_fx_quote=copy.deepcopy(self.opening_allocation_fx))
                return
            before = len(self.account_events)
            super()._execute_target_rebalance(weights, data_map, ts, equity, codes)
            for event in self.account_events[before:]:
                if event["type"] == "TARGET_PLAN":
                    event["allocation_basis"] = "FULL_CNY_ACCOUNT_CONVERTED_AT_NONFUTURE_FX"
                    event["allocation_fx_quote"] = copy.deepcopy(self.opening_allocation_fx)

        def after_rebalance_bar(self, timestamp, data_map, codes):
            stopped = super().after_rebalance_bar(timestamp, data_map, codes)
            self._sync_wallet(self.closing_at())
            cutoff = self.wallet.cutoff_at(self.account_day)
            self._accrue_through(cutoff, inclusive=True)
            self._background_through(cutoff)
            stock = self.account_snapshots[-1]
            stock["settled_cash"] = str(self.cash_for_native_state())
            stock["unsettled_cash"] = str(sum((p["amount"] for p in self.pending_cash), Decimal(0)))
            stock["dividend_receivable"] = str(sum((p["amount"] for p in self.receivables.values()), Decimal(0)))
            stock["equity"] = str(Decimal(str(self._calc_equity(self.close_frame, timestamp))))
            stock["unpaid_usd_interest"] = str(self.wallet.unpaid_interest["USD"])
            if self.expenses_enabled:
                stock["fixed_expense_owed_usd"] = str(self.owed("USD"))
            stock["as_of"] = cutoff
            stock["valuation_marks"] = {s: str(self.account_marks[s]) for s in codes}
            stock["stale_mark_symbols"] = [s for s in codes if not self.closing_information_known(self.bar_lookup[self.account_day, s])]
            security_value = sum((self.held(s) * self.account_marks[s] for s in codes), Decimal(0))
            mark = self.model["fx"]["close_marks"][self.account_day]
            snapshot = self.wallet.snapshot(as_of=cutoff, fx_mark=mark, usd_assets=security_value,
                exit_quote=self.model["fx"]["exit_quotes"][self.account_day], extra_spread_rate=self.fx_stress())
            snapshot.update(date=self.account_day, source_stock_event_sequence=len(self.account_events),
                            source_wallet_sequence=len(self.wallet.events))
            self.cny_snapshots.append(snapshot)
            background = self.background.snapshot(as_of=cutoff, fx_mark=mark, usd_assets="0")
            background["date"] = self.account_day
            self.background_snapshots.append(background)
            return stopped

    return HistoricalAccount


def execute(data, *, native=None):
    """Run the supplied bound model offline; classification survives execution."""
    validate_account_input(data)
    rights_check = recheck_use_rights(data["historical_artifact"], checked_at=now())
    with offline_connections() as network_attempts:
        if native is None:
            from backtest.engines.global_equity import GlobalEquityEngine
            native = GlobalEquityEngine
        import pandas as pd
        frames = {}
        for symbol in ASSETS:
            frame = pd.DataFrame([b for b in data["bars"] if b["symbol"] == symbol])
            frame.index = pd.DatetimeIndex(pd.to_datetime(frame["date"])).as_unit("ns")
            for key in ("open", "high", "low", "close", "volume"):
                frame[key] = pd.to_numeric(frame[key])
            frames[symbol] = frame
        dates = frames[ASSETS[0]].index
        close = pd.DataFrame({s: frames[s]["close"] for s in ASSETS}, index=dates)
        targets = pd.DataFrame(0.0, index=dates, columns=ASSETS)
        due_dates = []
        for intent in data["control_intents"]:
            due = pd.Timestamp(intent["execution_date"])
            for symbol in ASSETS:
                targets.loc[due, symbol] = float(intent["weights"][symbol])
            due_dates.append(intent["execution_date"])
        config = {"initial_cash": float(data["initial"]["settled_cash"]),
                  "position_adjustment": "rebalance", "leverage": 1.0,
                  "rebalance_mask": due_dates or [data["start"]]}
        engine = historical_class(native)(config, data)
        engine._execute_bars(dates, frames, close, targets, list(ASSETS), close_val_df=close)
        if engine.terminal_mark is not None:
            engine.actual_position_snapshots[-1] = engine.terminal_positions
        if network_attempts:
            raise ContractError("offline historical model attempted a network connection")
        from backtest.engines.base import BaseEngine
        result = {"schema_version": VERSION, "classification": "CONDITIONAL_HISTORICAL_ACCOUNT_MODEL",
            "data_kind": data["data_kind"], "historical_engine_ready": False, "goal_complete": False,
            "events": engine.account_events, "snapshots": engine.account_snapshots,
            "wallet_events": engine.wallet_journal, "cny_snapshots": engine.cny_snapshots,
            "opening_cny_snapshot": engine.opening_cny_snapshot,
            "cny_cash_background_events": engine.background.events,
            "cny_cash_background_snapshots": engine.background_snapshots,
            "native_fill_records": [{**asdict(f), "timestamp": str(f.timestamp)} for f in engine.fill_records],
            "native_equity_snapshots": [{**asdict(s), "timestamp": str(s.timestamp)} for s in engine.equity_snapshots],
            "native_engine_source_sha256": {"GlobalEquityEngine": digest(inspect.getfile(native)),
                                            "BaseEngine": digest(inspect.getfile(BaseEngine))},
            "lineage": copy.deepcopy(data["lineage"]), "decision_calendar": sorted(due_dates),
            "network_attempts": 0,
            "source_use_recheck": rights_check,
            "limitations": ["Conditional model execution does not certify source, cash route or opening fills",
                            "Actual/assumed receipt clocks remain in input lineage; historical cuts were previously seen",
                            "Daily CNY marks use regular stock close prices at the explicit later cash cutoff",
                            "Terminal mark retains positions; complete liquidation requires its own future execution path"]}
        return result


def performance_input(data, result):
    """Whole-CNY diagnostics, including entry costs and aligned CNY cash rates.

    USD trade-cost amounts use that day's valuation FX for disclosure only;
    they are never deducted again from already net account equity.
    """
    model = data["historical_model"]
    snapshots = result["cny_snapshots"]
    initial = result["opening_cny_snapshot"]
    dates = [initial["date"]] + [s["date"] for s in snapshots]
    fills = []
    for event in result["events"]:
        if event["type"] != "FILL":
            continue
        rate = number(model["fx"]["close_marks"][event["date"]]["cny_per_usd"])
        commission, tax = number(event["commission"])*rate, number(event["tax"])*rate
        fills.append({"id": f"stock-fill-{event['sequence']}", "date": event["date"], "symbol": event["symbol"],
            "signed_quantity": str(event["qty"] * (1 if event["side"] == "BUY" else -1)),
            "price": str(number(event["price"])*rate), "commission": str(commission), "platform": "0",
            "tax": str(tax), "fee": str(commission+tax)})
    costs = []
    entry = next(e for e in result["wallet_events"] if e["type"] == "FX_EXECUTION")
    conversion_cost = number(entry["debit"]) - number(entry["credit"]) * number(entry["quote"]["cny_per_usd"])
    if conversion_cost < 0:
        raise ContractError("entry conversion cost unexpectedly negative")
    costs.append({"id": "entry-fx-cost", "date": dates[1], "category": "fx", "amount": str(conversion_cost)})
    background = result["cny_cash_background_snapshots"]
    previous = number(initial["marked_equity_cny"])
    rf = []
    for snapshot in background:
        current = number(snapshot["marked_equity_cny"])
        rf.append({"date": snapshot["date"], "return": str(current/previous-1)})
        previous = current
    context = {
        "data": data["lineage"]["artifact_sha256"],
        "account": canonical_hash({k: model[k] for k in ("instruments", "initial", "cash_rules", "withholding", "dividend_posting")}),
        "costs": canonical_hash({"terms": model["terms"], "scenario": model["scenario"]}),
        "execution": canonical_hash({"model": model["execution"], "policy": "FULL_CNY_SEQUENTIAL_INTEGER_FEE_AWARE"}),
        "fx": canonical_hash(model["fx"]),
    }
    if model["schema_version"] == "historical-account-model/2":
        for event in result["wallet_events"]:
            if event["type"] != "FIXED_EXPENSE_BOOKED":
                continue
            economic_day = stamp(event["at"]).astimezone(ZoneInfo(model["cash_rules"]["timezone"])).date().isoformat()
            recognized = next((d for d in dates[1:] if d >= economic_day), None)
            if recognized is None:
                raise ContractError("booked fixed expense outside measured terminal cutoff")
            amount = number(event["amount"])
            if event["currency"] == "USD":
                amount *= number(model["fx"]["close_marks"][recognized]["cny_per_usd"])
            costs.append({"id": "fixed-expense-" + event["expense_id"], "date": recognized,
                          "category": "fixed", "amount": str(amount)})
        context["costs"] = canonical_hash({"terms": model["terms"], "scenario": model["scenario"],
                                           "fixed_expenses": model["fixed_expenses"], "expense_review": model["expense_review"]})
    return {"schema_version": "1", "currency": "CNY", "sampling": "daily_session_close", "calendar": dates,
        "calendar_source": "bound session inventory; regular stock close carried to explicit calendar-day cash cutoff",
        "periods_per_year": 252, "year_day_basis": 365, "flow_policy": "end_boundary_only",
        "observations": [{"date": initial["date"], "equity": initial["marked_equity_cny"], "external_flow_end": "0"}]+
                        [{"date": s["date"], "equity": s["marked_equity_cny"], "external_flow_end": "0"} for s in snapshots],
        "risk_free": {"source": "same frozen net CNY cash schedule, separate unconverted wallet; not USD cash with FX exposure",
                      "currency": "CNY", "period_returns": rf},
        "fills": fills, "cost_events": costs, "comparison_context": context}
