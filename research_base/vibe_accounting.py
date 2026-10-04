"""Explicit synthetic long-only USD accounting over the installed Vibe engine.

This bridge changes the declared model, not the vendor source. It uses engine
positions/fills/capital; the independent Fraction replay lives elsewhere.
"""
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP

from .reference import low_frequency_engine as contract


def accounting_class(base):
    class AccountingUS(base):
        def __init__(self, config, fees, data):
            contract.validate(data, "synthetic")
            super().__init__(config, fees)
            self.account_data = data
            self.pending_cash, self.pending_shares, self.receivables = [], [], {}
            self.account_events, self.account_snapshots = [], []
            self.account_day = None
            self.phase = None
            self.seeded = False
            self.capacity_used = {}

        def emit(self, kind, **fields):
            # Sequence is also the identifier used by settlement events.
            event = {"sequence": len(self.account_events) + 1, "date": self.account_day,
                     "type": kind, **fields}
            self.account_events.append(contract.serial(event))
            return event["sequence"]

        def due(self, lag):
            sessions = self.account_data["calendar"]["sessions"]
            index = sessions.index(self.account_day) + lag
            if index >= len(sessions):
                raise contract.ContractError("calendar does not cover settlement")
            return sessions[index]

        def held(self, symbol):
            position = self.positions.get(symbol)
            return int(position.size) if position else 0

        def sellable(self, symbol):
            return self.held(symbol) - sum(p["qty"] for p in self.pending_shares if p["symbol"] == symbol)

        def assets_not_cash(self):
            return sum((p["amount"] for p in self.pending_cash), Decimal(0)) + sum(
                (p["amount"] for p in self.receivables.values()), Decimal(0))

        def before_rebalance_bar(self, timestamp, data_map, codes):
            from backtest.models import Position
            self.account_day = str(timestamp.date())
            self.capacity_used = {s: 0 for s in codes}
            if not self.seeded:
                for s, qty in self.account_data["initial"]["holdings"].items():
                    if qty:
                        self.positions[s] = Position(s, 1, float(self.account_data["initial"]["marks"][s]),
                                                     timestamp, qty, 1.0, -1, 0.0)
                self.seeded = True
            for p in list(self.pending_cash):
                if p["due"] <= self.account_day:
                    self.capital += float(p["amount"])
                    self.emit("CASH_SETTLEMENT", fill_sequence=p["sequence"], amount=p["amount"])
                    self.pending_cash.remove(p)
            for p in list(self.pending_shares):
                if p["due"] <= self.account_day:
                    self.emit("SHARE_SETTLEMENT", fill_sequence=p["sequence"], qty=p["qty"])
                    self.pending_shares.remove(p)
            actions = [a for a in self.account_data["actions"] if a["effective_date"] == self.account_day]
            for a in sorted(actions, key=lambda a: (a["type"] != "split", a["id"])):
                s = a["symbol"]
                if contract.stamp(a["known_at"]) > contract.stamp(data_map[s].loc[timestamp, "open_at"]):
                    raise contract.ContractError("late action: historical backfill prohibited")
                if a["type"] == "split":
                    ratio = Decimal(a["numerator"]) / Decimal(a["denominator"])
                    quantities = [Decimal(self.held(s)) * ratio] + [
                        Decimal(p["qty"]) * ratio for p in self.pending_shares if p["symbol"] == s]
                    if any(q != q.to_integral_value() for q in quantities):
                        raise contract.ContractError("fractional split/cash-in-lieu unsupported")
                    if s in self.positions:
                        p = self.positions[s]
                        self.positions[s] = replace(p, size=float(quantities[0]), entry_price=p.entry_price / float(ratio))
                    for p in self.pending_shares:
                        if p["symbol"] == s:
                            p["qty"] = int(Decimal(p["qty"]) * ratio)
                    self.emit("SPLIT", action_id=a["id"], symbol=s,
                              numerator=a["numerator"], denominator=a["denominator"])
                else:
                    qty = self.held(s)
                    gross, tax = Decimal(a["gross_per_share"]), Decimal(a["withholding_rate"])
                    amount = qty * gross * (1 - tax)
                    self.receivables[a["id"]] = {"amount": amount, "due": a["pay_date"]}
                    self.emit("DIV_EX", action_id=a["id"], symbol=s, entitled_qty=qty,
                              gross_per_share=gross, withholding_rate=tax, net_amount=amount, pay_date=a["pay_date"])
            for aid, p in list(self.receivables.items()):
                if p["due"] == self.account_day:
                    self.capital += float(p["amount"])
                    self.emit("DIV_PAY", action_id=aid, amount=p["amount"])
                    del self.receivables[aid]
            return False

        def _calc_open_equity(self, data_map, close_df, ts):
            return super()._calc_open_equity(data_map, close_df, ts) + float(self.assets_not_cash())

        def _calc_equity(self, close_df, ts):
            return super()._calc_equity(close_df, ts) + float(self.assets_not_cash())

        def can_execute(self, symbol, direction, bar):
            if (self.phase == "sell" and direction != 0) or (self.phase == "buy" and direction == 0):
                return False
            if direction == 0 and self.sellable(symbol) <= 0:
                return False
            return super().can_execute(symbol, direction, bar)

        def _execute_target_rebalance(self, weights, data_map, ts, equity, codes):
            sessions = self.account_data["calendar"]["sessions"]
            requested = {sessions[sessions.index(p["decision_date"]) + 1]
                         for p in self.account_data["control_intents"]}
            if self.account_day not in requested:
                return  # an empty intent calendar must preserve opening holdings
            # Commit sales before planning buys. Only settled proceeds enter
            # capital, so the native basket fitter cannot spend sale receivables.
            self.phase = "sell"
            try:
                super()._execute_target_rebalance(weights, data_map, ts, equity, codes)
                self.phase = "buy"
                super()._execute_target_rebalance(weights, data_map, ts, equity, codes)
            finally:
                self.phase = None

        def _plan_reduction(self, before, target_size, price):
            bars = self.account_data["bars"]
            bar = next(b for b in bars if b["date"] == self.account_day and b["symbol"] == before.symbol)
            remaining = bar["open_capacity_shares"] - self.capacity_used[before.symbol]
            qty = min(before.size - target_size, self.sellable(before.symbol), max(remaining, 0))
            return super()._plan_reduction(before, before.size - qty, price)

        def _execute_position_increase(self, order, ts):
            bar = next(b for b in self.account_data["bars"] if b["date"] == self.account_day and b["symbol"] == order.symbol)
            size = min(order.size, max(bar["open_capacity_shares"] - self.capacity_used[order.symbol], 0))
            if size <= 0:
                return
            if size != order.size:
                order = replace(order, size=size, margin=size * order.price,
                                commission=self.calc_commission(size, order.price, order.direction, True))
            super()._execute_position_increase(order, ts)

        def _record_fill(self, **fields):
            super()._record_fill(**fields)
            s = fields["symbol"]
            signed = Decimal(str(fields["signed_quantity"]))
            if signed != signed.to_integral_value() or signed == 0:
                raise contract.ContractError("positive integer fill required")
            qty = int(abs(signed))
            self.capacity_used[s] += qty
            price, fee = Decimal(str(fields["execution_price"])), Decimal(str(fields["fee"]))
            side = "BUY" if signed > 0 else "SELL"
            f = self.contract_fees
            q = Decimal(f["fee_quantum"])
            comm = max(qty * price * Decimal(f["commission_rate"]), Decimal(f["minimum_commission"]))
            comm = comm.quantize(q, rounding=ROUND_HALF_UP)
            platform = Decimal(f["platform_per_order"]).quantize(q, rounding=ROUND_HALF_UP)
            tax = (qty * price * Decimal(f["buy_tax_rate" if side == "BUY" else "sell_tax_rate"])).quantize(q, rounding=ROUND_HALF_UP)
            lag = self.account_data["settlement"]["share_sessions" if side == "BUY" else "cash_sessions"]
            due = self.due(lag)
            bar = next(b for b in self.account_data["bars"] if b["date"] == self.account_day and b["symbol"] == s)
            seq = self.emit("FILL", symbol=s, side=side, qty=qty, price=price, raw_open=bar["open"],
                            commission=comm, platform=platform, tax=tax, fee=fee, settlement_date=due)
            if lag and side == "BUY":
                self.pending_shares.append({"sequence": seq, "symbol": s, "qty": qty, "due": due})
            elif lag:
                proceeds = qty * price - fee
                self.capital -= float(proceeds)  # undo native immediate credit
                self.pending_cash.append({"sequence": seq, "amount": proceeds, "due": due})

        def after_rebalance_bar(self, timestamp, data_map, codes):
            holdings = {s: self.held(s) for s in codes}
            # Equity originates in the native valuation path plus explicit
            # non-cash assets. Never substitute the replay's calculated equity.
            import pandas as pd
            frame = pd.DataFrame({s: data_map[s]["close"] for s in codes})
            equity = self._calc_equity(frame, timestamp)
            self.account_snapshots.append(contract.serial({"date": self.account_day,
                "settled_cash": Decimal(str(self.capital)), "unsettled_cash": sum((p["amount"] for p in self.pending_cash), Decimal(0)),
                "dividend_receivable": sum((p["amount"] for p in self.receivables.values()), Decimal(0)),
                "holdings": holdings, "sellable": {s: self.sellable(s) for s in codes}, "equity": Decimal(str(equity))}))
            return False

        def _execute_bars(self, *args, **kwargs):
            super()._execute_bars(*args, **kwargs)
            # BaseEngine overwrites the final snapshot even with no positions.
            # Restore the actual pre-terminal account mark, including receivables.
            last = self.account_snapshots[-1]
            terminal = self.terminal_mark or self.equity_snapshots[-1]
            self.equity_snapshots[-1] = replace(terminal, capital=float(last["settled_cash"]),
                                              equity=float(last["equity"]), positions=sum(bool(q) for q in last["holdings"].values()))

    return AccountingUS
