"""S02-E01 accounting kernel 0.2.1. Standard library, offline, no broker API.

Synthetic controls and future research share Engine.run / execute_targets.
Formal result generation is deliberately gated, including unimplemented protocol
dimensions. The frozen strategy protocol v0.1 is not altered by code versioning.
"""
import argparse
import copy
import json
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

VERSION = "0.2.1"
UNIVERSE = ["SPY", "EFA", "IEF", "GLD"]


class ContractError(ValueError):
    pass


class FormalGate(ContractError):
    pass


def number(x, name, minimum=None):
    if x is None or isinstance(x, bool):
        raise ContractError(f"{name}: explicit finite numeric value required")
    try:
        out = Decimal(str(x))
    except Exception as exc:
        raise ContractError(f"{name}: invalid number") from exc
    if not out.is_finite() or (minimum is not None and out < minimum):
        raise ContractError(f"{name}: invalid range or nonfinite")
    return out


def required(obj, name):
    if name not in obj or obj[name] is None:
        raise ContractError(f"{name}: explicit value required")
    return obj[name]


def integer(x, name, minimum=0):
    v = number(x, name, minimum)
    if v != v.to_integral_value():
        raise ContractError(f"{name}: integer required")
    return int(v)


def day(x):
    try:
        return date.fromisoformat(x)
    except (ValueError, TypeError) as exc:
        raise ContractError(f"invalid ISO date: {x}") from exc


def stamp(x):
    try:
        v = datetime.fromisoformat(x.replace("Z", "+00:00"))
        if v.utcoffset() is None:
            raise ValueError("timezone required")
        return v
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContractError(f"timezone-aware timestamp required: {x}") from exc


def serial(x):
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, dict):
        return {k: serial(v) for k, v in x.items()}
    if isinstance(x, (tuple, list)):
        return [serial(v) for v in x]
    return x


