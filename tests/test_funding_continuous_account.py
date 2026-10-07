"""Meaningful continuous-account/2 candidate controls, all invented fixtures.

The 31-day minute cadence verifies continuity/bookkeeping/per-event complexity;
it is not market data, historical return evidence or a venue margin model.
"""
import copy
from datetime import datetime, timedelta, timezone
from decimal import localcontext, ROUND_DOWN, Inexact, Rounded
from fractions import Fraction
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from research_base import funding_continuous_account as module

START = datetime(2020, 1, 1, tzinfo=timezone.utc)
METRICS = {}


def at(seconds):
    return (START + timedelta(seconds=seconds)).isoformat()


def event(i, kind, *, seconds=None, **fields):
    t = at(i if seconds is None else seconds)
    return {'id': 'fixture-' + str(i), 'type': kind, 'event_time': t,
            'applied_at': t, 'known_at': t, 'sequence': i, **fields}


def fill(i, kind, side, qty, price, *, seconds=None, slip='0', fee='0', **fields):
    return event(i, kind, seconds=seconds, side=side, qty=qty,
                 reference_price=price, slippage_rate=slip, fee_rate=fee, **fields)


def rational(value):
    return Fraction(str(value))


def complete_probe(account):
    return module.canonical_bytes({
        'snapshot': account.snapshot(), 'ledger': account.ledger,
        'spot_records': [[module.decimal_text(q), module.decimal_text(p)] for q, p in account.spot_lots.records],
        'spot_cursor': account.spot_lots.cursor(),
        'perp_records': [[module.decimal_text(q), module.decimal_text(p)] for q, p in account.perp_lots.records],
        'perp_cursor': account.perp_lots.cursor(),
        'status_records': {'transfers': account.transfers, 'settlements': account.settlements},
        'event_hashes': account._event_hashes,
    })


