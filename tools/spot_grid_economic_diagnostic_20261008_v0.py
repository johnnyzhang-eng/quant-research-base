"""Frozen, offline conditional BTC grid economics. No native bot or fill proof.

Exact monetary arithmetic, minute-volume participation, and complete wallets are
the experiment. Two OHLC paths are scenarios, not bounds on real executions.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from fractions import Fraction as F
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import zipfile

PRIVATE = Path('.grid-local-root-not-configured')
PROTOCOL = PRIVATE / 'protocols/spot-grid-economic-diagnostic-20261008-v0/protocol-v0.json'
PROTOCOL_SHA = '256d2f05f8334160b616ca0771e6bc42d037f2e04187e089baf8a083c0d372eb'
OUTPUT = PRIVATE / 'reports/spot-grid-economic-diagnostic-20261008-v0/run-v0'
FEES = (0, 10, 25, 100)
PATHS = ('OHLC', 'OLHC')
Q = F(1, 100000000)
P = F(1, 100000000)
CAPITAL = F(10000)
PARTICIPATION = F(1, 100)


class Guard(ValueError):
    pass


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decimal(value):
    """Exact finite decimal output; money here has a terminating denominator."""
    value = F(value)
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise Guard('nonterminating monetary output')
    places = max(twos, fives)
    scale = 10 ** places
    integer = value.numerator * (scale // value.denominator)
    sign = '-' if integer < 0 else ''
    integer = abs(integer)
    if not places:
        return sign + str(integer)
    body = str(integer).zfill(places + 1)
    return (sign + body[:-places] + '.' + body[-places:]).rstrip('0').rstrip('.')


def ratio_text(value):
    with localcontext() as context:
        context.prec = 80
        value = F(value)
        return format(Decimal(value.numerator) / Decimal(value.denominator), '.20f')


def floor_ticks(value, quantum=Q):
    value = F(value)
    if value < 0:
        raise Guard('negative quantity or price')
    return int(value // quantum)


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def parse_number(text):
    try:
        d = Decimal(text)
    except Exception as exc:
        raise Guard('invalid decimal field') from exc
    if not d.is_finite():
        raise Guard('nonfinite decimal field')
    return F(d)


@dataclass(frozen=True)
class Bar:
    index: int
    open_ms: int
    open: F
    high: F
    low: F
    close: F
    base: F
    quote: F
    trades: int
    taker_buy_base: F
    taker_buy_quote: F

    def budgets(self):
        return (floor_ticks(PARTICIPATION * (self.base - self.taker_buy_base)),
                floor_ticks(PARTICIPATION * self.taker_buy_base))


def parse_csv(raw, *, rows_expected, start_ms):
    result = []
    for index, row in enumerate(csv.reader(io.StringIO(raw.decode('utf-8')))):
        if len(row) != 12:
            raise Guard('raw CSV column count')
        try:
            opened, closed, trades = int(row[0]), int(row[6]), int(row[8])
        except ValueError as exc:
            raise Guard('raw CSV integer field') from exc
        if opened != start_ms + index * 60000 or closed != opened + 59999:
            raise Guard('raw CSV clock gap duplicate or shift')
        values = [parse_number(row[i]) for i in (1, 2, 3, 4, 5, 7, 9, 10)]
        o, h, l, c, base, quote, tb, tq = values
        if not (0 < l <= o <= h and l <= c <= h):
            raise Guard('raw OHLC shape')
        if not (0 <= tb <= base and 0 <= tq <= quote and trades >= 0):
            raise Guard('raw directional volume or trade count')
        result.append(Bar(index, opened, o, h, l, c, base, quote, trades, tb, tq))
    if len(result) != rows_expected:
        raise Guard('raw CSV row count')
    return result


def read_protocol(admitted_sha):
    if admitted_sha != PROTOCOL_SHA:
        raise Guard('exact protocol admission missing')
    raw = PROTOCOL.read_bytes()
    if sha(raw) != PROTOCOL_SHA:
        raise Guard('protocol bytes changed')
    p = json.loads(raw)
    if p['scenario_matrix']['fee_bps_per_side'] != list(FEES):
        raise Guard('fee arms changed')
    if p['scenario_matrix']['grid_scenarios'] != 8:
        raise Guard('matrix changed')
    return p, raw


def load_bars(protocol):
    b = protocol['bindings']
    evidence = [
        ('collection_manifest_path', 'collection_manifest_sha256'),
        ('raw_zip_path', 'zip_sha256'), ('checksum_path', 'checksum_sha256')]
    retained = {}
    for path_name, hash_name in evidence:
        raw = Path(b[path_name]).read_bytes()
        if sha(raw) != b[hash_name]:
            raise Guard('input evidence hash mismatch')
        retained[path_name] = raw
    checksum = retained['checksum_path'].decode('ascii').split()
    if len(checksum) != 2 or checksum[0] != b['zip_sha256']:
        raise Guard('provider checksum mismatch')
    if checksum[1].lstrip('*') != 'BTCUSDT-1m-2020-01.zip':
        raise Guard('provider checksum filename mismatch')
    with zipfile.ZipFile(io.BytesIO(retained['raw_zip_path'])) as archive:
        if archive.namelist() != [b['zip_member']] or archive.testzip() is not None:
            raise Guard('ZIP member or CRC mismatch')
        raw = archive.read(b['zip_member'])
    if sha(raw) != b['csv_sha256']:
        raise Guard('raw CSV hash mismatch')
    source_dir = Path(b['collection_manifest_path']).parent
    if sha((source_dir / 'source-contract.json').read_bytes()) != b['source_contract_sha256']:
        raise Guard('retained source contract changed')
    terms = source_dir / 'objects' / b['terms_object_sha256']
    if sha(terms.read_bytes()) != b['terms_object_sha256']:
        raise Guard('retained terms changed')
    bars = parse_csv(raw, rows_expected=44640, start_ms=1577836800000)
    feasibility = Path(__file__).resolve().parent.parent / b['feasibility_report_relative_path']
    if sha(feasibility.read_bytes()) != b['feasibility_report_sha256']:
        raise Guard('feasibility evidence changed')
    return bars


def geometry(bars):
    if len(bars) < 3 or [x.index for x in bars] != list(range(len(bars))):
        raise Guard('need consecutive observation initialization and grid bars')
    anchor, initialization = bars[0].close, bars[1].open
    levels = [floor_ticks(anchor * (F(4, 5) + F(i, 50)), P) * P for i in range(21)]
    if not all(a < b for a, b in zip(levels, levels[1:])):
        raise Guard('grid levels not strictly increasing')
    qticks = floor_ticks(F(5000) / (10 * initialization))
    if qticks <= 0:
        raise Guard('initial quantity is dust')
    return levels, qticks


def preflight(bars):
    levels, qticks = geometry(bars)
    quantity = 10 * qticks * Q
    notional = quantity * bars[1].open
    checks = []
    for bps in FEES:
        rate = F(bps, 10000)
        cash = CAPITAL - notional * (1 + rate)
        reserve = qticks * Q * sum(levels[:10], F(0)) * (1 + rate)
        if cash < 0 or reserve > cash:
            raise Guard('entire initial book cannot be funded at every fee arm')
        checks.append({'fee_bps': bps, 'nonnegative_cash': True, 'entire_book_funded': True})
    return {'rows': len(bars), 'fee_arm_checks': checks, 'month_returns_computed': False}


MINUTE_FIELDS = [
    'bar_index', 'bar_open_utc', 'bar_close_exclusive_utc', 'close', 'cash_USDT', 'BTC',
    'fees_USDT', 'cash_reserved_USDT', 'BTC_reserved', 'NAV_USDT', 'peak_USDT',
    'drawdown_fraction', 'drawdown_numerator', 'drawdown_denominator',
    'modeled_trade_count', 'grid_fill_count', 'passive_buy_budget_BTC',
    'passive_sell_budget_BTC', 'passive_buy_used_BTC', 'passive_sell_used_BTC',
    'bar_grid_fill_count', 'bar_partial_fill_count', 'bar_open_gap_fill_count',
    'bar_touch_only_fill_count', 'outside_range_bar', 'bar_outside_range_fill_count',
    'bar_fills_in_outside_range_count', 'bar_outside_bar_ohlc_fill_count',
    'bar_deferred_activation_count']
FILL_FIELDS = [
    'event_id', 'bar_index', 'bar_open_utc', 'phase', 'phase_sequence', 'fill_kind',
    'slot_id', 'order_id', 'order_created_bar', 'order_due_bar', 'order_activated_bar',
    'side', 'limit_price', 'model_execution_price', 'outside_bar_ohlc', 'quantity',
    'notional_USDT', 'fee_USDT', 'cash_before_USDT', 'cash_after_USDT', 'BTC_before',
    'BTC_after', 'cash_reserved_before_USDT', 'cash_reserved_after_USDT',
    'BTC_reserved_before', 'BTC_reserved_after', 'direction_budget_before_BTC',
    'direction_budget_after_BTC', 'order_remaining_before', 'order_remaining_after',
    'phase_reference_price', 'touch_only', 'fill_in_outside_range_bar', 'outside_range_fill']
TRANSITION_FIELDS = [
    'transition_id', 'bar_index', 'bar_open_utc', 'phase', 'slot_id', 'order_id',
    'transition', 'reason', 'due_bar', 'activation_bar', 'remaining_quantity',
    'slot_held_quantity', 'reserved_USDT', 'reserved_BTC']


class CaptureSink:
    def __init__(self):
        self.minutes, self.fills, self.transitions = [], [], []

    def minute(self, row): self.minutes.append(dict(row))
    def fill(self, row): self.fills.append(dict(row))
    def transition(self, row): self.transitions.append(dict(row))


class NullSink:
    def minute(self, row): pass
    def fill(self, row): pass
    def transition(self, row): pass


class CsvSink:
    def __init__(self, folder):
        self.folder = folder
        folder.mkdir(mode=0o700, exist_ok=False)
        self.streams, self.writers = [], {}
        for name, fields in [('minute-account.csv', MINUTE_FIELDS),
                             ('modeled-fills.csv', FILL_FIELDS),
                             ('order-transition-trace.csv', TRANSITION_FIELDS)]:
            fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            stream = os.fdopen(fd, 'w', newline='', encoding='utf-8', buffering=262144)
            self.streams.append(stream)
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            self.writers[name] = writer

    def minute(self, row): self.writers['minute-account.csv'].writerow(row)
    def fill(self, row): self.writers['modeled-fills.csv'].writerow(row)
    def transition(self, row): self.writers['order-transition-trace.csv'].writerow(row)

    def close(self):
        for stream in self.streams:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()


@dataclass
class Order:
    identity: int
    slot: int
    side: str
    limit: F
    remaining: int
    created: int
    due: int
    activated: int | None = None
    defer_reason: str | None = None


class Grid:
    def __init__(self, bars, bps, path, sink=None):
        if bps not in FEES or path not in PATHS:
            raise Guard('unfrozen model scenario')
        self.bars, self.bps, self.path = bars, bps, path
        self.sink = sink or NullSink()
        self.levels, self.qticks = geometry(bars)
        self.rate = F(bps, 10000)
        self.cash, self.btc, self.fees = CAPITAL, 0, F(0)
        self.cash_reserved, self.btc_reserved = F(0), 0
        self.held = [0] * 20
        self.active, self.pending = {}, {}
        self.next_order = self.next_transition = 1
        self.trade_count = self.grid_fill_count = 0
        self.peak, self.mdd = CAPITAL, F(0)
        self.totals = {k: 0 for k in ['partials', 'gap_fills', 'touch_only_fills',
            'outside_range_bars', 'outside_range_fills', 'fills_in_outside_range_bars',
            'outside_bar_ohlc_fills', 'deferred_activation_attempts']}

    def trace(self, order, bar, transition, reason='', phase='OPEN'):
        self.sink.transition({'transition_id': self.next_transition, 'bar_index': bar.index,
            'bar_open_utc': utc(bar.open_ms), 'phase': phase, 'slot_id': order.slot,
            'order_id': order.identity, 'transition': transition, 'reason': reason,
            'due_bar': order.due, 'activation_bar': '' if order.activated is None else order.activated,
            'remaining_quantity': decimal(order.remaining * Q),
            'slot_held_quantity': decimal(self.held[order.slot] * Q),
            'reserved_USDT': decimal(self.cash_reserved), 'reserved_BTC': decimal(self.btc_reserved * Q)})
        self.next_transition += 1

    def new_order(self, slot, side, bar, due):
        limit = self.levels[slot if side == 'BUY' else slot + 1]
        order = Order(self.next_order, slot, side, limit, self.qticks, bar.index, due)
        self.next_order += 1
        self.pending[order.identity] = order
        self.trace(order, bar, 'QUEUED', phase='INITIALIZE' if bar.index == 1 else 'AFTER_FILL')
        return order

    def initialize(self, bar):
        quantity = 10 * self.qticks * Q
        notional = quantity * bar.open
        fee = notional * self.rate
        before = self.cash
        self.cash -= notional + fee
        self.fees += fee
        self.btc = 10 * self.qticks
        self.held[10:] = [self.qticks] * 10
        self.trade_count += 1
        self.sink.fill(initial_fill(bar, quantity, notional, fee, before, self.cash))
        for slot in range(20):
            self.new_order(slot, 'BUY' if slot < 10 else 'SELL', bar, 2)
        self.invariants()

    def activate(self, bar):
        attempts = 0
        for order in sorted(list(self.pending.values()), key=lambda x: (x.due, x.identity, x.slot)):
            if order.due > bar.index:
                continue
            reason = ''
            if ((order.side == 'BUY' and order.limit >= bar.open) or
                    (order.side == 'SELL' and order.limit <= bar.open)):
                reason = 'new_order_marketable'
            opposite = [x for x in self.active.values() if x.side != order.side]
            if not reason and opposite:
                if ((order.side == 'BUY' and order.limit >= min(x.limit for x in opposite)) or
                        (order.side == 'SELL' and order.limit <= max(x.limit for x in opposite))):
                    reason = 'selfcross_or_equal_opposite'
            reserve = order.remaining * Q * order.limit * (1 + self.rate) if order.side == 'BUY' else F(0)
            if not reason and order.side == 'BUY' and reserve > self.cash - self.cash_reserved:
                reason = 'insufficient_unreserved_cash'
            if reason:
                attempts += 1
                if reason != order.defer_reason:
                    self.trace(order, bar, 'DEFER_REASON', reason)
                    order.defer_reason = reason
                continue
            order.activated = bar.index
            self.pending.pop(order.identity)
            self.active[order.identity] = order
            if order.side == 'BUY':
                self.cash_reserved += reserve
            else:
                if self.held[order.slot] != order.remaining:
                    raise Guard('SELL inventory reserve mismatch')
                self.btc_reserved += order.remaining
            self.trace(order, bar, 'ACTIVATED', 'eligible')
        self.totals['deferred_activation_attempts'] += attempts
        self.invariants()
        return attempts

    def invariants(self):
        if self.cash < 0 or self.btc < 0 or self.btc != sum(self.held):
            raise Guard('wallet or inventory identity')
        if not all(0 <= h <= self.qticks for h in self.held):
            raise Guard('slot inventory domain')
        reserve = sum((x.remaining * Q * x.limit * (1 + self.rate)
                       for x in self.active.values() if x.side == 'BUY'), F(0))
        reserved_btc = sum(x.remaining for x in self.active.values() if x.side == 'SELL')
        if self.cash_reserved != reserve or not 0 <= reserve <= self.cash:
            raise Guard('cash reserve counted or spent incorrectly')
        if self.btc_reserved != reserved_btc or not 0 <= reserved_btc <= self.btc:
            raise Guard('inventory reserve identity')
        orders = list(self.active.values()) + list(self.pending.values())
        if len(orders) not in (0, 20) or len({x.slot for x in orders}) != len(orders):
            raise Guard('one live intent per grid slot')
        for x in orders:
            if x.side == 'BUY' and self.held[x.slot] + x.remaining != self.qticks:
                raise Guard('partial BUY inventory identity')
            if x.side == 'SELL' and self.held[x.slot] != x.remaining:
                raise Guard('partial SELL inventory identity')
        buys = [x.limit for x in self.active.values() if x.side == 'BUY']
        sells = [x.limit for x in self.active.values() if x.side == 'SELL']
        if buys and sells and max(buys) >= min(sells):
            raise Guard('active book selfcross')

    def execute(self, order, bar, budgets, phase, sequence, phase_price, gap, counts):
        side = 0 if order.side == 'BUY' else 1
        ticks = min(order.remaining, budgets[side])
        if ticks <= 0:
            return False
        if order.activated is None or order.activated > bar.index or order.due > bar.index:
            raise Guard('fill precedes activation')
        before_cash, before_btc = self.cash, self.btc
        before_reserve, before_reserved_btc = self.cash_reserved, self.btc_reserved
        before_remaining, before_budget = order.remaining, budgets[side]
        quantity = ticks * Q
        notional, fee = quantity * order.limit, quantity * order.limit * self.rate
        if order.side == 'BUY':
            cost = notional + fee
            self.cash -= cost
            self.cash_reserved -= cost
            self.held[order.slot] += ticks
            self.btc += ticks
        else:
            if ticks > self.held[order.slot]:
                raise Guard('SELL exceeds slot inventory')
            self.cash += notional - fee
            self.held[order.slot] -= ticks
            self.btc -= ticks
            self.btc_reserved -= ticks
        self.fees += fee
        self.trade_count += 1
        self.grid_fill_count += 1
        order.remaining -= ticks
        budgets[side] -= ticks
        partial = order.remaining > 0
        touch = (bar.low == order.limit if order.side == 'BUY' else bar.high == order.limit)
        outside_bar = not (bar.low <= order.limit <= bar.high)
        outside_range_bar = bar.low < self.levels[0] or bar.high > self.levels[-1]
        outside_range = phase_price < self.levels[0] or phase_price > self.levels[-1]
        classifications = {'partials': partial, 'gap_fills': gap, 'touch_only_fills': touch,
            'outside_range_fills': outside_range, 'fills_in_outside_range_bars': outside_range_bar,
            'outside_bar_ohlc_fills': outside_bar}
        for key, condition in classifications.items():
            counts[key] += int(condition)
            self.totals[key] += int(condition)
        counts['fills'] += 1
        self.sink.fill({'event_id': self.trade_count, 'bar_index': bar.index,
            'bar_open_utc': utc(bar.open_ms), 'phase': phase, 'phase_sequence': sequence,
            'fill_kind': 'CONDITIONAL_GRID', 'slot_id': order.slot, 'order_id': order.identity,
            'order_created_bar': order.created, 'order_due_bar': order.due,
            'order_activated_bar': order.activated, 'side': order.side,
            'limit_price': decimal(order.limit), 'model_execution_price': decimal(order.limit),
            'outside_bar_ohlc': int(outside_bar), 'quantity': decimal(quantity),
            'notional_USDT': decimal(notional), 'fee_USDT': decimal(fee),
            'cash_before_USDT': decimal(before_cash), 'cash_after_USDT': decimal(self.cash),
            'BTC_before': decimal(before_btc * Q), 'BTC_after': decimal(self.btc * Q),
            'cash_reserved_before_USDT': decimal(before_reserve),
            'cash_reserved_after_USDT': decimal(self.cash_reserved),
            'BTC_reserved_before': decimal(before_reserved_btc * Q),
            'BTC_reserved_after': decimal(self.btc_reserved * Q),
            'direction_budget_before_BTC': decimal(before_budget * Q),
            'direction_budget_after_BTC': decimal(budgets[side] * Q),
            'order_remaining_before': decimal(before_remaining * Q),
            'order_remaining_after': decimal(order.remaining * Q),
            'phase_reference_price': decimal(phase_price), 'touch_only': int(touch),
            'fill_in_outside_range_bar': int(outside_range_bar), 'outside_range_fill': int(outside_range)})
        self.trace(order, bar, 'PARTIAL_FILL' if partial else 'FULL_FILL', phase=phase)
        if not partial:
            self.active.pop(order.identity)
            self.new_order(order.slot, 'SELL' if order.side == 'BUY' else 'BUY', bar, bar.index + 1)
        self.invariants()
        return True

    def traverse(self, bar, budgets, counts):
        executed = set()
        orders = sorted(self.active.values(), key=lambda x: (-x.limit if x.side == 'BUY' else x.limit,
                                                             x.identity, x.slot))
        for order in orders:
            if ((order.side == 'BUY' and bar.open <= order.limit) or
                    (order.side == 'SELL' and bar.open >= order.limit)):
                if self.execute(order, bar, budgets, 'OPEN_GAP', 0, bar.open, True, counts):
                    executed.add(order.identity)
        vertices = ([bar.open, bar.high, bar.low, bar.close] if self.path == 'OHLC'
                    else [bar.open, bar.low, bar.high, bar.close])
        phases = ['O_H', 'H_L', 'L_C'] if self.path == 'OHLC' else ['O_L', 'L_H', 'H_C']
        for sequence, (start, end, phase) in enumerate(zip(vertices, vertices[1:], phases), 1):
            if start == end:
                continue
            side = 'BUY' if end < start else 'SELL'
            candidates = [x for x in self.active.values() if x.side == side and
                          min(start, end) <= x.limit <= max(start, end)]
            candidates.sort(key=lambda x: (-x.limit if side == 'BUY' else x.limit, x.identity, x.slot))
            for order in candidates:
                if order.identity not in executed:
                    if self.execute(order, bar, budgets, phase, sequence, order.limit, False, counts):
                        executed.add(order.identity)

    def run(self):
        preflight(self.bars)
        for bar in self.bars:
            original = bar.budgets()
            budgets = list(original)
            counts = {k: 0 for k in ['fills', 'partials', 'gap_fills', 'touch_only_fills',
                'outside_range_fills', 'fills_in_outside_range_bars', 'outside_bar_ohlc_fills']}
            deferred = 0
            if bar.index == 1:
                self.initialize(bar)
            elif bar.index >= 2:
                deferred = self.activate(bar)
                self.traverse(bar, budgets, counts)
            outside = bar.low < self.levels[0] or bar.high > self.levels[-1]
            self.totals['outside_range_bars'] += int(outside)
            nav = self.cash + self.btc * Q * bar.close
            self.peak = max(self.peak, nav)
            drawdown = 1 - nav / self.peak
            self.mdd = max(self.mdd, drawdown)
            self.sink.minute(minute_row(bar, self.cash, self.btc, self.fees,
                self.cash_reserved, self.btc_reserved, nav, self.peak, drawdown,
                self.trade_count, self.grid_fill_count, original, budgets, counts, outside, deferred))
        last = self.bars[-1]
        end_bar = Bar(len(self.bars), last.open_ms + 60000, last.close, last.close,
                      last.close, last.close, F(0), F(0), 0, F(0), F(0))
        for order in sorted(list(self.active.values()) + list(self.pending.values()), key=lambda x: x.identity):
            if order.identity in self.active:
                if order.side == 'BUY':
                    self.cash_reserved -= order.remaining * Q * order.limit * (1 + self.rate)
                else:
                    self.btc_reserved -= order.remaining
                self.active.pop(order.identity)
            else:
                self.pending.pop(order.identity)
            self.trace(order, end_bar, 'CANCEL_AT_END', 'release_only_not_flat', 'TERMINAL')
        self.invariants()
        if self.cash_reserved or self.btc_reserved:
            raise Guard('terminal reservations not released')
        result = summary(self.bars, self.bps, 'grid', self.path, self.cash, self.btc,
                         self.fees, self.mdd, self.trade_count, self.grid_fill_count)
        result.update(self.totals)
        result['trace_semantics'] = ('All economic/order transitions and changes of activation-defer reason; '
            'unchanged defer attempts counted in each minute and totals rather than repeated trace rows.')
        result['transition_count'] = self.next_transition - 1
        return result


def initial_fill(bar, quantity, notional, fee, before, after):
    row = {k: '' for k in FILL_FIELDS}
    row.update(event_id=1, bar_index=bar.index, bar_open_utc=utc(bar.open_ms), phase='INITIALIZE',
        phase_sequence=0, fill_kind='HYPOTHETICAL_INITIALIZATION_BUY', order_id='INIT',
        order_created_bar=1, order_due_bar=1, order_activated_bar=1, side='BUY',
        model_execution_price=decimal(bar.open), outside_bar_ohlc=0, quantity=decimal(quantity),
        notional_USDT=decimal(notional), fee_USDT=decimal(fee), cash_before_USDT=decimal(before),
        cash_after_USDT=decimal(after), BTC_before='0', BTC_after=decimal(quantity),
        cash_reserved_before_USDT='0', cash_reserved_after_USDT='0',
        BTC_reserved_before='0', BTC_reserved_after='0', phase_reference_price=decimal(bar.open),
        touch_only=0, fill_in_outside_range_bar=0, outside_range_fill=0)
    return row


def minute_row(bar, cash, btc, fees, cash_reserved, btc_reserved, nav, peak, dd,
               trade_count, grid_count, original=None, budgets=None, counts=None,
               outside=False, deferred=0):
    counts = counts or {k: 0 for k in ['fills', 'partials', 'gap_fills', 'touch_only_fills',
                'outside_range_fills', 'fills_in_outside_range_bars', 'outside_bar_ohlc_fills']}
    return {'bar_index': bar.index, 'bar_open_utc': utc(bar.open_ms),
        'bar_close_exclusive_utc': utc(bar.open_ms + 60000), 'close': decimal(bar.close),
        'cash_USDT': decimal(cash), 'BTC': decimal(btc * Q), 'fees_USDT': decimal(fees),
        'cash_reserved_USDT': decimal(cash_reserved), 'BTC_reserved': decimal(btc_reserved * Q),
        'NAV_USDT': decimal(nav), 'peak_USDT': decimal(peak), 'drawdown_fraction': ratio_text(dd),
        'drawdown_numerator': dd.numerator, 'drawdown_denominator': dd.denominator,
        'modeled_trade_count': trade_count, 'grid_fill_count': grid_count,
        'passive_buy_budget_BTC': '' if original is None else decimal(original[0] * Q),
        'passive_sell_budget_BTC': '' if original is None else decimal(original[1] * Q),
        'passive_buy_used_BTC': '' if original is None else decimal((original[0] - budgets[0]) * Q),
        'passive_sell_used_BTC': '' if original is None else decimal((original[1] - budgets[1]) * Q),
        'bar_grid_fill_count': counts['fills'], 'bar_partial_fill_count': counts['partials'],
        'bar_open_gap_fill_count': counts['gap_fills'], 'bar_touch_only_fill_count': counts['touch_only_fills'],
        'outside_range_bar': int(outside), 'bar_outside_range_fill_count': counts['outside_range_fills'],
        'bar_fills_in_outside_range_count': counts['fills_in_outside_range_bars'],
        'bar_outside_bar_ohlc_fill_count': counts['outside_bar_ohlc_fills'],
        'bar_deferred_activation_count': deferred}


def summary(bars, bps, model, path, cash, btc, fees, mdd, trades, fills):
    rate = F(bps, 10000)
    quantity = btc * Q
    nav = cash + quantity * bars[-1].close
    exit_fee = quantity * bars[-1].close * rate
    liquidation = nav - exit_fee
    levels, qticks = geometry(bars)
    initial_notional = (10 * qticks * Q * bars[1].open) if model != 'cash' else F(0)
    return {'schema': 'conditional-spot-grid-account-summary/1', 'model': model,
        'fee_bps_per_side': bps if model != 'cash' else None, 'ohlc_path': path,
        'minute_rows': len(bars), 'initial_NAV_USDT': '10000',
        'anchor_close_USDT': decimal(bars[0].close), 'initialization_open_USDT': decimal(bars[1].open),
        'grid_levels_USDT': [decimal(x) for x in levels], 'maximum_slot_quantity': decimal(qticks * Q),
        'initialization_notional_USDT': decimal(initial_notional),
        'initialization_quantity_BTC': decimal(10 * qticks * Q) if model != 'cash' else '0',
        'initialization_fee_USDT': decimal(initial_notional * rate),
        'cash_USDT': decimal(cash), 'BTC': decimal(quantity), 'ordinary_fees_USDT': decimal(fees),
        'marked_terminal_NAV_USDT': decimal(nav), 'hypothetical_exit_fee_USDT': decimal(exit_fee),
        'fee_inclusive_terminal_NAV_USDT': decimal(liquidation),
        'marked_month_return': decimal(nav / CAPITAL - 1),
        'fee_inclusive_month_return': decimal(liquidation / CAPITAL - 1),
        'close_MDD_fraction': ratio_text(mdd), 'close_MDD_numerator': mdd.numerator,
        'close_MDD_denominator': mdd.denominator, 'modeled_trade_count': trades,
        'grid_fill_count': fills, 'cash_reserved_after_cancel_USDT': '0',
        'BTC_reserved_after_cancel': '0', 'hypothetical_exit_is_actual_fill': False,
        'actual_flat_confirmed': False, 'initialization_and_exit_volume_certified': False,
        'native_bot_replication': False, 'actual_orders': 0, 'new_network_requests': 0,
        'paid_calls': 0, 'annualized_return': None, 'clean_OOS': False}


def benchmark(bars, bps, model, sink=None):
    if model not in ('hold', 'cash') or bps not in FEES:
        raise Guard('invalid benchmark')
    sink = sink or NullSink()
    preflight(bars)
    levels, qticks = geometry(bars)
    rate = F(bps, 10000)
    cash, btc, fees, peak, mdd, trades = CAPITAL, 0, F(0), CAPITAL, F(0), 0
    for bar in bars:
        if model == 'hold' and bar.index == 1:
            quantity = 10 * qticks * Q
            notional = quantity * bar.open
            fee = notional * rate
            before = cash
            cash -= notional + fee
            fees += fee
            btc = 10 * qticks
            trades = 1
            sink.fill(initial_fill(bar, quantity, notional, fee, before, cash))
        nav = cash + btc * Q * bar.close
        peak = max(peak, nav)
        dd = 1 - nav / peak
        mdd = max(mdd, dd)
        sink.minute(minute_row(bar, cash, btc, fees, F(0), 0, nav, peak, dd, trades, 0,
            outside=bar.low < levels[0] or bar.high > levels[-1]))
    return summary(bars, bps, model, None, cash, btc, fees, mdd, trades, 0)


def write_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return raw


def replay(protocol, protocol_raw, bars):
    preflight(bars)
    if OUTPUT.exists() or OUTPUT.is_symlink():
        raise Guard('run directory already exists; preserve previous results')
    for parent in [OUTPUT.parent, OUTPUT.parent.parent]:
        if parent.is_symlink():
            raise Guard('private report parent symlink')
    OUTPUT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    OUTPUT.mkdir(mode=0o700)
    source_bytes = Path(__file__).read_bytes()
    test_file = Path(__file__).with_name('test_spot_grid_economic_diagnostic_20261008_v0.py')
    test_bytes = test_file.read_bytes()
    fd = os.open(OUTPUT / 'protocol.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(protocol_raw)
        stream.flush()
        os.fsync(stream.fileno())
    summaries = []
    started = time.monotonic()
    try:
        for bps in FEES:
            for path in PATHS:
                name = f'grid-fee{bps:02d}bps-{path}'
                sink = CsvSink(OUTPUT / name)
                try:
                    result = Grid(bars, bps, path, sink).run()
                finally:
                    sink.close()
                result['path_id'] = name
                write_json(OUTPUT / name / 'summary.json', result)
                summaries.append(result)
                print(json.dumps({'completed_path': name, 'rows': result['minute_rows'],
                    'model_fills': result['grid_fill_count'], 'actual_orders': 0}), flush=True)
        for bps in FEES:
            name = f'hold-fee{bps:02d}bps'
            sink = CsvSink(OUTPUT / name)
            try:
                result = benchmark(bars, bps, 'hold', sink)
            finally:
                sink.close()
            result['path_id'] = name
            write_json(OUTPUT / name / 'summary.json', result)
            summaries.append(result)
        sink = CsvSink(OUTPUT / 'cash')
        try:
            result = benchmark(bars, 0, 'cash', sink)
        finally:
            sink.close()
        result['path_id'] = 'cash'
        write_json(OUTPUT / 'cash/summary.json', result)
        summaries.append(result)
        if Path(__file__).read_bytes() != source_bytes or test_file.read_bytes() != test_bytes:
            raise Guard('runtime source changed')
        by_name = {x['path_id']: x for x in summaries}
        comparisons = []
        for bps in FEES:
            for path in PATHS:
                grid_id, hold_id = f'grid-fee{bps:02d}bps-{path}', f'hold-fee{bps:02d}bps'
                g, h = by_name[grid_id], by_name[hold_id]
                comparisons.append({'grid_path_id': grid_id, 'matched_hold_path_id': hold_id,
                    'cash_path_id': 'cash', 'fee_bps_per_side': bps, 'ohlc_path': path,
                    'primary_fee_case': bps == 25,
                    'marked_grid_minus_hold_USDT': decimal(F(g['marked_terminal_NAV_USDT']) - F(h['marked_terminal_NAV_USDT'])),
                    'fee_inclusive_grid_minus_hold_USDT': decimal(F(g['fee_inclusive_terminal_NAV_USDT']) - F(h['fee_inclusive_terminal_NAV_USDT'])),
                    'fee_inclusive_grid_minus_cash_USDT': decimal(F(g['fee_inclusive_terminal_NAV_USDT']) - CAPITAL)})
        artifacts = []
        for f in sorted(OUTPUT.rglob('*')):
            if f.is_file():
                raw = f.read_bytes()
                artifacts.append({'relative_path': f.relative_to(OUTPUT).as_posix(), 'bytes': len(raw), 'sha256': sha(raw)})
        report = {'schema': 'conditional-spot-grid-economic-run/1',
            'created_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'CONDITIONAL_MODEL_ONLY_AWAITING_INDEPENDENT_ECONOMIC_AUDIT',
            'protocol_sha256': PROTOCOL_SHA, 'producer_sha256': sha(source_bytes), 'controls_source_sha256': sha(test_bytes),
            'input_bindings': protocol['bindings'], 'rows_per_path': len(bars), 'grid_paths': 8,
            'holding_paths': 4, 'cash_paths': 1, 'unique_account_paths': len(summaries),
            'total_minute_rows': len(bars) * len(summaries), 'all_path_summaries': summaries,
            'paired_comparisons': comparisons, 'artifacts': artifacts, 'elapsed_seconds': time.monotonic() - started,
            'actual_orders': 0, 'new_network_requests': 0, 'credentials_reads': 0, 'paid_calls': 0,
            'github_writes': 0, 'native_bot_replication': False, 'fill_probability_estimated': False,
            'OHLC_paths_are_true_bounds': False, 'profitability_or_execution_admitted': False,
            'independent_audit_complete': False, 'multiple_testing_adjusted': False,
            'annualized_return': None, 'clean_OOS': False,
            'limits': protocol['trial_and_native_boundaries'],
            'initialization_exit_participation_limit_applies': False,
            'path_dependence_notice': 'The two OHLC scenarios share observations, same-side limit '
                'priority and separate fixed direction budgets. Full-fill replacements wait for the '
                'next bar and new marketable orders defer, removing intrabar replacement feedback. '
                'Economic equality may therefore be structural despite differing cross-side logs; '
                'it is not two independent trials or evidence of actual matching robustness.',
            'observed_terminal_pair_equalities': [
                {'fee_bps': bps,
                 'cash_equal': by_name[f'grid-fee{bps:02d}bps-OHLC']['cash_USDT'] == by_name[f'grid-fee{bps:02d}bps-OLHC']['cash_USDT'],
                 'BTC_equal': by_name[f'grid-fee{bps:02d}bps-OHLC']['BTC'] == by_name[f'grid-fee{bps:02d}bps-OLHC']['BTC'],
                 'fee_inclusive_NAV_equal': by_name[f'grid-fee{bps:02d}bps-OHLC']['fee_inclusive_terminal_NAV_USDT'] == by_name[f'grid-fee{bps:02d}bps-OLHC']['fee_inclusive_terminal_NAV_USDT']}
                for bps in FEES]}
        raw = write_json(OUTPUT / 'report.json', report)
        return {'report_sha256': sha(raw), 'producer_sha256': sha(source_bytes),
                'control_source_sha256': sha(test_bytes), 'unique_paths': len(summaries),
                'minute_rows': len(bars) * len(summaries), 'actual_orders': 0}
    except Exception as exc:
        write_json(OUTPUT / 'failed-run.json', {'status': 'PRESERVED_PARTIAL_NO_SUCCESS_CLAIM',
            'exception_class': type(exc).__name__, 'protocol_sha256': PROTOCOL_SHA,
            'producer_sha256': sha(source_bytes), 'completed_paths': [x['path_id'] for x in summaries],
            'actual_orders': 0, 'new_network_requests': 0})
        raise


def main():
    global PRIVATE, PROTOCOL, OUTPUT
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('preflight', 'replay'))
    parser.add_argument('--approved-protocol-sha', required=True)
    parser.add_argument('--local-root', type=Path, required=True,
                        help='Explicit local owner-supplied evidence root; no home-directory discovery.')
    args = parser.parse_args()
    PRIVATE = args.local_root.resolve()
    PROTOCOL = PRIVATE / 'protocols/spot-grid-economic-diagnostic-20261008-v0/protocol-v0.json'
    OUTPUT = PRIVATE / 'reports/spot-grid-economic-diagnostic-20261008-v0/run-v0'
    protocol, raw = read_protocol(args.approved_protocol_sha)
    if protocol['required_private_outputs']['minute_columns'] != MINUTE_FIELDS:
        raise Guard('minute output schema mismatch')
    if protocol['required_private_outputs']['fill_columns'] != FILL_FIELDS:
        raise Guard('fill output schema mismatch')
    if protocol['required_private_outputs']['transition_columns'] != TRANSITION_FIELDS:
        raise Guard('transition output schema mismatch')
    bars = load_bars(protocol)
    result = preflight(bars) if args.mode == 'preflight' else replay(protocol, raw, bars)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
