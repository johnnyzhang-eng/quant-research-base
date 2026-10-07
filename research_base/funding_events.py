"""Canonical funding observations and a bounded, synthetic cash bridge.

An observed rate is not a cash settlement. Explicit synthetic fixture contracts
bind pre-action holdings and scheduled postings. This module performs no I/O,
network access, broker operations, or claim of real settlement eligibility.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from . import funding_account as ledger


MAX_TIME_MS = 253402300799999
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
BRIDGE_PROTOCOL_ID = "synthetic-funding-events/1"


class CompilerError(ValueError):
    pass


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise CompilerError('Canonical finite JSON required') from exc


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def number(value, label, *, positive=False):
    try:
        result = ledger.input_decimal(value, label, positive=positive)
    except ledger.AccountingError as exc:
        raise CompilerError(str(exc)) from exc
    return ledger.decimal_text(result)


def time_ms(value, label='time_ms'):
    # No float, implicit timezone, rounding, or timestamp conversion by division.
    if isinstance(value, str):
        if not re.fullmatch(r'0|[1-9][0-9]{0,14}', value):
            raise CompilerError(label+': require canonical nonnegative integer milliseconds')
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TIME_MS:
        raise CompilerError(label+': milliseconds outside UTC datetime domain')
    return value


def iso_ms(value):
    return (EPOCH+timedelta(milliseconds=time_ms(value))).isoformat(timespec='milliseconds')


def name(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z0-9_:-]{1,50}', value):
        raise CompilerError(label+': uppercase market/symbol namespace required')
    return value


def event_identity(market, symbol, calc_time_ms):
    key = {'market': name(market, 'market'), 'symbol': name(symbol, 'symbol'),
           'calc_time_ms': time_ms(calc_time_ms, 'calc_time')}
    return 'funding:'+digest(key)


def sequence(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CompilerError(label+': nonnegative integer required')
    return value


def compile_observations(rows, mark_rows, *, market, symbol):
    """Compile observations under a caller-declared instrument namespace.

    Rows contain calc_time, funding_interval_hours, last_funding_rate and
    source_line. Marks are closed one-minute OHLC reference intervals. Neither
    input supplies cash eligibility, an exact settlement mark or account credit.
    """
    name(market, 'market')
    name(symbol, 'symbol')
    if not isinstance(rows, list) or not isinstance(mark_rows, list):
        raise CompilerError('Funding rows and mark rows must be explicit lists')
    groups = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
                'calc_time', 'funding_interval_hours', 'last_funding_rate', 'source_line'}:
            raise CompilerError('Unknown or missing observation field')
        t = time_ms(row['calc_time'], 'calc_time')
        interval = number(row['funding_interval_hours'], 'declared_interval', positive=True)
        rate = number(row['last_funding_rate'], 'observed_rate')
        if ledger.decimal(rate).copy_abs() > ledger.ONE:
            raise CompilerError('Rate outside bounded synthetic numeric range')
        line = sequence(row['source_line'], 'source_line')
        key = event_identity(market, symbol, t)
        semantic = {'calc_time_ms': t, 'declared_interval_hours': interval, 'observed_rate': rate}
        instance = {'source_line': line,
                    'raw_calc_time': str(row['calc_time']),
                    'raw_declared_interval': str(row['funding_interval_hours']),
                    'raw_observed_rate': str(row['last_funding_rate'])}
        group = groups.setdefault(key, {'semantic_variants': {}, 'instances': []})
        group['semantic_variants'][digest(semantic)] = semantic
        group['instances'].append(instance)
    bars = {}
    for bar in mark_rows:
        if not isinstance(bar, dict) or set(bar) != {
                'open_time_ms', 'close_time_ms', 'open', 'high', 'low', 'close'}:
            raise CompilerError('Mark reference fields differ')
        t = time_ms(bar['open_time_ms'], 'mark_open')
        end = time_ms(bar['close_time_ms'], 'mark_close')
        if t % 60000 or end != t+59999:
            raise CompilerError('Mark reference is not an exact closed one-minute interval')
        low, high = number(bar['low'], 'mark_low', positive=True), number(bar['high'], 'mark_high', positive=True)
        opening, closing = number(bar['open'], 'mark_open_price', positive=True), number(bar['close'], 'mark_close_price', positive=True)
        if not ledger.decimal(low) <= ledger.decimal(opening) <= ledger.decimal(high) or not ledger.decimal(low) <= ledger.decimal(closing) <= ledger.decimal(high):
            raise CompilerError('Mark reference OHLC outside low/high envelope')
        normalized = {'open_time_ms': t, 'close_time_ms': end, 'low': low, 'high': high,
                      'open': opening, 'close': closing}
        if t in bars and bars[t] != normalized:
            raise CompilerError('Conflicting mark reference minute')
        bars[t] = normalized
    compiled, quarantined = [], []
    for key, group in groups.items():
        variants = sorted(group['semantic_variants'].values(), key=canonical)
        instances = sorted(group['instances'], key=canonical)
        if len(variants) != 1:
            quarantined.append({'event_id': key, 'semantic_variants': variants,
                                'source_instances': instances,
                                'reason': 'CONFLICTING_OBSERVATION_IDENTITY',
                                'cannot_post_actual_cash': True})
            continue
        semantic = variants[0]
        t = semantic['calc_time_ms']
        bar = bars.get(t//60000*60000)
        reference = None if bar is None else {
            'status': 'CONTAINING_MINUTE_REFERENCE_NOT_SETTLEMENT_MARK',
            'minute_open_ms': bar['open_time_ms'], 'minute_close_ms': bar['close_time_ms'],
            'low': bar['low'], 'high': bar['high'],
            'exact_settlement_mark_provided': False}
        compiled.append({'schema': 'canonical-funding-observation/1',
                         'kind': 'ARCHIVE_OBSERVATION_ONLY', 'event_id': key,
                         'market': market, 'symbol': symbol, **semantic,
                         'calc_time_utc': iso_ms(t),
                         'offset_from_containing_minute_ms': t % 60000,
                         'source_instances': instances, 'duplicate_instances': len(instances)-1,
                         'declared_interval_is_rate_multiplier': False,
                         'mark_reference_envelope': reference,
                         'eligible_perp_qty_signed': None, 'exact_settlement_mark': None,
                         'actual_payment_time_ms': None, 'historical_known_at_ms': None,
                         'account_cash_credit_evidence': None, 'fee_evidence': None,
                         'rate_settlement_cash_semantics_verified': False,
                         'cannot_post_actual_cash': True})
    compiled.sort(key=lambda item: (item['calc_time_ms'], item['event_id']))
    quarantined.sort(key=lambda item: item['event_id'])
    return {'schema': 'funding-observation-compilation/1', 'input_rows': len(rows),
            'accepted_observations': compiled, 'quarantined_conflicts': quarantined,
            'actual_cash_postings': [], 'actual_cash_posting_qualified': False,
            'ordering_is_observation_time_not_proof_of_venue_event_order': True}


CONTRACT_FIELDS = {
    'schema', 'mode', 'event_id', 'market', 'symbol', 'settlement_time_ms',
    'payment_time_ms', 'rate', 'eligible_perp_qty_signed', 'settlement_mark',
    'pre_event_account_view_sha256', 'capture_sequence', 'posting_sequence',
    'rate_known_at_ms', 'eligibility_known_at_ms', 'mark_known_at_ms',
    'eligibility_evidence_kind', 'payment_evidence_kind'}


def make_synthetic_contract(*, market, symbol, settlement_time_ms, payment_time_ms,
                            rate, eligible_perp_qty_signed, settlement_mark,
                            pre_event_account_view_sha256, capture_sequence=0,
                            posting_sequence=0, rate_known_at_ms=None,
                            eligibility_known_at_ms=None, mark_known_at_ms=None):
    """Construct an explicit fixture; no archive-to-cash qualification inference."""
    t = time_ms(settlement_time_ms, 'settlement_time')
    p = time_ms(payment_time_ms, 'payment_time')
    known = [time_ms(value, label) for value, label in (
        (rate_known_at_ms, 'rate_known_at'), (eligibility_known_at_ms, 'eligibility_known_at'),
        (mark_known_at_ms, 'mark_known_at'))]
    if p < t or any(value > t for value in known):
        raise CompilerError('Synthetic payment or knowledge times are not causal')
    cseq, pseq = sequence(capture_sequence, 'capture_sequence'), sequence(posting_sequence, 'posting_sequence')
    if p == t and pseq <= cseq:
        raise CompilerError('Same-time posting must follow capture by explicit sequence')
    qty = number(eligible_perp_qty_signed, 'synthetic_eligible_quantity')
    normalized_rate = number(rate, 'synthetic_rate')
    mark = number(settlement_mark, 'synthetic_settlement_mark', positive=True)
    if ledger.decimal(qty) > ledger.ZERO or ledger.decimal(normalized_rate).copy_abs() > ledger.ONE:
        raise CompilerError('Only held short or explicit flat eligibility is supported')
    if not isinstance(pre_event_account_view_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', pre_event_account_view_sha256):
        raise CompilerError('Explicit pre-event account view hash required')
    return {'schema': 'synthetic-funding-qualification/1', 'mode': 'EXPLICIT_SYNTHETIC_FIXTURE_ONLY',
            'event_id': event_identity(market, symbol, t), 'market': market, 'symbol': symbol,
            'settlement_time_ms': t, 'payment_time_ms': p, 'rate': normalized_rate,
            'eligible_perp_qty_signed': qty, 'settlement_mark': mark,
            'pre_event_account_view_sha256': pre_event_account_view_sha256,
            'capture_sequence': cseq, 'posting_sequence': pseq,
            'rate_known_at_ms': known[0], 'eligibility_known_at_ms': known[1], 'mark_known_at_ms': known[2],
            'eligibility_evidence_kind': 'DECLARED_SYNTHETIC_PRE_EVENT_STATE',
            'payment_evidence_kind': 'DECLARED_SYNTHETIC_CASH_SCHEDULE'}


def validate_contract(contract):
    if not isinstance(contract, dict) or set(contract) != CONTRACT_FIELDS:
        raise CompilerError('Only explicit synthetic qualification contracts are accepted')
    expected = make_synthetic_contract(**{k: contract[k] for k in CONTRACT_FIELDS - {
        'schema', 'mode', 'event_id', 'eligibility_evidence_kind', 'payment_evidence_kind'}})
    if contract != expected:
        raise CompilerError('Synthetic contract identity, mode, evidence or normalized inputs differ')
    return expected


class SyntheticFundingBridge:
    """Bounded fixture bridge; no real credit evidence or broker integration."""
    def __init__(self, initial_spot_cash, initial_deriv_wallet_cash, *, market, symbol):
        self.market = name(market, 'market')
        self.symbol = name(symbol, 'symbol')
        self.account = ledger.CarryAccount(initial_spot_cash, initial_deriv_wallet_cash)
        self.operations = []
        self._operation_hashes = {}
        self.settlements = {}
        self._last_order = None

    def pre_event_view(self):
        return copy.deepcopy(self.account.view())

    def pre_event_view_sha256(self):
        return digest(self.pre_event_view())

    def _guard_order(self, order):
        if self._last_order is not None and order <= self._last_order:
            raise CompilerError('Bridge causal order must be strictly increasing')

    def _commit(self, op_id, operation, transition):
        hashed = digest(operation)
        if op_id in self._operation_hashes:
            if self._operation_hashes[op_id] != hashed:
                raise CompilerError('Conflicting committed bridge identity')
            return {'status': 'IDEMPOTENT_COMMITTED_NOOP', 'operation_id': op_id,
                    'state': self.pre_event_view()}
        if len(self.operations) >= ledger.NUMERIC_DOMAIN['max_committed_events']:
            raise CompilerError('Bridge committed operation count exceeds frozen 128-step bound')
        staged = copy.deepcopy(self)
        try:
            details = transition(staged)
        except ledger.AccountingError as exc:
            raise CompilerError(str(exc)) from exc
        staged._operation_hashes[op_id] = hashed
        staged.operations.append(copy.deepcopy(operation))
        self.__dict__.clear()
        self.__dict__.update(staged.__dict__)
        return {'status': 'COMMITTED', 'operation_id': op_id, 'details': details,
                'state': self.pre_event_view()}

    def apply_action(self, event):
        if not isinstance(event, dict) or event.get('type') in {'funding_settlement', 'funding_post'}:
            raise CompilerError('Funding is only accepted through the synthetic contract bridge')
        ledger.identifier(event.get('id'), 'action id')
        operation = {'kind': 'action', 'event': copy.deepcopy(event)}
        def transition(staged):
            order = (ledger.utc_time(event.get('event_time'), 'event_time'),
                     sequence(event.get('sequence'), 'sequence'))
            staged._guard_order(order)
            result = staged.account.apply_event(event)
            if result['status'] != 'COMMITTED':
                raise CompilerError('Untracked account event cannot be adopted by the bridge')
            staged._last_order = order
            return {'ledger_event_id': event['id']}
        return self._commit('action:'+event['id'], operation, transition)

    def capture(self, contract):
        contract = validate_contract(contract)
        if contract['market'] != self.market or contract['symbol'] != self.symbol:
            raise CompilerError('Synthetic contract instrument namespace differs from account')
        key = contract['event_id']
        operation = {'kind': 'capture', 'contract': copy.deepcopy(contract)}
        def transition(staged):
            order = (ledger.utc_time(iso_ms(contract['settlement_time_ms']), 'settlement'),
                     contract['capture_sequence'])
            staged._guard_order(order)
            if staged.pre_event_view_sha256() != contract['pre_event_account_view_sha256']:
                raise CompilerError('Synthetic eligibility is not bound to current pre-event state')
            qty = ledger.decimal(contract['eligible_perp_qty_signed'])
            if qty != staged.account.perp_qty_signed:
                raise CompilerError('Synthetic eligible quantity differs from pre-event held quantity')
            if key in staged.settlements:
                raise CompilerError('Duplicate synthetic obligation identity')
            with ledger.exact_money_context():
                amount = -qty * ledger.decimal(contract['settlement_mark']) * ledger.decimal(contract['rate'])
                amount_text = ledger.decimal_text(amount)
            ledger_request = None
            if qty != ledger.ZERO:
                stamp = iso_ms(contract['settlement_time_ms'])
                ledger_request = {'id': key+':capture', 'type': 'funding_settlement',
                    'event_time': stamp, 'applied_at': stamp,
                    'known_at': iso_ms(max(contract['rate_known_at_ms'], contract['eligibility_known_at_ms'], contract['mark_known_at_ms'])),
                    'sequence': contract['capture_sequence'], 'settlement_id': key,
                    'settlement_time': stamp, 'payment_time': iso_ms(contract['payment_time_ms']),
                    'eligible_perp_qty_signed': contract['eligible_perp_qty_signed'],
                    'settlement_mark': contract['settlement_mark'], 'rate': contract['rate']}
                staged.account.apply_event(ledger_request)
            staged.settlements[key] = {'contract': copy.deepcopy(contract),
                'captured_synthetic_cash_amount': amount_text,
                'status': 'NO_SYNTHETIC_OBLIGATION_FLAT' if qty == ledger.ZERO else 'PENDING_SYNTHETIC_POST',
                'ledger_capture_request': ledger_request, 'actual_cash_posting_verified': False}
            staged._last_order = order
            return copy.deepcopy(staged.settlements[key])
        return self._commit('capture:'+key, operation, transition)

    def post(self, event_id):
        if not isinstance(event_id, str) or not re.fullmatch(r'funding:[0-9a-f]{64}', event_id):
            raise CompilerError('Canonical obligation identity required')
        operation = {'kind': 'post', 'event_id': event_id}
        def transition(staged):
            item = staged.settlements.get(event_id)
            if item is None:
                raise CompilerError('Explicit synthetic captured obligation absent')
            contract = item['contract']
            if item['status'] == 'NO_SYNTHETIC_OBLIGATION_FLAT':
                raise CompilerError('Flat settlement creates no payable cash obligation')
            if item['status'] != 'PENDING_SYNTHETIC_POST':
                raise CompilerError('Synthetic obligation already posted')
            stamp = iso_ms(contract['payment_time_ms'])
            order = (ledger.utc_time(stamp, 'payment_time'), contract['posting_sequence'])
            staged._guard_order(order)
            request = {'id': event_id+':post', 'type': 'funding_post',
                       'event_time': stamp, 'applied_at': stamp, 'known_at': stamp,
                       'sequence': contract['posting_sequence'], 'settlement_id': event_id}
            staged.account.apply_event(request)
            item['status'] = 'SYNTHETIC_POSTED'
            item['ledger_post_request'] = request
            staged._last_order = order
            return {'synthetic_cash_amount': item['captured_synthetic_cash_amount'],
                    'ledger_post_request': request, 'actual_cash_posting_verified': False}
        return self._commit('post:'+event_id, operation, transition)

    def snapshot(self):
        result = {'schema': 'synthetic-funding-bridge-snapshot/1',
                  'protocol_id': BRIDGE_PROTOCOL_ID,
                  'ledger_protocol_id': ledger.PROTOCOL_ID,
                  'market': self.market, 'symbol': self.symbol,
                  'initial_spot_cash': ledger.decimal_text(self.account.initial_spot_cash),
                  'initial_deriv_wallet_cash': ledger.decimal_text(self.account.initial_deriv_wallet_cash),
                  'complete_committed_operations': copy.deepcopy(self.operations),
                  'final_account_snapshot': self.account.snapshot(),
                  'final_synthetic_settlements': copy.deepcopy(self.settlements),
                  'incomplete_intent': False}
        result['snapshot_sha256'] = digest(result)
        return result

    @classmethod
    def restore(cls, snapshot):
        required = {'schema', 'protocol_id', 'ledger_protocol_id', 'market', 'symbol', 'initial_spot_cash',
                    'initial_deriv_wallet_cash', 'complete_committed_operations',
                    'final_account_snapshot', 'final_synthetic_settlements',
                    'incomplete_intent', 'snapshot_sha256'}
        if not isinstance(snapshot, dict) or set(snapshot) != required:
            raise CompilerError('Bridge snapshot fields differ')
        unsigned = {k: v for k, v in snapshot.items() if k != 'snapshot_sha256'}
        if digest(unsigned) != snapshot['snapshot_sha256']:
            raise CompilerError('Bridge snapshot hash differs')
        if snapshot['schema'] != 'synthetic-funding-bridge-snapshot/1' or snapshot['protocol_id'] != BRIDGE_PROTOCOL_ID or snapshot['ledger_protocol_id'] != ledger.PROTOCOL_ID or snapshot['incomplete_intent'] is not False:
            raise CompilerError('Bridge snapshot scope or unresolved intent differs')
        operations = snapshot['complete_committed_operations']
        if not isinstance(operations, list) or len(operations) > ledger.NUMERIC_DOMAIN['max_committed_events']:
            raise CompilerError('Bounded complete operation log required')
        result = cls(snapshot['initial_spot_cash'], snapshot['initial_deriv_wallet_cash'],
                     market=snapshot['market'], symbol=snapshot['symbol'])
        for operation in operations:
            if not isinstance(operation, dict):
                raise CompilerError('Snapshot operation is not an object')
            kind = operation.get('kind')
            if kind == 'action' and set(operation) == {'kind', 'event'}:
                response = result.apply_action(operation['event'])
            elif kind == 'capture' and set(operation) == {'kind', 'contract'}:
                response = result.capture(operation['contract'])
            elif kind == 'post' and set(operation) == {'kind', 'event_id'}:
                response = result.post(operation['event_id'])
            else:
                raise CompilerError('Snapshot operation fields differ')
            if response['status'] != 'COMMITTED':
                raise CompilerError('Snapshot contains a duplicate committed operation')
        if result.account.snapshot() != snapshot['final_account_snapshot'] or result.settlements != snapshot['final_synthetic_settlements']:
            raise CompilerError('Bridge final state differs from complete deterministic replay')
        return result