def put(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


class ContinuousLedgerTests(unittest.TestCase):
    def test_31_day_full_minute_cadence_single_capital_and_global_identity(self):
        account = module.CarryAccount('1000', '1000', max_events=50000)
        started = time.perf_counter()
        seq = 0

        def apply(kind, seconds, **fields):
            nonlocal seq
            item = event(seq, kind, seconds=seconds, **fields)
            seq += 1
            account.apply_event(item)
            return item

        for minute in range(44640):
            seconds = minute * 60
            apply('mark', seconds, spot_mark='100', perp_mark='100')
            if minute == 0:
                apply('spot_fill', seconds, side='buy', qty='1', reference_price='100',
                      slippage_rate='0', fee_rate='.001')
                apply('perp_fill', seconds, side='sell', qty='1', reference_price='100',
                      slippage_rate='0', fee_rate='.001')
            if minute % 480 == 1:
                apply('funding_post', seconds, settlement_id='month-funding-' + str(minute - 1))
            if minute % 480 == 0:
                apply('funding_settlement', seconds, settlement_id='month-funding-' + str(minute),
                      settlement_time=at(seconds), payment_time=at(seconds + 60),
                      eligible_perp_qty_signed='-1', settlement_mark='100', rate='.0001')
            if minute % 1000 == 0:
                self.assertEqual(account.view()['initial_allocated_equity'], '2000')
                self.assertTrue(account.reconciliation()['identity_exact'])
        apply('perp_fill', 44639 * 60 + 1, side='buy', qty='1', reference_price='100',
              slippage_rate='0', fee_rate='.001')
        apply('spot_fill', 44639 * 60 + 2, side='sell', qty='1', reference_price='100',
              slippage_rate='0', fee_rate='.001')
        self.assertEqual(len(account.events), 44830)
        self.assertEqual(len(account.ledger), 44830)
        self.assertEqual(len(account._event_hashes), 44830)
        self.assertEqual(len(account.settlements), 93)
        expected = Fraction(2000) + 93 * Fraction('.01') - 4 * Fraction('.1')
        self.assertEqual(rational(account.view()['nav']), expected)
        self.assertEqual(account.view()['funding_cash_posted'], '0.93')
        self.assertEqual(account.view()['fees_total'], '0.4')
        self.assertTrue(account.reconciliation()['closed_reconciliation'])
        before = account.snapshot()
        self.assertEqual(account.apply_event(account.events[0])['status'], 'IDEMPOTENT_COMMITTED_NOOP')
        self.assertEqual(account.snapshot(), before)
        METRICS['full_month'] = {'committed_events': 44830, 'minute_marks': 44640,
                                 'funding_captures': 93, 'funding_posts': 93, 'fill_events': 4,
                                 'initial_equity': '2000', 'final_NAV': account.view()['nav'],
                                 'independent_Fraction_expected_NAV': str(expected),
                                 'fees': account.view()['fees_total'], 'posted_funding': '0.93',
                                 'run_max_events': account.max_events,
                                 'same_account_no_capital_or_ID_reset': True,
                                 'journal_chain_sha256': account._journal_chain_sha256,
                                 'elapsed_seconds_observed_not_benchmark': time.perf_counter() - started}

    def test_cross_month_delayed_funding_transfer_restart_and_old_IDs(self):
        account = module.CarryAccount('10000', '10000', max_events=512)
        first = event(0, 'mark', spot_mark='100', perp_mark='100')
        account.apply_event(first)
        account.apply_event(fill(1, 'spot_fill', 'buy', '2', '100', slip='.005', fee='.001'))
        account.apply_event(fill(2, 'perp_fill', 'sell', '2', '100', slip='.005', fee='.001'))
        for i in range(3, 300):
            account.apply_event(event(i, 'mark', spot_mark='100', perp_mark='100'))
        jan31 = 31 * 86400 - 1
        feb1 = 31 * 86400
        nav_before = account.view()['nav']
        account.apply_event(event(300, 'funding_settlement', seconds=jan31,
                                  settlement_id='cross-month-funding', settlement_time=at(jan31),
                                  payment_time=at(feb1 + 1), eligible_perp_qty_signed='-2',
                                  settlement_mark='100', rate='.005'))
        account.apply_event(event(301, 'transfer_send', seconds=jan31,
                                  transfer_id='cross-month-transfer', source='spot', target='deriv', amount='100'))
        self.assertEqual(account.view()['nav'], nav_before)
        self.assertEqual(account.view()['pending_funding_memo_not_in_nav'], '1')
        self.assertEqual(account.view()['transfer_inflight'], '100')
        snapshot = json.loads(json.dumps(account.snapshot()))
        resumed = module.CarryAccount.restore(snapshot)
        self.assertEqual(resumed.snapshot(), snapshot)
        for target in (account, resumed):
            target.apply_event(event(302, 'transfer_receive', seconds=feb1,
                                      transfer_id='cross-month-transfer'))
            target.apply_event(event(303, 'funding_post', seconds=feb1 + 1,
                                      settlement_id='cross-month-funding'))
            target.apply_event(event(304, 'mark', seconds=feb1 + 2, spot_mark='105', perp_mark='105'))
            target.apply_event(fill(305, 'perp_fill', 'buy', '2', '105', seconds=feb1 + 3, slip='.005', fee='.001'))
            target.apply_event(fill(306, 'spot_fill', 'sell', '2', '105', seconds=feb1 + 4, slip='.005', fee='.001'))
        self.assertEqual(resumed.snapshot(), account.snapshot())
        fees = sum(Fraction(2) * p * Fraction('.001') for p in
                   (Fraction('100.5'), Fraction('99.5'), Fraction('105.525'), Fraction('104.475')))
        expected = Fraction(20000) + 2 * (Fraction('104.475') - Fraction('100.5')) + 2 * (Fraction('99.5') - Fraction('105.525')) + 1 - fees
        self.assertEqual(rational(resumed.view()['nav']), expected)
        self.assertEqual(resumed.view()['initial_allocated_equity'], '20000')
        before = complete_probe(resumed)
        self.assertEqual(resumed.apply_event(first)['status'], 'IDEMPOTENT_COMMITTED_NOOP')
        conflict = dict(first, spot_mark='99')
        with self.assertRaises(module.AccountingError):
            resumed.apply_event(conflict)
        self.assertEqual(complete_probe(resumed), before)
        METRICS['cross_month'] = {'events_before_restart': 302, 'events_after_resume': 307,
                                  'pending_funding_before': '1', 'inflight_transfer_before': '100',
                                  'final_NAV': resumed.view()['nav'], 'Fraction_expected': str(expected),
                                  'fees': str(fees), 'old_ID_replay_noop_and_conflict_rejected': True,
                                  'single_initial_equity': '20000'}

    def test_FIFO_immutable_records_partial_consumption_independent_cost(self):
        account = module.CarryAccount('100', '100', max_events=32)
        account.apply_event(event(0, 'mark', spot_mark='3', perp_mark='3'))
        for i, kind, q, p in ((1, 'spot_fill', '1', '1'), (2, 'spot_fill', '2', '2'),
                              (3, 'perp_fill', '1', '1'), (4, 'perp_fill', '2', '2')):
            account.apply_event(fill(i, kind, 'buy' if kind == 'spot_fill' else 'sell', q, p))
        original_spot = list(account.spot_lots.records)
        original_perp = list(account.perp_lots.records)
        account.apply_event(fill(5, 'spot_fill', 'sell', '1.5', '3'))
        account.apply_event(fill(6, 'perp_fill', 'buy', '1.5', '3'))
        cost = Fraction(1) * 1 + Fraction('.5') * 2
        self.assertEqual(rational(account.view()['realized_spot_gross']), Fraction('1.5') * 3 - cost)
        self.assertEqual(rational(account.view()['realized_perp_gross']), cost - Fraction('1.5') * 3)
        self.assertEqual(account.spot_lots.records, original_spot)
        self.assertEqual(account.perp_lots.records, original_perp)
        self.assertEqual(account.view()['spot_entry_notional_exact'], '3')
        self.assertEqual(account.spot_lots.head, 1)
        self.assertEqual(account.spot_lots.head_remaining, module.Decimal('1.5'))
        self.assertEqual(account.view()['nav'], '200')
        self.assertTrue(account.reconciliation()['identity_exact'])

    def test_fixed_event_budget_restore_no_expansion_or_duplicate_after_cap(self):
        for bad in (0, -1, True, 1.0, '50000', 1000001):
            with self.assertRaises(module.AccountingError):
                module.CarryAccount('1', '1', max_events=bad)
        account = module.CarryAccount('1', '1', max_events=129)
        for i in range(129):
            account.apply_event(event(i, 'mark', spot_mark='1', perp_mark='1'))
        snapshot = account.snapshot()
        resumed = module.CarryAccount.restore(snapshot)
        self.assertEqual(resumed.max_events, 129)
        self.assertEqual(resumed.apply_event(account.events[0])['status'], 'IDEMPOTENT_COMMITTED_NOOP')
        before = complete_probe(resumed)
        with self.assertRaises(module.AccountingError):
            resumed.apply_event(event(129, 'mark', spot_mark='1', perp_mark='1'))
        self.assertEqual(complete_probe(resumed), before)
        enlarged = copy.deepcopy(snapshot)
        enlarged['run_contract']['max_events'] = 130
        enlarged['snapshot_sha256'] = module.sha256_value({k: v for k, v in enlarged.items() if k != 'snapshot_sha256'})
        with self.assertRaises(module.AccountingError):
            module.CarryAccount.restore(enlarged)
        with self.assertRaises(TypeError):
            module.CarryAccount.restore(snapshot, max_events=130)

    def test_rejected_event_preserves_FIFO_status_journal_and_identity(self):
        account = module.CarryAccount('100', '30', max_events=32)
        account.apply_event(event(0, 'mark', spot_mark='10', perp_mark='10'))
        account.apply_event(fill(1, 'spot_fill', 'buy', '2', '10'))
        account.apply_event(fill(2, 'perp_fill', 'sell', '1', '10'))
        rejected = [fill(3, 'spot_fill', 'sell', '3', '10'),
                    fill(4, 'perp_fill', 'buy', '2', '10'),
                    fill(5, 'perp_fill', 'sell', '2', '10'),
                    fill(6, 'spot_fill', 'buy', '1', '10', fee='.5'),
                    event(7, 'funding_post', settlement_id='absent')]
        rejected[3]['known_at'] = at(8)
        future_input = fill(8, 'spot_fill', 'sell', '1', '10')
        future_input['knowledge_inputs'] = [{'id': 'future-price', 'known_at': at(9)}]
        rejected.append(future_input)
        before = complete_probe(account)
        for item in rejected:
            with self.assertRaises(module.AccountingError):
                account.apply_event(item)
            self.assertEqual(complete_probe(account), before)
        # Failure after staged lot consumption still cannot alter base lot data.
        late_failure = fill(9, 'perp_fill', 'buy', '1', '1000')
        with self.assertRaises(module.AccountingError):
            account.apply_event(late_failure)
        self.assertEqual(complete_probe(account), before)

    def test_pending_net_zero_not_falsely_all_posted_and_cannot_supply_margin(self):
        account = module.CarryAccount('1000', '21', max_events=32)
        account.apply_event(event(0, 'mark', spot_mark='100', perp_mark='100'))
        account.apply_event(fill(1, 'spot_fill', 'buy', '2', '100'))
        account.apply_event(fill(2, 'perp_fill', 'sell', '1', '100'))
        for i, rate in ((3, '1'), (4, '-1')):
            account.apply_event(event(i, 'funding_settlement', settlement_id='memo-' + str(i),
                                      settlement_time=at(i), payment_time=at(9),
                                      eligible_perp_qty_signed='-1', settlement_mark='100', rate=rate))
        self.assertEqual(account.view()['pending_funding_memo_not_in_nav'], '0')
        self.assertEqual(account.view()['pending_settlement_count'], 2)
        self.assertFalse(account.reconciliation()['all_settled_funding_posted'])
        before = complete_probe(account)
        for item in (fill(5, 'perp_fill', 'sell', '1', '100'),
                     event(6, 'transfer_send', transfer_id='bad-transfer', source='deriv', target='spot', amount='30')):
            with self.assertRaises(module.AccountingError):
                account.apply_event(item)
            self.assertEqual(complete_probe(account), before)
        account.apply_event(event(7, 'funding_post', seconds=9, settlement_id='memo-3'))
        account.apply_event(event(8, 'funding_post', seconds=9, settlement_id='memo-4'))
        self.assertEqual(account.view()['deriv_wallet_cash_signed'], '21')
        self.assertEqual(account.view()['pending_settlement_count'], 0)

    def test_sticky_ever_breach_survives_flat_restore_topup_blocks_reopen(self):
        account = module.CarryAccount('1000', '50', max_events=32)
        account.apply_event(event(0, 'mark', spot_mark='100', perp_mark='100'))
        account.apply_event(fill(1, 'spot_fill', 'buy', '1', '100'))
        account.apply_event(fill(2, 'perp_fill', 'sell', '1', '100'))
        account.apply_event(event(3, 'mark', spot_mark='100', perp_mark='150'))
        self.assertTrue(account.view()['perp_margin_breached'])
        account.apply_event(fill(4, 'perp_fill', 'buy', '1', '150'))
        self.assertFalse(account.view()['perp_margin_breached'])
        self.assertTrue(account.view()['ever_margin_breached'])
        resumed = module.CarryAccount.restore(account.snapshot())
        resumed.apply_event(event(5, 'transfer_send', transfer_id='topup', source='spot', target='deriv', amount='100'))
        resumed.apply_event(event(6, 'transfer_receive', transfer_id='topup'))
        resumed.apply_event(event(7, 'mark', spot_mark='100', perp_mark='100'))
        before = complete_probe(resumed)
        with self.assertRaisesRegex(module.AccountingError, 'permanently'):
            resumed.apply_event(fill(8, 'perp_fill', 'sell', '1', '100'))
        self.assertEqual(complete_probe(resumed), before)

    def test_explicit_synthetic_liquidation_debit_liability_exact_attribution(self):
        account = module.CarryAccount('1000', '50', max_events=16)
        account.apply_event(event(0, 'mark', spot_mark='100', perp_mark='100'))
        account.apply_event(fill(1, 'spot_fill', 'buy', '1', '100'))
        account.apply_event(fill(2, 'perp_fill', 'sell', '1', '100'))
        account.apply_event(event(3, 'mark', spot_mark='100', perp_mark='200'))
        account.apply_event(fill(4, 'liquidation_fill', 'buy', '1', '200', fee='.01', fixed_fee='5'))
        expected_wallet = Fraction(50) + 100 - 200 - 2 - 5
        self.assertEqual(rational(account.view()['deriv_wallet_cash_signed']), expected_wallet)
        self.assertEqual(rational(account.view()['deriv_debit_liability']), -expected_wallet)
        self.assertEqual(account.view()['liquidation_count'], 1)
        self.assertTrue(account.reconciliation()['identity_exact'])
        self.assertFalse(account.reconciliation()['closed_reconciliation'])

    def test_snapshot_full_replay_rejects_tampered_truncated_duplicate_and_v1(self):
        account = module.CarryAccount('10', '10', max_events=16)
        for i in range(5):
            account.apply_event(event(i, 'mark', spot_mark=str(i + 1), perp_mark=str(i + 1)))
        snapshot = account.snapshot()
        for mutation in ('state', 'truncate', 'duplicate', 'chain', 'v1', 'pending_intent'):
            altered = copy.deepcopy(snapshot)
            if mutation == 'state':
                altered['final_state']['nav'] = '9999'
            elif mutation == 'truncate':
                altered['complete_committed_events'] = altered['complete_committed_events'][1:]
                altered['committed_event_count'] -= 1
                altered['final_state']['committed_event_count'] -= 1
            elif mutation == 'duplicate':
                altered['complete_committed_events'].append(altered['complete_committed_events'][-1])
                altered['committed_event_count'] += 1
            elif mutation == 'chain':
                altered['journal_chain_sha256'] = '0' * 64
            elif mutation == 'v1':
                altered['schema'] = 'funding-account/1/snapshot'
            else:
                altered['incomplete_intent'] = True
            altered['snapshot_sha256'] = module.sha256_value({k: v for k, v in altered.items() if k != 'snapshot_sha256'})
            with self.assertRaises(module.AccountingError):
                module.CarryAccount.restore(altered)

    def test_per_event_no_history_deepcopy_or_status_map_scan(self):
        account = module.CarryAccount('1000', '1000', max_events=1000)
        account.apply_event(event(0, 'mark', spot_mark='100', perp_mark='100'))
        for i in range(1, 201):
            if i % 2:
                account.apply_event(event(i, 'transfer_send', transfer_id='old-' + str(i), source='spot', target='deriv', amount='1'))
            else:
                account.apply_event(event(i, 'transfer_receive', transfer_id='old-' + str(i - 1)))
        class NoScanDict(dict):
            def values(self):
                raise AssertionError('Historical status map scanned')
            def items(self):
                raise AssertionError('Historical status map scanned')
            def __iter__(self):
                raise AssertionError('Historical status map scanned')
        account.transfers = NoScanDict(account.transfers)
        account.settlements = NoScanDict(account.settlements)
        forbidden = {id(account), id(account.events), id(account.ledger), id(account._event_hashes),
                     id(account.transfers), id(account.settlements), id(account.spot_lots.records), id(account.perp_lots.records)}
        original = copy.deepcopy
        def guarded(value, *args, **kwargs):
            if id(value) in forbidden:
                raise AssertionError('Historical account/history/map/lotlist deep-copied')
            return original(value, *args, **kwargs)
        with patch.object(module.copy, 'deepcopy', side_effect=guarded):
            account.apply_event(event(201, 'mark', spot_mark='101', perp_mark='101'))
            account.view()
            account.reconciliation()
            account.apply_event(event(202, 'transfer_send', transfer_id='new', source='spot', target='deriv', amount='1'))
            account.apply_event(event(203, 'transfer_receive', transfer_id='new'))
        self.assertEqual(len(account.transfers), 101)
        self.assertEqual(len(account.events), 204)

    def test_exact_money_under_hostile_context_and_external_feedback_rejected(self):
        cash = '1234567890123456789012345678901234567890.1234567890123456789'
        q, p, slip, fee = '0.123456789012345678901234567890123456789', '123.987654321098765432109876543210987654', '.00012345', '.0003456'
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_DOWN
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            account = module.CarryAccount(cash, cash, max_events=256)
            account.apply_event(event(0, 'mark', spot_mark='123', perp_mark='123'))
            account.apply_event(fill(1, 'spot_fill', 'buy', q, p, slip=slip, fee=fee))
            account.apply_event(fill(2, 'perp_fill', 'sell', q, p, slip=slip, fee=fee))
            view = account.view()
        buy = rational(p) * (1 + rational(slip))
        sell = rational(p) * (1 - rational(slip))
        expected_fees = rational(q) * (buy + sell) * rational(fee)
        expected_NAV = 2 * rational(cash) + rational(q) * (sell - buy) - expected_fees
        self.assertEqual(rational(view['fees_total']), expected_fees)
        self.assertEqual(rational(view['nav']), expected_NAV)
        before = complete_probe(account)
        with self.assertRaises(module.AccountingError):
            account.apply_event(fill(3, 'spot_fill', 'buy', '1E61', '1'))
        self.assertEqual(complete_probe(account), before)


if __name__ == '__main__':
    unittest.main()
