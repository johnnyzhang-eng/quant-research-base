"""Small exact-answer controls for the conditional economic model, no network."""
from __future__ import annotations
import argparse
import csv
from fractions import Fraction as F
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spot_grid_economic_diagnostic_20261008_v0 as m


def bars(specs):
    result = []
    for i, spec in enumerate(specs):
        o, h, l, c = map(F, spec[:4])
        base, tb = map(F, spec[4:6]) if len(spec) > 4 else (F(0), F(0))
        result.append(m.Bar(i, 1577836800000 + i * 60000, o, h, l, c,
            base, base * o, 1, tb, tb * o))
    return result


FLAT = (100, 100, 100, 100, 0, 0)


def grid(specs, fee=25, path='OHLC'):
    sink = m.CaptureSink()
    model = m.Grid(bars(specs), fee, path, sink)
    summary = model.run()
    return model, summary, sink


class EconomicsControls(unittest.TestCase):
    def test_initial_buy_cost_and_hold_cash_comparison(self):
        data = bars([FLAT] * 3)
        for bps, expected_cash in [(0, '5000'), (10, '4995'), (25, '4987.5'), (100, '4950')]:
            gs, hs, cs = m.CaptureSink(), m.CaptureSink(), m.CaptureSink()
            g = m.Grid(data, bps, 'OHLC', gs).run()
            h = m.benchmark(data, bps, 'hold', hs)
            c = m.benchmark(data, bps, 'cash', cs)
            self.assertEqual(gs.minutes[0]['NAV_USDT'], '10000')
            self.assertEqual(gs.minutes[1]['cash_USDT'], expected_cash)
            self.assertEqual(gs.minutes[1]['BTC'], '50')
            self.assertEqual(gs.minutes[1]['cash_USDT'], hs.minutes[1]['cash_USDT'])
            self.assertEqual(gs.minutes[1]['fees_USDT'], hs.minutes[1]['fees_USDT'])
            self.assertEqual(g['fee_inclusive_terminal_NAV_USDT'], h['fee_inclusive_terminal_NAV_USDT'])
            self.assertEqual(c['fee_inclusive_terminal_NAV_USDT'], '10000')
            self.assertEqual(len(cs.fills), 0)

    def test_full_book_insufficient_gap_rejected_not_partial_grid(self):
        data = bars([FLAT, (50, 50, 50, 50, 0, 0), FLAT])
        with self.assertRaisesRegex(m.Guard, 'entire initial book'):
            m.preflight(data)
        sink = m.CaptureSink()
        with self.assertRaises(m.Guard):
            m.Grid(data, 25, 'OHLC', sink).run()
        self.assertEqual(sink.minutes, [])
        self.assertEqual(sink.fills, [])

    def test_warmup_init_and_submission_are_distinct(self):
        _, _, sink = grid([(100, 100, 100, 100, 0, 0), (100, 130, 70, 100, 10000, 5000), FLAT])
        self.assertEqual(len(sink.fills), 1)
        self.assertEqual(sink.fills[0]['fill_kind'], 'HYPOTHETICAL_INITIALIZATION_BUY')
        self.assertEqual(sink.fills[0]['bar_index'], 1)
        activated = [x for x in sink.transitions if x['transition'] == 'ACTIVATED']
        self.assertEqual(len(activated), 20)
        self.assertEqual({x['bar_index'] for x in activated}, {2})

    def test_quantity_price_rounding_and_initialization_not_nominal(self):
        data = bars([FLAT, (103, 103, 103, 103, 0, 0), FLAT])
        levels, qticks = m.geometry(data)
        expected_ticks = (F(5000) / (10 * F(103))) // F(1, 100000000)
        self.assertEqual(qticks, expected_ticks)
        self.assertLess(10 * qticks * m.Q * F(103), 5000)
        result = m.benchmark(data, 25, 'hold')
        n = 10 * expected_ticks * F(1, 100000000) * 103
        self.assertEqual(F(result['initialization_notional_USDT']), n)
        self.assertEqual(F(result['cash_USDT']), F(10000) - n * F(401, 400))
        odd = bars([(F('100.000000037'), F('100.000000037'), F('100.000000037'), F('100.000000037'), 0, 0), FLAT, FLAT])
        ls, _ = m.geometry(odd)
        self.assertEqual(ls[0], F('80.00000002'))

    def test_reservation_is_wallet_subset_not_extra_NAV(self):
        _, _, sink = grid([FLAT] * 3)
        row = sink.minutes[2]
        self.assertEqual(F(row['cash_reserved_USDT']), F('4461.125'))
        self.assertEqual(row['BTC_reserved'], '50')
        self.assertEqual(F(row['NAV_USDT']), F(row['cash_USDT']) + F(row['BTC']) * 100)
        self.assertEqual(row['NAV_USDT'], '9987.5')

    def test_one_budget_shared_across_levels(self):
        _, result, sink = grid([FLAT, FLAT, (100, 100, 94, 95, 700, 0)])
        fills = [x for x in sink.fills if x['fill_kind'] == 'CONDITIONAL_GRID']
        self.assertEqual([(x['side'], x['limit_price'], x['quantity']) for x in fills],
                         [('BUY', '98', '5'), ('BUY', '96', '2')])
        self.assertEqual(sink.minutes[2]['passive_buy_used_BTC'], '7')
        self.assertEqual(sink.minutes[2]['passive_sell_used_BTC'], '0')
        self.assertEqual(result['partials'], 1)
        self.assertEqual(result['BTC'], '57')

    def test_directional_volume_does_not_migrate_between_sides(self):
        _, result, sink = grid([FLAT, FLAT, (100, 104, 98, 100, 500, 500)])
        fills = [x for x in sink.fills if x['fill_kind'] == 'CONDITIONAL_GRID']
        self.assertEqual([(x['side'], x['limit_price'], x['quantity']) for x in fills], [('SELL', '102', '5')])
        self.assertEqual(sink.minutes[2]['passive_buy_budget_BTC'], '0')
        self.assertEqual(sink.minutes[2]['passive_sell_used_BTC'], '5')
        self.assertEqual(result['grid_fill_count'], 1)

    def test_partial_does_not_create_early_sell(self):
        _, result, sink = grid([FLAT, FLAT, (100, 100, 98, 98, 200, 0),
            (99, 100, 98, 99, 500, 500)])
        self.assertEqual(result['grid_fill_count'], 1)
        self.assertEqual(result['BTC'], '52')
        fill = sink.fills[1]
        self.assertEqual(fill['order_remaining_after'], '3')
        self.assertEqual(F(fill['fee_USDT']), F('0.49'))
        self.assertFalse(any(x['slot_id'] == 9 and x['transition'] == 'QUEUED' and x['bar_index'] > 1
                             for x in sink.transitions))

    def test_next_bar_full_completion_not_same_bar_roundtrip(self):
        _, result, sink = grid([FLAT, FLAT, (100, 104, 98, 100, 1000, 500)])
        fills = [x for x in sink.fills if x['fill_kind'] == 'CONDITIONAL_GRID']
        self.assertEqual({x['slot_id'] for x in fills}, {9, 10})
        self.assertEqual(result['grid_fill_count'], 2)
        queued = [x for x in sink.transitions if x['transition'] == 'QUEUED' and x['bar_index'] == 2]
        self.assertEqual({x['due_bar'] for x in queued}, {3})
        self.assertFalse(any(x['bar_index'] == 2 and x['order_created_bar'] == 2 for x in fills))

    def test_old_gap_uses_limit_without_free_price_improvement(self):
        _, result, sink = grid([FLAT, FLAT, FLAT, (90, 91, 89, 90, 500, 0)])
        fill = sink.fills[1]
        self.assertEqual(fill['phase'], 'OPEN_GAP')
        self.assertEqual(fill['model_execution_price'], '98')
        self.assertEqual(fill['phase_reference_price'], '90')
        self.assertEqual(fill['outside_bar_ohlc'], 1)
        self.assertEqual(result['outside_bar_ohlc_fills'], 1)

    def test_new_marketable_order_defers_not_maker_at_open(self):
        _, result, sink = grid([FLAT, FLAT, (100, 100, 98, 98, 500, 0),
                               (110, 110, 110, 110, 500, 500)])
        slot9_after = [x for x in sink.fills[1:] if x['slot_id'] == 9 and x['bar_index'] == 3]
        self.assertEqual(slot9_after, [])
        defer = [x for x in sink.transitions if x['slot_id'] == 9 and x['transition'] == 'DEFER_REASON']
        self.assertEqual(defer[0]['reason'], 'new_order_marketable')
        self.assertGreater(result['deferred_activation_attempts'], 0)

    def test_selfcross_equal_price_candidate_rejected(self):
        data = bars([FLAT] * 3)
        g = m.Grid(data, 25, 'OHLC')
        g.initialize(data[1])
        # A coherent finite synthetic inventory state for the activation guard,
        # not a claim that these prior fills were observed in this three-bar path.
        slot8 = next(x for x in g.pending.values() if x.slot == 8)
        slot8.side, slot8.limit = 'SELL', F(98)
        g.held[8] = g.qticks
        g.btc += g.qticks
        g.cash -= 5 * F(96) * F(401, 400)
        g.activate(data[2])  # SELL98 deferred marketable; BUY98 remains old active.
        nextbar = m.Bar(3, data[2].open_ms + 60000, F(97), F(99), F(96), F(97), F(0), F(0), 0, F(0), F(0))
        g.activate(nextbar)
        self.assertEqual(slot8.defer_reason, 'selfcross_or_equal_opposite')
        self.assertIn(slot8.identity, g.pending)
        self.assertNotIn(slot8.identity, g.active)

    def test_later_insufficient_reserve_defers_goal_without_resize(self):
        data = bars([FLAT] * 3)
        g = m.Grid(data, 25, 'OHLC')
        g.initialize(data[1])
        g.cash = F(100)  # Synthetic later-wallet activation guard, not initial run admission.
        g.activate(data[2])
        buys = [x for x in g.pending.values() if x.side == 'BUY']
        self.assertEqual(len(buys), 10)
        self.assertEqual({x.remaining for x in buys}, {g.qticks})
        self.assertEqual({x.defer_reason for x in buys}, {'insufficient_unreserved_cash'})
        self.assertEqual(g.cash_reserved, 0)

    def test_complete_roundtrip_both_fees_and_terminal_cost(self):
        data = [FLAT, FLAT, (100, 100, 98, 98, 500, 0), (99, 100, 90, 90, 500, 500)]
        _, result, sink = grid(data)
        self.assertEqual(result['cash_USDT'], '4995.025')
        self.assertEqual(result['BTC'], '50')
        self.assertEqual(result['ordinary_fees_USDT'], '14.975')
        self.assertEqual(result['marked_terminal_NAV_USDT'], '9495.025')
        self.assertEqual(result['hypothetical_exit_fee_USDT'], '11.25')
        self.assertEqual(result['fee_inclusive_terminal_NAV_USDT'], '9483.775')
        h = m.benchmark(bars(data), 25, 'hold')
        self.assertEqual(F(result['fee_inclusive_terminal_NAV_USDT']) - F(h['fee_inclusive_terminal_NAV_USDT']), F('7.525'))
        self.assertLess(F(result['fee_inclusive_terminal_NAV_USDT']), 10000)
        self.assertEqual(result['modeled_trade_count'], 3)
        self.assertEqual(len(sink.fills), 3)  # Exit is a valuation, not a hidden trade.
        self.assertFalse(result['actual_flat_confirmed'])

    def test_cancel_releases_reserves_preserves_nonflat_inventory(self):
        g, result, sink = grid([FLAT] * 3)
        self.assertEqual(g.cash_reserved, 0)
        self.assertEqual(g.btc_reserved, 0)
        self.assertEqual(result['BTC'], '50')
        cancels = [x for x in sink.transitions if x['transition'] == 'CANCEL_AT_END']
        self.assertEqual(len(cancels), 20)
        self.assertEqual({x['bar_index'] for x in cancels}, {3})
        self.assertEqual(F(result['fee_inclusive_terminal_NAV_USDT']), F('9975'))

    def test_exact_touch_is_counted_not_mislabelled_observed(self):
        _, result, sink = grid([FLAT, FLAT, (100, 100, 98, 98, 500, 0)])
        self.assertEqual(result['touch_only_fills'], 1)
        self.assertEqual(sink.fills[1]['touch_only'], 1)
        self.assertFalse(result['native_bot_replication'])

    def test_outside_range_old_book_continues_without_expansion(self):
        _, result, sink = grid([FLAT, FLAT, FLAT, (70, 71, 69, 70, 500, 0)])
        self.assertEqual(result['grid_levels_USDT'][0], '80')
        self.assertEqual(result['grid_levels_USDT'][-1], '120')
        self.assertEqual(result['outside_range_fills'], 1)
        self.assertEqual(result['fills_in_outside_range_bars'], 1)
        self.assertEqual(sink.fills[1]['model_execution_price'], '98')

    def test_subquantum_budget_no_fill_no_fee(self):
        _, result, sink = grid([FLAT, FLAT, (100, 100, 98, 98, F('0.00000099'), 0)])
        self.assertEqual(result['grid_fill_count'], 0)
        self.assertEqual(result['ordinary_fees_USDT'], '12.5')
        self.assertEqual(sink.minutes[2]['passive_buy_used_BTC'], '0')

    def test_economically_equal_paths_are_correlated_not_two_samples(self):
        data = [FLAT, FLAT, (100, 104, 98, 100, 1000, 500),
                (100, 106, 94, 100, 1000, 500), (100, 104, 96, 100, 1200, 600)]
        _, a, sa = grid(data, path='OHLC')
        _, b, sb = grid(data, path='OLHC')
        economic = ['cash_USDT', 'BTC', 'fees_USDT', 'NAV_USDT', 'peak_USDT', 'cash_reserved_USDT', 'BTC_reserved']
        for x, y in zip(sa.minutes, sb.minutes):
            self.assertEqual({k: x[k] for k in economic}, {k: y[k] for k in economic})
        self.assertEqual(a['fee_inclusive_terminal_NAV_USDT'], b['fee_inclusive_terminal_NAV_USDT'])
        self.assertNotEqual([x['phase'] for x in sa.fills], [x['phase'] for x in sb.fills])

    def test_fee_pressure_controls_no_omitted_side(self):
        data = [FLAT, FLAT, (100, 100, 98, 98, 500, 0), (99, 100, 99, 100, 500, 500)]
        values = []
        for bps in m.FEES:
            _, s, _ = grid(data, fee=bps)
            f = F(bps, 10000)
            # q5 BUY98/SELL100 roundtrip; initial50 and final50 incur both fees.
            expected = F(10000) - 5000 * f + 5 * ((1-f)*100 - (1+f)*98) - 5000 * f
            self.assertEqual(F(s['fee_inclusive_terminal_NAV_USDT']), expected)
            values.append(expected)
        self.assertEqual(values, sorted(values, reverse=True))

    def test_fee_known_answer_general_formula(self):
        # Independent quote-fee arithmetic, not market data or another strategy arm.
        f = F(25, 10000)
        cash = F(10000) - F(50*100) * (1+f)
        self.assertEqual(cash, F('4987.5'))
        cash -= F(98) * (1+f)
        cash += F(100) * (1-f)
        self.assertEqual(cash, F('4989.005'))
        self.assertEqual(cash + F(50*100)*(1-f), F('9976.505'))
        self.assertEqual((1-f)*100 - (1+f)*98, F('1.505'))

    def test_parser_clock_volume_and_finite_guards(self):
        rows = []
        for i in range(3):
            rows.append([str(1577836800000+i*60000), '100', '101', '99', '100', '10',
                str(1577836800000+i*60000+59999), '1000', '2', '4', '400', '0'])
        def encoded(r):
            stream = io.StringIO(); csv.writer(stream).writerows(r); return stream.getvalue().encode()
        parsed = m.parse_csv(encoded(rows), rows_expected=3, start_ms=1577836800000)
        self.assertEqual(parsed[0].budgets(), (6000000, 4000000))
        for index, col, wrong in [(1, 0, rows[0][0]), (1, 6, '0'), (1, 9, '11'),
                                   (1, 10, '1001'), (1, 1, 'NaN'), (1, 8, '-1')]:
            bad = [r[:] for r in rows]; bad[index][col] = wrong
            with self.assertRaises(m.Guard):
                m.parse_csv(encoded(bad), rows_expected=3, start_ms=1577836800000)
        with self.assertRaises(m.Guard):
            m.parse_csv(encoded(rows[:2]), rows_expected=3, start_ms=1577836800000)

    def test_wallet_event_and_minute_serialization_exact(self):
        _, _, sink = grid([FLAT, FLAT, (100, 100, 94, 95, 700, 0)])
        for row in sink.minutes:
            self.assertEqual(F(row['NAV_USDT']), F(row['cash_USDT']) + F(row['BTC']) * F(row['close']))
            self.assertLessEqual(F(row['cash_reserved_USDT']), F(row['cash_USDT']))
            self.assertEqual(F(row['drawdown_numerator'], row['drawdown_denominator']),
                             1 - F(row['NAV_USDT']) / F(row['peak_USDT']))
        for row in sink.fills:
            cost = F(row['notional_USDT']) + F(row['fee_USDT'])
            self.assertEqual(F(row['cash_before_USDT']) - F(row['cash_after_USDT']), cost)
            self.assertEqual(F(row['BTC_after']) - F(row['BTC_before']), F(row['quantity']))
        self.assertEqual(list(sink.minutes[0]), m.MINUTE_FIELDS)
        self.assertEqual(set(sink.fills[0]), set(m.FILL_FIELDS))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EconomicsControls)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.output:
        if not args.output.is_relative_to(m.PRIVATE / 'reports/spot-grid-economic-diagnostic-20261008-v0'):
            raise m.Guard('controls output outside owned private family')
        args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        m.write_json(args.output, {'schema':'conditional-grid-economic-controls/1',
            'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'producer_sha256':hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest(),
            'controls_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'protocol_sha256':m.PROTOCOL_SHA,'raw_market_data_used':False,
            'actual_orders':0,'network_requests':0,'paid_calls':0})
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
