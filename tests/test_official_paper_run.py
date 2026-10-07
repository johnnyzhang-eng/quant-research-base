import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_base.evidence import ContractError, load_json, verify_run
from research_base.official_paper_run import local_readiness, run_paper, validate_config

AT = '2026-01-05T15:01:00+00:00'


def config(action='preflight'):
    return {'schema_version': 'official-paper-run/1', 'binding': {
        'peer_host': '127.0.0.1', 'peer_port': 11111, 'acc_id': 123,
        'allowed_acc_ids': [123], 'allowed_codes': ['US.CONTROL'],
        'market': 'US', 'environment': 'SIMULATE'}, 'envelope': {
        'max_quantity': 1, 'max_notional_usd': '100', 'max_limit_price_usd': '100',
        'max_pending_orders': 1, 'max_snapshot_age_seconds': 30,
        'submission_start': '2026-01-05T15:00:00+00:00',
        'submission_end': '2026-01-05T15:10:00+00:00'}, 'state_file': 'state/control.sqlite',
        'action': action, 'intent': None if action == 'preflight' else 'invented-control',
        'payload': {'code': 'US.CONTROL', 'side': 'BUY', 'quantity': 1, 'limit_price': '10.00'} if action == 'submit' else None,
        'fee_bound': {'amount_usd': '1.00', 'evidence': 'invented upper-bound control',
                      'known_at': '2026-01-05T15:00:00+00:00',
                      'valid_until': '2026-01-05T15:10:00+00:00'} if action == 'submit' else None}


class InventedTransport:
    def __init__(self): self.sent = 0; self.queries = 0
    def peer_identity(self): return ('127.0.0.1', 11111)
    def get_accounts(self):
        return [{'acc_id': 123, 'trd_env': 'SIMULATE', 'markets': ['US'],
                 'sim_acc_type': 'STOCK_AND_OPTION', 'acc_status': 'ACTIVE',
                 'raw': {'sim_acc_type': 'STOCK_AND_OPTION', 'acc_status': 'ACTIVE'}}]
    def snapshot(self, *, acc_id, trd_env, at):
        return {'observed_at': at, 'request_started_at': at, 'received_at': at,
                'snapshot_clock_basis': 'non_atomic_refreshed_request',
                'settled_cash_usd': '100', 'cash_evidence': 'invented settled control',
                'sellable': {}, 'positions_evidence': 'invented positions control',
                'open_orders_complete': True, 'open_orders': []}
    def place_order(self, **kwargs):
        self.sent += 1
        raise TimeoutError('invented lost response after server acceptance')
    def query_orders(self, **kwargs):
        self.queries += 1
        return []  # Eventual absence is not a resend permit.
    def cancel_order(self, **kwargs): raise AssertionError('Unknown order cannot cancel')


class PaperRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'config.json'
    def tearDown(self): self.tmp.cleanup()
    def write(self, value): self.path.write_text(json.dumps(value)); return self.path
    def test_local_inventory_cannot_be_official_even_when_sdk_found(self):
        self.write(config())
        with patch('research_base.official_paper_run.find_spec', return_value=object()):
            result = local_readiness(self.path, self.root)
        self.assertTrue(result['sdk_import_discoverable'])
        self.assertFalse(result['provider_contacted'])
        self.assertEqual(result['send_attempts'], 0)
        self.assertFalse(result['economic_reconciliation_verified'])
    def test_lost_submit_survives_restart_and_same_intent_never_resends(self):
        self.write(config('submit')); transport = InventedTransport()
        code, first = run_paper(self.path, self.root, self.root/'runs', transport=transport, observed_at=AT, clock=lambda: AT)
        self.assertEqual(code, 0)
        self.assertEqual(transport.sent, 1)
        self.assertEqual(load_json(first/'adapter-state.json')['intents'][0]['state'], 'UNKNOWN')
        code, second = run_paper(self.path, self.root, self.root/'runs', transport=transport, observed_at=AT, clock=lambda: AT)
        self.assertEqual(code, 0)
        self.assertEqual(transport.sent, 1)
        self.assertGreaterEqual(transport.queries, 1)
        result = load_json(second/'result.json')
        self.assertEqual(result['durable_submission_intents_this_run'], 0)
        self.assertFalse(result['official_chain_verified'])
        self.assertFalse(result['economic_reconciliation_verified'])
        self.assertTrue(verify_run(second, self.root/'runs/registry.jsonl')['verified'])
    def test_recovery_of_unknown_is_query_only(self):
        transport = InventedTransport()
        self.write(config('submit'))
        run_paper(self.path, self.root, self.root/'runs', transport=transport, observed_at=AT, clock=lambda: AT)
        self.write(config('recover'))
        code, folder = run_paper(self.path, self.root, self.root/'runs', transport=transport, observed_at=AT, clock=lambda: AT)
        self.assertEqual(code, 0)
        self.assertEqual(transport.sent, 1)
        self.assertEqual(load_json(folder/'result.json')['unresolved_intents'], 1)
    def test_closed_window_retains_attempt_without_sending(self):
        self.write(config('submit')); transport = InventedTransport()
        code, folder = run_paper(self.path, self.root, self.root/'runs', transport=transport,
                                observed_at='2026-01-06T15:01:00+00:00', clock=lambda: '2026-01-06T15:01:00+00:00')
        self.assertEqual(code, 1)
        self.assertEqual(transport.sent, 0)
        result = load_json(folder/'result.json')
        self.assertIn(result['errors'][0]['reason'], {'SUBMISSION_WINDOW_CLOSED', 'FEE_BOUND_NOT_KNOWN_OR_EXPIRED'})
        self.assertTrue(verify_run(folder, self.root/'runs/registry.jsonl')['verified'])
    def test_limits_real_environment_and_mixed_actions_rejected(self):
        for key, value in [('max_quantity', 2), ('max_pending_orders', 2),
                           ('submission_end', '2026-01-06T15:10:00+00:00')]:
            item = config(); item['envelope'][key] = value
            with self.subTest(key=key), self.assertRaises(ContractError): validate_config(item)
        item = config(); item['binding']['environment'] = 'REAL'
        with self.assertRaises(ContractError): validate_config(item)
        item = config(); item['payload'] = config('submit')['payload']
        with self.assertRaises(ContractError): validate_config(item)
    def test_symlink_and_traversal_rejected(self):
        self.write(config()); linked = self.root/'linked.json'; linked.symlink_to(self.path)
        with self.assertRaises(ContractError): local_readiness(linked, self.root)
        item = config(); item['state_file'] = '../escape.sqlite'
        with self.assertRaises(ContractError): validate_config(item)
        real = self.root/'real'; real.mkdir(); (self.root/'linked-state').symlink_to(real, target_is_directory=True)
        item = config(); item['state_file'] = 'linked-state/control.sqlite'; self.write(item)
        with self.assertRaises(ContractError):
            run_paper(self.path, self.root, self.root/'runs', transport=InventedTransport(), observed_at=AT)

    def test_registry_symlink_does_not_write_outside_private_root(self):
        self.write(config()); runs = self.root/'runs'; runs.mkdir()
        with tempfile.TemporaryDirectory() as other:
            target = Path(other)/'registry.jsonl'
            target.write_text('existing evidence\n')
            (runs/'registry.jsonl').symlink_to(target)
            with self.assertRaises(ContractError):
                run_paper(self.path, self.root, runs, transport=InventedTransport(), observed_at=AT, clock=lambda: AT)
            self.assertEqual(target.read_text(), 'existing evidence\n')

    def test_sqlite_sidecar_symlink_rejected_before_opening_database(self):
        self.write(config()); state = self.root/'state'; state.mkdir()
        with tempfile.TemporaryDirectory() as other:
            target = Path(other)/'unrelated-file'; target.write_text('untouched')
            (state/'control.sqlite-wal').symlink_to(target)
            with self.assertRaises(ContractError):
                run_paper(self.path, self.root, self.root/'runs', transport=InventedTransport(), observed_at=AT, clock=lambda: AT)
            self.assertFalse((state/'control.sqlite').exists())
            self.assertEqual(target.read_text(), 'untouched')


if __name__ == '__main__': unittest.main()