def validate(data, mode):
    if mode not in ("synthetic", "formal"):
        raise ContractError("mode must be explicitly synthetic or formal")
    if mode == "formal":
        missing = [k for k in ("actual_fee_evidence", "settlement_evidence",
                   "corporate_action_acceptance", "raw_market_data_acceptance",
                   "cash_interest_evidence", "fx_account_acceptance")
                   if not data.get("acceptance", {}).get(k)]
        raise FormalGate("FORMAL_RESULT_BLOCKED: " + ", ".join(missing or [
            "evidence not independently verified"]) + "; protocol-wide FX, "
            "risk-matched benchmarks, real liquidity model and cost cases not implemented")
    if required(data, "data_kind") != "synthetic_fixture":
        raise ContractError("synthetic mode refuses market/history inputs")
    if required(data, "account_currency") != "USD":
        raise ContractError("single USD ledger only; cross-currency conversion not implemented")
    symbols = required(data, "symbols")
    if not symbols or len(set(symbols)) != len(symbols):
        raise ContractError("unique nonempty symbols required")
    calendar = required(data, "calendar")
    sessions = required(calendar, "sessions")
    if not sessions or sessions != sorted(set(sessions)):
        raise ContractError("sessions must be unique and increasing")
    for d in sessions:
        day(d)
    required(calendar, "source")
    ends = required(calendar, "month_end_sessions")
    if len(set(ends)) != len(ends) or any(d not in sessions for d in ends):
        raise ContractError("month ends must be explicit unique calendar sessions")
    for d in ends:
        later_same_month = any(s > d and s[:7] == d[:7] for s in sessions)
        if later_same_month:
            raise ContractError("month end contradicts supplied calendar")
    first, last = required(data, "start"), required(data, "end")
    if first not in sessions or last not in sessions or first > last:
        raise ContractError("start/end must lie in calendar")
    initial = required(data, "initial")
    if set(required(initial, "holdings")) != set(symbols) or set(required(initial, "marks")) != set(symbols):
        raise ContractError("untracked position/mark or omitted symbol in opening state")
    number(required(initial, "settled_cash"), "initial cash", 0)
    for sym in symbols:
        integer(required(initial["holdings"], sym), "initial shares")
        number(required(initial["marks"], sym), "initial mark", Decimal("0.000001"))
        meta = data["instruments"][sym]
        if required(meta, "currency") != "USD":
            raise ContractError("instrument currency differs from USD ledger")
        integer(required(meta, "lot_size"), "lot size", 1)
        number(required(meta, "price_tick"), "tick", Decimal("0.000001"))
        seed = required(data["seed_volume_history"], sym)
        if len(seed) != 20:
            raise ContractError("explicit 20 prior volumes required for controls")
        for v in seed:
            number(v, "seed volume", 0)
    fees = required(data, "fees")
    for key in ("commission_rate", "minimum_commission", "platform_per_order",
                "buy_tax_rate", "sell_tax_rate", "slippage_rate", "annual_cash_rate"):
        number(required(fees, key), key, 0)
    number(required(fees, "fee_quantum"), "fee quantum", Decimal("0.000001"))
    required(fees, "evidence")
    settlement = required(data, "settlement")
    integer(required(settlement, "cash_sessions"), "cash settlement")
    integer(required(settlement, "share_sessions"), "share settlement")
    if required(settlement, "reuse_unsettled_proceeds") is not False:
        raise ContractError("only explicit no-reuse policy implemented")
    if required(settlement, "resell_unsettled_shares") is not False:
        raise ContractError("only explicit no-resale policy implemented")
    required(settlement, "evidence")
    rows = required(data, "bars")
    seen = set()
    for bar in rows:
        k = (required(bar, "date"), required(bar, "symbol"))
        if k in seen or k[0] not in sessions or k[1] not in symbols:
            raise ContractError("duplicate or out-of-calendar bar")
        seen.add(k)
        if stamp(required(bar, "open_at")) >= stamp(required(bar, "close_at")):
            raise ContractError("open must precede close")
        if stamp(required(bar, "available_at")) < stamp(bar["close_at"]):
            raise ContractError("bar availability before close")
        if stamp(required(bar, "open_quote_known_at")) > stamp(bar["open_at"]):
            raise ContractError("opening quote information was late")
        if required(bar, "open_status") not in ("tradable", "halted", "unknown"):
            raise ContractError("explicit opening tradability status required")
        if "open_capacity_shares" not in bar:
            raise ContractError("open_capacity_shares key required, unknown must be null")
        if bar["open_capacity_shares"] is not None:
            integer(bar["open_capacity_shares"], "open capacity", 0)
        for key in ("open", "high", "low", "close", "volume"):
            if key not in bar:
                raise ContractError(f"bar {key}: key required, unknown must be null")
            if bar[key] is not None:
                v = number(bar[key], key, 0)
                if key != "volume" and v <= 0:
                    raise ContractError("nonpositive price")
        if all(bar[k] is not None for k in ("open", "high", "low", "close")):
            o, h, l, c = (number(bar[k], k) for k in ("open", "high", "low", "close"))
            if not l <= min(o, c) <= max(o, c) <= h:
                raise ContractError("invalid OHLC range")
    for d in sessions:
        if first <= d <= last and any((d, s) not in seen for s in symbols):
            raise ContractError(f"missing entire bar: {d}; provide explicit null fields")
    ids = set()
    actions = required(data, "actions")
    for a in actions:
        aid = required(a, "id")
        if aid in ids or required(a, "symbol") not in symbols:
            raise ContractError("duplicate action ID or unknown symbol")
        ids.add(aid)
        stamp(required(a, "known_at"))
        if required(a, "effective_date") not in sessions:
            raise ContractError("action date must be calendar session")
        if a["effective_date"] < first:
            raise ContractError("pre-start actions must be reflected explicitly in opening state")
        if a["type"] == "split":
            integer(required(a, "numerator"), "split numerator", 1)
            integer(required(a, "denominator"), "split denominator", 1)
        elif a["type"] == "dividend":
            number(required(a, "gross_per_share"), "gross dividend", 0)
            tax = number(required(a, "withholding_rate"), "withholding", 0)
            if tax > 1:
                raise ContractError("withholding exceeds 100%")
            pay = required(a, "pay_date")
            if pay not in sessions or pay < a["effective_date"]:
                raise ContractError("explicit payment session on/after ex date required")
            if required(a, "share_basis") != "post_split":
                raise ContractError("dividend basis must explicitly be post_split")
        else:
            raise ContractError("unknown corporate action")
    for intent in required(data, "control_intents"):
        if intent["decision_date"] not in sessions:
            raise ContractError("control decision not in calendar")
        for sym in symbols:
            number(required(intent["weights"], sym), "target weight", 0)
        if sum(number(v, "weight") for v in intent["weights"].values()) > 1:
            raise ContractError("leverage forbidden")


