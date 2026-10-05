"""Explicit synthetic long-only USD accounting over the installed Vibe engine.

This bridge changes the declared model, not the vendor source. It uses engine
positions/fills/capital; the independent Fraction replay lives elsewhere.
"""
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR

from .reference import low_frequency_engine as contract


def accounting_class(base, *, input_mode="synthetic"):
    if input_mode not in {"synthetic", "historical_model"}:
        raise contract.ContractError("explicit supported accounting input mode required")
    class AccountingUS(base):
        def __init__(self, config, fees, data):
            if input_mode == "synthetic":
                contract.validate(data, "synthetic")
            else:
                from .historical_contract import validate_account_input
                validate_account_input(data)
            super().__init__(config, fees)
            self.account_data = data
            self.pending_cash, self.pending_shares, self.receivables = [], [], {}
            self.account_events, self.account_snapshots = [], []
            self.account_day = None
            self.phase = None
            self.seeded = False
            self.capacity_used = {}
            self.bar_lookup = {(b["date"], b["symbol"]): b for b in data["bars"]}
            self.account_marks = {s: Decimal(v) for s, v in data["initial"]["marks"].items()}
            self.prior_volumes = {s: [Decimal(v) for v in values] for s, values in data["seed_volume_history"].items()}
            self.order_metadata = {}
            self.close_frame = None
            self.actions_by_day = {}
            for action in data["actions"]:
                self.actions_by_day.setdefault(action["effective_date"], []).append(action)

        def cash_for_planning(self):
            # Registered USD decision quantum; quantities and event prices/fees
            # stay exact. This addresses binary state representation only.
            return Decimal(str(self.capital)).quantize(Decimal("0.00000001"))

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

        def execution_date(self, intent):
            sessions = self.account_data["calendar"]["sessions"]
            if input_mode == "historical_model":
                return intent["execution_date"]
            return sessions[sessions.index(intent["decision_date"]) + 1]

        def settlement_lag(self, side):
            return self.account_data["settlement"]["share_sessions" if side == "BUY" else "cash_sessions"]

        def fill_fee_details(self, qty, price, side):
            f = self.contract_fees
            q = Decimal(f["fee_quantum"])
            comm = max(qty * price * Decimal(f["commission_rate"]), Decimal(f["minimum_commission"]))
            return {"commission": comm.quantize(q, rounding=ROUND_HALF_UP),
                    "platform": Decimal(f["platform_per_order"]).quantize(q, rounding=ROUND_HALF_UP),
                    "tax": (qty * price * Decimal(f["buy_tax_rate" if side == "BUY" else "sell_tax_rate"])).quantize(q, rounding=ROUND_HALF_UP)}

        def closing_information_known(self, bar):
            return True  # synthetic contract timing was checked before execution

        def dividend_payment_due(self, receivable):
            return receivable["due"] == self.account_day

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
            if self.close_frame is None:
                import pandas as pd
                self.close_frame = pd.DataFrame({s: data_map[s]["close"] for s in codes})
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
            actions = self.actions_by_day.get(self.account_day, [])
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
                    self.account_marks[s] /= ratio
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
                if self.dividend_payment_due(p):
                    self.capital += float(p["amount"])
                    self.emit("DIV_PAY", action_id=aid, amount=p["amount"])
                    del self.receivables[aid]
            return False

        def _calc_open_equity(self, data_map, close_df, ts):
            value = self.cash_for_planning() + self.assets_not_cash()
            for s in self.account_data["symbols"]:
                bar = self.bar_lookup[self.account_day, s]
                price = Decimal(bar["open"]) if bar["open"] is not None else self.account_marks[s]
                value += self.held(s) * price
            return float(value)

        def planning_mark(self, symbol):
            bar = self.bar_lookup[self.account_day, symbol]
            return Decimal(bar["open"]) if bar["open"] is not None else self.account_marks[symbol]

        def _safe_price(self, close_df, ts, symbol, fallback, **kwargs):
            import pandas as pd
            value = close_df.loc[ts, symbol]
            return float(value) if pd.notna(value) else float(self.account_marks[symbol])

        def _calc_equity(self, close_df, ts):
            return super()._calc_equity(close_df, ts) + float(self.assets_not_cash())

        def can_execute(self, symbol, direction, bar):
            if (self.phase == "sell" and direction != 0) or (self.phase == "buy" and direction == 0):
                return False
            if direction == 0 and self.sellable(symbol) <= 0:
                return False
            return super().can_execute(symbol, direction, bar)

        def _execute_target_rebalance(self, weights, data_map, ts, equity, codes):
            from backtest.engines.base import _OpenOrder
            sessions = self.account_data["calendar"]["sessions"]
            requested = {self.execution_date(p) for p in self.account_data["control_intents"]}
            if self.account_day not in requested:
                return  # an empty intent calendar must preserve opening holdings
            intent = next(p for p in self.account_data["control_intents"]
                          if self.execution_date(p) == self.account_day)
            order = sorted(codes, key=lambda s: (("SPY", "EFA", "IEF", "GLD").index(s) if s in ("SPY", "EFA", "IEF", "GLD") else 4, s))
            nav = Decimal(str(equity)).quantize(Decimal("0.00000001"))
            deltas = {}
            for s in order:
                bar = self.bar_lookup[self.account_day, s]
                mark = self.planning_mark(s)
                target = int((nav * Decimal(str(weights[s])) / mark).to_integral_value(rounding=ROUND_FLOOR))
                deltas[s] = target - self.held(s)
            self.emit("TARGET_PLAN", decision_date=intent["decision_date"], opening_equity=nav,
                      allocation_policy="SPY_EFA_IEF_GLD_SEQUENTIAL_FEE_AWARE", deltas=deltas)
            for side in ("SELL", "BUY"):
                for s in order:
                    delta = deltas[s]
                    if (side == "SELL" and delta >= 0) or (side == "BUY" and delta <= 0):
                        continue
                    bar = self.bar_lookup[self.account_day, s]
                    wanted = abs(delta)
                    self.emit("ORDER_ATTEMPT", symbol=s, side=side, requested_qty=wanted,
                              decision_date=intent["decision_date"], observed_at=bar["open_at"])
                    reason = ("MISSING_OPEN" if bar["open"] is None else
                              "LATE_OPEN_QUOTE" if (bar["open_quote_known_at"] is None or
                                  contract.stamp(bar["open_quote_known_at"]) > contract.stamp(bar["open_at"])) else
                              "UNKNOWN_OPEN_TRADABILITY" if bar["open_status"] == "unknown" else
                              "HALTED_AT_OPEN" if bar["open_status"] == "halted" else
                              "UNKNOWN_OPEN_LIQUIDITY" if bar["open_capacity_shares"] is None else
                              "ZERO_OPEN_LIQUIDITY" if bar["open_capacity_shares"] <= 0 else
                              "VOLUME_WARMUP_INCOMPLETE" if len(self.prior_volumes[s]) < 20 else None)
                    if reason:
                        self.emit("ORDER_CANCEL", symbol=s, side=side, requested_qty=wanted, reason=reason)
                        continue
                    capacity = int(sum(self.prior_volumes[s][-20:]) / 20 * Decimal("0.001"))
                    capacity = min(capacity, bar["open_capacity_shares"] - self.capacity_used[s])
                    qty = min(wanted, capacity)
                    reasons = ["CAPACITY"] if qty < wanted else []
                    direction = 1 if side == "BUY" else -1
                    price = self.apply_slippage(float(bar["open"]), direction)
                    if side == "SELL":
                        if qty > self.sellable(s):
                            qty = self.sellable(s); reasons.append("UNSETTLED_SHARES")
                    else:
                        low, high = 0, qty
                        while low < high:
                            middle = (low + high + 1) // 2
                            fee = self.calc_commission(middle, price, 1, True)
                            if middle * Decimal(str(price)) + Decimal(str(fee)) <= self.cash_for_planning():
                                low = middle
                            else:
                                high = middle - 1
                        if low < qty:
                            reasons.append("INSUFFICIENT_SETTLED_CASH")
                        qty = low
                    if qty <= 0:
                        self.emit("ORDER_CANCEL", symbol=s, side=side, requested_qty=wanted,
                                  reason="|".join(reasons) or "NO_EXECUTABLE_QUANTITY")
                        continue
                    fee = self.calc_commission(qty, price, 1, side == "BUY")
                    if side == "SELL" and qty * Decimal(str(price)) - Decimal(str(fee)) <= 0:
                        self.emit("ORDER_CANCEL", symbol=s, side=side, requested_qty=wanted, reason="NONPOSITIVE_NET_PROCEEDS")
                        continue
                    self.order_metadata = {"requested_qty": wanted, "decision_date": intent["decision_date"],
                                           "intent_source": intent.get("source", "SYNTHETIC_CONTROL_INTENT"), "reduction_reasons": reasons}
                    if side == "SELL":
                        before = self.positions[s]
                        if qty == self.held(s):
                            self._close_position(s, price, ts, "signal")
                        else:
                            self._execute_partial_reduction(self._plan_reduction(before, before.size-qty, price), ts)
                    else:
                        fill = _OpenOrder(s, 1, price, qty, 1.0, qty*price, fee)
                        if s in self.positions:
                            self._execute_position_increase(fill, ts)
                        else:
                            self._execute_open_order(fill, ts)
                    if qty < wanted:
                        self.emit("ORDER_REMAINDER_CANCEL", symbol=s, side=side, unfilled_qty=wanted-qty, reasons=reasons)

        def _plan_reduction(self, before, target_size, price):
            bar = self.bar_lookup[self.account_day, before.symbol]
            remaining = bar["open_capacity_shares"] - self.capacity_used[before.symbol]
            qty = min(before.size - target_size, self.sellable(before.symbol), max(remaining, 0))
            return super()._plan_reduction(before, before.size - qty, price)

        def _execute_position_increase(self, order, ts):
            bar = self.bar_lookup[self.account_day, order.symbol]
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
            fee_details = self.fill_fee_details(qty, price, side)
            lag = self.settlement_lag(side)
            due = self.due(lag)
            bar = self.bar_lookup[self.account_day, s]
            seq = self.emit("FILL", symbol=s, side=side, qty=qty, price=price, raw_open=bar["open"],
                            **fee_details, fee=fee, settlement_date=due, **self.order_metadata)
            if lag and side == "BUY":
                self.pending_shares.append({"sequence": seq, "symbol": s, "qty": qty, "due": due})
            elif lag:
                proceeds = qty * price - fee
                self.capital -= float(proceeds)  # undo native immediate credit
                self.pending_cash.append({"sequence": seq, "amount": proceeds, "due": due})

        def after_rebalance_bar(self, timestamp, data_map, codes):
            if self.capital < -1e-8:
                raise contract.ContractError("Negative native cash beyond registered monetary tolerance")
            for s in codes:
                bar = self.bar_lookup[self.account_day, s]
                if self.capacity_used[s] and (bar["volume"] is None or Decimal(bar["volume"]) < self.capacity_used[s]):
                    self.emit("POST_CLOSE_FILL_DIAGNOSTIC", symbol=s, reason="UNKNOWN_OR_INSUFFICIENT_DAILY_VOLUME",
                              simulated_qty=self.capacity_used[s], daily_volume=bar["volume"], observed_at=bar["available_at"],
                              action="REVIEW_NOT_RETROACTIVE_CANCEL")
                if bar["close"] is not None and self.closing_information_known(bar):
                    self.account_marks[s] = Decimal(bar["close"])
                if bar["volume"] is None or not self.closing_information_known(bar):
                    self.prior_volumes[s] = []
                else:
                    self.prior_volumes[s].append(Decimal(bar["volume"]))
            holdings = {s: self.held(s) for s in codes}
            # Equity originates in the native valuation path plus explicit
            # non-cash assets. Never substitute the replay's calculated equity.
            equity = self._calc_equity(self.close_frame, timestamp)
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
