"""Operator entry point. Default is zero-network planning, never live trading.

The only order destination is the Spot Test Network. Production mode is a
separate GET-only deposit window, and never transfers funds. Adapter admission
is frozen after independent review; no environment-based key discovery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from . import binance_spot_testnet as adapter

# Filled only after source-specific independent acceptance, not by a CLI flag.
ADMITTED_ADAPTER_SHA256 = "64189cdeaaaccf52ac5e8446e2233c1c63a5d8a5d6da025577df923d3d734891"
FINITE_PROBE_ADMITTED = False
PROBE_JOURNAL_PATH = (Path.home() / '.local/state/quant-research/'
                      'binance-spot-testnet-probe-v0.sqlite')


def require_admitted_adapter():
    actual = hashlib.sha256(Path(adapter.__file__).read_bytes()).hexdigest()
    if actual != ADMITTED_ADAPTER_SHA256:
        raise adapter.GuardError('adapter has no matching independent admission')


def load_document(path: Path):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 16384:
            raise adapter.GuardError('bounded regular document required')
        data = json.loads(os.read(fd, 16385))
        if not isinstance(data, dict):
            raise adapter.GuardError('document object required')
        return data
    except (OSError, json.JSONDecodeError, UnicodeError):
        raise adapter.GuardError('document cannot be safely loaded') from None
    finally:
        if fd is not None:
            os.close(fd)


def require_wallet_readonly(restrictions):
    if not isinstance(restrictions, dict) or restrictions.get('enableReading') is not True:
        raise adapter.GuardError('wallet key does not report read permission')
    # Local GET-only transport is independent of server key permissions. Enforce
    # both; do not silently accept a more powerful key or change its permissions.
    forbidden = ('enableWithdrawals', 'enableSpotAndMarginTrading', 'enableFutures',
                 'enableInternalTransfer', 'permitsUniversalTransfer',
                 'enableMargin', 'enableVanillaOptions')
    for field in forbidden:
        if restrictions.get(field) is not False:
            raise adapter.GuardError('wallet key permissions not proven read-only')
    return {'read_permission_reported': True,
            'listed_financial_permissions_disabled': True,
            'local_transport_GET_only': True}


def check_recovery_journal(journal, manifest):
    row = journal.get()
    if row is None or row['post_used'] != 1 or row['manifest_hash'] != manifest.sha256:
        raise adapter.GuardError('recover requires an existing spent matching intent')


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('dry-run', 'auth-check', 'order-test', 'probe',
                                         'recover', 'wallet-window'), default='dry-run')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--keys', type=Path)
    parser.add_argument('--journal', type=Path)
    parser.add_argument('--approved-hash')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--cancel', action='store_true')
    args = parser.parse_args(argv)
    if args.mode == 'dry-run':
        if args.execute or args.cancel or args.keys is not None or args.approved_hash:
            raise adapter.GuardError('dry-run rejects execution flags and credentials')
        result = {'mode':'dry-run', 'network_calls':0, 'credentials_read':False,
                  'production_orders_or_transfers_possible':False,
                  'testnet_order_max_notional_USDT':'25',
                  'testnet_order_fee_included_in_cap':False,
                  'new_order_submission_budget':1, 'cancel_budget':1,
                  'continuous_trading_enabled':False,
                  'testnet_account_connected':False,
                  'production_wallet_connected':False,
                  'adapter_source_matches_reviewed_SHA':False,
                  'finite_probe_admitted':FINITE_PROBE_ADMITTED}
        result['adapter_source_matches_reviewed_SHA'] = (
            hashlib.sha256(Path(adapter.__file__).read_bytes()).hexdigest() == ADMITTED_ADAPTER_SHA256)
        if args.manifest:
            manifest = adapter.OrderManifest(**load_document(args.manifest))
            result.update({'manifest_sha256':manifest.sha256,
                           'manifest':manifest.document(),
                           'market_filters_verified_for_manifest':False})
        return result
    require_admitted_adapter()
    if args.mode in ('order-test','probe','recover') and not FINITE_PROBE_ADMITTED:
        raise adapter.GuardError('finite probe awaits separate journal admission')
    if not args.execute or args.keys is None:
        raise adapter.GuardError('explicit execution and dedicated private key file required')
    if args.mode == 'wallet-window':
        if args.manifest is None or args.cancel or args.journal is not None:
            raise adapter.GuardError('wallet-window requires a separate explicit window only')
        plan = load_document(args.manifest)
        fields = {'schema','base','coin','start_ms','end_ms','limit','max_pages'}
        if set(plan) != fields or plan['schema'] != 'wallet-readonly-window/1' or plan['base'] != adapter.PRODUCTION_WALLET:
            raise adapter.GuardError('wallet window schema or origin mismatch')
        if args.approved_hash != adapter.digest(plan):
            raise adapter.GuardError('exact wallet plan hash required')
        adapter.integer(plan['start_ms']); adapter.integer(plan['end_ms'])
        adapter.integer(plan['limit'], low=1, high=1000)
        adapter.integer(plan['max_pages'], low=1, high=3)
        adapter.asset_code(plan['coin'])
        if not 0 < plan['end_ms'] - plan['start_ms'] < 90*24*60*60*1000:
            raise adapter.GuardError('wallet window must be positive and under 90 days')
        key = adapter.load_credentials(args.keys, adapter.WALLET_SCOPE)
        client = adapter.GuardedClient(adapter.WALLET_SCOPE, key)
        client.sync_clock()
        reader = adapter.WalletReader(client)
        permissions = require_wallet_readonly(reader.restrictions())
        result = reader.deposit_window(plan['start_ms'], plan['end_ms'], coin=plan['coin'],
                                       limit=plan['limit'], max_pages=plan['max_pages'])
        return {'mode':args.mode, 'permissions':permissions, 'window':result,
                'continuous_monitor_running':False,'transfers_submitted':0}
    if args.cancel and args.mode not in ('probe','recover'):
        raise adapter.GuardError('cancel belongs only to finite probe or recovery')
    if args.mode == 'auth-check':
        if args.manifest is not None or args.journal is not None or args.approved_hash:
            raise adapter.GuardError('auth-check is a separate read-only mode')
        key = adapter.load_credentials(args.keys, adapter.TESTNET_SCOPE)
        client = adapter.GuardedClient(adapter.TESTNET_SCOPE, key)
        clock = client.sync_clock()
        account = client.account()
        totals = adapter.balances(account)
        return {'mode':args.mode,'origin':adapter.TESTNET,
                'signed_account_GET_received':True,
                'can_trade_reported':account.get('canTrade') is True,
                'balance_asset_count':len(totals),'balances_exposed':False,
                'clock_offset_ms':clock.offset_ms,
                'orders_submitted':0,'account_isolated_or_reconciled':False}
    if args.manifest is None or args.journal is None:
        raise adapter.GuardError('finite lifecycle requires manifest and private journal')
    if args.journal.absolute() != PROBE_JOURNAL_PATH.absolute():
        raise adapter.GuardError('finite runner uses one fixed private journal path')
    manifest = adapter.OrderManifest(**load_document(args.manifest))
    if args.approved_hash != manifest.sha256:
        raise adapter.GuardError('exact order manifest hash required')
    journal = adapter.Journal(args.journal)
    try:
        if args.mode == 'recover':
            check_recovery_journal(journal, manifest)
        key = adapter.load_credentials(args.keys, adapter.TESTNET_SCOPE)
        client = adapter.GuardedClient(adapter.TESTNET_SCOPE, key)
        client.sync_clock()
        runner = adapter.FiniteRunner(client,journal,manifest)
        if args.mode == 'order-test':
            result = runner.validate_order_test(execute=True, approved_hash=manifest.sha256)
        else:
            result = runner.run(execute=True, approved_hash=manifest.sha256,cancel=args.cancel)
        return {'mode':args.mode,'origin':adapter.TESTNET,'finite_result':result,
                'validation_only':args.mode=='order-test','real_money_orders':0,
                'automatic_close_of_residual_position':False}
    finally:
        journal.close()


def main():
    try:
        result = cli()
    except adapter.APIError as exc:
        # Never print raw request/URL/headers or remote error messages.
        print(json.dumps({'completed':False,'error_category':exc.category,
                          'http_status':exc.status,'api_code':exc.code,
                          'ambiguous':exc.ambiguous}))
        return 2
    except (adapter.GuardError, TypeError, ValueError, OSError):
        print(json.dumps({'completed':False,'local_guard_rejected':True}))
        return 2
    print(json.dumps(result,sort_keys=True,indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
