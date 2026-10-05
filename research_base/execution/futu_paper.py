"""Guarded, injected OpenD paper transport; importing this module opens no socket.

This is separate from the fictional CNY OMS. Order snapshots are cumulative
provider evidence, not an execution ledger. Paper fees/deals remain unknown.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Protocol
from zoneinfo import ZoneInfo

CLASSIFICATION = 'invented_provider_shaped_controls_not_official_acceptance'
TERMINAL = frozenset({'FILLED_ALL', 'CANCELLED_ALL', 'CANCELLED_PART', 'FAILED', 'DISABLED', 'DELETED'})
ACTIVE = frozenset({'UNSUBMITTED', 'WAITING_SUBMIT', 'SUBMITTING', 'SUBMITTED', 'FILLED_PART', 'CANCELLING_PART', 'CANCELLING_ALL'})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _time(value):
    if not isinstance(value, str):
        raise ValueError('EXPLICIT_TIMESTAMP_REQUIRED')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return parsed.astimezone(timezone.utc)


def _money(value, *, nonnegative=True):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('EXPLICIT_DECIMAL_REQUIRED')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('INVALID_DECIMAL') from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError('INVALID_DECIMAL')
    return result


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value):
        raise ValueError('EXPLICIT_ID_REQUIRED')
    return str(value)


@dataclass(frozen=True)
class FutuPaperBinding:
    peer_host: str
    peer_port: int
    acc_id: int
    allowed_acc_ids: tuple[int, ...]
    allowed_codes: tuple[str, ...]
    market: str = 'US'
    environment: str = 'SIMULATE'

    def validate(self):
        if self.environment != 'SIMULATE' or self.market != 'US':
            raise ValueError('ONLY_EXPLICIT_US_SIMULATE_SUPPORTED')
        if not self.peer_host or type(self.peer_port) is not int or not 1 <= self.peer_port <= 65535:
            raise ValueError('FIXED_PEER_REQUIRED')
        if type(self.acc_id) is not int or self.acc_id <= 0 or self.acc_id not in self.allowed_acc_ids:
            raise ValueError('EXPLICIT_ACCOUNT_WHITELIST_REQUIRED')
        if any(type(v) is not int or v <= 0 for v in self.allowed_acc_ids) or len(set(self.allowed_acc_ids)) != len(self.allowed_acc_ids):
            raise ValueError('INVALID_ACCOUNT_WHITELIST')
        if not self.allowed_codes or any(not isinstance(v, str) or not v.startswith('US.') or len(v) <= 3 for v in self.allowed_codes):
            raise ValueError('EXPLICIT_US_CODE_WHITELIST_REQUIRED')
        if len(set(self.allowed_codes)) != len(self.allowed_codes):
            raise ValueError('DUPLICATE_CODE')
        return self


@dataclass(frozen=True)
class PaperRiskEnvelope:
    max_quantity: int
    max_notional_usd: str
    max_limit_price_usd: str
    max_pending_orders: int
    max_snapshot_age_seconds: int
    submission_start: str
    submission_end: str

    def validate(self):
        if type(self.max_quantity) is not int or self.max_quantity <= 0 or type(self.max_pending_orders) is not int or self.max_pending_orders <= 0:
            raise ValueError('FINITE_QUANTITY_AND_PENDING_LIMIT_REQUIRED')
        if _money(self.max_notional_usd) <= 0 or _money(self.max_limit_price_usd) <= 0:
            raise ValueError('FINITE_MONETARY_LIMIT_REQUIRED')
        if type(self.max_snapshot_age_seconds) is not int or not 0 <= self.max_snapshot_age_seconds <= 300:
            raise ValueError('BOUNDED_FRESHNESS_REQUIRED')
        if _time(self.submission_start) >= _time(self.submission_end):
            raise ValueError('FINITE_SUBMISSION_WINDOW_REQUIRED')
        return self


class PaperTransport(Protocol):
    """All records are JSON values. No absence response authorizes resubmission."""
    def peer_identity(self) -> tuple[str, int]: ...
    def get_accounts(self) -> list[dict]: ...
    def snapshot(self, *, acc_id: int, trd_env: str, at: str) -> dict: ...
    def place_order(self, *, acc_id: int, trd_env: str, remark: str, code: str, side: str, quantity: int, limit_price: str) -> dict: ...
    def query_orders(self, *, acc_id: int, trd_env: str, remark: str, order_id: str | None, since: str, at: str) -> list[dict]: ...
    def cancel_order(self, *, acc_id: int, trd_env: str, order_id: str) -> dict: ...


class FutuPaperAdapter:
    """Durable intent/reservation guard. Constructor and recovery never send.

    fee_bound is an explicit assumed upper bound for this single order, NOT an
    official fee query. All provider records are retained with SHA256 privately.
    """
    def __init__(self, path, transport: PaperTransport, *, binding: FutuPaperBinding, envelope: PaperRiskEnvelope, clock=None):
        self.binding, self.envelope, self.transport = binding.validate(), envelope.validate(), transport
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS intents(intent TEXT PRIMARY KEY,payload TEXT NOT NULL,remark TEXT UNIQUE NOT NULL,
        state TEXT NOT NULL,order_id TEXT UNIQUE,reserve_cash TEXT NOT NULL,reserve_qty INTEGER NOT NULL,
        created_at TEXT NOT NULL,watermark TEXT,provider_status TEXT,filled TEXT NOT NULL DEFAULT '0',
        cancel_token INTEGER NOT NULL DEFAULT 0,cancel_state TEXT,fee_bound TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records(sequence INTEGER PRIMARY KEY,kind TEXT NOT NULL,intent TEXT,
        observed_at TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS blockers(reason TEXT PRIMARY KEY);''')
        with self._tx():
            for key, value in [('binding', asdict(binding)), ('envelope', asdict(envelope))]:
                encoded = canonical(value)
                previous = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
                if previous and previous[0] != encoded:
                    raise ValueError('PERSISTED_BINDING_OR_ENVELOPE_CHANGED')
                self.db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)', (key, encoded))
            self.db.execute("UPDATE intents SET state='UNKNOWN' WHERE state='SUBMITTING'")

    class _Transaction:
        def __init__(self, db): self.db = db
        def __enter__(self): self.db.execute('BEGIN IMMEDIATE'); return self.db
        def __exit__(self, kind, value, trace): self.db.execute('ROLLBACK' if kind else 'COMMIT')

    def _tx(self): return self._Transaction(self.db)
    def close(self): self.db.close()

    def _record(self, kind, intent, at, payload):
        encoded = canonical(payload)
        self.db.execute('INSERT INTO records(kind,intent,observed_at,payload,sha256) VALUES(?,?,?,?,?)',
                        (kind, intent, at, encoded, hashlib.sha256(encoded.encode()).hexdigest()))

    def _guard(self, at):
        _time(at)
        if self.transport.peer_identity() != (self.binding.peer_host, self.binding.peer_port):
            raise ValueError('FIXED_PEER_CHANGED')
        accounts = self.transport.get_accounts()
        if self.transport.peer_identity() != (self.binding.peer_host,self.binding.peer_port):
            raise ValueError('FIXED_PEER_CHANGED_DURING_ACCOUNT_QUERY')
        with self._tx(): self._record('ACCOUNTS', None, at, accounts)
        matches = [r for r in accounts if type(r.get('acc_id')) is int and r['acc_id'] == self.binding.acc_id]
        if len(matches) != 1 or matches[0].get('trd_env') != 'SIMULATE' or self.binding.market not in matches[0].get('markets', []) or matches[0].get('sim_acc_type') != 'STOCK_AND_OPTION' or matches[0].get('acc_status') != 'ACTIVE':
            raise ValueError('LIVE_ACCOUNT_ENVIRONMENT_MARKET_NOT_BOUND')

    def _payload(self, payload):
        if set(payload) != {'code', 'side', 'quantity', 'limit_price'}:
            raise ValueError('UNKNOWN_ORDER_FIELDS')
        if payload['code'] not in self.binding.allowed_codes or payload['side'] not in {'BUY', 'SELL'}:
            raise ValueError('ORDER_NOT_WHITELISTED')
        qty, price = payload['quantity'], _money(payload['limit_price'])
        if type(qty) is not int or not 0 < qty <= self.envelope.max_quantity or price <= 0 or price != price.quantize(Decimal('.01')):
            raise ValueError('WHOLE_SHARES_CENT_LIMIT_REQUIRED')
        if price > _money(self.envelope.max_limit_price_usd) or qty * price > _money(self.envelope.max_notional_usd):
            raise ValueError('ORDER_RISK_LIMIT_EXCEEDED')
        return dict(payload, limit_price=str(price))

    def _fee(self, fee_bound, at):
        if not isinstance(fee_bound, dict) or set(fee_bound) != {'amount_usd', 'evidence', 'known_at', 'valid_until'} or not isinstance(fee_bound['evidence'], str) or not fee_bound['evidence'].strip():
            raise ValueError('EXPLICIT_FEE_BOUND_EVIDENCE_REQUIRED')
        if not _time(fee_bound['known_at']) <= _time(at) <= _time(fee_bound['valid_until']):
            raise ValueError('FEE_BOUND_NOT_KNOWN_OR_EXPIRED')
        amount = _money(fee_bound['amount_usd'])
        if amount <= 0: raise ValueError('POSITIVE_EXPLICIT_FEE_BOUND_REQUIRED')
        return amount

    def preflight(self, *, at):
        self._guard(at)
        snapshot = self.transport.snapshot(acc_id=self.binding.acc_id, trd_env='SIMULATE', at=at)
        with self._tx(): self._record('PREFLIGHT', None, at, snapshot)
        checked = _time(self.clock())
        started, received = _time(snapshot.get('request_started_at')), _time(snapshot.get('received_at'))
        if snapshot.get('snapshot_clock_basis') != 'non_atomic_refreshed_request' or not started <= received <= checked:
            raise ValueError('STALE_OR_FUTURE_SNAPSHOT')
        if (checked-started).total_seconds() > self.envelope.max_snapshot_age_seconds:
            raise ValueError('STALE_OR_FUTURE_SNAPSHOT')
        if snapshot.get('open_orders_complete') is not True or not isinstance(snapshot.get('open_orders'), list):
            raise ValueError('OPEN_ORDERS_UNKNOWN')
        return snapshot

    def submit(self, intent, payload, *, at, fee_bound):
        intent = _id(intent)
        p = self._payload(payload)
        fee = self._fee(fee_bound, at)
        # Idempotent local retry never calls the provider, including after crash.
        with self._tx():
            prior = self.db.execute('SELECT * FROM intents WHERE intent=?', (intent,)).fetchone()
            if prior:
                if prior['payload'] != canonical(p) or prior['fee_bound'] != canonical(fee_bound):
                    raise ValueError('INTENT_PAYLOAD_CONFLICT')
                return dict(prior)
        if not _time(self.envelope.submission_start) <= _time(at) <= _time(self.envelope.submission_end):
            raise ValueError('SUBMISSION_WINDOW_CLOSED')
        snapshot = self.preflight(at=at)
        remark = 'qr-' + hashlib.sha256(canonical([asdict(self.binding), intent, p]).encode()).hexdigest()[:48]
        with self._tx():
            # Recheck inside the write lock: simultaneous same intent sends once.
            prior = self.db.execute('SELECT * FROM intents WHERE intent=?', (intent,)).fetchone()
            if prior:
                if prior['payload'] != canonical(p) or prior['fee_bound'] != canonical(fee_bound):
                    raise ValueError('INTENT_PAYLOAD_CONFLICT')
                return dict(prior)
            if self.db.execute('SELECT 1 FROM blockers').fetchone() or self.db.execute("SELECT 1 FROM intents WHERE state IN ('UNKNOWN','RECONCILIATION_REQUIRED','SUBMITTING')").fetchone():
                raise ValueError('UNRESOLVED_INTENT_OR_RISK_PAUSE')
            local = self.db.execute("SELECT * FROM intents WHERE state NOT IN ('TERMINAL')").fetchall()
            bound = {r['order_id'] for r in local if r['order_id']}
            unbound = [r for r in snapshot['open_orders'] if _id(r.get('order_id')) not in bound]
            if len(local) + len(unbound) >= self.envelope.max_pending_orders:
                raise ValueError('PENDING_ORDER_LIMIT')
            # Never infer cash from buying power or unsettled sale proceeds.
            if not snapshot.get('cash_evidence') or snapshot.get('settled_cash_usd') is None:
                raise ValueError('SETTLED_AVAILABLE_USD_CASH_UNKNOWN')
            cash = _money(snapshot['settled_cash_usd'])
            total_notional = p['quantity'] * _money(p['limit_price'])
            total_quantity = p['quantity']
            for existing in local:
                saved = json.loads(existing['payload'])
                total_notional += saved['quantity'] * _money(saved['limit_price'])
                total_quantity += saved['quantity']
            for existing in unbound:
                qty = existing.get('remaining_quantity')
                price = existing.get('limit_price')
                if type(qty) is not int or qty < 0 or price is None: raise ValueError('PROVIDER_PENDING_EXPOSURE_UNKNOWN')
                total_quantity += qty
                total_notional += qty * _money(price)
            if total_quantity > self.envelope.max_quantity or total_notional > _money(self.envelope.max_notional_usd):
                raise ValueError('AGGREGATE_PENDING_RISK_LIMIT')
            reserved_cash = sum((_money(r['reserve_cash']) for r in local), Decimal(0))
            reserved_qty = sum(r['reserve_qty'] for r in local if json.loads(r['payload'])['code'] == p['code'])
            for order in unbound:
                if order.get('side') == 'BUY':
                    if order.get('reserved_cash_usd') is None: raise ValueError('PROVIDER_RESERVATION_UNKNOWN')
                    reserved_cash += _money(order['reserved_cash_usd'])
                elif order.get('side') == 'SELL' and order.get('code') == p['code']:
                    q = order.get('remaining_quantity')
                    if type(q) is not int or q < 0: raise ValueError('PROVIDER_SELL_RESERVATION_UNKNOWN')
                    reserved_qty += q
                elif order.get('side') not in {'BUY','SELL'}: raise ValueError('PROVIDER_ORDER_SIDE_UNKNOWN')
            reserve = p['quantity'] * _money(p['limit_price']) + fee if p['side'] == 'BUY' else fee
            if reserve + reserved_cash > cash:
                raise ValueError('INSUFFICIENT_SETTLED_AVAILABLE_CASH')
            sell = 0
            if p['side'] == 'SELL':
                if not snapshot.get('positions_evidence') or not isinstance(snapshot.get('sellable'), dict):
                    raise ValueError('SELLABLE_QUANTITY_UNKNOWN')
                available = snapshot['sellable'].get(p['code'])
                if type(available) is not int or available < 0: raise ValueError('SELLABLE_QUANTITY_UNKNOWN')
                if p['quantity'] + reserved_qty > available: raise ValueError('INSUFFICIENT_SETTLED_SELLABLE')
                sell = p['quantity']
            self.db.execute('INSERT INTO intents(intent,payload,remark,state,reserve_cash,reserve_qty,created_at,fee_bound) VALUES(?,?,?,?,?,?,?,?)',
                            (intent, canonical(p), remark, 'SUBMITTING', str(reserve), sell, at, canonical(fee_bound)))
            self._record('BEFORE_PLACE', intent, at, {'remark': remark, 'payload': p, 'fee_bound': fee_bound, 'fee_classification': 'explicit_assumed_upper_bound_not_provider_actual'})
        # Durable SUBMITTING exists before crossing the transport boundary.
        try:
            self._guard(at)
            send_at = self.clock()
            if not _time(self.envelope.submission_start) <= _time(send_at) <= _time(self.envelope.submission_end):
                raise ValueError('SUBMISSION_WINDOW_CLOSED_AT_SEND')
            self._fee(fee_bound, send_at)
            if (_time(send_at)-_time(snapshot['request_started_at'])).total_seconds() > self.envelope.max_snapshot_age_seconds:
                raise ValueError('PREFLIGHT_EXPIRED_AT_SEND')
            response = self.transport.place_order(acc_id=self.binding.acc_id, trd_env='SIMULATE', remark=remark, **p)
            with self._tx():
                self._record('PLACE_RESPONSE', intent, at, response)
                self.db.execute("UPDATE intents SET state='UNKNOWN' WHERE intent=? AND state='SUBMITTING'", (intent,))
                if response.get('order'):
                    self._apply(intent, response['order'], at)
        except Exception as exc:
            with self._tx():
                self.db.execute("UPDATE intents SET state='UNKNOWN' WHERE intent=? AND state='SUBMITTING'", (intent,))
                self._record('PLACE_UNCERTAIN', intent, at, {'exception_type': type(exc).__name__})
        return self.intent(intent)

    def intent(self, intent):
        row = self.db.execute('SELECT * FROM intents WHERE intent=?', (_id(intent),)).fetchone()
        if not row: raise ValueError('UNKNOWN_LOCAL_INTENT')
        return dict(row)

    def _apply(self, intent, order, at):
        row = self.db.execute('SELECT * FROM intents WHERE intent=?', (intent,)).fetchone()
        p = json.loads(row['payload'])
        if order.get('remark') != row['remark'] or order.get('code') != p['code'] or order.get('side') != p['side'] or _money(order.get('quantity')) != p['quantity'] or _money(order.get('limit_price')) != _money(p['limit_price']):
            raise ValueError('PROVIDER_ORDER_INTENT_CONFLICT')
        oid = _id(order.get('order_id'))
        if row['order_id'] and row['order_id'] != oid:
            raise ValueError('PROVIDER_ORDER_ID_CHANGED')
        filled = _money(order.get('dealt_quantity'))
        if filled > p['quantity'] or filled < _money(row['filled']):
            self.db.execute('INSERT OR IGNORE INTO blockers VALUES(?)', ('CUMULATIVE_REVISION_REQUIRES_RECONCILIATION',))
            raise ValueError('CUMULATIVE_FILL_REVISION_OR_OVERFILL')
        status = order.get('status')
        if (status == 'FILLED_ALL' and filled != p['quantity']) or (status == 'CANCELLED_ALL' and filled != 0) or (status in {'CANCELLED_PART','FILLED_PART'} and not 0 < filled < p['quantity']):
            raise ValueError('PROVIDER_STATUS_FILL_CONFLICT')
        if filled > 0 and _money(order.get('dealt_avg_price')) <= 0:
            raise ValueError('FILLED_AVERAGE_PRICE_UNKNOWN')
        known = status in TERMINAL or status in ACTIVE
        state = 'TERMINAL' if status in TERMINAL else ('OPEN' if known else 'RECONCILIATION_REQUIRED')
        if row['state'] == 'TERMINAL' and state != 'TERMINAL':
            state = 'TERMINAL'  # Stale provider callbacks cannot resurrect an order.
        if filled > 0:
            self.db.execute('INSERT OR IGNORE INTO blockers VALUES(?)', ('PAPER_FILL_FEES_AND_EXECUTIONS_UNVERIFIED',))
        self.db.execute('UPDATE intents SET order_id=?,state=?,provider_status=?,filled=?,watermark=?,reserve_cash=?,reserve_qty=? WHERE intent=?',
                        (oid,state,status,str(filled),at,'0' if state == 'TERMINAL' else row['reserve_cash'],0 if state == 'TERMINAL' else row['reserve_qty'],intent))

    def recover(self, intent, *, at):
        row = self.intent(intent)
        if _time(at) < _time(row['watermark'] or row['created_at']): raise ValueError('QUERY_WATERMARK_REVERSED')
        self._guard(at)
        orders = self.transport.query_orders(acc_id=self.binding.acc_id, trd_env='SIMULATE', remark=row['remark'], order_id=row['order_id'], since=row['created_at'], at=at)
        with self._tx():
            self._record('ORDER_QUERY', intent, at, orders)
            self.db.execute('UPDATE intents SET watermark=? WHERE intent=?', (at,intent))
            matches = [o for o in orders if o.get('remark') == row['remark'] or (row['order_id'] and str(o.get('order_id')) == row['order_id'])]
            unique = {}
            conflicts = False
            for order in matches:
                oid = _id(order.get('order_id'))
                if oid in unique and canonical(unique[oid]) != canonical(order):
                    conflicts = True
                unique[oid] = order
            if conflicts:
                self.db.execute('INSERT OR IGNORE INTO blockers VALUES(?)', ('NON_ATOMIC_DUPLICATE_ORDER_CONFLICT',))
                self._record('QUERY_CONFLICT', intent, at, {'reason':'NON_ATOMIC_DUPLICATE_ORDER_CONFLICT'})
                return {'intent':self.intent(intent),'authoritative_absence':False,'resend_allowed':False,
                        'economic_reconciliation':'UNVERIFIED_PAPER_FEES_AND_DEALS','fee_actual_usd':None}
            if len(unique) > 1:
                self.db.execute("UPDATE intents SET state='RECONCILIATION_REQUIRED' WHERE intent=?", (intent,))
                self.db.execute('INSERT OR IGNORE INTO blockers VALUES(?)', ('AMBIGUOUS_REMARK_MATCH',))
            elif unique:
                try: self._apply(intent, next(iter(unique.values())), at)
                except ValueError as exc:
                    self.db.execute('INSERT OR IGNORE INTO blockers VALUES(?)', (str(exc),))
                    self._record('QUERY_CONFLICT', intent, at, {'reason':str(exc)})
            # Empty/non-atomic/eventually consistent result NEVER releases or sends.
        return {'intent':self.intent(intent), 'authoritative_absence':False, 'resend_allowed':False,
                'economic_reconciliation':'UNVERIFIED_PAPER_FEES_AND_DEALS', 'fee_actual_usd':None}

    def reconcile(self, intent, *, at):
        return self.recover(intent, at=at)

    def pause(self, reason, *, at):
        if not isinstance(reason,str) or not reason.strip(): raise ValueError('PAUSE_REASON_REQUIRED')
        with self._tx():
            self.db.execute('INSERT OR IGNORE INTO blockers VALUES(?)', (reason,))
            self._record('RISK_PAUSE', None, at, {'reason':reason})

    def cancel(self, intent, *, at):
        self._guard(at)
        with self._tx():
            row = self.intent(intent)
            if not row['order_id']: raise ValueError('QUERY_TO_BIND_ORDER_BEFORE_CANCEL')
            if row['state'] == 'TERMINAL': return row
            if row['cancel_state'] in {'REQUESTING','UNKNOWN','REQUEST_ACCEPTED'}: return row
            token = row['cancel_token'] + 1
            self.db.execute("UPDATE intents SET cancel_token=?,cancel_state='REQUESTING' WHERE intent=?", (token,intent))
            self._record('BEFORE_CANCEL', intent, at, {'order_id':row['order_id'],'cancel_token':token})
        try:
            response = self.transport.cancel_order(acc_id=self.binding.acc_id,trd_env='SIMULATE',order_id=row['order_id'])
        except Exception as exc:
            response = {'request_accepted':None,'exception_type':type(exc).__name__}
        self.cancel_response(intent, token, response, at=at)
        return self.intent(intent)

    def cancel_response(self, intent, token, response, *, at):
        with self._tx():
            row = self.intent(intent)
            self._record('CANCEL_RESPONSE', intent, at, {'cancel_token':token,'response':response})
            if token != row['cancel_token'] or row['cancel_state'] != 'REQUESTING' or row['state'] == 'TERMINAL': return
            status = 'REQUEST_ACCEPTED' if response.get('request_accepted') is True else ('REJECTED' if response.get('request_accepted') is False else 'UNKNOWN')
            self.db.execute('UPDATE intents SET cancel_state=? WHERE intent=?', (status,intent))

    def snapshot(self):
        return {'binding':asdict(self.binding),'envelope':asdict(self.envelope),
                'intents':[dict(r) for r in self.db.execute('SELECT * FROM intents ORDER BY intent')],
                'blockers':[r[0] for r in self.db.execute('SELECT reason FROM blockers ORDER BY reason')],
                'records':[dict(r) for r in self.db.execute('SELECT * FROM records ORDER BY sequence')],
                'execution_records':[], 'actual_fees':None,
                'classification':'guarded_paper_transport_economic_reconciliation_unverified'}


class OpenDTransport:
    """Injected official SDK call mapping. Never imports SDK or creates a context.

    peer_identity must attest the actual pinned context socket, not echo user
    input. cash_resolver may supply reviewed settled-cash/sellability evidence;
    absence of that resolver deliberately leaves send preflight blocked.
    """
    def __init__(self, context, sdk, *, peer_identity, cash_resolver=None, clock=None):
        self.context,self.sdk,self._peer,self.cash_resolver = context,sdk,peer_identity,cash_resolver
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    def peer_identity(self): return self._peer()

    def _records(self, result):
        ret,data = result
        if ret != self.sdk.RET_OK: raise RuntimeError('OPEND_QUERY_OR_SEND_ERROR')
        rows = data.to_dict('records') if hasattr(data,'to_dict') else data
        if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows): raise ValueError('PROVIDER_TABLE_REQUIRED')
        # Preserve all raw fields while normalizing enum/numpy scalars for JSON.
        def plain(v):
            if hasattr(v,'item'): return plain(v.item())
            if isinstance(v,dict): return {str(k):plain(x) for k,x in v.items()}
            if isinstance(v,(list,tuple)): return [plain(x) for x in v]
            if isinstance(v,(str,int,float,bool)) or v is None: return v
            return str(v)
        return [plain(row) for row in rows]

    def _args(self, acc_id, trd_env):
        if trd_env != 'SIMULATE' or type(acc_id) is not int or acc_id <= 0: raise ValueError('EXPLICIT_SIMULATE_ACCOUNT_REQUIRED')
        return {'trd_env':self.sdk.TrdEnv.SIMULATE,'acc_id':acc_id}

    def get_accounts(self):
        rows = self._records(self.context.get_acc_list())
        return [{'acc_id':r['acc_id'],'trd_env':str(r['trd_env']),'markets':[str(m) for m in r['trdmarket_auth']],'sim_acc_type':str(r.get('sim_acc_type')),'acc_status':str(r.get('acc_status')),'raw':r} for r in rows]

    def _order(self, r):
        return {'order_id':_id(r['order_id']),'remark':r.get('remark'), 'code':r['code'],'side':str(r['trd_side']),
                'quantity':str(r['qty']),'limit_price':str(r['price']), 'status':str(r['order_status']),
                'dealt_quantity':str(r['dealt_qty']),'dealt_avg_price':str(r['dealt_avg_price']),
                'updated_time':r.get('updated_time'),'raw':r,'actual_fee_usd':None}

    def place_order(self, *, acc_id,trd_env,remark,code,side,quantity,limit_price):
        if len(remark.encode('utf-8')) > 64: raise ValueError('REMARK_TOO_LONG')
        rows = self._records(self.context.place_order(price=float(limit_price),qty=quantity,code=code,
            trd_side=getattr(self.sdk.TrdSide,side),order_type=self.sdk.OrderType.NORMAL,
            remark=remark,time_in_force=self.sdk.TimeInForce.DAY,session=self.sdk.Session.RTH,
            adjust_limit=0,**self._args(acc_id,trd_env)))
        if len(rows) != 1: raise ValueError('PLACE_ORDER_RESPONSE_AMBIGUOUS')
        return {'order':self._order(rows[0]),'raw':rows}

    def query_orders(self, *, acc_id,trd_env,remark,order_id,since,at):
        args = self._args(acc_id,trd_env)
        current = self._records(self.context.order_list_query(order_id=order_id or '',refresh_cache=True,**args))
        exchange_zone = ZoneInfo('America/New_York')
        start = _time(since).astimezone(exchange_zone).date().isoformat()
        end = _time(at).astimezone(exchange_zone).date().isoformat()
        historical = self._records(self.context.history_order_list_query(start=start,end=end,**args))
        # Preserve duplicate snapshots: adapter checks identity; latest query is a
        # non-atomic view and makes no authoritative absence assertion.
        return [self._order(r) for r in historical+current]

    def snapshot(self, *, acc_id,trd_env,at):
        args = self._args(acc_id,trd_env)
        started = self.clock()
        accounts = self._records(self.context.accinfo_query(currency=self.sdk.Currency.USD,refresh_cache=True,**args))
        positions = self._records(self.context.position_list_query(refresh_cache=True,**args))
        orders = self._records(self.context.order_list_query(refresh_cache=True,**args))
        received = self.clock()
        result = {'observed_at':received,'request_started_at':started,'received_at':received,
                  'snapshot_clock_basis':'non_atomic_refreshed_request','provider_as_of':None,'settled_cash_usd':None,'cash_evidence':None,'sellable':None,'positions_evidence':None,
                  'open_orders_complete':True,'open_orders':[self._order(r) for r in orders if str(r['order_status']) not in TERMINAL],
                  'raw':{'account':accounts,'positions':positions,'orders':orders},'actual_fee_usd':None}
        if self.cash_resolver:
            resolved = self.cash_resolver(result['raw'],at)
            allowed = {'settled_cash_usd','cash_evidence','sellable','positions_evidence','open_orders'}
            if not isinstance(resolved,dict) or set(resolved)-allowed: raise ValueError('UNKNOWN_CASH_RESOLVER_FIELDS')
            result.update(resolved)
        return result

    def cancel_order(self, *, acc_id,trd_env,order_id):
        ret,data = self.context.modify_order(self.sdk.ModifyOrderOp.CANCEL,order_id,0,0,**self._args(acc_id,trd_env))
        return {'request_accepted':True if ret == self.sdk.RET_OK else None,'raw':str(data)}

    def query_deals(self, **kwargs):
        return {'supported':False,'records':None,'reason':'OFFICIAL_OPEND_SIMULATE_DEAL_QUERY_UNSUPPORTED'}
