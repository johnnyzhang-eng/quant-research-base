#!/usr/bin/env python3
"""Independent exact-rational model for a frozen, conditional spot-grid audit.

This file imports no producer calculation or test helpers. Network, credential,
order and paid-service operations are absent. Inputs are read only; the optional
whole-month audit never imports the producer.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction as F
from pathlib import Path

PROTOCOL_SHA = "256d2f05f8334160b616ca0771e6bc42d037f2e04187e089baf8a083c0d372eb"
ZIP_SHA = "02df6da44ed8145fbb9ed819858185d9e2f15eb025c5bec8a4ea2d8738cd0d19"
CSV_SHA = "e0dd165b294294cfa3e2a27afb31817048c654a1d1893ab3acdc810ab0dfe3bd"
CHECKSUM_SHA = "f9534a5fa863e3fd3458c8b8e8254bc61974a9ed8f094d9b77c705895b7a9c7d"
Q = F(1, 100000000)
D = F(10000)
ASSERTIONS = 0


class AuditError(Exception):
    pass


def require(condition, message):
    global ASSERTIONS
    ASSERTIONS += 1
    if not condition:
        raise AuditError(message)


def floor_q(x):
    require(x >= 0, "negative quantization input")
    return (x // Q) * Q


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc(t):
    return datetime.fromtimestamp(t // 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def finite_text(x):
    x = F(x)
    n, d = x.numerator, x.denominator
    sign = "-" if n < 0 else ""
    n = abs(n)
    whole, remainder = divmod(n, d)
    if not remainder:
        return sign + str(whole)
    digits = []
    while remainder:
        require(len(digits) < 100, "unexpected nonfinite decimal output")
        digit, remainder = divmod(remainder * 10, d)
        digits.append(str(digit))
    return sign + str(whole) + "." + "".join(digits)


@dataclass(frozen=True)
class Bar:
    t: int
    o: F
    h: F
    l: F
    c: F
    v: F
    tb: F

    @property
    def buy_capacity(self):
        return floor_q((self.v - self.tb) / 100)

    @property
    def sell_capacity(self):
        return floor_q(self.tb / 100)


def bar(o, h=None, l=None, c=None, v="2000", tb="1000", t=0):
    o = F(str(o))
    return Bar(t, o, F(str(h if h is not None else o)),
               F(str(l if l is not None else o)),
               F(str(c if c is not None else o)), F(v), F(tb))


def raw_bars(zip_path, checksum_path):
    require(sha(zip_path) == ZIP_SHA, "raw ZIP binding")
    require(sha(checksum_path) == CHECKSUM_SHA, "checksum byte binding")
    checksum = Path(checksum_path).read_text().split()
    require(checksum == [ZIP_SHA, "BTCUSDT-1m-2020-01.zip"], "checksum identity")
    with zipfile.ZipFile(zip_path) as z:
        require(z.namelist() == ["BTCUSDT-1m-2020-01.csv"], "archive member")
        require(z.testzip() is None, "archive CRC")
        raw = z.read(z.namelist()[0])
    require(hashlib.sha256(raw).hexdigest() == CSV_SHA, "CSV bytes binding")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
    require(len(rows) == 44640, "raw row count")
    result = []
    for i, row in enumerate(rows):
        require(len(row) == 12, f"raw schema at {i}")
        require(int(row[0]) == 1577836800000 + 60000 * i, f"raw open at {i}")
        require(int(row[6]) == int(row[0]) + 59999, f"raw close at {i}")
        o, h, l, c, v, qv, tb, tqv = map(F, [row[1], row[2], row[3],
                                           row[4], row[5], row[7], row[9], row[10]])
        require(0 < l <= min(o, c) <= max(o, c) <= h, f"raw OHLC at {i}")
        require(0 <= tb <= v and 0 <= tqv <= qv, f"directional volume at {i}")
        tc = F(row[8])
        require(tc >= 0 and tc.denominator == 1, f"trade count at {i}")
        result.append(Bar(int(row[0]), o, h, l, c, v, tb))
    return result


@dataclass
class Slot:
    index: int
    oid: int
    side: str
    price: F
    remaining: F
    held: F
    created: int = 1
    due: int = 2
    active: bool = False
    activated: int | None = None
    defer_reason: str | None = None


class IndependentGrid:
    """A separate slot-state implementation derived from protocol text."""

    def __init__(self, rows, bps, path):
        require(path in ("OHLC", "OLHC"), "unknown sensitivity path")
        require(bps in (0, 10, 25, 100), "unexpected cost arm")
        require(len(rows) >= 3, "minimum clocks")
        self.rows, self.rate, self.path = rows, F(bps, 10000), path
        self.q = floor_q(F(5000) / (10 * rows[1].o))
        require(self.q > 0, "empty initial lot")
        self.levels = [floor_q(rows[0].c * (F(4, 5) + F(i, 50)))
                       for i in range(21)]
        require(all(a < b for a, b in zip(self.levels, self.levels[1:])),
                "collapsed price levels")
        initial_notional = 10 * self.q * rows[1].o
        for f in (F(0), F(1, 1000), F(1, 400), F(1, 100)):
            cash = D - initial_notional * (1 + f)
            require(cash >= 0 and self.q * sum(self.levels[:10]) * (1 + f) <= cash,
                    "whole initial book cannot be funded")
        self.cash = D
        self.fees = F(0)
        self.peak = D
        self.maximum_drawdown = F(0)
        self.trade_count = 0
        self.total_fills = 0
        self.slots = []
        self.next_oid = 20
        self.next_transition = 1
        self.outside_bars = 0
        self.totals = {k: 0 for k in ("partial", "gap", "touch_only", "outside_range",
                                      "in_outside_range_bar", "outside_ohlc", "defer")}

    @property
    def btc(self):
        return sum((s.held for s in self.slots), F(0))

    @property
    def quote_reserved(self):
        return sum((s.remaining * s.price * (1 + self.rate) for s in self.slots
                    if s.active and s.side == "BUY"), F(0))

    @property
    def base_reserved(self):
        return sum((s.remaining for s in self.slots if s.active and s.side == "SELL"), F(0))

    def invariant(self):
        require(self.cash >= self.quote_reserved >= 0, "negative/unfunded quote")
        require(self.btc >= self.base_reserved >= 0, "negative/unfunded base")
        buys, sells = [], []
        for s in self.slots:
            require(0 <= s.held <= self.q and 0 < s.remaining <= self.q,
                    "slot holdings/remainder range")
            if s.side == "BUY":
                require(s.held + s.remaining == self.q, "BUY slot identity")
                if s.active:
                    buys.append(s.price)
            else:
                require(s.held == s.remaining, "SELL slot identity")
                if s.active:
                    sells.append(s.price)
        require(not buys or not sells or max(buys) < min(sells), "crossing active book")

    def step(self, i):
        b = self.rows[i]
        fills, transitions = [], []
        counters = {k: 0 for k in self.totals}
        budget = {"BUY": b.buy_capacity, "SELL": b.sell_capacity}
        capacity = budget.copy()

        def trace(s, transition, reason="", phase="OPEN"):
            transitions.append(self.trace_record(s, i, transition, reason, phase))

        if i == 1:
            notional = 10 * self.q * b.o
            self.fees = notional * self.rate
            self.cash = D - notional - self.fees
            self.trade_count = 1
            self.slots = [Slot(j, j, "BUY" if j < 10 else "SELL",
                               self.levels[j if j < 10 else j + 1], self.q,
                               F(0) if j < 10 else self.q) for j in range(20)]
            for s in self.slots:
                trace(s, "QUEUED", phase="INITIALIZE")
        elif i >= 2:
            due = sorted((s for s in self.slots if not s.active and s.due <= i),
                         key=lambda s: (s.due, s.oid, s.index))
            for s in due:
                reason = ""
                if (s.side == "BUY" and s.price >= b.o) or (s.side == "SELL" and s.price <= b.o):
                    reason = "new_order_marketable"
                opposite = [x.price for x in self.slots if x.active and x.side != s.side]
                if not reason and opposite and ((s.side == "BUY" and s.price >= min(opposite)) or
                                 (s.side == "SELL" and s.price <= max(opposite))):
                    reason = "selfcross_or_equal_opposite"
                if not reason and s.side == "BUY" and s.remaining * s.price * (1 + self.rate) > self.cash - self.quote_reserved:
                    reason = "insufficient_unreserved_cash"
                if reason:
                    counters["defer"] += 1
                    if s.defer_reason != reason:
                        trace(s, "DEFER_REASON", reason)
                        s.defer_reason = reason
                else:
                    s.active, s.activated = True, i
                    trace(s, "ACTIVATED", "eligible")
            if due:
                self.invariant()
            seen = set()

            def execute(s, phase, reference):
                if s.oid in seen:
                    return
                qty = floor_q(min(s.remaining, budget[s.side]))
                if not qty:
                    return
                before = {"cash": self.cash, "BTC": self.btc, "quote_reserved": self.quote_reserved,
                          "base_reserved": self.base_reserved, "budget": budget[s.side],
                          "remaining": s.remaining}
                old = (s.index, s.oid, s.side, s.price, s.created, s.due, s.activated)
                seen.add(s.oid)
                gross = qty * s.price
                fee = gross * self.rate
                if s.side == "BUY":
                    self.cash -= gross + fee
                    s.held += qty
                else:
                    require(qty <= s.held, "SELL quantity over inventory")
                    self.cash += gross - fee
                    s.held -= qty
                self.fees += fee
                budget[s.side] -= qty
                s.remaining -= qty
                counters["partial"] += int(s.remaining > 0)
                counters["gap"] += int(phase == "gap")
                counters["touch_only"] += int(b.l == s.price if s.side == "BUY" else b.h == s.price)
                counters["outside_range"] += int(reference < self.levels[0] or reference > self.levels[-1])
                counters["in_outside_range_bar"] += int(b.l < self.levels[0] or b.h > self.levels[-1])
                counters["outside_ohlc"] += int(s.price < b.l or s.price > b.h)
                remaining_after = s.remaining
                phase_name = "OPEN_GAP" if phase == "gap" else (
                    ("O_H", "H_L", "L_C") if self.path == "OHLC" else
                    ("O_L", "L_H", "H_C"))[int(phase[-1]) - 1]
                trace(s, "PARTIAL_FILL" if remaining_after else "FULL_FILL", phase=phase_name)
                if not s.remaining:
                    s.side = "SELL" if s.side == "BUY" else "BUY"
                    s.price = self.levels[s.index + (1 if s.side == "SELL" else 0)]
                    s.remaining = self.q
                    s.oid, self.next_oid = self.next_oid, self.next_oid + 1
                    s.created, s.due, s.active, s.activated = i, i + 1, False, None
                    s.defer_reason = None
                    trace(s, "QUEUED", phase="AFTER_FILL")
                self.trade_count += 1
                self.total_fills += 1
                self.invariant()
                after = {"cash": self.cash, "BTC": self.btc, "quote_reserved": self.quote_reserved,
                         "base_reserved": self.base_reserved, "budget": budget[old[2]],
                         "remaining": remaining_after}
                fills.append({"bar": i, "phase": phase, "reference": reference,
                              "order": old, "quantity": qty, "gross": gross, "fee": fee,
                              "before": before, "after": after})

            for side in ("BUY", "SELL"):
                eligible = [s for s in self.slots if s.active and s.side == side and
                            (b.o <= s.price if side == "BUY" else b.o >= s.price)]
                eligible.sort(key=lambda s: ((-s.price if side == "BUY" else s.price), s.oid, s.index))
                for s in eligible:
                    execute(s, "gap", b.o)
            vertices = (b.o, b.h, b.l, b.c) if self.path == "OHLC" else (b.o, b.l, b.h, b.c)
            for seg, (a, z) in enumerate(zip(vertices, vertices[1:]), 1):
                if a == z:
                    continue
                side = "BUY" if z < a else "SELL"
                eligible = [s for s in self.slots if s.active and s.side == side and
                            min(a, z) <= s.price <= max(a, z)]
                eligible.sort(key=lambda s: ((-s.price if side == "BUY" else s.price), s.oid, s.index))
                for s in eligible:
                    execute(s, f"segment{seg}", s.price)
        if fills or transitions or i <= 1:
            self.invariant()
            self.cached_btc = self.btc
            self.cached_quote_reserved = self.quote_reserved
            self.cached_base_reserved = self.base_reserved
        nav = self.cash + self.cached_btc * b.c
        self.peak = max(self.peak, nav)
        dd = (self.peak - nav) / self.peak
        self.maximum_drawdown = max(self.maximum_drawdown, dd)
        for k in counters:
            self.totals[k] += counters[k]
        account = {"cash": self.cash, "BTC": self.cached_btc, "fees": self.fees,
                   "cash_reserved": self.cached_quote_reserved, "BTC_reserved": self.cached_base_reserved,
                   "NAV": nav, "peak": self.peak, "drawdown": dd,
                   "trade_count": self.trade_count, "grid_fill_count": self.total_fills,
                   "buy_budget": capacity["BUY"], "sell_budget": capacity["SELL"],
                   "buy_used": capacity["BUY"] - budget["BUY"],
                   "sell_used": capacity["SELL"] - budget["SELL"],
                   "bar_fill_count": len(fills), "counts": counters,
                   "outside_range_bar": int(b.l < self.levels[0] or b.h > self.levels[-1])}
        self.outside_bars += account["outside_range_bar"]
        return account, fills, transitions

    def trace_record(self, s, i, transition, reason, phase):
        t = self.rows[i].t if i < len(self.rows) else self.rows[-1].t + 60000
        record = {"transition_id": self.next_transition, "bar_index": i,
            "bar_open_utc": utc(t), "phase": phase, "slot_id": s.index,
            "order_id": s.oid + 1, "transition": transition, "reason": reason,
            "due_bar": s.due, "activation_bar": "" if s.activated is None else s.activated,
            "remaining_quantity": s.remaining, "slot_held_quantity": s.held,
            "reserved_USDT": self.quote_reserved, "reserved_BTC": self.base_reserved}
        self.next_transition += 1
        return record

    def cancel_transitions(self):
        records = []
        for s in sorted(self.slots, key=lambda x: x.oid):
            s.active = False
            records.append(self.trace_record(s, len(self.rows), "CANCEL_AT_END",
                                             "release_only_not_flat", "TERMINAL"))
        require(self.quote_reserved == self.base_reserved == 0, "cancellation releases only reserves")
        return records

    def terminal(self):
        marked = self.cash + self.btc * self.rows[-1].c
        exit_fee = self.btc * self.rows[-1].c * self.rate
        return {"marked": marked, "exit_fee": exit_fee, "liquidation": marked - exit_fee,
                "MDD": self.maximum_drawdown, "cash": self.cash, "BTC": self.btc,
                "fees": self.fees, "fills": self.total_fills, "counts": self.totals.copy()}


def self_controls():
    before = ASSERTIONS
    fixture = [bar(100, t=60000*i) for i in range(2)] + [bar(100, 110, 90, 100, t=120000)]
    outputs = []
    for path in ("OHLC", "OLHC"):
        m = IndependentGrid(fixture, 25, path)
        states = [m.step(i) for i in range(3)]
        require(states[0][0]["cash"] == D and states[0][0]["BTC"] == 0, "observe only")
        require(states[1][0]["cash"] == F("4987.5") and states[1][0]["BTC"] == 50,
                "common actual initialization")
        require(states[1][0]["grid_fill_count"] == 0, "no initialization bar grid fills")
        require(states[2][0]["cash"] == F("5042.5") and states[2][0]["BTC"] == 50,
                "four exact direction-capped fills")
        require(m.fees == F("17.5") and m.total_fills == 4, "full fee/count identity")
        require(m.terminal()["liquidation"] == 10030, "terminal fee once")
        outputs.append(m.terminal())
    require(outputs[0] == outputs[1], "conditional path economic identity")
    require(floor_q(F("0.000000019")) == Q, "subquantum floor")
    try:
        bad = [bar(100), bar(50), bar(50)]
        IndependentGrid(bad, 25, "OHLC")
    except AuditError as e:
        require("whole initial book" in str(e), "known deficit category")
    else:
        require(False, "known initial gap deficit escaped")
    # Adjacent profitable roundtrip remains below untouched CASH after startup/exit.
    f = F(1, 400)
    paired = 5 * ((1 - f) * 92 - (1 + f) * 90)
    hold = D - 5000 * (1 + f) + 5000 * (1 - f)
    require(paired == F("7.725") and hold == 9975 and hold + paired == F("9982.725") < D,
            "positive pair != positive total wealth")
    # Exactly one old partial per bar; incomplete BUY cannot create a SELL yet.
    partial_rows = [bar(100), bar(100), bar(100, 103, 97, 100, v="500", tb="250"),
                    bar(100, 103, 97, 100, v="500", tb="250"), bar(100)]
    partial_models = []
    for path in ("OHLC", "OLHC"):
        m = IndependentGrid(partial_rows, 25, path)
        steps = [m.step(i) for i in range(3)]
        a, fs, _ = steps[-1]
        require(len(fs) == 2 and all(x["quantity"] == F("2.5") for x in fs),
                "independent directional partial quantities")
        require(a["cash"] == F("4996.25") and a["BTC"] == 50 and a["fees"] == F("13.75"),
                "partial exact cash/fees")
        require(m.slots[9].held == F("2.5") and m.slots[9].side == "BUY" and
                m.slots[9].remaining == F("2.5"), "incomplete BUY waits")
        a3, fs3, _ = m.step(3)
        require(len(fs3) == 2 and all(x["order"][4] == 1 for x in fs3),
                "only old partial remainders complete")
        a4, fs4, _ = m.step(4)
        require(not fs4 and a4["counts"]["defer"] == 2,
                "equal-price marketable replacements remain deferred")
        partial_models.append(m.terminal())
    require(partial_models[0] == partial_models[1], "partial path identity")
    gap_rows = [bar(100), bar(100), bar(100), bar(90, 92, 88, 90, v="500", tb="250")]
    gap_models = []
    for path in ("OHLC", "OLHC"):
        m = IndependentGrid(gap_rows, 25, path)
        out = [m.step(i) for i in range(4)][-1]
        require(len(out[1]) == 1 and out[1][0]["order"][3] == 98 and
                out[1][0]["quantity"] == F("2.5"), "old gap fixed-limit price priority")
        require(out[0]["counts"]["gap"] == 1 and out[0]["counts"]["outside_ohlc"] == 1,
                "unqualified gap price is classified")
        gap_models.append(m.terminal())
    require(gap_models[0] == gap_models[1], "gap-first path identity")
    # A deliberately omitted marketability fence would make cross-side ID matter.
    def activation_instrument(order, marketability):
        active = []
        for side in order:
            if marketability:  # both candidate prices equal OPEN=100
                continue
            if active and active[0] != side:  # same-price self-cross
                continue
            active.append(side)
        return active
    require(activation_instrument(["BUY", "SELL"], True) ==
            activation_instrument(["SELL", "BUY"], True) == [], "proper fence instrument")
    require(activation_instrument(["BUY", "SELL"], False) !=
            activation_instrument(["SELL", "BUY"], False), "bad fence instrument detects divergence")
    return {"assertions": ASSERTIONS - before, "known_answer_groups": 9,
            "producer_helpers_called": 0, "actual_market_return_runs": 0}


PRODUCER_SHA = "47d0f95ac48a39dfb5814130cdd89eebf86d4f0aa8c5c4d9ef7642d2696b4c49"
PRODUCER_TEST_SHA = "b04218cba7b1be65cef3c68aa4fe6ff02ff51a6645f579ef6f34006bd6f91de3"
PRODUCER_REPORT_SHA = "bd52944e873ef2b08f0178024279425b5cad98b79d8b6a568169f5cb7750d790"
MINUTE_KEYS = ("bar_index", "bar_open_utc", "bar_close_exclusive_utc", "close",
    "cash_USDT", "BTC", "fees_USDT", "cash_reserved_USDT", "BTC_reserved", "NAV_USDT",
    "peak_USDT", "drawdown_fraction", "drawdown_numerator", "drawdown_denominator",
    "modeled_trade_count", "grid_fill_count", "passive_buy_budget_BTC", "passive_sell_budget_BTC",
    "passive_buy_used_BTC", "passive_sell_used_BTC", "bar_grid_fill_count", "bar_partial_fill_count",
    "bar_open_gap_fill_count", "bar_touch_only_fill_count", "outside_range_bar",
    "bar_outside_range_fill_count", "bar_fills_in_outside_range_count",
    "bar_outside_bar_ohlc_fill_count", "bar_deferred_activation_count")
FILL_KEYS = ("event_id", "bar_index", "bar_open_utc", "phase", "phase_sequence", "fill_kind",
    "slot_id", "order_id", "order_created_bar", "order_due_bar", "order_activated_bar", "side",
    "limit_price", "model_execution_price", "outside_bar_ohlc", "quantity", "notional_USDT",
    "fee_USDT", "cash_before_USDT", "cash_after_USDT", "BTC_before", "BTC_after",
    "cash_reserved_before_USDT", "cash_reserved_after_USDT", "BTC_reserved_before",
    "BTC_reserved_after", "direction_budget_before_BTC", "direction_budget_after_BTC",
    "order_remaining_before", "order_remaining_after", "phase_reference_price", "touch_only",
    "fill_in_outside_range_bar", "outside_range_fill")
TRACE_KEYS = ("transition_id", "bar_index", "bar_open_utc", "phase", "slot_id", "order_id",
    "transition", "reason", "due_bar", "activation_bar", "remaining_quantity",
    "slot_held_quantity", "reserved_USDT", "reserved_BTC")


def compare_record(actual, expected, label):
    require(set(actual) == set(expected), label + " schema")
    for key, value in expected.items():
        received = actual[key]
        if key in ("drawdown_fraction", "close_MDD_fraction"):
            require(abs(F(received) - value) <= F(1, 10**20), label + " rounded ratio " + key)
        elif isinstance(value, (F, int)) and not isinstance(value, bool):
            require(F(str(received)) == value, label + " exact numeric " + key)
        else:
            require(received == value, label + " exact value " + key)


def expected_minute(i, b, state, benchmark=False):
    c = state["counts"]
    dd = state["drawdown"]
    return dict(zip(MINUTE_KEYS, [i, utc(b.t), utc(b.t + 60000), b.c,
        state["cash"], state["BTC"], state["fees"], state["cash_reserved"], state["BTC_reserved"],
        state["NAV"], state["peak"], dd, dd.numerator, dd.denominator,
        state["trade_count"], state["grid_fill_count"],
        "" if benchmark else state["buy_budget"], "" if benchmark else state["sell_budget"],
        "" if benchmark else state["buy_used"], "" if benchmark else state["sell_used"],
        state["bar_fill_count"], c["partial"], c["gap"], c["touch_only"], state["outside_range_bar"],
        c["outside_range"], c["in_outside_range_bar"], c["outside_ohlc"], c["defer"]]))


def expected_initial(b, q, rate):
    qty = 10*q
    gross, fee = qty*b.o, qty*b.o*rate
    row = {k: "" for k in FILL_KEYS}
    row.update(event_id=1, bar_index=1, bar_open_utc=utc(b.t), phase="INITIALIZE", phase_sequence=0,
        fill_kind="HYPOTHETICAL_INITIALIZATION_BUY", order_id="INIT", order_created_bar=1,
        order_due_bar=1, order_activated_bar=1, side="BUY", model_execution_price=b.o,
        outside_bar_ohlc=0, quantity=qty, notional_USDT=gross, fee_USDT=fee,
        cash_before_USDT=D, cash_after_USDT=D-gross-fee, BTC_before=F(0), BTC_after=qty,
        cash_reserved_before_USDT=F(0), cash_reserved_after_USDT=F(0), BTC_reserved_before=F(0),
        BTC_reserved_after=F(0), phase_reference_price=b.o, touch_only=0,
        fill_in_outside_range_bar=0, outside_range_fill=0)
    return row


def expected_fill(event, model, b, number):
    slot, oid, side, price, created, due, activated = event["order"]
    phase = event["phase"]
    sequence = 0 if phase == "gap" else int(phase[-1])
    phase_name = "OPEN_GAP" if not sequence else (
        ("O_H", "H_L", "L_C") if model.path == "OHLC" else
        ("O_L", "L_H", "H_C"))[sequence-1]
    a, z = event["before"], event["after"]
    return dict(zip(FILL_KEYS, [number, event["bar"], utc(b.t), phase_name, sequence,
        "CONDITIONAL_GRID", slot, oid+1, created, due, activated, side, price, price,
        int(not b.l <= price <= b.h), event["quantity"], event["gross"], event["fee"],
        a["cash"], z["cash"], a["BTC"], z["BTC"], a["quote_reserved"], z["quote_reserved"],
        a["base_reserved"], z["base_reserved"], a["budget"], z["budget"],
        a["remaining"], z["remaining"], event["reference"],
        int(b.l == price if side == "BUY" else b.h == price),
        int(b.l < model.levels[0] or b.h > model.levels[-1]),
        int(event["reference"] < model.levels[0] or event["reference"] > model.levels[-1])]))


def benchmark_steps(rows, bps, cash_only):
    q = floor_q(F(5000) / (10*rows[1].o))
    rate = F(bps, 10000)
    lower = floor_q(rows[0].c*F(4, 5))
    upper = floor_q(rows[0].c*F(6, 5))
    cash, btc, fees, peak, mdd = D, F(0), F(0), D, F(0)
    for i, b in enumerate(rows):
        if i == 1 and not cash_only:
            btc = 10*q
            fees = btc*b.o*rate
            cash = D - btc*b.o-fees
        nav = cash+btc*b.c
        peak = max(peak, nav)
        dd = (peak-nav)/peak
        mdd = max(mdd, dd)
        state = {"cash": cash, "BTC": btc, "fees": fees, "cash_reserved": F(0),
            "BTC_reserved": F(0), "NAV": nav, "peak": peak, "drawdown": dd,
            "trade_count": int(i >= 1 and not cash_only), "grid_fill_count": 0,
            "bar_fill_count": 0, "counts": {k: 0 for k in ("partial", "gap", "touch_only",
                "outside_range", "in_outside_range_bar", "outside_ohlc", "defer")},
            "outside_range_bar": int(b.l < lower or b.h > upper)}
        yield state, mdd


def reader(path, keys):
    handle = Path(path).open(newline="")
    rows = csv.DictReader(handle)
    require(tuple(rows.fieldnames) == keys, "CSV fixed column order")
    return handle, rows


def next_record(rows, expected, label):
    actual = next(rows, None)
    require(actual is not None, label+" absent row")
    compare_record(actual, expected, label)
    return actual


def exhausted(rows, label):
    require(next(rows, None) is None, label+" extra rows")


def instrument_controls():
    start = ASSERTIONS
    proper = {"cash_USDT": "100", "fees_USDT": "0.25", "bar_index": "2", "phase": "H_L"}
    expected = {"cash_USDT": F(100), "fees_USDT": F(1, 4), "bar_index": 2, "phase": "H_L"}
    compare_record(proper, expected, "known compare control")
    for field, value in (("fees_USDT", "0"), ("bar_index", "1"), ("cash_USDT", "100.00000001"),
                         ("phase", "L_H")):
        hostile = dict(proper, **{field: value})
        try:
            compare_record(hostile, expected, "bad instrument "+field)
        except AuditError:
            require(True, "instrument caught "+field)
        else:
            require(False, "instrument false acceptance "+field)
    return {"groups": 5, "assertions": ASSERTIONS-start,
        "in_memory_wrong_fee_clock_quantum_phase_cases_rejected": 4,
        "producer_files_mutated": 0}


def summary_check(actual, rows, q, rate, state, terminal, model, path_id, path=None):
    amount = F(0) if model == "cash" else 10*q*rows[1].o
    qty = F(0) if model == "cash" else 10*q
    marked = state["cash"]+state["BTC"]*rows[-1].c
    exit_fee = state["BTC"]*rows[-1].c*rate
    expected = {"model": model, "path_id": path_id, "minute_rows": len(rows),
        "fee_bps_per_side": None if model == "cash" else int(rate*10000), "ohlc_path": path,
        "initial_NAV_USDT": D, "anchor_close_USDT": rows[0].c,
        "initialization_open_USDT": rows[1].o, "maximum_slot_quantity": q,
        "initialization_notional_USDT": amount, "initialization_quantity_BTC": qty,
        "initialization_fee_USDT": amount*rate,
        "cash_USDT": state["cash"], "BTC": state["BTC"], "ordinary_fees_USDT": state["fees"],
        "marked_terminal_NAV_USDT": marked, "hypothetical_exit_fee_USDT": exit_fee,
        "fee_inclusive_terminal_NAV_USDT": marked-exit_fee,
        "marked_month_return": marked/D-1, "fee_inclusive_month_return": (marked-exit_fee)/D-1,
        "close_MDD_fraction": terminal["MDD"], "close_MDD_numerator": terminal["MDD"].numerator,
        "close_MDD_denominator": terminal["MDD"].denominator,
        "modeled_trade_count": state["trade_count"], "grid_fill_count": state["grid_fill_count"],
        "cash_reserved_after_cancel_USDT": F(0), "BTC_reserved_after_cancel": F(0),
        "hypothetical_exit_is_actual_fill": False, "actual_flat_confirmed": False,
        "initialization_and_exit_volume_certified": False, "native_bot_replication": False,
        "actual_orders": 0, "new_network_requests": 0, "paid_calls": 0,
        "annualized_return": None, "clean_OOS": False}
    compare_record({key: actual[key] for key in expected}, expected, path_id+" summary")
    levels = [floor_q(rows[0].c*(F(4,5)+F(i,50))) for i in range(21)]
    require([F(v) for v in actual["grid_levels_USDT"]] == levels, path_id+" all levels")
    if model == "grid":
        counters = terminal["counts"]
        totals = {"partials": counters["partial"], "gap_fills": counters["gap"],
            "touch_only_fills": counters["touch_only"], "outside_range_bars": terminal["outside_bars"],
            "outside_range_fills": counters["outside_range"],
            "fills_in_outside_range_bars": counters["in_outside_range_bar"],
            "outside_bar_ohlc_fills": counters["outside_ohlc"],
            "deferred_activation_attempts": counters["defer"], "transition_count": terminal["transitions"]}
        compare_record({key: actual[key] for key in totals}, totals, path_id+" totals")
    return {"path_id": path_id, "model": model, "fee_bps_per_side": expected["fee_bps_per_side"],
        "ohlc_path": path, "marked_terminal_NAV_USDT": finite_text(marked),
        "fee_inclusive_terminal_NAV_USDT": finite_text(marked-exit_fee),
        "cumulative_paid_fees_USDT": finite_text(state["fees"]),
        "initialization_fee_subset_USDT": finite_text(amount*rate),
        "conditional_grid_fees_excluding_initialization_USDT": finite_text(state["fees"]-amount*rate),
        "modeled_exit_fee_USDT": finite_text(exit_fee), "terminal_BTC": finite_text(state["BTC"]),
        "close_MDD_exact": [terminal["MDD"].numerator, terminal["MDD"].denominator],
        "grid_fill_rows": state["grid_fill_count"], "minute_rows_exactly_compared": len(rows)}


def audit_one(root, rows, path_id, bps, sensitivity, summary):
    folder = root/path_id
    handles = []
    try:
        mh, minutes = reader(folder/"minute-account.csv", MINUTE_KEYS); handles.append(mh)
        fh, fills = reader(folder/"modeled-fills.csv", FILL_KEYS); handles.append(fh)
        th, traces = reader(folder/"order-transition-trace.csv", TRACE_KEYS); handles.append(th)
        fee_sum = F(0)
        fill_count, trace_count = 0, 0
        if sensitivity:
            m = IndependentGrid(rows, bps, sensitivity)
            for i, b in enumerate(rows):
                state, events, transitions = m.step(i)
                next_record(minutes, expected_minute(i, b, state), f"{path_id} minute {i}")
                if i == 1:
                    r = next_record(fills, expected_initial(b, m.q, m.rate), path_id+" initialization")
                    fee_sum += F(r["fee_USDT"]); fill_count += 1
                for event in events:
                    fill_count += 1
                    r = next_record(fills, expected_fill(event, m, b, fill_count), path_id+" fill")
                    fee_sum += F(r["fee_USDT"])
                for event in transitions:
                    next_record(traces, event, path_id+" transition"); trace_count += 1
            terminal = m.terminal()
            for event in m.cancel_transitions():
                next_record(traces, event, path_id+" cancel"); trace_count += 1
            terminal.update(outside_bars=m.outside_bars, transitions=trace_count)
            result = summary_check(summary, rows, m.q, m.rate, state, terminal, "grid", path_id, sensitivity)
        else:
            cash_only = path_id == "cash"
            q = floor_q(F(5000)/(10*rows[1].o)); rate = F(bps, 10000)
            for i, (state, mdd) in enumerate(benchmark_steps(rows, bps, cash_only)):
                next_record(minutes, expected_minute(i, rows[i], state, True), f"{path_id} minute {i}")
                if i == 1 and not cash_only:
                    r = next_record(fills, expected_initial(rows[i], q, rate), path_id+" initialization")
                    fee_sum += F(r["fee_USDT"]); fill_count += 1
            result = summary_check(summary, rows, q, rate, state, {"MDD": mdd},
                                   "cash" if cash_only else "hold", path_id)
        exhausted(minutes, path_id+" minutes"); exhausted(fills, path_id+" fills")
        exhausted(traces, path_id+" transitions")
        require(fee_sum == state["fees"] == F(summary["ordinary_fees_USDT"]),
                path_id+" all fill fees == minute cumulative == legacy summary")
        require(fill_count == state["trade_count"], path_id+" all modeled trade rows")
        local_summary = json.loads((folder/"summary.json").read_text())
        require(local_summary == summary, path_id+" report and local summary identity")
        result.update(all_fill_rows=fill_count, lifecycle_rows_exactly_compared=trace_count,
                      fee_sum_identity_verified=True)
        print(json.dumps({"completed_path": path_id, "minute_rows": len(rows),
                          "fill_rows": fill_count, "transition_rows": trace_count}), flush=True)
        return result
    finally:
        for handle in handles:
            handle.close()


def actual_audit(args):
    start = ASSERTIONS
    work = Path(__file__).resolve().parent
    bindings = {Path(__file__): sha(__file__),
        work/"spot_grid_economic_diagnostic_20261008_v0.py": PRODUCER_SHA,
        work/"test_spot_grid_economic_diagnostic_20261008_v0.py": PRODUCER_TEST_SHA,
        Path(args.protocol): PROTOCOL_SHA, Path(args.run_root)/"report.json": PRODUCER_REPORT_SHA,
        Path(args.zip): ZIP_SHA, Path(args.checksum): CHECKSUM_SHA}
    for path, expected in bindings.items():
        require(sha(path) == expected, "frozen input SHA before")
    report = json.loads((Path(args.run_root)/"report.json").read_text())
    root = Path(args.run_root)
    require(report["producer_sha256"] == PRODUCER_SHA and report["controls_source_sha256"] == PRODUCER_TEST_SHA
            and report["protocol_sha256"] == PROTOCOL_SHA, "report source/protocol bindings")
    require(sha(root/"protocol.json") == PROTOCOL_SHA, "run protocol copy binding")
    require(report["unique_account_paths"] == 13 and report["total_minute_rows"] == 580320,
            "13 paths denominator")
    artifacts = report["artifacts"]
    require(len(artifacts) == 53 and len({a["relative_path"] for a in artifacts}) == 53,
            "53 unique artifacts")
    actual_files = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p.name != "report.json"}
    require(actual_files == {a["relative_path"] for a in artifacts}, "exact artifact roster")
    for a in artifacts:
        path = root/a["relative_path"]
        require(path.stat().st_size == a["bytes"] and sha(path) == a["sha256"], "actual artifact hash and bytes")
    controls = self_controls()
    instrument = instrument_controls()
    rows = raw_bars(args.zip, args.checksum)
    indexed = {s["path_id"]: s for s in report["all_path_summaries"]}
    expected_ids = {"cash"} | {f"hold-fee{bps:02d}bps" for bps in (0,10,25,100)} | {
        f"grid-fee{bps:02d}bps-{path}" for bps in (0,10,25,100) for path in ("OHLC","OLHC")}
    require(set(indexed) == expected_ids and len(report["all_path_summaries"]) == 13, "fixed 13 scenario identities")
    results = []
    results.append(audit_one(root, rows, "cash", 0, None, indexed["cash"]))
    for bps in (0,10,25,100):
        hid = f"hold-fee{bps:02d}bps"
        results.append(audit_one(root, rows, hid, bps, None, indexed[hid]))
        for path in ("OHLC","OLHC"):
            pid = f"grid-fee{bps:02d}bps-{path}"
            results.append(audit_one(root, rows, pid, bps, path, indexed[pid]))
    indexed_results = {s["path_id"]: s for s in results}
    pairings = []
    for paired in report["paired_comparisons"]:
        gid, hid = paired["grid_path_id"], paired["matched_hold_path_id"]
        grid, hold = indexed_results[gid], indexed_results[hid]
        delta = F(grid["fee_inclusive_terminal_NAV_USDT"])-F(hold["fee_inclusive_terminal_NAV_USDT"])
        require(F(paired["fee_inclusive_grid_minus_hold_USDT"]) == delta, "paired same-cost holding delta")
        require(F(paired["marked_grid_minus_hold_USDT"]) ==
            F(grid["marked_terminal_NAV_USDT"])-F(hold["marked_terminal_NAV_USDT"]), "paired marked delta")
        cash_delta = F(grid["fee_inclusive_terminal_NAV_USDT"])-D
        require(F(paired["fee_inclusive_grid_minus_cash_USDT"]) == cash_delta, "same capital cash delta")
        pairings.append({"grid_path_id": gid, "holding_path_id": hid,
            "fee_inclusive_grid_minus_hold_USDT": finite_text(delta),
            "fee_inclusive_grid_minus_cash_USDT": finite_text(cash_delta)})
    require(len(pairings) == 8, "eight paired sensitivity comparisons")
    for bps in (0,10,25,100):
        a, b = [indexed_results[f"grid-fee{bps:02d}bps-{p}"] for p in ("OHLC","OLHC")]
        for key in ("marked_terminal_NAV_USDT", "fee_inclusive_terminal_NAV_USDT", "cumulative_paid_fees_USDT",
                    "terminal_BTC", "close_MDD_exact", "grid_fill_rows"):
            require(a[key] == b[key], "structural path equality "+key)
    for path, expected in bindings.items():
        require(sha(path) == expected, "frozen input SHA after")
    for a in artifacts:
        require(sha(root/a["relative_path"]) == a["sha256"], "output artifact SHA after")
    result = {"schema": "conditional-spot-grid-independent-economic-audit/1",
        "status": "accepted_only_for_fixed_conditional_single_seen_month_accounting_model",
        "independent_source_sha256": bindings[Path(__file__)], "producer_source_sha256": PRODUCER_SHA,
        "producer_tests_sha256": PRODUCER_TEST_SHA, "producer_report_sha256": PRODUCER_REPORT_SHA,
        "protocol_sha256": PROTOCOL_SHA, "raw_zip_sha256": ZIP_SHA, "raw_csv_sha256": CSV_SHA,
        "raw_checksum_sha256": CHECKSUM_SHA, "own_known_answer_controls": controls,
        "comparison_instrument_controls": instrument, "actual_assertions": ASSERTIONS-start,
        "raw_rows": len(rows), "unique_paths": len(results),
        "minute_rows_exactly_compared": sum(s["minute_rows_exactly_compared"] for s in results),
        "all_fill_rows_exactly_compared": sum(s["all_fill_rows"] for s in results),
        "lifecycle_rows_exactly_compared": sum(s["lifecycle_rows_exactly_compared"] for s in results),
        "artifact_hashes_and_sizes_verified": 53, "frozen_source_and_inputs_unchanged": True,
        "results": results, "paired_comparisons": pairings,
        "fee_field_interpretation": {
            "legacy_ordinary_fees_USDT": "cumulative initialization plus conditional grid fees in BOTH grid and HOLD",
            "initialization_fee_USDT": "subset of cumulative paid fees; do not add it a second time",
            "all_modeled_costs_USDT": "legacy ordinary_fees_USDT plus hypothetical_exit_fee_USDT",
            "algorithmic_summary_fee_bug_found": False},
        "path_invariance": {
            "observation": "four fee cases share exact economic endpoints, fees, close MDD and fill counts between OHLC and OLHC",
            "mechanism": "fixed fully funded old book; gap first; one visit per old order per bar; independent side capacities; price priority within each side; replacement next bar only; nonmarketable new orders cannot self-cross across sides",
            "proof_scope": "wallet operations across sides commute; same-side order priority is preserved; cross-side ID order has no economic effect under the stated activation fences; induction preserves inventory, side fill quantities, reserves and cash",
            "known_bad_instrument": "dropping new-order marketability permits same-price opposite candidates to activate in ID order and breaks invariance",
            "distinct_previously_seen_market_periods": 1, "independent_clean_OOS_market_trials": 0,
            "OHLC_paths_are_true_bounds": False,
            "real_fill_robustness_established": False},
        "limits": ["Previously inspected January 2020 BTCUSDT only; one market period and one fixed configuration, not a clean OOS trial.",
            "All orders are conditional modeling events; real order book, queue, latency and spread not identified by minute OHLC.",
            "One-percent aggregate directional volume is a scenario budget, not a proof of volume available at an individual limit.",
            "Touch fills, fixed-limit gap fills and both OHLC paths are modeling proxies, not actual fills or rigorous bounds.",
            "Quote-USDT fee, 1e-8 quantity/price grids and explicit scenario rates do not prove historical account/native-bot rules.",
            "Initialization at row1 open and terminal liquidation at last close are hypothetical with uncertified execution volume.",
            "Total NAV and same-cost holding comparison support this model's diagnostic; they do not establish strategy profitability or execution eligibility."],
        "producer_helpers_imported_or_run": 0, "producer_tests_run": 0, "producer_market_run_relaunched": 0,
        "actual_orders": 0, "network_requests": 0, "credentials_reads": 0, "paid_calls": 0, "github_writes": 0,
        "profitability_or_execution_admitted": False, "native_bot_replication": False,
        "annualized_return": None, "clean_OOS": False, "actual_flat_confirmed": False}
    output = Path(args.output_json)
    with output.open("x") as h:
        json.dump(result, h, ensure_ascii=False, indent=2, sort_keys=True); h.write("\n")
    text = "Independent exact-rational audit accepted the fixed conditional accounting model only.\n"
    text += f"13 paths / 580320 minute rows / {result['all_fill_rows_exactly_compared']} fills / {result['lifecycle_rows_exactly_compared']} lifecycle rows.\n"
    text += "Legacy ordinary_fees includes initialization; add only modeled exit fee for all-costs.\n"
    text += "OHLC pair equality is structurally explained, not independent market samples or actual-fill robustness.\n"
    text += "No profitability, historical native-rule, clean-OOS or execution admission.\n"
    with Path(args.output_txt).open("x") as h:
        h.write(text)
    print(json.dumps({"complete": True, "report_sha256": sha(output),
        "assertions": result["actual_assertions"], "source_sha256": result["independent_source_sha256"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-controls", action="store_true")
    parser.add_argument("--run-root")
    parser.add_argument("--protocol")
    parser.add_argument("--zip")
    parser.add_argument("--checksum")
    parser.add_argument("--output-json")
    parser.add_argument("--output-txt")
    args = parser.parse_args()
    if args.self_controls:
        print(json.dumps({"model": self_controls(), "instrument": instrument_controls()}, sort_keys=True))
    else:
        if any(getattr(args, key) is None for key in ("run_root", "protocol", "zip", "checksum", "output_json", "output_txt")):
            parser.error("Actual audit requires explicit read-only input bindings and exclusive output paths.")
        actual_audit(args)
