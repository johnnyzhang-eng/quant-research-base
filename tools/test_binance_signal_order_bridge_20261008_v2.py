"""Focused producer controls for latest artificial risk; no producer-v1 rerun."""
import copy
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import ROUND_DOWN, localcontext
from fractions import Fraction as F
from pathlib import Path
from unittest.mock import patch

import binance_signal_order_bridge_20261008_v2 as b

NOW = 9_000_000
ASSERTIONS = 0


def fixture():
    return (
        {"mode": b.MODE, "protocol_sha256": b.PROTOCOL_SHA, "source_intent_id": "own-v2-artificial-A",
         "source_row_sha256": "a" * 64, "cutoff_ms": NOW - 2000, "eligible_ms": NOW - 1000,
         "schedule_rebalance": True, "planned_weights": {"BTC": "0.4", "ETH": "0.3"},
         "source_sell_assets": [], "source_buy_assets": ["BTCUSDT"], "historical_PIT_verified": False,
         "actual_known_at": None},
        {"mode": b.MODE, "portfolio_id": "own-v2-artificial", "asof_ms": NOW,
         "fixture_equity_USDT": "50", "available_cash_USDT": "50", "positions": {"BTC": "0", "ETH": "0"}, "pending_orders": []},
        {"mode": b.MODE, "symbol": "BTCUSDT", "reference_price": "100.17", "bidPrice": "100",
         "askPrice": "101.2", "asof_ms": NOW, "provenance": "ARTIFICIAL_FAKE"},
        {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "isSpotTradingAllowed": True,
            "filters": [{"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1", "stepSize": "0.001"},
                {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "25"}]}]},
        {"mode": b.MODE, "requested_pair": "BTCUSDT", "gross_quote_cap_USDT": "25", "capital_is_artificial": True,
         "fee_rate": "0.004", "slippage_rate": "0.003", "combined_friction_rate": "0.007", "ttl_ms": 1000},
        {"halted": False, "account_state_known": True})


