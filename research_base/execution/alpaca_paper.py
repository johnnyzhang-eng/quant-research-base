"""Fixed-origin Alpaca PAPER transport. No network activity on import/init.

Account.cash is virtual paper cash, not actual settled securities cash. The
default send preflight is deliberately limited to a flat, unlevered paper
account. Subsequent economic verification is a separate observation, never a
license to clear an unknown order, infer actual fees, or send again.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import http.client
import json
import re
import ssl
from urllib.parse import urlencode
from uuid import UUID

from .futu_paper import DurablePaperAdapter, _money, _time, canonical

HOST = 'paper-api.alpaca.markets'
ORIGIN = 'https://' + HOST


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('CANONICAL_ACCOUNT_OR_ORDER_UUID_REQUIRED')
    return value


@dataclass(frozen=True)
class AlpacaPaperBinding:
    acc_id: str
    allowed_acc_ids: tuple[str, ...]
    allowed_codes: tuple[str, ...]
    peer_host: str = HOST
    peer_port: int = 443
    market: str = 'US'
    environment: str = 'PAPER'

    def validate(self):
        if (self.peer_host, self.peer_port, self.market, self.environment) != (HOST, 443, 'US', 'PAPER') or type(self.peer_port) is not int:
            raise ValueError('ONLY_FIXED_ALPACA_PAPER_ORIGIN_SUPPORTED')
        _uuid(self.acc_id)
        if self.acc_id not in self.allowed_acc_ids or not self.allowed_acc_ids or len(set(self.allowed_acc_ids)) != len(self.allowed_acc_ids):
            raise ValueError('EXPLICIT_PAPER_ACCOUNT_WHITELIST_REQUIRED')
        for value in self.allowed_acc_ids:
            _uuid(value)
        if not self.allowed_codes or len(set(self.allowed_codes)) != len(self.allowed_codes) or any(not isinstance(c, str) or not re.fullmatch(r'US\.[A-Z][A-Z0-9.\-]{0,14}', c) for c in self.allowed_codes):
            raise ValueError('EXPLICIT_EQUITY_CODE_WHITELIST_REQUIRED')
        return self


class PaperHTTP:
    """Verified TLS, fixed host, no proxy, redirects, retries or error body logs."""
    def __init__(self, key_id, secret_key):
        if any(not isinstance(x, str) or not x or '\n' in x or '\r' in x for x in (key_id, secret_key)):
            raise ValueError('EXPLICIT_PRIVATE_PAPER_CREDENTIALS_REQUIRED')
        self._headers = {'APCA-API-KEY-ID': key_id, 'APCA-API-SECRET-KEY': secret_key}

    def peer_identity(self):
        return HOST, 443

    def request(self, method, path, *, params=None, body=None, absent_ok=False):
        allowed_get = {'/v2/account', '/v2/clock', '/v2/positions', '/v2/orders',
                       '/v2/orders:by_client_order_id', '/v2/account/activities'}
        order_path = bool(re.fullmatch(r'/v2/orders/[0-9a-f\-]{36}', path))
        asset_path = bool(re.fullmatch(r'/v2/assets/[A-Z][A-Z0-9.\-]{0,14}', path))
        if not ((method == 'GET' and (path in allowed_get or order_path or asset_path)) or
                (method == 'POST' and path == '/v2/orders') or (method == 'DELETE' and order_path)):
            raise ValueError('PAPER_REQUEST_NOT_WHITELISTED')
        if absent_ok and not (method == 'GET' and (order_path or path == '/v2/orders:by_client_order_id')):
            raise ValueError('ABSENCE_ALLOWED_ONLY_FOR_ORDER_LOOKUP')
        target = path + ('?' + urlencode(params) if params else '')
        headers = dict(self._headers, Accept='application/json')
        encoded = None if body is None else canonical(body).encode()
        if encoded is not None:
            headers['Content-Type'] = 'application/json'
        conn = http.client.HTTPSConnection(HOST, 443, timeout=15, context=ssl.create_default_context())
        try:
            conn.request(method, target, body=encoded, headers=headers)
            response = conn.getresponse()
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError('PAPER_RESPONSE_TOO_LARGE')
            if response.status == 404 and absent_ok:
                return None
            if not 200 <= response.status < 300:
                raise RuntimeError('PAPER_HTTP_STATUS_' + str(response.status))
            return None if response.status == 204 else json.loads(data)
        finally:
            conn.close()


class AlpacaPaperTransport:
    def __init__(self, http, *, binding, envelope=None, clock=None):
        self.http, self.binding = http, binding.validate()
        self.envelope = None if envelope is None else envelope.validate()
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    def peer_identity(self):
        return self.http.peer_identity()

    def _bound(self, acc_id, trd_env):
        if acc_id != self.binding.acc_id or trd_env != 'PAPER' or self.peer_identity() != (HOST, 443):
            raise ValueError('ALPACA_PAPER_BINDING_CHANGED')
        account = self.http.request('GET', '/v2/account')
        if not isinstance(account, dict) or account.get('id') != acc_id or account.get('status') != 'ACTIVE' or account.get('currency') != 'USD':
            raise ValueError('ALPACA_ACCOUNT_ID_STATUS_OR_CURRENCY_MISMATCH')
        if any(account.get(k) is not False for k in ('account_blocked', 'trading_blocked', 'trade_suspended_by_user')):
            raise ValueError('ALPACA_ACCOUNT_BLOCK_FLAGS_UNKNOWN_OR_TRUE')
        if self.peer_identity() != (HOST, 443):
            raise ValueError('PAPER_ORIGIN_CHANGED_DURING_QUERY')
        return account

    def get_accounts(self):
        account = self._bound(self.binding.acc_id, 'PAPER')
        return [{'acc_id': account['id'], 'environment': 'PAPER', 'provider': 'alpaca', 'raw': account}]

    def _order(self, raw):
        if not isinstance(raw, dict):
            raise ValueError('ALPACA_ORDER_OBJECT_REQUIRED')
        oid = _uuid(raw.get('id'))
        filled = _money(raw.get('filled_qty'))
        status = raw.get('status')
        mapped = {'new': 'SUBMITTED', 'accepted': 'SUBMITTED', 'pending_new': 'SUBMITTING',
                  'accepted_for_bidding': 'SUBMITTED', 'partially_filled': 'FILLED_PART',
                  'filled': 'FILLED_ALL', 'pending_cancel': 'CANCELLING_ALL',
                  'rejected': 'FAILED', 'expired': 'DISABLED'}.get(status)
        if status == 'canceled':
            mapped = 'CANCELLED_PART' if filled > 0 else 'CANCELLED_ALL'
        # Pending replacements and all unknown states require reconciliation.
        return {'order_id': oid, 'remark': raw.get('client_order_id'),
                'code': 'US.' + str(raw.get('symbol')), 'side': str(raw.get('side')).upper(),
                'quantity': raw.get('qty'), 'limit_price': raw.get('limit_price'),
                'status': mapped or 'UNMAPPED_ALPACA_STATUS', 'dealt_quantity': str(filled),
                'dealt_avg_price': raw.get('filled_avg_price') if filled else '0',
                'provider': 'alpaca_paper', 'raw': raw}

    def snapshot(self, *, acc_id, trd_env, at):
        started = self.clock()
        account = self._bound(acc_id, trd_env)
        positions = self.http.request('GET', '/v2/positions')
        orders = self.http.request('GET', '/v2/orders', params={'status': 'open', 'limit': 500, 'nested': 'false'})
        market_clock = self.http.request('GET', '/v2/clock')
        if not isinstance(orders, list) or len(orders) >= 500 or not isinstance(positions, list):
            raise ValueError('PAPER_OPEN_ORDER_OR_POSITION_COVERAGE_UNKNOWN')
        assets = {}
        for code in self.binding.allowed_codes:
            symbol = code[3:]
            asset = self.http.request('GET', '/v2/assets/' + symbol)
            if not isinstance(asset, dict) or asset.get('symbol') != symbol or asset.get('class') != 'us_equity' or asset.get('status') != 'active' or asset.get('tradable') is not True:
                raise ValueError('ALLOWED_US_EQUITY_NOT_TRADABLE')
            assets[code] = asset
        received = self.clock()
        # Only a flat paper account may supply virtual cash to the first probe.
        cash = None
        if not positions and not orders and _money(account.get('cash')) == _money(account.get('equity')) and _money(account.get('long_market_value')) == 0 and _money(account.get('short_market_value')) == 0:
            cash = account['cash']
        return {'request_started_at': started, 'received_at': received,
                'snapshot_clock_basis': 'non_atomic_refreshed_request',
                'settled_cash_usd': cash,
                'cash_evidence': 'flat unlevered PAPER virtual cash; not actual settled securities cash' if cash is not None else None,
                'sellable': {}, 'positions_evidence': None,
                'open_orders_complete': True,
                'open_orders': [self._pending(o) for o in orders],
                'raw_account': account, 'raw_positions': positions, 'raw_orders': orders,
                'market_clock': market_clock, 'assets': assets,
                'scope': 'FIRST_FLAT_PAPER_ACCOUNT_PROBE_ONLY'}

    def _pending(self, raw):
        order = self._order(raw)
        remaining = _money(order['quantity']) - _money(order['dealt_quantity'])
        if remaining < 0 or remaining != remaining.to_integral_value() or raw.get('type') != 'limit' or raw.get('order_class') not in ('simple', ''):
            raise ValueError('PAPER_PENDING_EXPOSURE_UNKNOWN')
        return dict(order, remaining_quantity=int(remaining), reserved_cash_usd=None)

    def place_order(self, *, acc_id, trd_env, remark, code, side, quantity, limit_price):
        self._bound(acc_id, trd_env)
        if code not in self.binding.allowed_codes or side != 'BUY' or type(quantity) is not int or quantity != 1 or not re.fullmatch(r'qr-[0-9a-f]{48}', remark):
            raise ValueError('FIRST_PAPER_PROBE_ONE_WHITELISTED_BUY_REQUIRED')
        price = _money(limit_price)
        if price <= 0 or price != price.quantize(Decimal('.01')):
            raise ValueError('POSITIVE_CENT_LIMIT_REQUIRED')
        if self.envelope is None or quantity > self.envelope.max_quantity or price > _money(self.envelope.max_limit_price_usd) or quantity * price > _money(self.envelope.max_notional_usd):
            raise ValueError('FINITE_TRANSPORT_RISK_ENVELOPE_REQUIRED')
        clock = self.http.request('GET', '/v2/clock')
        if not isinstance(clock, dict) or clock.get('is_open') is not True or not 0 <= (_time(self.clock()) - _time(clock.get('timestamp'))).total_seconds() <= 30:
            raise ValueError('FRESH_OPEN_REGULAR_MARKET_REQUIRED')
        if not _time(self.envelope.submission_start) <= _time(self.clock()) <= _time(self.envelope.submission_end):
            raise ValueError('SUBMISSION_WINDOW_CLOSED_AT_HTTP_SEND')
        raw = self.http.request('POST', '/v2/orders', body={'symbol': code[3:], 'qty': str(quantity),
            'side': 'buy', 'type': 'limit', 'time_in_force': 'day', 'limit_price': str(price),
            'extended_hours': False, 'order_class': 'simple', 'client_order_id': remark})
        return {'order': self._order(raw)}

    def query_orders(self, *, acc_id, trd_env, remark, order_id, since, at):
        self._bound(acc_id, trd_env)
        if order_id:
            raw = self.http.request('GET', '/v2/orders/' + _uuid(order_id), absent_ok=True)
        else:
            raw = self.http.request('GET', '/v2/orders:by_client_order_id', params={'client_order_id': remark}, absent_ok=True)
        return [] if raw is None else [self._order(raw)]

    def cancel_order(self, *, acc_id, trd_env, order_id):
        self._bound(acc_id, trd_env)
        self.http.request('DELETE', '/v2/orders/' + _uuid(order_id))
        return {'request_accepted': True, 'terminal_cancel_verified': False}

    def activities(self, *, order_id, all_account=False, from_at=None, until_at=None):
        """Finite paginated provider evidence. Empty is not proof of zero fees."""
        self._bound(self.binding.acc_id, 'PAPER')
        if all_account and (from_at is None or until_at is None or _time(from_at) >= _time(until_at)):
            raise ValueError('FINITE_ACCOUNT_ACTIVITY_INTERVAL_REQUIRED')
        result, seen, token = [], {}, None
        for _ in range(10):
            params = {'direction': 'asc', 'page_size': 100}
            if not all_account:
                params['order_id'] = _uuid(order_id)
            else:
                params.update(after=from_at, until=until_at)
            if token:
                params['page_token'] = token
            page = self.http.request('GET', '/v2/account/activities', params=params)
            if not isinstance(page, list) or len(page) > 100:
                raise ValueError('INVALID_PAPER_ACTIVITY_PAGE')
            for row in page:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in seen:
                    raise ValueError('REPEATED_OR_INVALID_ACTIVITY_ID')
                seen[row['id']] = row
                result.append(row)
            if len(page) < 100:
                return {'rows': result, 'query_complete': True, 'actual_regulatory_fees_verified': False,
                        'scope': 'complete_account_interval' if all_account else 'single_order',
                        'from_at': from_at, 'until_at': until_at}
            token = page[-1]['id']
        raise ValueError('PAPER_ACTIVITY_PAGE_LIMIT_NO_COMPLETENESS')


class AlpacaPaperAdapter(DurablePaperAdapter):
    def _guard(self, at):
        _time(at)
        if self.transport.peer_identity() != (HOST, 443):
            raise ValueError('FIXED_PAPER_ORIGIN_CHANGED')
        accounts = self.transport.get_accounts()
        with self._tx():
            self._record('ACCOUNTS', None, at, accounts)
        if self.transport.peer_identity() != (HOST, 443) or len(accounts) != 1 or accounts[0].get('acc_id') != self.binding.acc_id or accounts[0].get('environment') != 'PAPER' or accounts[0].get('provider') != 'alpaca':
            raise ValueError('ALPACA_PAPER_ACCOUNT_NOT_BOUND')


def reconcile_paper_probe(before, order, after, activities):
    """Independent comparison for one flat-start PAPER buy, no fee assumption.

    Requires a complete account activity interval (including non-trade cash
    entries), unchanged account identity, and post-query order/position/cash
    observations. The snapshot is non-atomic; matching is a bounded observation,
    not official acceptance, a settlement certificate or real execution proof.
    """
    result = {'scope': 'ONE_FLAT_START_PAPER_BUY_OBSERVATION', 'matched': False,
              'official_chain_verified': False, 'actual_fees_verified': False,
              'goal_complete': False, 'issues': []}
    try:
        account0, account1 = before['raw_account'], after['raw_account']
        if account0['id'] != account1['id'] or before['raw_positions'] or before['raw_orders']:
            raise ValueError('PROBE_NOT_SAME_FLAT_START_ACCOUNT')
        if order['side'] != 'BUY' or _money(order['quantity']) != 1 or order['status'] not in {'FILLED_ALL', 'CANCELLED_ALL', 'CANCELLED_PART', 'FAILED', 'DISABLED'}:
            raise ValueError('TERMINAL_PAPER_BUY_REQUIRED')
        if activities.get('query_complete') is not True or activities.get('scope') != 'complete_account_interval':
            raise ValueError('COMPLETE_ACCOUNT_INTERVAL_REQUIRED')
        if activities.get('from_at') != before['request_started_at'] or activities.get('until_at') != after['received_at']:
            raise ValueError('ACTIVITY_AND_ACCOUNT_INTERVAL_MISMATCH')
        qty, notional, cash_delta, seen = Decimal(0), Decimal(0), Decimal(0), set()
        for row in activities['rows']:
            if not isinstance(row.get('id'), str) or row['id'] in seen:
                raise ValueError('DUPLICATE_ACTIVITY_ID')
            seen.add(row['id'])
            if row['activity_type'] == 'FILL':
                if row['order_id'] != order['order_id'] or 'US.' + row['symbol'] != order['code'] or row['side'] != 'buy':
                    raise ValueError('FOREIGN_OR_MISMATCHED_PROBE_FILL')
                if not _time(activities['from_at']) <= _time(row['transaction_time']) <= _time(activities['until_at']):
                    raise ValueError('FILL_OUTSIDE_OBSERVATION_INTERVAL')
                q, p = _money(row['qty']), _money(row['price'])
                if q <= 0 or p <= 0:
                    raise ValueError('NONPOSITIVE_FILL')
                if p > _money(order['limit_price']):
                    raise ValueError('PAPER_BUY_FILL_ABOVE_LIMIT')
                qty += q
                notional += q * p
            else:
                cash_delta += _money(row.get('net_amount'), nonnegative=False)
        if qty != _money(order['dealt_quantity']):
            raise ValueError('FILL_TOTAL_DOES_NOT_MATCH_ORDER')
        if qty and abs(notional / qty - _money(order['dealt_avg_price'])) > Decimal('0.0001'):
            raise ValueError('FILL_AVERAGE_DOES_NOT_MATCH_ORDER')
        if _money(account1['cash'], nonnegative=False) != _money(account0['cash'], nonnegative=False) - notional + cash_delta:
            raise ValueError('PAPER_CASH_DOES_NOT_RECONCILE')
        positions = after['raw_positions']
        if qty:
            if len(positions) != 1 or 'US.' + positions[0]['symbol'] != order['code'] or _money(positions[0]['qty']) != qty or positions[0].get('side') != 'long':
                raise ValueError('PAPER_POSITION_DOES_NOT_RECONCILE')
        elif positions:
            raise ValueError('UNEXPECTED_PAPER_POSITION')
        if after['raw_orders']:
            raise ValueError('OPEN_ORDERS_REMAIN_AT_TERMINAL_OBSERVATION')
        result.update(matched=True, fill_quantity=str(qty), fill_notional_usd=str(notional),
                      observed_nontrade_net_usd=str(cash_delta))
    except (KeyError, TypeError, ValueError) as exc:
        result['issues'].append(str(exc))
    return result