class Engine:
    def __init__(self, data, mode):
        validate(data, mode)
        self.data = copy.deepcopy(data)
        self.symbols = data["symbols"]
        self.calendar = data["calendar"]["sessions"]
        self.month_ends = set(data["calendar"]["month_end_sessions"])
        self.bars = {(b["date"], b["symbol"]): b for b in data["bars"]}
        self.cash = number(data["initial"]["settled_cash"], "cash")
        self.holdings = dict(data["initial"]["holdings"])
        self.marks = {s: number(data["initial"]["marks"][s], "mark") for s in self.symbols}
        self.pending_cash = []
        self.pending_shares = []
        self.receivables = {}
        self.events, self.snapshots, self.signals, self.pending_intents = [], [], [], []
        self.index = {s: Decimal(100) for s in self.symbols}
        self.previous_close = dict(self.marks)
        self.monthly = {s: [] for s in self.symbols}
        self.volumes = {s: [number(v, "volume") for v in data["seed_volume_history"][s]]
                        for s in self.symbols}
        self.fee = data["fees"]
        self.day = data["start"]
        self.last_day = None

    def emit(self, kind, **kw):
        event = {"sequence": len(self.events), "date": self.day, "type": kind, **serial(kw)}
        self.events.append(event)
        return event

    def due_date(self, lag):
        idx = self.calendar.index(self.day) + lag
        if idx >= len(self.calendar):
            raise ContractError("calendar does not cover settlement/next execution date")
        return self.calendar[idx]

    def available_shares(self, s):
        locked = sum(x["qty"] for x in self.pending_shares if x["symbol"] == s)
        return self.holdings[s] - locked

    def settle(self):
        for p in list(self.pending_cash):
            if p["due"] <= self.day:
                self.cash += p["amount"]
                self.emit("CASH_SETTLEMENT", fill_sequence=p["fill_sequence"], amount=p["amount"])
                self.pending_cash.remove(p)
        for p in list(self.pending_shares):
            if p["due"] <= self.day:
                self.emit("SHARE_SETTLEMENT", fill_sequence=p["fill_sequence"], qty=p["qty"])
                self.pending_shares.remove(p)

    def corporate_actions(self):
        effective = [a for a in self.data["actions"] if a["effective_date"] == self.day]
        ratios = {s: Decimal(1) for s in self.symbols}
        div = {s: Decimal(0) for s in self.symbols}
        for a in sorted(effective, key=lambda a: (a["type"] != "split", a["id"])):
            s = a["symbol"]
            if stamp(a["known_at"]) > stamp(self.bars[self.day, s]["open_at"]):
                raise ContractError(f"late corporate action {a['id']}; no historical backfill")
            if a["type"] == "split":
                ratio = Decimal(a["numerator"]) / Decimal(a["denominator"])
                new = Decimal(self.holdings[s]) * ratio
                if new != new.to_integral_value():
                    raise ContractError("fractional split entitlement/cash-in-lieu unsupported")
                self.holdings[s] = int(new)
                for p in self.pending_shares:
                    if p["symbol"] == s:
                        q = Decimal(p["qty"]) * ratio
                        if q != q.to_integral_value():
                            raise ContractError("fractional pending split unsupported")
                        p["qty"] = int(q)
                self.marks[s] /= ratio
                ratios[s] *= ratio
                self.emit("SPLIT", action_id=a["id"], symbol=s,
                          numerator=a["numerator"], denominator=a["denominator"])
            else:
                gross = number(a["gross_per_share"], "dividend")
                tax = number(a["withholding_rate"], "tax")
                amount = Decimal(self.holdings[s]) * gross * (1 - tax)
                self.receivables[a["id"]] = {"amount": amount, "due": a["pay_date"]}
                div[s] += gross
                self.emit("DIV_EX", action_id=a["id"], symbol=s, entitled_qty=self.holdings[s],
                          gross_per_share=gross, withholding_rate=tax, net_amount=amount,
                          pay_date=a["pay_date"])
        for aid, p in list(self.receivables.items()):
            if p["due"] == self.day:
                self.cash += p["amount"]
                self.emit("DIV_PAY", action_id=aid, amount=p["amount"])
                del self.receivables[aid]
        return ratios, div

    def value(self, prices):
        return (self.cash + sum(p["amount"] for p in self.pending_cash)
                + sum(p["amount"] for p in self.receivables.values())
                + sum(Decimal(self.holdings[s]) * prices[s] for s in self.symbols))

    def price_and_fees(self, sym, side, raw, qty):
        tick = number(self.data["instruments"][sym]["price_tick"], "tick")
        slip = number(self.fee["slippage_rate"], "slip")
        price = raw * (1 + slip if side == "BUY" else 1 - slip)
        rounding = ROUND_CEILING if side == "BUY" else ROUND_FLOOR
        price = (price / tick).to_integral_value(rounding=rounding) * tick
        if price <= 0:
            raise ContractError("invalid execution price after adverse rounding")
        notional = price * qty
        quantum = number(self.fee["fee_quantum"], "fee quantum")
        def round_fee(x):
            return (x / quantum).to_integral_value(rounding=ROUND_HALF_UP) * quantum
        commission = round_fee(max(number(self.fee["minimum_commission"], "minimum"),
                               notional * number(self.fee["commission_rate"], "rate")))
        platform = round_fee(number(self.fee["platform_per_order"], "platform"))
        tax = round_fee(notional * number(self.fee["buy_tax_rate" if side == "BUY" else "sell_tax_rate"], "tax"))
        return price, commission, platform, tax

    def execute_targets(self, intent):
        prices = dict(self.marks)
        for s in self.symbols:
            if self.bars[self.day, s]["open"] is not None:
                prices[s] = number(self.bars[self.day, s]["open"], "open")
            elif self.holdings[s]:
                self.emit("STALE_OPEN_MARK", symbol=s, mark=prices[s])
        nav = self.value(prices)
        deltas = {}
        for s in self.symbols:
            lot = int(self.data["instruments"][s]["lot_size"])
            target = int((nav * number(intent["weights"][s], "weight") / prices[s] / lot)
                         .to_integral_value(rounding=ROUND_FLOOR)) * lot
            deltas[s] = target - self.holdings[s]
        for side in ("SELL", "BUY"):
            for s in self.symbols:
                delta = deltas[s]
                if (side == "SELL" and delta >= 0) or (side == "BUY" and delta <= 0):
                    continue
                bar = self.bars[self.day, s]
                wanted = abs(delta)
                self.emit("ORDER_ATTEMPT", symbol=s, side=side, requested_qty=wanted,
                          decision_date=intent["decision_date"], intent_source=intent["source"],
                          observed_at=bar["open_at"])
                why = None
                if bar["open"] is None:
                    why = "MISSING_OPEN"
                elif bar["open_status"] == "unknown":
                    why = "UNKNOWN_OPEN_TRADABILITY"
                elif bar["open_status"] == "halted":
                    why = "HALTED_AT_OPEN"
                elif bar["open_capacity_shares"] is None:
                    why = "UNKNOWN_OPEN_LIQUIDITY"
                elif bar["open_capacity_shares"] == 0:
                    why = "ZERO_OPEN_LIQUIDITY"
                if why:
                    self.emit("ORDER_CANCEL", symbol=s, side=side, requested_qty=wanted, reason=why)
                    continue
                lot = int(self.data["instruments"][s]["lot_size"])
                if len(self.volumes[s]) < 20:
                    self.emit("ORDER_CANCEL", symbol=s, side=side, requested_qty=wanted,
                              reason="VOLUME_WARMUP_INCOMPLETE")
                    continue
                cap = int((sum(self.volumes[s][-20:]) / 20 * Decimal("0.001") / lot)
                          .to_integral_value(rounding=ROUND_FLOOR)) * lot
                cap = min(cap, int(bar["open_capacity_shares"]) // lot * lot)
                qty = min(wanted, cap)
                reasons = ["CAPACITY"] if qty < wanted else []
                if side == "SELL":
                    sellable = self.available_shares(s)
                    if qty > sellable:
                        qty = sellable // lot * lot
                        reasons.append("UNSETTLED_SHARES")
                else:
                    # Binary search fee-aware affordable integer lots; no negative cash.
                    lo, hi = 0, qty // lot
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        p, c, f, t = self.price_and_fees(s, side, prices[s], mid * lot)
                        if p * mid * lot + c + f + t <= self.cash:
                            lo = mid
                        else:
                            hi = mid - 1
                    if lo * lot < qty:
                        reasons.append("INSUFFICIENT_SETTLED_CASH")
                    qty = lo * lot
                if qty == 0:
                    self.emit("ORDER_CANCEL", symbol=s, side=side, requested_qty=wanted,
                              reason="|".join(reasons) or "NO_EXECUTABLE_QUANTITY")
                    continue
                p, c, f, t = self.price_and_fees(s, side, prices[s], qty)
                fees = c + f + t
                cash_change = -(p * qty + fees) if side == "BUY" else p * qty - fees
                if side == "SELL" and cash_change <= 0:
                    self.emit("ORDER_CANCEL", symbol=s, side=side, requested_qty=wanted,
                              reason="NONPOSITIVE_NET_PROCEEDS")
                    continue
                due = self.due_date(int(self.data["settlement"]["share_sessions" if side == "BUY" else "cash_sessions"]))
                ev = self.emit("FILL", symbol=s, side=side, qty=qty, requested_qty=wanted,
                               raw_open=prices[s], price=p, commission=c, platform=f, tax=t,
                               fee=fees, settlement_date=due, decision_date=intent["decision_date"],
                               intent_source=intent["source"], reduction_reasons=reasons)
                if side == "BUY":
                    self.cash += cash_change
                    self.holdings[s] += qty
                    if due > self.day:
                        self.pending_shares.append({"symbol": s, "qty": qty, "due": due,
                                                    "fill_sequence": ev["sequence"]})
                else:
                    self.holdings[s] -= qty
                    if due == self.day:
                        self.cash += cash_change
                    else:
                        self.pending_cash.append({"amount": cash_change, "due": due,
                                                  "fill_sequence": ev["sequence"]})
                if qty < wanted:
                    self.emit("ORDER_REMAINDER_CANCEL", symbol=s, side=side,
                              unfilled_qty=wanted-qty, reasons=reasons)

    def schedule(self, weights, source, available_at):
        close_at = max(stamp(self.bars[self.day, s]["close_at"]) for s in self.symbols)
        if stamp(available_at) != close_at:
            raise ContractError("late close data: next-open timing not accepted")
        due = self.due_date(1)
        for s in self.symbols:
            b = self.bars.get((due, s))
            if b and stamp(available_at) >= stamp(b["open_at"]):
                raise ContractError("signal not available before execution open")
        self.pending_intents.append({"decision_date": self.day, "due": due,
                                     "weights": serial(weights), "source": source})
        self.emit("INTENT", weights=weights, execution_date=due, source=source,
                  available_at=available_at)

    def run(self):
        for d in self.calendar:
            if not self.data["start"] <= d <= self.data["end"]:
                continue
            self.day = d
            if self.last_day is not None:
                elapsed = (day(d) - day(self.last_day)).days
                interest = self.cash * number(self.fee["annual_cash_rate"], "cash rate") * elapsed / 365
                if interest:
                    self.cash += interest
                    self.emit("INTEREST", amount=interest, days=elapsed)
            self.settle()
            ratios, dividends = self.corporate_actions()
            for intent in list(self.pending_intents):
                if intent["due"] == d:
                    self.execute_targets(intent)
                    self.pending_intents.remove(intent)
            complete = True
            for s in self.symbols:
                bar = self.bars[d, s]
                day_fills = [e for e in self.events if e["date"] == d and
                             e["type"] == "FILL" and e["symbol"] == s]
                if day_fills and (bar["volume"] is None or
                    number(bar["volume"], "volume") < sum(e["qty"] for e in day_fills)):
                    self.emit("POST_CLOSE_FILL_DIAGNOSTIC", symbol=s,
                              reason="UNKNOWN_OR_INSUFFICIENT_DAILY_VOLUME",
                              simulated_qty=sum(e["qty"] for e in day_fills), daily_volume=bar["volume"],
                              observed_at=bar["available_at"], action="REVIEW_NOT_RETROACTIVE_CANCEL")
                if bar["close"] is None:
                    self.emit("STALE_CLOSE_MARK", symbol=s, mark=self.marks[s])
                    complete = False
                    # A broken total-return chain is never silently bridged.
                    self.previous_close[s] = None
                    self.index[s] = None
                else:
                    close = number(bar["close"], "close")
                    prev = self.previous_close[s]
                    if prev is not None and self.index[s] is not None:
                        self.index[s] *= ratios[s] * (close + dividends[s]) / prev
                    self.previous_close[s] = close
                    self.marks[s] = close
                    if self.index[s] is None:
                        complete = False
                if bar["volume"] is not None:
                    self.volumes[s].append(number(bar["volume"], "volume"))
                else:
                    self.volumes[s] = []
            if d in self.month_ends:
                if not complete:
                    self.emit("SIGNAL_SKIPPED", reason="BROKEN_TOTAL_RETURN_CHAIN")
                else:
                    for s in self.symbols:
                        self.monthly[s].append(self.index[s])
                    if min(len(self.monthly[s]) for s in self.symbols) < 10:
                        self.emit("SIGNAL_SKIPPED", reason="TEN_MONTH_WARMUP")
                    else:
                        weights = {s: Decimal("0.25") if self.index[s] > sum(self.monthly[s][-10:]) / 10
                                   else Decimal(0) for s in self.symbols}
                        obs = {"date": d, "index": dict(self.index),
                               "sma10": {s: sum(self.monthly[s][-10:]) / 10 for s in self.symbols},
                               "weights": weights}
                        self.signals.append(serial(obs))
                        latest = max((self.bars[d, s]["available_at"] for s in self.symbols), key=stamp)
                        self.schedule(weights, "S02-E01_MONTHLY_SMA10", latest)
            for intent in self.data["control_intents"]:
                if intent["decision_date"] == d:
                    if any(i["decision_date"] == d for i in self.pending_intents):
                        raise ContractError("two intents on one day unsupported")
                    latest = max((self.bars[d, s]["available_at"] for s in self.symbols), key=stamp)
                    self.schedule(intent["weights"], "SYNTHETIC_CONTROL_INTENT", latest)
            if self.cash < 0 or any(q < 0 for q in self.holdings.values()):
                raise ContractError("negative settled cash or short position")
            self.snapshots.append(serial({"date": d, "settled_cash": self.cash,
                "unsettled_cash": sum(p["amount"] for p in self.pending_cash),
                "dividend_receivable": sum(p["amount"] for p in self.receivables.values()),
                "holdings": dict(self.holdings),
                "sellable": {s: self.available_shares(s) for s in self.symbols},
                "marks": dict(self.marks), "equity": self.value(self.marks)}))
            self.last_day = d
        return {"engine_version": VERSION, "classification": "SYNTHETIC_CONTROL_ONLY",
                "events": self.events, "signals": self.signals, "snapshots": self.snapshots,
                "pending_intents": self.pending_intents,
                "formal_history_trials": 0, "broker_orders": 0}


def run(data, mode):
    return Engine(data, mode).run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--mode", required=True, choices=("synthetic", "formal"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        out = run(json.loads(args.input.read_text()), args.mode)
    except ContractError as exc:
        print(json.dumps({"status": "STOPPED", "reason": str(exc)}, ensure_ascii=False))
        return 2
    if args.output:
        args.output.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    else:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
