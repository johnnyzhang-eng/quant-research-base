"""Invented controls of real protocol mappings; no provider contact or orders."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_base.execution.alpaca_paper import (AlpacaPaperAdapter, AlpacaPaperBinding,
    AlpacaPaperTransport, PaperHTTP, HOST, reconcile_paper_probe)
from research_base.official_paper_run import validate_config, run_paper, run_alpaca
from test_futu_paper import envelope, AT, LATER, FEE

ACCOUNT = '00000000-0000-0000-0000-000000000001'
ORDER = '00000000-0000-0000-0000-000000000002'
PAYLOAD = {'code': 'US.TEST', 'side': 'BUY', 'quantity': 1, 'limit_price': '10.00'}


def binding(**changes):
    return AlpacaPaperBinding(**dict({'acc_id': ACCOUNT, 'allowed_acc_ids': (ACCOUNT,), 'allowed_codes': ('US.TEST',)}, **changes))


class InventedHTTP:
    def __init__(self):
        self.peer = (HOST, 443)
        self.account = {'id': ACCOUNT, 'status': 'ACTIVE', 'currency': 'USD',
            'account_blocked': False, 'trading_blocked': False, 'trade_suspended_by_user': False,
            'cash': '100', 'equity': '100', 'long_market_value': '0', 'short_market_value': '0'}
        self.calls, self.orders, self.positions, self.activity_pages = [], [], [], [[]]
        self.order, self.lose_response, self.closed = None, False, False

    def peer_identity(self): return self.peer

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        if path == '/v2/account': return copy.deepcopy(self.account)
        if path == '/v2/clock': return {'is_open': not self.closed, 'timestamp': AT}
        if path == '/v2/positions': return copy.deepcopy(self.positions)
        if path.startswith('/v2/assets/'):
            return {'symbol': 'TEST', 'class': 'us_equity', 'status': 'active', 'tradable': True}
        if path == '/v2/account/activities': return copy.deepcopy(self.activity_pages.pop(0))
        if method == 'GET' and path == '/v2/orders': return copy.deepcopy(self.orders)
        if method == 'GET' and path.startswith('/v2/orders'): return copy.deepcopy(self.order)
        if method == 'DELETE': return None
        if method == 'POST':
            b = kwargs['body']
            self.order = {'id': ORDER, 'client_order_id': b['client_order_id'], 'symbol': b['symbol'],
                'side': b['side'], 'qty': b['qty'], 'limit_price': b['limit_price'], 'status': 'new',
                'filled_qty': '0', 'filled_avg_price': None, 'type': 'limit', 'order_class': 'simple'}
            self.orders = [self.order]
            if self.lose_response: raise TimeoutError('invented lost accepted response')
            return copy.deepcopy(self.order)
        raise AssertionError((method, path))


def config(action='submit'):
    return {'schema_version': 'official-paper-run/2', 'provider': 'alpaca',
        'binding': {'acc_id': ACCOUNT, 'allowed_acc_ids': [ACCOUNT], 'allowed_codes': ['US.TEST']},
        'envelope': vars(envelope()), 'state_file': 'alpaca/paper.sqlite', 'action': action,
        'intent': 'probe' if action != 'preflight' else None,
        'payload': PAYLOAD if action == 'submit' else None, 'fee_bound': FEE if action == 'submit' else None}


class AlpacaControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.http = InventedHTTP()
        self.transport = AlpacaPaperTransport(self.http, binding=binding(), envelope=envelope(), clock=lambda: AT)
        self.adapter = AlpacaPaperAdapter(self.root/'paper.sqlite', self.transport, binding=binding(), envelope=envelope(), clock=lambda: AT)

    def tearDown(self):
        self.adapter.close()
        self.tmp.cleanup()

    def submit(self): return self.adapter.submit('probe', PAYLOAD, at=AT, fee_bound=FEE)

    def test_fixed_paper_host_and_account_whitelist(self):
        for change in ({'peer_host': 'api.alpaca.markets'}, {'environment': 'LIVE'}, {'peer_port': True}, {'acc_id': '123'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                binding(**change).validate()
        self.http.peer = ('api.alpaca.markets', 443)
        with self.assertRaisesRegex(ValueError, 'ORIGIN'):
            self.submit()
        self.assertEqual(self.http.calls, [])

    def test_account_mismatch_or_unknown_flags_blocks_send(self):
        for field, value in [('id', ORDER), ('currency', 'HKD'), ('status', 'INACTIVE'), ('trading_blocked', None)]:
            original = self.http.account[field]
            self.http.account[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.submit()
            self.http.account[field] = original
        self.assertFalse(any(c[0] == 'POST' for c in self.http.calls))

    def test_actual_body_and_restart_lost_response_no_resend(self):
        self.http.lose_response = True
        self.assertEqual(self.submit()['state'], 'UNKNOWN')
        post = [c for c in self.http.calls if c[0] == 'POST']
        self.assertEqual(len(post), 1)
        body = post[0][2]['body']
        self.assertEqual((body['qty'], body['type'], body['time_in_force'], body['extended_hours']), ('1', 'limit', 'day', False))
        self.adapter.close()
        self.adapter = AlpacaPaperAdapter(self.root/'paper.sqlite', self.transport, binding=binding(), envelope=envelope(), clock=lambda: AT)
        self.assertEqual(self.adapter.recover('probe', at=LATER)['intent']['order_id'], ORDER)
        self.submit()
        self.assertEqual(sum(c[0] == 'POST' for c in self.http.calls), 1)

    def test_empty_query_preserves_cash_and_unknown(self):
        self.http.lose_response = True
        self.submit()
        self.http.order = None
        r = self.adapter.recover('probe', at=LATER)
        self.assertEqual(r['intent']['reserve_cash'], '11.00')
        self.assertEqual(r['intent']['state'], 'UNKNOWN')
        self.assertFalse(r['resend_allowed'])

    def test_partial_cancel_race_binds_terminal_only_after_query(self):
        self.submit()
        self.http.order.update(status='partially_filled', filled_qty='0.4', filled_avg_price='9.90')
        self.adapter.recover('probe', at=LATER)
        row = self.adapter.cancel('probe', at=LATER)
        self.assertEqual(row['state'], 'OPEN')
        self.assertEqual(row['cancel_state'], 'REQUEST_ACCEPTED')
        self.http.order.update(status='canceled')
        row = self.adapter.recover('probe', at=LATER)['intent']
        self.assertEqual((row['state'], row['filled'], row['reserve_cash']), ('TERMINAL', '0.4', '0'))
        self.assertIn('PAPER_FILL_FEES_AND_EXECUTIONS_UNVERIFIED', self.adapter.snapshot()['blockers'])

    def test_existing_position_cash_not_equity_and_truncation_block(self):
        self.http.positions = [{'symbol': 'TEST', 'qty': '1'}]
        with self.assertRaisesRegex(ValueError, 'CASH_UNKNOWN'): self.submit()
        self.http.positions = []
        self.http.account['equity'] = '101'
        with self.assertRaisesRegex(ValueError, 'CASH_UNKNOWN'): self.submit()
        self.http.account['equity'] = '100'
        self.http.orders = [{}] * 500
        with self.assertRaisesRegex(ValueError, 'COVERAGE_UNKNOWN'): self.submit()
        self.assertFalse(any(c[0] == 'POST' for c in self.http.calls))

    def test_closed_market_unknown_after_durable_intent_no_post(self):
        self.http.closed = True
        self.assertEqual(self.submit()['state'], 'UNKNOWN')
        self.assertFalse(any(c[0] == 'POST' for c in self.http.calls))

    def test_activity_pagination_repeat_rejected_not_complete(self):
        page = [{'id': str(i), 'activity_type': 'FILL'} for i in range(100)]
        self.http.activity_pages = [page, page]
        with self.assertRaisesRegex(ValueError, 'REPEATED'):
            self.transport.activities(order_id=ORDER)
        calls = [c for c in self.http.calls if c[1] == '/v2/account/activities']
        self.assertEqual(calls[1][2]['params']['page_token'], '99')

    def test_finite_runner_seals_without_claiming_official_acceptance(self):
        p = self.root/'config.json'
        p.write_text(json.dumps(config()))
        code, folder = run_paper(p, self.root, self.root/'evidence', transport=self.transport, observed_at=AT, clock=lambda: AT)
        self.assertEqual(code, 0)
        r = json.loads((folder/'result.json').read_text())
        self.assertFalse(r['official_chain_verified'])
        self.assertFalse(r['goal_complete'])
        self.assertEqual(r['durable_submission_intents_this_run'], 1)
        self.assertIn('alpaca_transport', json.loads((folder/'code-hashes.json').read_text()))

    def test_config_provider_and_secret_permissions_checked_before_network(self):
        c = config('preflight')
        self.assertIsInstance(validate_config(c)[0], AlpacaPaperBinding)
        c['provider'] = 'unknown'
        with self.assertRaises(ValueError): validate_config(c)
        p, secret = self.root/'config.json', self.root/'credentials.json'
        p.write_text(json.dumps(config('preflight')))
        secret.write_text('{}')
        secret.chmod(0o644)
        with self.assertRaisesRegex(ValueError, '0600'):
            run_alpaca(p, secret, self.root, self.root/'evidence')

    def test_import_and_http_initialization_do_not_connect(self):
        with patch('http.client.HTTPSConnection') as conn:
            PaperHTTP('invented-key', 'invented-secret')
            conn.assert_not_called()

    def test_redirect_not_followed_no_retry_no_error_body(self):
        response = unittest.mock.Mock(status=302)
        response.read.return_value = b'invented-key secret response'
        conn = unittest.mock.Mock()
        conn.getresponse.return_value = response
        with patch('http.client.HTTPSConnection', return_value=conn) as factory:
            with self.assertRaisesRegex(RuntimeError, '^PAPER_HTTP_STATUS_302$'):
                PaperHTTP('invented-key', 'invented-secret').request('GET', '/v2/account')
            self.assertEqual(factory.call_args.args[:2], (HOST, 443))
            context = factory.call_args.kwargs['context']
            self.assertTrue(context.check_hostname)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(conn.request.call_count, 1)
            conn.close.assert_called_once()

    def test_http_unapproved_operations_rejected_before_connection(self):
        with patch('http.client.HTTPSConnection') as conn:
            with self.assertRaises(ValueError): PaperHTTP('k', 's').request('DELETE', '/v2/orders')
            with self.assertRaises(ValueError): PaperHTTP('k', 's').request('GET', 'https://api.alpaca.markets/v2/account')
            conn.assert_not_called()

    def test_independent_fill_cash_position_observation_and_mutations(self):
        before = self.transport.snapshot(acc_id=ACCOUNT, trd_env='PAPER', at=AT)
        self.submit()
        self.http.order.update(status='filled', filled_qty='1', filled_avg_price='9.90')
        order = self.transport._order(self.http.order)
        after = copy.deepcopy(before)
        after['received_at'] = LATER
        after['raw_account']['cash'] = '90.10'
        after['raw_positions'] = [{'symbol': 'TEST', 'qty': '1', 'side': 'long'}]
        fills = {'rows': [{'id': 'invented-fill', 'activity_type': 'FILL', 'order_id': ORDER,
            'symbol': 'TEST', 'qty': '1', 'price': '9.90', 'side': 'buy', 'transaction_time': LATER}],
            'query_complete': True, 'scope': 'complete_account_interval', 'from_at': AT, 'until_at': LATER}
        r = reconcile_paper_probe(before, order, after, fills)
        self.assertTrue(r['matched'])
        self.assertFalse(r['actual_fees_verified'])
        for mutation in ('cash', 'position', 'duplicate', 'wrong_order', 'incomplete'):
            a, f = copy.deepcopy(after), copy.deepcopy(fills)
            if mutation == 'cash': a['raw_account']['cash'] = '90.11'
            if mutation == 'position': a['raw_positions'][0]['qty'] = '0.9'
            if mutation == 'duplicate': f['rows'] *= 2
            if mutation == 'wrong_order': f['rows'][0]['order_id'] = ACCOUNT
            if mutation == 'incomplete': f['query_complete'] = False
            with self.subTest(mutation=mutation):
                self.assertFalse(reconcile_paper_probe(before, order, a, f)['matched'])