class Controls(unittest.TestCase):
    def assertEqual(self, *a, **kw):
        global ASSERTIONS; ASSERTIONS += 1
        return super().assertEqual(*a, **kw)

    def assertTrue(self, *a, **kw):
        global ASSERTIONS; ASSERTIONS += 1
        return super().assertTrue(*a, **kw)

    def assertRaises(self, *a, **kw):
        global ASSERTIONS; ASSERTIONS += 1
        return super().assertRaises(*a, **kw)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="producer-v2-offline-")
        self.registries = []
        self.no_http = patch.object(b.adapter, "HTTPS", side_effect=AssertionError("HTTP forbidden"))
        self.no_http.start()

    def tearDown(self):
        self.no_http.stop()
        for r in self.registries:
            r.close()
        self.tmp.cleanup()

    def case(self, inputs=None):
        d = Path(self.tmp.name) / ("bridge-fake-" + str(len(self.registries)))
        d.mkdir(mode=0o700)
        r = b.FakeSignalRegistry(d / "fake-signal-registry.sqlite3"); self.registries.append(r)
        x = inputs or fixture()
        result = b.compile_position_deltas(*x, r, now_ms=NOW)
        self.assertEqual(result["status"], "READY_FAKE_BTC_PROJECTION")
        plan = result["plan"]; s = b.FakeScenario(plan)
        snapshot = b.fake_risk_snapshot(plan, s)
        return r, plan, s, b.FakeRiskProvider(plan, snapshot), snapshot

    def spent(self, r, plan):
        return r.db.execute("SELECT fake_dispatch_used FROM signals WHERE business_key=?", (plan["business_key"],)).fetchone()[0]

    def order_row(self, r, plan):
        p = r.path.parent / ("fake-order-" + plan["business_key"] + ".sqlite3")
        if not p.exists(): return None
        c = sqlite3.connect(p)
        try:
            return c.execute("SELECT post_used,state FROM intent").fetchone()
        finally:
            c.close()

    def execute(self, r, plan, s, provider, **kw):
        return b.execute_fake(plan, r, s, risk_provider=provider, **kw)

    def blocked_initial(self, update):
        r, plan, s, provider, snap = self.case()
        update(snap); provider.publish(snap)
        with self.assertRaises(b.RiskBlocked): self.execute(r, plan, s, provider)
        self.assertEqual(s.calls.get("POST /api/v3/order", 0), 0)
        self.assertEqual(self.spent(r, plan), 1)
        self.assertEqual(self.order_row(r, plan), None)
        with self.assertRaises(b.BridgeError): self.execute(r, plan, s, provider)

    def cash(self, plan, s, snap, value):
        text = b.decimal_string(F(value))
        s.baseline["balances"][1]["free"] = text
        s.current["balances"][1]["free"] = b.decimal_string(F(value) - F(plan["maximum_cash_debit_USDT"]))
        snap.update(available_cash_USDT=text, total_cash_USDT=text,
                    equity_USDT=b.decimal_string(F(value) + F(snap["positions"]["BTC"]) * F(snap["quote"]["reference_price"])))

    def test_good_fraction_budget_and_three_latest_fences(self):
        r, plan, s, provider, snap = self.case()
        before = copy.deepcopy(plan)
        gross = F('100.47') * F('.199'); fee = gross * F(1, 250)
        self.assertEqual(gross, F('19.99353')); self.assertEqual(fee, F('.07997412'))
        expected_nav = F(50) - gross - fee + F('.199') * F('100.17')
        self.assertEqual(expected_nav, F('49.86032588'))
        result = self.execute(r, plan, s, provider)
        self.assertEqual(s.calls['POST /api/v3/order'], 1)
        self.assertTrue(result['result']['reconciliation']['passed'])
        self.assertEqual([x['stage'] for x in result['risk_checks']], ['initial-after-grant', 'request-before-sign', 'after-sign-guard-before-transport'])
        self.assertEqual(F(result['risk_checks'][-1]['projected_equity_USDT']), expected_nav)
        self.assertEqual(plan, before)
        self.assertEqual(provider.reads, 3)

    def test_compile_then_halt_unknown_and_strict_boolean_rejections(self):
        for key, value in [('halted', True), ('halted', None), ('halted', 0), ('account_state_known', False), ('account_state_known', None), ('account_state_known', 1)]:
            with self.subTest(key=key, value=value):
                self.blocked_initial(lambda snap: snap.update({key: value}))

    def test_floor_and_equity_identity_are_not_declared_only(self):
        for value in ['30', '29.999999999999999999']:
            self.blocked_initial(lambda snap: snap.update(equity_USDT=value, total_cash_USDT=value, available_cash_USDT=value))
        self.blocked_initial(lambda snap: snap.update(equity_USDT='50', total_cash_USDT='20', available_cash_USDT='20'))
        self.blocked_initial(lambda snap: snap.update(initial_capital_USDT='51'))

    def test_projected_floor_exact_boundary_and_one_unit_good_control(self):
        for epsilon, allowed in [('0', False), ('0.000000000000000001', True)]:
            r, plan, s, provider, snap = self.case()
            qty, price, mark = map(F, [plan['quantity'], plan['order_manifest']['price'], snap['quote']['reference_price']])
            cost = qty * (price - mark) + qty * price * F(plan['fee_rate'])
            money = F(30) + cost + F(epsilon)
            self.cash(plan, s, snap, money); provider.publish(snap)
            self.assertTrue(F(snap['equity_USDT']) > 30)
            if allowed:
                out = self.execute(r, plan, s, provider)
                self.assertEqual(F(out['risk_checks'][-1]['projected_equity_USDT']), F(30) + F(epsilon))
                self.assertTrue(out['result']['reconciliation']['passed'])
            else:
                with self.assertRaises(b.RiskBlocked): self.execute(r, plan, s, provider)
                self.assertEqual(s.calls.get('POST /api/v3/order', 0), 0)
                self.assertEqual(self.spent(r, plan), 1)

    def test_risk_wallet_quote_time_stale_future_and_boundaries(self):
        for field in ['risk_asof_ms', 'wallet_asof_ms', 'quote_asof_ms']:
            for value in [NOW - 5001, NOW + 1, True]:
                self.blocked_initial(lambda snap: snap.update({field: value}))
        r, plan, s, provider, snap = self.case()
        for field in ['risk_asof_ms', 'wallet_asof_ms', 'quote_asof_ms']: snap[field] = NOW - 5000
        provider.publish(snap)
        self.assertTrue(self.execute(r, plan, s, provider)['result']['reconciliation']['passed'])

    def test_identity_pending_and_extra_assets_reject(self):
        for key, value in [('portfolio_id', 'different'), ('pair', 'ETHUSDT'), ('protocol_sha256', 'b' * 64),
                           ('cutoff_ms', NOW - 2001), ('pending_orders', ['unknown']), ('fee_asset', 'BTC')]:
            self.blocked_initial(lambda snap: snap.update({key: value}))
        self.blocked_initial(lambda snap: snap['positions'].update(ETH='0.001'))
        self.blocked_initial(lambda snap: snap['positions'].update(SOL='0'))
        self.blocked_initial(lambda snap: snap['positions'].update(BTC='0.001'))
        self.blocked_initial(lambda snap: snap.update(unknown_unit='cash'))

    def test_reaudit_changed_cash_quote_and_fee_never_rewrites_manifest(self):
        r, plan, s, provider, snap = self.case(); before = copy.deepcopy(plan)
        self.cash(plan, s, snap, '40')
        snap['quote']['reference_price'] = '100.2'; snap['quote']['askPrice'] = '102'; snap['fee_rate'] = '0.003'
        provider.publish(snap)
        out = self.execute(r, plan, s, provider)
        self.assertTrue(out['result']['reconciliation']['passed']); self.assertEqual(plan, before)
        for update in [lambda z: z.update(fee_rate='0.004000000000000001'),
                       lambda z: z.update(available_cash_USDT='20'),
                       lambda z: z['quote'].update(askPrice='100.47'),
                       lambda z: z['quote'].update(bidPrice='999'),
                       lambda z: z['quote'].update(reference_price='102')]: self.blocked_initial(update)

    def test_latest_mark_exposure_not_old_price(self):
        x = list(fixture()); x[0]['planned_weights'] = {'BTC': '0.5', 'ETH': '0'}
        r, plan, s, provider, snap = self.case(tuple(x))
        snap['quote']['reference_price'] = '101'; snap['quote']['askPrice'] = '102'
        provider.publish(snap)
        self.assertTrue(F(plan['quantity']) * F('101') > 25)
        with self.assertRaises(b.RiskBlocked): self.execute(r, plan, s, provider)
        self.assertEqual(s.calls.get('POST /api/v3/order', 0), 0)

    def late_hook(self, stage, change):
        r, plan, s, provider, snap = self.case()
        original = getattr(b.adapter, stage)
        def hook(*a, **kw):
            result = original(*a, **kw)
            is_post = stage == 'sign' and 'newOrderRespType=' in a[1] or stage == 'guard_request' and a[1].method == 'POST'
            if is_post:
                change(s, snap); provider.publish(snap)
            return result
        with patch.object(b.adapter, stage, hook):
            with self.assertRaises(b.adapter.GuardError): self.execute(r, plan, s, provider)
        self.assertEqual(s.calls.get('POST /api/v3/order', 0), 0)
        self.assertEqual(self.order_row(r, plan)[0], 1)
        self.assertEqual(self.spent(r, plan), 1)
        # Invalid/halted latest observations do not obstruct query-only recovery.
        reads = provider.reads
        for _ in range(2): self.execute(r, plan, s, provider, recover=True)
        self.assertEqual(provider.reads, reads)
        self.assertEqual(s.calls['GET /api/v3/order'], 2)
        self.assertEqual(s.calls.get('POST /api/v3/order', 0), 0)

    def test_sign_and_guard_late_halt_or_unknown_remain_local(self):
        for stage in ['sign', 'guard_request']:
            self.late_hook(stage, lambda s, z: z.update(halted=True))
            self.late_hook(stage, lambda s, z: z.update(account_state_known=None))
        self.assertTrue(issubclass(b.RiskBlocked, b.adapter.GuardError))

    def test_sign_and_guard_late_cash_quote_floor_or_clock(self):
        changes = [lambda s, z: z.update(available_cash_USDT='19'),
                   lambda s, z: z['quote'].update(askPrice='1'),
                   lambda s, z: z.update(equity_USDT='30', total_cash_USDT='30', available_cash_USDT='30'),
                   lambda s, z: z.update(risk_asof_ms=NOW - 1),
                   lambda s, z: z.update(wallet_asof_ms=NOW + 1)]
        for change in changes:
            for stage in ['sign', 'guard_request']: self.late_hook(stage, change)

    def test_late_wallet_evidence_mismatch_and_extra_api_asset(self):
        self.late_hook('sign', lambda s, z: z.update(total_cash_USDT='40', available_cash_USDT='40', equity_USDT='40'))
        r, plan, s, provider, snap = self.case()
        s.baseline['balances'].append({'asset': 'ETH', 'free': '0.01', 'locked': '0'})
        with self.assertRaises(b.adapter.GuardError): self.execute(r, plan, s, provider)
        self.assertEqual(s.calls.get('POST /api/v3/order', 0), 0)

    def test_expiry_equality_and_sign_guard_crossing(self):
        r, plan, s, provider, snap = self.case(); s.now = plan['order_manifest']['expires_ms']
        out = self.execute(r, plan, s, provider)
        self.assertEqual(out['post_dispatch_ms'], [s.now])
        for stage in ['sign', 'guard_request']:
            self.late_hook(stage, lambda s, z: setattr(s, 'now', s.manifest.expires_ms + 1))

    def test_hostile_decimal_context_does_not_round_projected_floor(self):
        r, plan, s, provider, snap = self.case()
        qty, price, mark = map(F, [plan['quantity'], plan['order_manifest']['price'], snap['quote']['reference_price']])
        cost = qty * (price - mark) + qty * price * F(plan['fee_rate'])
        self.cash(plan, s, snap, F(30) + cost); provider.publish(snap)
        with localcontext() as ctx:
            ctx.prec = 2; ctx.rounding = ROUND_DOWN
            with self.assertRaises(b.RiskBlocked): self.execute(r, plan, s, provider)
        self.assertEqual(s.calls.get('POST /api/v3/order', 0), 0)

    def test_fresh_process_once_dedup_and_durable_clock_recovery(self):
        r, plan, s, provider, snap = self.case()
        original = b.adapter.sign
        def halt(secret, encoded):
            out = original(secret, encoded)
            if 'newOrderRespType=' in encoded:
                snap['halted'] = True; provider.publish(snap)
            return out
        with patch.object(b.adapter, 'sign', halt):
            with self.assertRaises(b.RiskBlocked): self.execute(r, plan, s, provider)
        clocks = json.loads(r.db.execute('SELECT risk_clocks_json FROM signals').fetchone()[0])
        self.assertEqual(clocks, {'risk_ms': NOW, 'wallet_ms': NOW, 'quote_ms': NOW, 'read_ms': NOW})
        child = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import binance_signal_order_bridge_20261008_v2 as b
