"""Independent Fraction replay of linked USD stock and CNY/USD wallet journals.

No production TradingTerms, CashAccount, fee/interest/FX or Vibe amount helpers
are imported. Source cursors form a checked dependency graph: wallet sync derives
cash from stock events; engine interest derives money from independently rebuilt
wallet accrual. Reported sync values are never external funding.
"""
from __future__ import annotations

import calendar
import copy
import hashlib
import json
from datetime import date, datetime, time, timedelta
from fractions import Fraction as F
from zoneinfo import ZoneInfo


class ReplayContractError(ValueError):
    pass


def _day(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise ReplayContractError("canonical YYYY-MM-DD date required") from None


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise ReplayContractError("aware timestamp required") from None


def _f(value):
    if isinstance(value, bool) or isinstance(value, float):
        raise ReplayContractError("exact monetary string/integer required")
    try:
        return F(value)
    except (TypeError, ValueError, ZeroDivisionError):
        raise ReplayContractError("invalid monetary fraction") from None


def _round(value, quantum, mode):
    ratio = value / quantum
    n, remainder = divmod(ratio.numerator, ratio.denominator)
    if mode in {"ceil", "ceiling"}:
        n += bool(remainder)
    elif mode in {"floor"}:
        pass
    elif mode == "DOWN":
        n = abs(ratio.numerator) // ratio.denominator * (-1 if ratio < 0 else 1)
    elif mode in {"HALF_UP", "half_up"}:
        absolute = abs(ratio)
        n = (absolute.numerator * 2 + absolute.denominator) // (2 * absolute.denominator)
        n *= -1 if ratio < 0 else 1
    elif mode == "HALF_EVEN":
        if remainder * 2 > ratio.denominator or (remainder * 2 == ratio.denominator and n % 2):
            n += 1
    else:
        raise ReplayContractError("unknown rounding mode")
    return n * quantum


def independent_replay(data, result, monetary_tolerance="0.00000001"):
    try:
        return _Replay(data, result, monetary_tolerance).run()
    except (ReplayContractError, ValueError, KeyError, TypeError, IndexError, ZeroDivisionError) as error:
        return {"method": "independent Fraction stock/wallet dependency replay", "passed": False,
                "monetary_tolerance": monetary_tolerance,
                "errors": [{"field": "input/event contract", "actual": str(error), "expected": "complete acyclic explicit journal"}],
                "replayed": []}


class _Replay:
    def __init__(self, data, result, tolerance):
        self.data, self.result = data, result
        self.model = data["historical_model"]
        self.terms = self.model["terms"]
        self.rules = self.model["cash_rules"]
        self.fx = self.model["fx"]
        self.scenario = self.model["scenario"]
        if self.scenario not in {"C0", "C1", "C2", "C3"}:
            raise ReplayContractError("scenario missing")
        self.tolerance = _f(tolerance)
        if not 0 <= self.tolerance <= F(1, 10**8):
            raise ReplayContractError("monetary tolerance outside USD0..1e-8")
        self.errors, self.checks, self.replayed = [], 0, []
        self.events, self.wallet_events = result["events"], result["wallet_events"]
        self._sequences(self.events, "stock")
        self._sequences(self.wallet_events, "wallet")
        self.wallet_indices = {e["sequence"]: i + 1 for i, e in enumerate(self.wallet_events)}
        self.stock_indices = {e["sequence"]: i + 1 for i, e in enumerate(self.events)}
        self.quotes = {(b["date"], b["symbol"]): b for b in data["bars"]}
        self.sessions = data["calendar"]["sessions"]
        if self.sessions != sorted(set(self.sessions)):
            raise ReplayContractError("duplicate or unordered sessions")
        for day in self.sessions:
            _day(day)
        self.days = [d for d in self.sessions if data["start"] <= d <= data["end"]]
        self.stock_cache, self.wallet_cache, self.visiting = {}, {}, set()
        self.credit_values = {}
        self.marks_cache = {}
        self.seen_credit_sources = set()
        self.action_positions = {}
        self.expected_attempts, self.observed_attempts = [], []
        entry = [i + 1 for i, e in enumerate(self.wallet_events) if e["type"] == "FX_EXECUTION"]
        self.check("one explicit entry conversion", len(entry), 1)
        if len(entry) != 1:
            raise ReplayContractError("one entry FX event required; no implicit terminal conversion")
        self.entry_index = entry[0]
        self.check("opening stock holdings zero", data["initial"]["holdings"], {s: 0 for s in data["symbols"]})
        first_open = min(_stamp(self.quotes[self.days[0], symbol]["open_at"]) for symbol in data["symbols"])
        self.check("entry no later than first open", _stamp(self.fx["entry"]["at"]) <= first_open, True)
        self.timezone = ZoneInfo(self.rules["timezone"])
        self.clock = self.rules["knowledge_clock"]
        self.expense_mode = self.model.get("schema_version") == "historical-account-model/2"
        self.expense_rows = {}
        self.expense_payment_values = {}
        self.seen_expense_payment_sources = set()
        if self.expense_mode:
            self._expense_configuration()
        for e in self.wallet_events:
            self.check("wallet clock:" + str(e["sequence"]), e.get("knowledge_clock"), self.clock)

    def _expense_configuration(self):
        review, rows = self.model["expense_review"], self.model["fixed_expenses"]
        fields = {"schema_version", "reviewer", "scope", "reviewed_at", "coverage_start_date",
                  "coverage_end_date", "explicit_zero", "schedule_sha256", "evidence"}
        if not isinstance(review, dict) or set(review) != fields or not isinstance(rows, list):
            raise ReplayContractError("explicit model-2 expense schedule/review required")
        checksum = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False,
                            allow_nan=False, separators=(",", ":")).encode()).hexdigest()
        self.check("expense schedule hash", review["schedule_sha256"], checksum)
        self.check("expense review schema", review["schema_version"], "fixed-expense-review/1")
        self.check("expense review author/scope", all(isinstance(review[k], str) and bool(review[k].strip()) for k in ("reviewer", "scope")), True)
        self.check("explicit zero expense schedule", review["explicit_zero"], not rows)
        freeze, reviewed = _stamp(self.clock["freeze_at"]), _stamp(review["reviewed_at"])
        self.check("expense review before freeze", reviewed <= freeze, True)
        self._expense_evidence(review["evidence"], min(reviewed, freeze))
        start, end = _day(review["coverage_start_date"]), _day(review["coverage_end_date"])
        self.check("expense coverage includes funding", start <= _stamp(self.rules["start_at"]).astimezone(self.timezone).date() <= end, True)
        self.check("expense coverage includes account end", _day(self.data["end"]) <= end, True)
        order = []
        for row in rows:
            if set(row) != {"schema_version", "id", "currency", "amount", "book_at", "category", "evidence"}:
                raise ReplayContractError("fixed expense schema differs")
            expense_id = row["id"]
            if not isinstance(expense_id, str) or not expense_id.strip() or expense_id in self.expense_rows:
                raise ReplayContractError("duplicate/empty fixed expense id")
            if row["currency"] not in {"CNY", "USD"}:
                raise ReplayContractError("fixed expense currency differs")
            at, amount = _stamp(row["book_at"]), _f(row["amount"])
            local_day = at.astimezone(self.timezone).date()
            cutoff = datetime.combine(local_day, time.fromisoformat(self.rules["accrual_cutoff"]), self.timezone)
            quantum = _f(self.rules["money_rounding"][row["currency"]]["quantum"])
            self.check("fixed expense amount type", isinstance(row["amount"], str), True)
            self.check("fixed expense schema", row["schema_version"], "fixed-expense/1")
            self.check("fixed expense category", row["category"], "fixed")
            self.check("fixed expense money grid", amount >= 0 and (amount / quantum).denominator == 1, True)
            self.check("fixed expense covered cutoff", start <= local_day <= end and at == cutoff and at >= _stamp(self.rules["start_at"]), True)
            self._expense_evidence(row["evidence"], self._knowledge(at))
            self.check("expense review after source receipt", _stamp(row["evidence"]["known_at"]) <= reviewed, True)
            self.expense_rows[expense_id] = row
            order.append((at, expense_id))
        self.check("fixed expense schedule order", order, sorted(order))

    def _expense_evidence(self, evidence, at):
        if set(evidence) != {"reference", "sha256", "known_at", "classification"}:
            raise ReplayContractError("expense evidence schema differs")
        checksum = evidence["sha256"]
        self.check("expense evidence reference", isinstance(evidence["reference"], str) and bool(evidence["reference"].strip()), True)
        self.check("expense evidence hash format", isinstance(checksum, str) and len(checksum) == 64 and all(c in "0123456789abcdef" for c in checksum), True)
        self.check("expense evidence classification", evidence["classification"], self.rules["classification"])
        self.check("expense evidence receipt clock", _stamp(evidence["known_at"]) <= at, True)

    def _owed(self, state, currency):
        return sum((book["remaining"] for expense_id, book in state.get("expense_books", {}).items()
                    if self.expense_rows[expense_id]["currency"] == currency), F(0))

    def _available(self, state, currency):
        return max(F(0), state["settled"][currency] + min(F(0), state["unpaid"][currency]) - self._owed(state, currency))

    def check(self, field, actual, expected, *, money=False, scale=F(1)):
        self.checks += 1
        if isinstance(expected, F):
            try:
                error = abs(_f(actual) - expected)
                matched = error <= (self.tolerance * scale if money else F(1, 10**20))
            except ReplayContractError:
                matched = False
        else:
            matched = type(actual) is type(expected) and actual == expected
        if not matched:
            self.errors.append({"field": field, "actual": str(actual), "expected": str(expected)})

    def _sequences(self, events, label):
        if not isinstance(events, list):
            raise ReplayContractError(label + " events required")
        sequences = [e["sequence"] for e in events]
        if sequences != list(range(1, len(events) + 1)) or any(type(x) is not int for x in sequences):
            raise ReplayContractError(label + " event sequences must be contiguous unique integers")

    def _enter(self, key):
        if key in self.visiting:
            raise ReplayContractError("cyclic stock/wallet source references")
        self.visiting.add(key)

    def _stock(self, index):
        if index in self.stock_cache:
            return copy.deepcopy(self.stock_cache[index])
        self._enter(("stock", index))
        if index == 0:
            entered = self._wallet(self.entry_index)["settled"]["USD"]
            self.check("initial USD cash from explicit FX", self.data["initial"]["settled_cash"], entered, money=True)
            state = {"cash": entered, "holdings": copy.deepcopy(self.data["initial"]["holdings"]),
                     "pending_cash": {}, "locked": {}, "receivables": {}, "day": None, "capacity": {}, "cash_available_at": None, "plan": None}
        else:
            state = self._stock(index - 1)
            event = self.events[index - 1]
            day = event["date"]
            _day(day)
            if state["day"] is not None and day < state["day"]:
                raise ReplayContractError("reverse stock dates")
            if day not in self.days:
                raise ReplayContractError("stock event outside declared path")
            if state["day"] != day:
                state["capacity"] = {}
                state["plan"] = None
            state["day"] = day
            self._stock_event(state, event)
        self.visiting.remove(("stock", index))
        self.stock_cache[index] = copy.deepcopy(state)
        return state

    def _rule(self, day, at):
        matches = [r for r in self.terms["rules"] if _day(r["from"]) <= _day(day) <= _day(r["through"])]
        if len(matches) != 1:
            raise ReplayContractError("trading date not covered exactly once")
        rule = matches[0]
        freeze = _stamp(self.terms["frozen_at"])
        if _stamp(rule["known_at"]) > freeze:
            raise ReplayContractError("trading rule after freeze")
        if rule["evidence"]["basis"] != "current_counterfactual" and _stamp(rule["known_at"]) > at:
            raise ReplayContractError("late trading rule")
        return rule

    def _order(self, day, symbol, side, qty):
        bar = self.quotes[day, symbol]
        rule = self._rule(day, _stamp(bar["open_at"]))
        raw, friction = _f(bar["open"]), _f(rule["execution"]["friction_rate"])
        friction = friction * 2 if self.scenario == "C1" else friction + F(1, 1000) if self.scenario in {"C2", "C3"} else friction
        price = _round(raw * (1 + friction if side == "BUY" else 1 - friction),
                       _f(rule["execution"]["price_tick"]), "ceil" if side == "BUY" else "floor")
        components = []
        for component in rule["fees"]:
            if side not in component["sides"]:
                continue
            basis = {"shares": F(qty), "order": F(1), "notional": price * qty}[component["basis"]]
            amount = max(basis * _f(component["rate"]), _f(component["minimum"]))
            if component["maximum"] is not None:
                amount = min(amount, _f(component["maximum"]))
            amount = _round(amount, _f(component["quantum"]), component["rounding"])
            if self.scenario == "C1" and component["category"] == "broker":
                amount *= 2
            components.append({"id": component["id"], "category": component["category"], "amount": amount})
        lag = rule["settlement"]["share_sessions" if side == "BUY" else "cash_sessions"]
        due = self.sessions[self.sessions.index(day) + lag]
        return raw, price, components, due

    def _stock_event(self, state, event):
        kind, day = event["type"], event["date"]
        label = "stock:" + str(event["sequence"]) + ":"
        if kind == "CASH_EXPENSE_DEBIT":
            if not self.expense_mode:
                raise ReplayContractError("expense debit requires model 2")
            sequence = event["source_wallet_sequence"]
            if type(sequence) is not int or sequence <= 0:
                raise ReplayContractError("integer expense payment source required")
            index = self.wallet_indices[sequence]
            source = self.wallet_events[index - 1]
            self.check(label + "expense payment type", source["type"], "FIXED_EXPENSE_PAYMENT")
            self.check(label + "expense payment currency", source["currency"], "USD")
            self._wallet(index)
            amount = self.expense_payment_values[sequence]
            self.check(label + "expense debit amount", event["amount"], amount)
            self.check(label + "expense debit identity", event["source_expense_id"], source["expense_id"])
            self.check(label + "expense debit time", event["debited_at"], source["at"])
            self.check(label + "unique expense debit", sequence not in self.seen_expense_payment_sources, True)
            self.check(label + "expense debit not before source day", _day(day) >= _stamp(source["at"]).astimezone(self.timezone).date(), True)
            self.seen_expense_payment_sources.add(sequence)
            state["cash"] -= amount
            self.check(label + "nonnegative native expense cash", state["cash"] >= 0, True)
            state["cash_available_at"] = _stamp(source["at"])
        elif kind == "CASH_INTEREST_CREDIT":
            sequence = event["source_wallet_sequence"]
            if type(sequence) is not int or sequence <= 0:
                raise ReplayContractError("integer wallet source cursor required")
            index = self.wallet_indices[sequence]
            source = self.wallet_events[index - 1]
            self.check(label + "source type", source["type"], "CASH_INTEREST")
            self.check(label + "source currency", source["currency"], "USD")
            self.check(label + "source day", event["source_wallet_day"], source["day"])
            self._wallet(index)
            amount = self.credit_values[sequence]
            self.check(label + "amount", event["amount"], amount)
            self.check(label + "unique credit", sequence not in self.seen_credit_sources, True)
            self.check(label + "not before source day", day >= source["day"], True)
            self.seen_credit_sources.add(sequence)
            state["cash"] += amount
            state["cash_available_at"] = _stamp(source["at"])
        elif kind == "CASH_SETTLEMENT":
            source = event["fill_sequence"]
            pending = state["pending_cash"].pop(source)
            self.check(label + "due day", pending[1] <= day, True)
            self.check(label + "amount", event["amount"], pending[0])
            state["cash"] += pending[0]
        elif kind == "SHARE_SETTLEMENT":
            pending = state["locked"].pop(event["fill_sequence"])
            self.check(label + "due day", pending[2] <= day, True)
            self.check(label + "quantity", event["qty"], pending[1])
        elif kind in {"SPLIT", "DIV_EX"}:
            action = next(a for a in self.data["actions"] if a["id"] == event["action_id"])
            self.check(label + "effective day", day, action["effective_date"])
            self.check(label + "action type", action["type"], "split" if kind == "SPLIT" else "dividend")
            self.check(label + "known before open", _stamp(action["known_at"]) <= _stamp(self.quotes[day, action["symbol"]]["open_at"]), True)
            self.check(label + "unique action", action["id"] not in self.action_positions, True)
            self.action_positions[action["id"]] = event["sequence"]
            symbol = action["symbol"]
            if kind == "SPLIT":
                ratio = F(action["numerator"], action["denominator"])
                new_qty = state["holdings"][symbol] * ratio
                self.check(label + "integral split", new_qty.denominator == 1, True)
                state["holdings"][symbol] = int(new_qty)
                self.check(label + "ratio numerator", event["numerator"], action["numerator"])
                self.check(label + "ratio denominator", event["denominator"], action["denominator"])
                for key, lock in state["locked"].items():
                    if lock[0] == symbol:
                        new_lock = lock[1] * ratio
                        self.check(label + "integral locked split", new_lock.denominator == 1, True)
                        state["locked"][key] = (symbol, int(new_lock), lock[2])
            else:
                expected_splits = {a["id"] for a in self.data["actions"] if a["effective_date"] == day and a["type"] == "split"}
                self.check(label + "splits before entitlement", expected_splits <= set(self.action_positions), True)
                qty = state["holdings"][symbol]
                net = qty * _f(action["gross_per_share"]) * (1 - _f(action["withholding_rate"]))
                self.check(label + "entitled quantity", event["entitled_qty"], qty)
                self.check(label + "net dividend", event["net_amount"], net)
                state["receivables"][action["id"]] = (net, action["pay_date"])
        elif kind == "DIV_PAY":
            amount, due = state["receivables"].pop(event["action_id"])
            action = next(a for a in self.data["actions"] if a["id"] == event["action_id"])
            if "payment_at" in action:
                self.check(label + "effective pay date", event["effective_pay_date"], due)
                self.check(label + "payment time", event["payment_at"], action["payment_at"])
                self.check(label + "posting basis", event["posting_basis"], "modelled_at_declared_cutoff")
                self.check(label + "not before effective payment", day >= due, True)
                state["cash_available_at"] = _stamp(action["payment_at"])
            else:
                self.check(label + "pay date", day, due)
            self.check(label + "net amount", event["amount"], amount)
            state["cash"] += amount
        elif kind == "TARGET_PLAN":
            self.check(label + "allocation basis", event["allocation_basis"], "FULL_CNY_ACCOUNT_CONVERTED_AT_NONFUTURE_FX")
            opening_at = min(_stamp(self.quotes[day, symbol]["open_at"]) for symbol in self.data["symbols"])
            prefix = max((i + 1 for i, e in enumerate(self.wallet_events) if _stamp(e["at"]) <= opening_at
                          and e.get("source_stock_event_sequence", 0) < event["sequence"]), default=0)
            wallet = self._wallet(prefix)
            quotes = [self.model["performance_boundary"]["fx_mark"]]
            quotes += [q for d, q in self.fx["close_marks"].items() if d < day]
            quote = max((q for q in quotes if _stamp(q["quoted_at"]) <= opening_at), key=lambda q: _stamp(q["quoted_at"]))
            self.check(label + "allocation FX source", event["allocation_fx_quote"], quote)
            mid = self._fx_quote(quote, opening_at)
            previous_day = max((d for d in self.days if d < day), default=None)
            marks = self._marks(previous_day) if previous_day else {s: _f(v) for s, v in self.data["initial"]["marks"].items()}
            value = state["cash"] + sum((p[0] for p in state["pending_cash"].values()), F(0))
            value += sum((p[0] for p in state["receivables"].values()), F(0)) + wallet["unpaid"]["USD"] - self._owed(wallet, "USD")
            for action in self.data["actions"]:
                if action["effective_date"] == day and action["type"] == "split":
                    marks[action["symbol"]] /= F(action["numerator"], action["denominator"])
            planning_prices = {}
            for symbol in self.data["symbols"]:
                bar = self.quotes[day, symbol]
                known = bar.get("open_quote_known_at", bar["open_at"])
                timely = known is not None and _stamp(known) <= opening_at
                price = _f(bar["open"]) if bar["open"] is not None and timely else marks[symbol]
                planning_prices[symbol] = price
                value += state["holdings"][symbol] * price
            cny = sum((wallet[k]["CNY"] for k in ("settled", "receivables", "unpaid")), F(0)) - self._owed(wallet, "CNY")
            nav = _round(value + cny / mid, F(1, 10**8), "HALF_EVEN")
            self.check(label + "full CNY allocation equity", event["opening_equity"], nav, money=True)
            intents = [i for i in self.data["control_intents"] if i["execution_date"] == day]
            if len(intents) != 1:
                raise ReplayContractError("target plan must bind exactly one execution-date intent")
            intent = intents[0]
            self.check(label + "bound decision date", event["decision_date"], intent["decision_date"])
            self.check(label + "intent available at open", _stamp(intent["available_at"]) <= opening_at, True)
            weights = intent["weights"]
            self.check(label + "weights cover instruments", sorted(weights), sorted(self.data["symbols"]))
            self.check(label + "long-only weights", all(_f(w) >= 0 for w in weights.values()) and sum((_f(w) for w in weights.values()), F(0)) <= 1, True)
            if nav <= 0:
                self.check(label + "nonpositive NAV trade halt", event["allocation_policy"], "TRADE_HALTED_NONPOSITIVE_NAV")
                deltas = {symbol: 0 for symbol in self.data["symbols"]}
            else:
                if self.expense_mode or "allocation_policy" in event:
                    self.check(label + "positive NAV allocation policy", event["allocation_policy"], "SPY_EFA_IEF_GLD_SEQUENTIAL_FEE_AWARE")
                deltas = {symbol: int(nav * _f(weights[symbol]) // planning_prices[symbol]) - state["holdings"][symbol] for symbol in self.data["symbols"]}
            self.check(label + "integer target deltas", all(type(v) is int for v in event["deltas"].values()), True)
            self.check(label + "causal target deltas", event["deltas"], deltas)
            state["plan"] = {"decision_date": intent["decision_date"], "deltas": deltas, "filled": {s: 0 for s in deltas}}
            for symbol, delta in deltas.items():
                if delta:
                    self.expected_attempts.append((day, symbol, "BUY" if delta > 0 else "SELL", abs(delta)))
        elif kind == "ORDER_ATTEMPT":
            if state["plan"] is None:
                raise ReplayContractError("order attempt requires independently bound target plan")
            plan = state["plan"]
            symbol = event["symbol"]
            delta = plan["deltas"][symbol]
            side = "BUY" if delta > 0 else "SELL"
            self.check(label + "nonzero target", delta != 0, True)
            self.check(label + "target side", event["side"], side)
            self.check(label + "target requested quantity", event["requested_qty"], abs(delta))
            self.check(label + "bound decision date", event["decision_date"], plan["decision_date"])
            self.check(label + "observed at open", event["observed_at"], self.quotes[day, symbol]["open_at"])
            self.observed_attempts.append((day, symbol, event["side"], event["requested_qty"]))
        elif kind == "FILL":
            symbol, side, qty = event["symbol"], event["side"], event["qty"]
            if side not in {"BUY", "SELL"} or type(qty) is not int or qty <= 0:
                raise ReplayContractError("positive integer fill and side required")
            if state["plan"] is not None:
                plan = state["plan"]
                delta = plan["deltas"][symbol]
                self.check(label + "fill matches target side", side, "BUY" if delta > 0 else "SELL")
                plan["filled"][symbol] += qty
                self.check(label + "fill within requested target", plan["filled"][symbol] <= abs(delta), True)
            actions = {a["id"] for a in self.data["actions"] if a["effective_date"] == day}
            self.check(label + "actions precede fill", actions <= set(self.action_positions), True)
            self.check(label + "due proceeds settled before fill", not any(p[1] <= day for p in state["pending_cash"].values()), True)
            self.check(label + "due shares settled before fill", not any(p[2] <= day for p in state["locked"].values()), True)
            raw, price, components, due = self._order(day, symbol, side, qty)
            bar = self.quotes[day, symbol]
            opening_at = _stamp(bar["open_at"])
            self.check(label + "cash credit available before fill", state["cash_available_at"] is None or state["cash_available_at"] <= opening_at, True)
            self.check(label + "opening quote received", _stamp(bar.get("open_quote_known_at", bar["open_at"])) <= _stamp(bar["open_at"]), True)
            self.check(label + "tradable quote", bar.get("open_status", "tradable"), "tradable")
            self.check(label + "whole lot", qty % self.data["instruments"][symbol]["lot_size"], 0)
            self.check(label + "raw open", event["raw_open"], raw)
            self.check(label + "price", event["price"], price)
            self.check(label + "settlement date", event["settlement_date"], due)
            self.check(label + "fees count", len(event["fees"]), len(components))
            for actual, expected in zip(event["fees"], components):
                for key in ("id", "category", "amount"):
                    self.check(label + "fee component " + key, actual[key], expected[key])
            broker = sum((c["amount"] for c in components if c["category"] == "broker"), F(0))
            tax = sum((c["amount"] for c in components if c["category"] == "tax"), F(0))
            total = broker + tax
            self.check(label + "commission", event["commission"], broker)
            self.check(label + "platform", event["platform"], F(0))
            self.check(label + "tax", event["tax"], tax)
            self.check(label + "fee total", event["fee"], total)
            used = state["capacity"].get((day, symbol), 0) + qty
            state["capacity"][day, symbol] = used
            self.check(label + "quote capacity", used <= self.quotes[day, symbol]["open_capacity_shares"], True)
            if side == "BUY":
                wallet_prefix = max((i + 1 for i, e in enumerate(self.wallet_events)
                    if _stamp(e["at"]) <= opening_at and e.get("source_stock_event_sequence", 0) < event["sequence"]), default=0)
                unpaid = self._wallet(wallet_prefix)["unpaid"]["USD"]
                owed = self._owed(self._wallet(wallet_prefix), "USD")
                self.check(label + "reserved negative unpaid interest and expenses", qty * price + total <= state["cash"] + min(F(0), unpaid) - owed, True)
                state["cash"] -= qty * price + total
                state["holdings"][symbol] += qty
                if due > day:
                    state["locked"][event["sequence"]] = (symbol, qty, due)
            else:
                available = state["holdings"][symbol] - sum(p[1] for p in state["locked"].values() if p[0] == symbol)
                self.check(label + "sellable", qty <= available, True)
                state["holdings"][symbol] -= qty
                proceeds = qty * price - total
                if due > day:
                    state["pending_cash"][event["sequence"]] = (proceeds, due)
                else:
                    state["cash"] += proceeds
            self.check(label + "nonnegative settled cash", state["cash"] >= 0, True)
        elif kind not in {"TARGET_PLAN", "ORDER_ATTEMPT", "CAPACITY_BUDGET", "ORDER_CANCEL", "ORDER_REMAINDER_CANCEL", "POST_CLOSE_FILL_DIAGNOSTIC", "SIGNAL", "SIGNAL_SKIPPED", "INTENT"}:
            raise ReplayContractError("unknown stock event semantics: " + kind)

    def _wallet(self, index):
        if index in self.wallet_cache:
            return copy.deepcopy(self.wallet_cache[index])
        self._enter(("wallet", index))
        if index == 0:
            state = {"settled": {"CNY": F(0), "USD": F(0)}, "receivables": {"CNY": F(0), "USD": F(0)},
                     "unpaid": {"CNY": F(0), "USD": F(0)}, "last_day": {"CNY": None, "USD": None}, "at": None}
            if self.expense_mode:
                state["expense_books"] = {}
        else:
            state = self._wallet(index - 1)
            event = self.wallet_events[index - 1]
            at = _stamp(event["at"])
            if state["at"] is not None and at < state["at"]:
                raise ReplayContractError("reverse wallet timestamp")
            state["at"] = at
            self._wallet_event(state, event)
        self.visiting.remove(("wallet", index))
        self.wallet_cache[index] = copy.deepcopy(state)
        return state

    def _knowledge(self, at):
        frozen = _stamp(self.clock["freeze_at"])
        return min(at, frozen) if self.clock["basis"] == "historical_point_in_time" else frozen

    def _fx_quote(self, quote, at, extra=F(0)):
        quoted, known = _stamp(quote["quoted_at"]), _stamp(quote["known_at"])
        self.check("FX price exists at use", quoted <= at, True)
        self.check("FX receipt clock", quoted <= known <= self._knowledge(at), True)
        self.check("FX evidence clock", _stamp(quote["evidence"]["known_at"]) <= self._knowledge(at), True)
        mid = _f(quote["cny_per_usd"])
        self.check("positive FX", mid > 0, True)
        if quote["schema_version"] == "fx-mark.v1":
            return mid
        if quote["schema_version"] != "fx-execution.v1":
            raise ReplayContractError("unknown FX schema")
        ask = mid * (1 + _f(quote["buy_usd_spread_bps"]) / 10000) * (1 + extra)
        bid = mid * (1 - _f(quote["sell_usd_spread_bps"]) / 10000) * (1 - extra)
        return ask, bid, _f(quote["buy_fixed_fee_cny"]), _f(quote["sell_fixed_fee_usd"])

    def _wallet_event(self, state, event):
        kind, at = event["type"], _stamp(event["at"])
        label = "wallet:" + str(event["sequence"]) + ":"
        if kind == "FUNDING":
            self.check(label + "first", event["sequence"], 1)
            amount = _f(self.rules["initial_cny"])
            self.check(label + "initial CNY", event["initial_cny"], amount)
            self.check(label + "funding time", at, _stamp(self.rules["start_at"]))
            self.check(label + "funding evidence", event["funding_evidence"], self.rules["funding_evidence"])
            state["settled"]["CNY"] = amount
        elif kind == "FX_EXECUTION":
            entry = self.fx["entry"]
            self.check(label + "entry time", at, _stamp(entry["at"]))
            self.check(label + "direction", event["direction"], "CNY_TO_USD")
            quote = entry["execution_quote"]
            self.check(label + "frozen entry quote", event["quote"], quote)
            extra = F(25, 10000) if self.scenario in {"C2", "C3"} else F(0)
            ask, _, fee, _ = self._fx_quote(quote, at, extra)
            principal = _f(entry["principal_cny"])
            rounding = self.rules["money_rounding"]["USD"]
            credit = _round(principal / ask, _f(rounding["quantum"]), rounding["mode"])
            debit = principal + fee
            self.check(label + "principal", event["principal"], principal)
            self.check(label + "source fee", event["source_fixed_fee"], fee)
            self.check(label + "debit", event["debit"], debit)
            self.check(label + "credit", event["credit"], credit)
            self.check(label + "execution FX", event["execution_cny_per_usd"], ask)
            self.check(label + "extra FX rate", event["extra_spread_rate"], extra)
            available = self._available(state, "CNY")
            self.check(label + "settled affordability", debit <= available, True)
            state["settled"]["CNY"] -= debit
            state["settled"]["USD"] += credit
        elif kind == "EXTERNAL_USD_SYNC":
            source = event["source_stock_event_sequence"]
            if type(source) is not int or source < 0:
                raise ReplayContractError("nonnegative integer stock source cursor required")
            index = self.stock_indices[source] if source else 0
            stock = self._stock(index)
            # Engine book dates can be next-session dates for weekend payments
            # and credits. Validate the actual economic time, not that label.
            previous_source = getattr(self, "last_sync_source", 0)
            self.check(label + "monotone source cursor", source >= previous_source, True)
            for source_event in self.events[previous_source:index]:
                if source_event["type"] == "DIV_PAY" and "payment_at" in source_event:
                    economic_at = _stamp(source_event["payment_at"])
                elif source_event["type"] in {"CASH_INTEREST_CREDIT", "CASH_EXPENSE_DEBIT"}:
                    origin = self.wallet_events[self.wallet_indices[source_event["source_wallet_sequence"]] - 1]
                    economic_at = _stamp(origin["at"])
                    if "credited_at" in source_event:
                        self.check(label + "credit economic time", source_event["credited_at"], origin["at"])
                    if "debited_at" in source_event:
                        self.check(label + "expense economic time", source_event["debited_at"], origin["at"])
                else:
                    bars = [b for (d, _), b in self.quotes.items() if d == source_event["date"]]
                    economic_at = min(_stamp(b["open_at"]) for b in bars)
                self.check(label + "source economic time not future", economic_at <= at, True)
            self.last_sync_source = source
            if source:
                self.check(label + "source cursor not future", self.events[index - 1]["date"] <= event["stock_snapshot_date"], True)
            self.check(label + "source snapshot belongs to path", event["stock_snapshot_date"] in self.days, True)
            cash = stock["cash"]
            receivables = sum((p[0] for p in stock["pending_cash"].values()), F(0)) + sum((p[0] for p in stock["receivables"].values()), F(0))
            self.check(label + "previous wallet settled", event["previous_settled"], state["settled"]["USD"], money=True)
            self.check(label + "derived USD cash", event["settled"], cash, money=True)
            self.check(label + "derived USD receivables", event["receivables"], receivables, money=True)
            state["settled"]["USD"], state["receivables"]["USD"] = cash, receivables
        elif kind in {"FIXED_EXPENSE_BOOKED", "FIXED_EXPENSE_PAYMENT"}:
            self._expense_event(state, event)
        elif kind == "CASH_INTEREST":
            self._interest(state, event)
        else:
            raise ReplayContractError("unknown wallet event: " + kind)
        self.check(label + "nonnegative CNY wallet", state["settled"]["CNY"] >= 0, True)
        self.check(label + "nonnegative USD wallet", state["settled"]["USD"] >= 0, True)

    def _expense_event(self, state, event):
        if not self.expense_mode:
            raise ReplayContractError("fixed expense journal requires model 2")
        expense_id, at = event["expense_id"], _stamp(event["at"])
        row = self.expense_rows[expense_id]
        label = "expense:" + str(event["sequence"]) + ":"
        currency = row["currency"]
        local_day = at.astimezone(self.timezone).date()
        cutoff = datetime.combine(local_day, time.fromisoformat(self.rules["accrual_cutoff"]), self.timezone)
        self.check(label + "cutoff", at, cutoff)
        self.check(label + "review coverage", _day(self.model["expense_review"]["coverage_start_date"]) <= local_day <= _day(self.model["expense_review"]["coverage_end_date"]), True)
        self.check(label + "currency", event["currency"], currency)
        books = state.setdefault("expense_books", {})
        if event["type"] == "FIXED_EXPENSE_BOOKED":
            if expense_id in books:
                raise ReplayContractError("duplicate fixed expense booking")
            self.check(label + "booking time", at, _stamp(row["book_at"]))
            self.check(label + "declared book time", event["book_at"], row["book_at"])
            self.check(label + "book amount", event["amount"], _f(row["amount"]))
            self.check(label + "category", event["category"], "fixed")
            self.check(label + "source evidence", event["evidence"], row["evidence"])
            self.check(label + "schedule hash", event["schedule_sha256"], self.model["expense_review"]["schedule_sha256"])
            books[expense_id] = {"remaining": _f(row["amount"]), "sequence": event["sequence"]}
        else:
            book = books[expense_id]
            self.check(label + "source booking", event["book_sequence"], book["sequence"])
            self.check(label + "not before booking", at >= _stamp(row["book_at"]), True)
            earlier = [r for r in self.model["fixed_expenses"] if r["id"] in books and r["currency"] == currency and books[r["id"]]["remaining"] > 0]
            self.check(label + "FIFO payable expense", expense_id, earlier[0]["id"] if earlier else None)
            gross = max(F(0), state["settled"][currency] + min(F(0), state["unpaid"][currency]))
            quantum = _f(self.rules["money_rounding"][currency]["quantum"])
            expected = _round(min(gross, book["remaining"]), quantum, "floor")
            self.check(label + "maximal affordable partial payment", event["amount"], expected)
            self.check(label + "positive payment", expected > 0, True)
            amount = _f(event["amount"])
            self.check(label + "nonnegative remaining liability", 0 < amount <= book["remaining"], True)
            book["remaining"] -= amount
            self.check(label + "remaining liability", event["remaining_owed"], book["remaining"])
            state["settled"][currency] -= amount
            self.expense_payment_values[event["sequence"]] = amount

    def _interest(self, state, event):
        currency, day = event["currency"], _day(event["day"])
        label = "interest:" + str(event["sequence"]) + ":"
        start = _stamp(self.rules["start_at"]).astimezone(self.timezone).date()
        previous = state["last_day"][currency] or start - timedelta(days=1)
        self.check(label + "consecutive calendar day", day, previous + timedelta(days=1))
        cutoff = datetime.combine(day, time.fromisoformat(self.rules["accrual_cutoff"]), self.timezone)
        self.check(label + "cutoff", _stamp(event["at"]), cutoff)
        if self.expense_mode and "expense_books" in state:
            expected = {r["id"] for r in self.model["fixed_expenses"] if _stamp(r["book_at"]) <= cutoff}
            self.check(label + "all due expense bookings before interest", set(state["expense_books"]), expected)
            # Both currencies are checked before their own accrual. A credit
            # emitted for the first currency is payable in the bridge retry
            # after the complete two-currency accrual, not before the second.
            for c in (currency,):
                gross = max(F(0), state["settled"][c] + min(F(0), state["unpaid"][c]))
                q = _f(self.rules["money_rounding"][c]["quantum"])
                self.check(label + "due expenses paid before interest:" + c,
                           _round(min(gross, self._owed(state, c)), q, "floor"), F(0))
        candidates = [r for r in self.rules["interest_schedules"] if r["currency"] == currency and _day(r["effective_date"]) <= day]
        rule = max(candidates, key=lambda r: _day(r["effective_date"]))
        beginning = datetime.combine(day, time(0), self.timezone)
        known_clock = self._knowledge(beginning)
        self.check(label + "known rate", _stamp(rule["known_at"]) <= known_clock, True)
        self.check(label + "source effective clock", _stamp(rule["source_effective_at"]) <= known_clock, True)
        balance, threshold = state["settled"][currency], _f(rule["eligibility_threshold"])
        eligible = F(0) if balance < threshold else balance if rule["eligibility_mode"] == "gate_all" else max(F(0), balance - threshold)
        annual = F(0)
        for tier in rule["tiers"]:
            self.check(label + "known tier", _stamp(tier["known_at"]) <= known_clock, True)
            lower = _f(tier["lower"])
            upper = _f(tier["upper"]) if tier["upper"] is not None else None
            if rule["tier_mode"] == "marginal":
                base = max(F(0), (min(eligible, upper) if upper is not None else eligible) - lower)
                annual += base * _f(tier["net_annual_rate"])
            elif eligible >= lower and (upper is None or eligible < upper):
                annual = eligible * _f(tier["net_annual_rate"])
                break
        if rule["day_count"] not in {"ACT365", "ACT360"} or rule["crediting"] not in {"daily", "calendar_month_end"}:
            raise ReplayContractError("unknown interest day basis or credit timing")
        if rule["eligibility_mode"] not in {"gate_all", "above_threshold"} or rule["tier_mode"] not in {"marginal", "whole_balance"}:
            raise ReplayContractError("unknown interest eligibility/tier semantics")
        if rule["rounding_timing"] not in {"daily_accrual", "on_credit"}:
            raise ReplayContractError("unknown interest rounding timing")
        raw = annual / (365 if rule["day_count"] == "ACT365" else 360)
        quantum = _f(rule["rounding_quantum"])
        accrued = _round(raw, quantum, rule["rounding_mode"]) if rule["rounding_timing"] == "daily_accrual" else raw
        unpaid = state["unpaid"][currency] + accrued
        due = rule["crediting"] == "daily" or day.day == calendar.monthrange(day.year, day.month)[1]
        credit = _round(unpaid, quantum, rule["rounding_mode"]) if due else F(0)
        remaining = F(0) if due else unpaid
        for key, value in [("eligible_balance", eligible), ("raw_accrual", raw), ("accrued", accrued), ("credited", credit), ("unpaid_interest", remaining)]:
            self.check(label + key, event[key], value)
        self.check(label + "effective date", event["effective_date"], rule["effective_date"])
        self.check(label + "basis", event["day_count"], rule["day_count"])
        self.check(label + "posting", event["crediting"], rule["crediting"])
        self.check(label + "rounding delta", event["credit_rounding_delta"], unpaid - credit if due else F(0))
        state["settled"][currency] += credit
        state["unpaid"][currency] = remaining
        state["last_day"][currency] = day
        self.credit_values[event["sequence"]] = credit

    def _marks(self, day):
        if not self.marks_cache:
            marks = {s: _f(v) for s, v in self.data["initial"]["marks"].items()}
            for session in self.days:
                for action in self.data["actions"]:
                    if action["effective_date"] == session and action["type"] == "split":
                        marks[action["symbol"]] /= F(action["numerator"], action["denominator"])
                cutoff = datetime.combine(_day(session), time.fromisoformat(self.rules["accrual_cutoff"]), self.timezone)
                for symbol in self.data["symbols"]:
                    bar = self.quotes[session, symbol]
                    if bar["close"] is not None and ("available_at" not in bar or _stamp(bar["available_at"]) <= cutoff):
                        marks[symbol] = _f(bar["close"])
                self.marks_cache[session] = dict(marks)
        return self.marks_cache[day]

    def _wallet_snapshot(self, snap, state, assets, mark, *, opening=False):
        day = snap.get("date", "opening")
        label = str(day) + ":CNY:"
        at = _stamp(snap["as_of"])
        mid = self._fx_quote(mark, at)
        usd = state["settled"]["USD"] + state["receivables"]["USD"] + state["unpaid"]["USD"] + assets - self._owed(state, "USD")
        cny = state["settled"]["CNY"] + state["receivables"]["CNY"] + state["unpaid"]["CNY"] - self._owed(state, "CNY")
        for currency in ("CNY", "USD"):
            wallet = snap["wallets"][currency]
            for key, value in [("settled", state["settled"][currency]), ("receivables", state["receivables"][currency]), ("unpaid_interest", state["unpaid"][currency]),
                ("available", self._available(state, currency))]:
                self.check(label + currency + key, wallet[key], value, money=True)
            if self.expense_mode:
                self.check(label + currency + "fixed expense owed", wallet["fixed_expense_owed"], self._owed(state, currency), money=True)
        if self.expense_mode:
            self.check(label + "expense schedule", snap["expense_schedule_sha256"], self.model["expense_review"]["schedule_sha256"])
            for currency in ("CNY", "USD"):
                self.check(label + currency + "liability", snap["fixed_expense_liabilities"][currency], self._owed(state, currency), money=True)
        self.check(label + "external assets", snap["usd_external_assets"], assets, money=True)
        self.check(label + "USD equity", snap["usd_total_equity"], usd, money=True)
        self.check(label + "FX mark", snap["fx_mark_cny_per_usd"], mid)
        self.check(label + "CNY equity", snap["marked_equity_cny"], cny + usd * mid, money=True, scale=max(F(1), mid))
        self.check(label + "knowledge clock", snap["knowledge_clock"], self.clock)
        if not opening:
            expected_exit = self.fx.get("exit_quotes", {}).get(day)
            self.check(label + "exit cost input", snap.get("exit_quote"), expected_exit)
            cash_exit = None
            if expected_exit is not None:
                extra = F(25, 10000) if self.scenario in {"C2", "C3"} else F(0)
                _, bid, _, fee = self._fx_quote(expected_exit, at, extra)
                available_usd = self._available(state, "USD")
                available_cny = self._available(state, "CNY")
                money = self.rules["money_rounding"]["CNY"]
                if available_usd == 0:
                    cash_exit = available_cny
                elif available_usd >= fee:
                    cash_exit = available_cny + _round((available_usd - fee) * bid, _f(money["quantum"]), money["mode"])
            if cash_exit is None:
                self.check(label + "cash exit", snap.get("settled_cash_exit_cny"), None)
            else:
                self.check(label + "cash exit", snap["settled_cash_exit_cny"], cash_exit, money=True)
            fully_settled = assets == 0 and all(state[k][c] == 0 for k in ("receivables", "unpaid") for c in ("CNY", "USD")) and all(self._owed(state, c) == 0 for c in ("CNY", "USD"))
            if cash_exit is not None and fully_settled:
                self.check(label + "whole exit", snap["exit_equity_cny"], cash_exit, money=True)
            else:
                self.check(label + "no implicit liquidation", snap.get("exit_equity_cny"), None)
        return cny + usd * mid

    def run(self):
        self.check("stock snapshot dates", [s["date"] for s in self.result["snapshots"]], self.days)
        self.check("CNY snapshot dates", [s["date"] for s in self.result["cny_snapshots"]], self.days)
        fingerprints = self.result.get("native_engine_source_sha256")
        if fingerprints is not None:
            for engine in ("GlobalEquityEngine", "BaseEngine"):
                digest = fingerprints.get(engine)
                if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise ReplayContractError("native engine file SHA256 required")
        elif self.data.get("data_kind") != "constructed_market_fixture" or not self.result.get("engine_source"):
            raise ReplayContractError("native engine source fingerprints required")
        # Evaluate the acyclic source graph in journal order, one cached prefix
        # at a time. A long historical tape does not consume Python call depth.
        for index in range(self.entry_index + 1):
            self._wallet(index)
        self._stock(0)
        stock_prefix = 0
        for index in range(self.entry_index + 1, len(self.wallet_events) + 1):
            event = self.wallet_events[index - 1]
            if event["type"] == "EXTERNAL_USD_SYNC":
                source = event["source_stock_event_sequence"]
                target = self.stock_indices[source] if source else 0
                for next_prefix in range(stock_prefix + 1, target + 1):
                    self._stock(next_prefix)
                stock_prefix = max(stock_prefix, target)
            self._wallet(index)
        for next_prefix in range(stock_prefix + 1, len(self.events) + 1):
            self._stock(next_prefix)
        if "control_intents" in self.data:
            expected_plans = sorted((i["execution_date"], i["decision_date"]) for i in self.data["control_intents"] if i["execution_date"] in self.days)
            actual_plans = sorted((e["date"], e["decision_date"]) for e in self.events if e["type"] == "TARGET_PLAN")
            self.check("bound intent plans exactly once", actual_plans, expected_plans)
        self.check("planned attempts exactly once", sorted(self.observed_attempts), sorted(self.expected_attempts))
        # Validate that every credited USD wallet day has exactly one engine posting.
        credits = [e for e in self.wallet_events if e["type"] == "CASH_INTEREST" and e["currency"] == "USD" and self.credit_values[e["sequence"]] != 0]
        engine_credits = [e for e in self.events if e["type"] == "CASH_INTEREST_CREDIT"]
        self.check("credited wallet sources exactly once", sorted(e["source_wallet_sequence"] for e in engine_credits), sorted(e["sequence"] for e in credits))
        if self.expense_mode:
            final_wallet = self._wallet(len(self.wallet_events))
            final_at = _stamp(self.result["cny_snapshots"][-1]["as_of"])
            expected = {r["id"] for r in self.model["fixed_expenses"] if _stamp(r["book_at"]) <= final_at}
            self.check("fixed expense bookings complete exactly once", set(final_wallet.get("expense_books", {})), expected)
            payments = [e for e in self.wallet_events if e["type"] == "FIXED_EXPENSE_PAYMENT" and e["currency"] == "USD"]
            debits = [e for e in self.events if e["type"] == "CASH_EXPENSE_DEBIT"]
            self.check("USD expense payment sources exactly once", sorted(e["source_wallet_sequence"] for e in debits), sorted(e["sequence"] for e in payments))
        for snap, cny_snap in zip(self.result["snapshots"], self.result["cny_snapshots"]):
            day = snap["date"]
            prefix = max((i + 1 for i, e in enumerate(self.events) if e["date"] <= day), default=0)
            state = self._stock(prefix)
            marks = self._marks(day)
            pending = sum((p[0] for p in state["pending_cash"].values()), F(0))
            receivable = sum((p[0] for p in state["receivables"].values()), F(0))
            assets = sum((state["holdings"][s] * marks[s] for s in state["holdings"]), F(0))
            at = _stamp(cny_snap["as_of"])
            wallet_prefix = max((i + 1 for i, e in enumerate(self.wallet_events) if _stamp(e["at"]) <= at), default=0)
            wallet = self._wallet(wallet_prefix)
            cutoff = datetime.combine(_day(day), time.fromisoformat(self.rules["accrual_cutoff"]), self.timezone)
            self.check(day + ":snapshot cutoff", at, cutoff)
            self.check(day + ":CNY source stock cursor", cny_snap.get("source_stock_event_sequence", prefix), prefix)
            self.check(day + ":CNY source wallet cursor", cny_snap.get("source_wallet_sequence", wallet_prefix), wallet_prefix)
            for currency in ("CNY", "USD"):
                self.check(day + ":calendar accrual complete:" + currency, wallet["last_day"][currency], _day(day))
            self.check(day + ":coupled settled USD", wallet["settled"]["USD"], state["cash"], money=True)
            self.check(day + ":coupled receivables USD", wallet["receivables"]["USD"], pending + receivable, money=True)
            self.check(day + ":USD unpaid interest", snap.get("unpaid_usd_interest", "0"), wallet["unpaid"]["USD"], money=True)
            if self.expense_mode:
                self.check(day + ":USD fixed expense owed", snap["fixed_expense_owed_usd"], self._owed(wallet, "USD"), money=True)
            equity = state["cash"] + pending + receivable + assets + wallet["unpaid"]["USD"] - self._owed(wallet, "USD")
            for key, value in [("settled_cash", state["cash"]), ("unsettled_cash", pending), ("dividend_receivable", receivable), ("equity", equity)]:
                self.check(day + ":USD:" + key, snap[key], value, money=True)
            self.check(day + ":holdings", snap["holdings"], state["holdings"])
            sellable = {s: state["holdings"][s] - sum(p[1] for p in state["locked"].values() if p[0] == s) for s in state["holdings"]}
            self.check(day + ":sellable", snap["sellable"], sellable)
            self.check(day + ":all due cash settled", not any(p[1] <= day for p in state["pending_cash"].values()), True)
            self.check(day + ":all due shares released", not any(p[2] <= day for p in state["locked"].values()), True)
            self.check(day + ":all due dividend paid", not any(p[1] <= day for p in state["receivables"].values()), True)
            day_actions = {a["id"] for a in self.data["actions"] if a["effective_date"] <= day}
            self.check(day + ":all actions applied", day_actions <= set(self.action_positions), True)
            at = _stamp(cny_snap["as_of"])
            wallet_prefix = max((i + 1 for i, e in enumerate(self.wallet_events) if _stamp(e["at"]) <= at), default=0)
            wallet = self._wallet(wallet_prefix)
            nav = self._wallet_snapshot(cny_snap, wallet, assets, self.fx["close_marks"][day])
            self.replayed.append({"date": day, "cash_exact_fraction": str(state["cash"]), "equity_exact_fraction": str(equity), "cny_equity_exact_fraction": str(nav)})
        opening = self.result["opening_cny_snapshot"]
        opening_at = _stamp(opening["as_of"])
        prefix = max((i + 1 for i, e in enumerate(self.wallet_events[:self.entry_index - 1]) if _stamp(e["at"]) <= opening_at), default=0)
        opening_wallet = self._wallet(prefix)
        # Opening FX is irrelevant with zero USD; an explicit model quote still
        # checks the valuation's timestamp/provenance and never creates income.
        mark = self.model.get("performance_boundary", {}).get("fx_mark") or opening.get("fx_mark") or self.fx.get("opening_mark")
        if mark is None:
            # Entry mark can only be used if it already existed at the boundary.
            entry_quote = self.fx["entry"]["execution_quote"]
            mark = {k: entry_quote[k] for k in ("quoted_at", "known_at", "cny_per_usd", "evidence")}
            mark["schema_version"] = "fx-mark.v1"
        self._wallet_snapshot(opening, opening_wallet, F(0), mark, opening=True)
        return {"method": "independent Fraction stock/wallet source-cursor dependency replay; no production money helpers",
                "passed": not self.errors, "monetary_tolerance": str(self.tolerance),
                "cny_snapshot_tolerance": "USD tolerance multiplied by max(1, source FX mark)",
                "errors": self.errors, "checks": self.checks, "replayed": self.replayed}