b.adapter.HTTPS=lambda *a,**k: (_ for _ in ()).throw(AssertionError('HTTP forbidden'))
r=b.FakeSignalRegistry(Path(sys.argv[2])); plan=list(r.snapshot().values())[0]['plan']; s=b.FakeScenario(plan)
p=b.FakeRiskProvider(plan, {})
blocked=False
try: b.execute_fake(plan,r,s,risk_provider=p)
except b.BridgeError: blocked=True
out=b.execute_fake(plan,r,s,risk_provider=p,recover=True)
print(json.dumps({'blocked':blocked,'routes':dict(s.calls),'reads':p.reads,'post_used':out['result']['new_order_post_reservations']}))
r.close()
'''
        done = subprocess.run([sys.executable, '-c', child, str(Path(b.__file__).parent), str(r.path)], capture_output=True, text=True, check=True)
        out = json.loads(done.stdout)
        self.assertTrue(out['blocked']); self.assertEqual(out['reads'], 0)
        self.assertEqual(out['routes'], {'GET /api/v3/order': 1}); self.assertEqual(out['post_used'], 1)
        repeated = b.compile_position_deltas(*fixture(), r, now_ms=NOW)
        self.assertEqual(repeated['status'], 'REUSE_NO_NEW_ORDER'); self.assertEqual(repeated['new_order_permission'], False)
        self.assertEqual(repeated['plan'], plan)

    def test_quote_drift_equality_and_exact_fee_inclusive_available_cash(self):
        r, plan, s, provider, snap = self.case()
        exact = F(plan['frozen_reference_price']) * F(101, 100)
        snap['quote']['reference_price'] = b.decimal_string(exact)
        snap['quote']['askPrice'] = '103'; provider.publish(snap)
        self.assertTrue(self.execute(r, plan, s, provider)['result']['reconciliation']['passed'])
        r, plan, s, provider, snap = self.case()
        cash = F(plan['maximum_cash_debit_USDT']); locked = F(50) - cash
        s.baseline['balances'][1].update(free=b.decimal_string(cash), locked=b.decimal_string(locked))
        s.current['balances'][1].update(free='0', locked=b.decimal_string(locked))
        snap['available_cash_USDT'] = b.decimal_string(cash); provider.publish(snap)
        self.assertTrue(self.execute(r, plan, s, provider)['result']['reconciliation']['passed'])
        self.blocked_initial(lambda z: z['quote'].update(reference_price=b.decimal_string(exact + F('0.000000000000000001'))))

    def test_wallet_malformed_values_and_clock_row_integrity(self):
        for field, value in [('free', '-1'), ('locked', None), ('asset', ['BTC'])]:
            r, plan, s, provider, snap = self.case()
            s.baseline['balances'][0][field] = value
            with self.assertRaises(b.adapter.GuardError): self.execute(r, plan, s, provider)
            self.assertEqual(s.calls.get('POST /api/v3/order', 0), 0)
        r, plan, s, provider, snap = self.case()
        self.execute(r, plan, s, provider)
        earlier = {'risk_ms': NOW - 1, 'wallet_ms': NOW, 'quote_ms': NOW, 'read_ms': NOW}
        with self.assertRaises(b.RiskBlocked): r.commit_risk_clocks(plan, earlier)
        r.db.execute("UPDATE signals SET risk_clocks_json='null'")
        with self.assertRaises(b.BridgeError): r.snapshot()


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Controls)
    stream = io.StringIO(); result = unittest.TextTestRunner(stream=stream).run(suite)
    print(json.dumps({'methods_run': result.testsRun, 'assertion_calls': ASSERTIONS, 'failures': len(result.failures),
        'errors': len(result.errors), 'skips': len(result.skipped), 'detail': stream.getvalue()}))
    sys.exit(not result.wasSuccessful())
