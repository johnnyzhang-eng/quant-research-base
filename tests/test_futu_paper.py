"""Invented provider-shaped controls, never a live OpenD connection."""
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from research_base.execution.futu_paper import FutuPaperAdapter, FutuPaperBinding, OpenDTransport, PaperRiskEnvelope

AT = '2030-01-02T15:00:00+00:00'
LATER = '2030-01-02T15:01:00+00:00'
FEE = {'amount_usd':'1','evidence':'invented upper bound not actual fee','known_at':AT,'valid_until':LATER}
PAYLOAD = {'code':'US.TEST','side':'BUY','quantity':1,'limit_price':'10.00'}


def binding(**changes):
    return FutuPaperBinding(**dict({'peer_host':'127.0.0.1','peer_port':11111,'acc_id':123,'allowed_acc_ids':(123,), 'allowed_codes':('US.TEST',)},**changes))


def envelope(**changes):
    return PaperRiskEnvelope(**dict({'max_quantity':1,'max_notional_usd':'100','max_limit_price_usd':'100', 'max_pending_orders':1,'max_snapshot_age_seconds':10, 'submission_start':AT,'submission_end':LATER},**changes))


class InventedProvider:
    def __init__(self):
        self.peer = ('127.0.0.1',11111)
        self.accounts = [{'acc_id':123,'trd_env':'SIMULATE','markets':['US'],'sim_acc_type':'STOCK_AND_OPTION','acc_status':'ACTIVE'}]
        self.state = {'observed_at':AT,'request_started_at':AT,'received_at':AT,'snapshot_clock_basis':'non_atomic_refreshed_request','settled_cash_usd':'100','cash_evidence':'invented settled available cash', 'sellable':{'US.TEST':1},'positions_evidence':'invented settled sellable', 'open_orders_complete':True,'open_orders':[]}
        self.sent=[]; self.orders=[]; self.cancelled=[]; self.raise_after_accept=False; self.raise_before_accept=False; self.observe=None
    def peer_identity(self): return self.peer
    def get_accounts(self): return copy.deepcopy(self.accounts)
    def snapshot(self,**kwargs): return copy.deepcopy(self.state)
    def place_order(self,**kwargs):
        if self.observe: self.observe()
        self.sent.append(kwargs)
        if self.raise_before_accept: raise TimeoutError()
        order={'order_id':str(len(self.sent)), 'remark':kwargs['remark'],'code':kwargs['code'],'side':kwargs['side'], 'quantity':str(kwargs['quantity']),'limit_price':kwargs['limit_price'], 'status':'SUBMITTED','dealt_quantity':'0','dealt_avg_price':'0'}
        self.orders.append(order)
        if self.raise_after_accept: raise TimeoutError()
        return {'order':copy.deepcopy(order)}
    def query_orders(self,**kwargs): return copy.deepcopy(self.orders)
    def cancel_order(self,**kwargs): self.cancelled.append(kwargs); return {'request_accepted':True}


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'paper.sqlite'; self.provider=InventedProvider()
        self.adapter=FutuPaperAdapter(self.path,self.provider,binding=binding(),envelope=envelope(),clock=lambda:AT)
    def tearDown(self): self.adapter.close(); self.tmp.cleanup()
    def submit(self,**kwargs): return self.adapter.submit('intent1',PAYLOAD,at=AT,fee_bound=FEE,**kwargs)

    def test_persist_before_send_and_local_idempotency(self):
        def observe():
            with sqlite3.connect(self.path) as db:
                state,cash=db.execute('SELECT state,reserve_cash FROM intents').fetchone()
                self.assertEqual((state,cash),('SUBMITTING','11.00'))
        self.provider.observe=observe
        self.assertEqual(self.submit()['state'],'OPEN')
        self.assertEqual(self.submit()['order_id'],'1')
        self.assertEqual(len(self.provider.sent),1)
        sent=self.provider.sent[0]
        self.assertEqual((sent['acc_id'],sent['trd_env']),(123,'SIMULATE'))
        self.assertLessEqual(len(sent['remark'].encode()),64)
        with self.assertRaisesRegex(ValueError,'CONFLICT'):
            self.adapter.submit('intent1',dict(PAYLOAD,limit_price='11'),at=AT,fee_bound=FEE)

    def test_unknown_absence_restart_never_resends_or_releases(self):
        self.provider.raise_before_accept=True
        self.assertEqual(self.submit()['state'],'UNKNOWN')
        self.adapter.close(); self.adapter=FutuPaperAdapter(self.path,self.provider,binding=binding(),envelope=envelope(),clock=lambda:AT)
        for _ in range(3):
            result=self.adapter.recover('intent1',at=LATER)
            self.assertFalse(result['authoritative_absence']); self.assertFalse(result['resend_allowed'])
            self.assertEqual(result['intent']['reserve_cash'],'11.00')
        self.submit(); self.assertEqual(len(self.provider.sent),1)
        self.provider.state['observed_at']=LATER
        with self.assertRaisesRegex(ValueError,'UNRESOLVED'):
            self.adapter.submit('intent2',PAYLOAD,at=LATER,fee_bound=FEE)

    def test_lost_accepted_response_binds_on_query(self):
        self.provider.raise_after_accept=True
        self.assertEqual(self.submit()['state'],'UNKNOWN')
        result=self.adapter.recover('intent1',at=LATER)
        self.assertEqual((result['intent']['order_id'],result['intent']['state']),('1','OPEN'))
        self.assertEqual(len(self.provider.sent),1)

    def test_crash_submitting_constructor_recovers_unknown(self):
        self.submit(); self.adapter.db.execute("UPDATE intents SET state='SUBMITTING'")
        self.adapter.close(); self.adapter=FutuPaperAdapter(self.path,self.provider,binding=binding(),envelope=envelope(),clock=lambda:AT)
        self.assertEqual(self.adapter.intent('intent1')['state'],'UNKNOWN'); self.assertEqual(len(self.provider.sent),1)

    def test_two_process_crash_durable_unknown_no_resend(self):
        import subprocess
        import sys
        first = """import os,sys
from tests.test_futu_paper import *
p=InventedProvider()
a=FutuPaperAdapter(sys.argv[1],p,binding=binding(),envelope=envelope(),clock=lambda:AT)
p.observe=lambda:os._exit(23)
a.submit('crash',PAYLOAD,at=AT,fee_bound=FEE)
"""
        second = """import sys,json
from tests.test_futu_paper import *
p=InventedProvider()
a=FutuPaperAdapter(sys.argv[1],p,binding=binding(),envelope=envelope(),clock=lambda:AT)
a.recover('crash',at=LATER)
a.submit('crash',PAYLOAD,at=AT,fee_bound=FEE)
r=a.intent('crash')
print(json.dumps({'state':r['state'],'reserve':r['reserve_cash'],'sends':len(p.sent)}))
a.close()
"""
        cwd=Path(__file__).resolve().parents[1]
        crashed=subprocess.run([sys.executable,'-c',first,str(self.path)],cwd=cwd,capture_output=True,text=True,timeout=10)
        self.assertEqual(crashed.returncode,23,crashed.stderr)
        restarted=subprocess.run([sys.executable,'-c',second,str(self.path)],cwd=cwd,capture_output=True,text=True,timeout=10)
        self.assertEqual(restarted.returncode,0,restarted.stderr)
        self.assertEqual(json.loads(restarted.stdout),{'state':'UNKNOWN','reserve':'11.00','sends':0})

    def test_persisted_binding_and_envelope_cannot_change(self):
        for b,e in [(binding(allowed_codes=('US.OTHER',)),envelope()),(binding(),envelope(max_quantity=2))]:
            with self.assertRaisesRegex(ValueError,'PERSISTED'):
                FutuPaperAdapter(self.path,self.provider,binding=b,envelope=e,clock=lambda:AT)

    def test_real_unknown_autoaccount_market_and_peer_rejected(self):
        for changes in [{'environment':'REAL'},{'environment':'unknown'},{'acc_id':0},{'market':'HK'},{'allowed_acc_ids':()}]:
            with self.assertRaises(ValueError): binding(**changes).validate()
        for accounts in [[{'acc_id':123,'trd_env':'REAL','markets':['US'],'sim_acc_type':'STOCK_AND_OPTION','acc_status':'ACTIVE'}], [{'acc_id':123,'trd_env':'SIMULATE','markets':['HK']}], [], self.provider.accounts*2]:
            self.provider.accounts=accounts
            with self.assertRaises(ValueError): self.submit()
        self.provider.peer=('remote.example',11111)
        with self.assertRaisesRegex(ValueError,'PEER'): self.submit()
        self.assertEqual(self.provider.sent,[])

    def test_whole_share_cent_price_and_finite_risk(self):
        for changes in [{'quantity':True},{'quantity':1.5},{'quantity':2},{'limit_price':'1.001'},{'limit_price':'101'}, {'code':'US.NOTALLOWED'},{'side':'SHORT'},{'order_type':'MARKET'}]:
            with self.assertRaises(ValueError): self.adapter.submit('intent1',dict(PAYLOAD,**changes),at=AT,fee_bound=FEE)
        with self.assertRaisesRegex(ValueError,'WINDOW'):
            self.adapter.submit('intent1',PAYLOAD,at='2030-01-03T15:00:00Z',fee_bound=dict(FEE,valid_until='2031-01-01T00:00:00Z'))
        self.assertEqual(self.provider.sent,[])

    def test_unknown_or_late_fee_and_cash_never_default_zero(self):
        for fee in [None,{},dict(FEE,evidence=''),dict(FEE,known_at=LATER),dict(FEE,amount_usd='NaN'),dict(FEE,amount_usd='0')]:
            with self.assertRaises(ValueError): self.adapter.submit('i',PAYLOAD,at=AT,fee_bound=fee)
        self.provider.state['settled_cash_usd']=None; self.provider.state['buying_power']='100000'
        with self.assertRaisesRegex(ValueError,'CASH_UNKNOWN'): self.submit()
        self.provider.state['settled_cash_usd']='10'
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT'): self.submit()
        self.assertEqual(self.provider.sent,[])

    def test_stale_future_unknown_openorders_block(self):
        for changes in [{'request_started_at':'2030-01-02T14:59:00Z'}, {'received_at':LATER}, {'open_orders_complete':False}]:
            original=copy.deepcopy(self.provider.state); self.provider.state.update(changes)
            with self.assertRaises(ValueError): self.submit()
            self.provider.state=original
        self.assertEqual(self.provider.sent,[])

    def test_partial_snapshots_keep_reserves_no_synthetic_execs(self):
        self.submit(); self.provider.orders[0].update(status='FILLED_PART',dealt_quantity='0.5',dealt_avg_price='10')
        self.adapter.recover('intent1',at=LATER)
        self.assertEqual(self.adapter.intent('intent1')['reserve_cash'],'11.00')
        self.assertEqual(self.adapter.snapshot()['execution_records'],[])
        self.assertIsNone(self.adapter.snapshot()['actual_fees'])
        self.assertIn('PAPER_FILL_FEES_AND_EXECUTIONS_UNVERIFIED',self.adapter.snapshot()['blockers'])

    def test_cumulative_decrease_and_ambiguous_remark_halt(self):
        self.submit(); self.provider.orders[0].update(status='FILLED_PART',dealt_quantity='0.5',dealt_avg_price='10')
        self.adapter.recover('intent1',at=LATER)
        self.provider.orders[0]['dealt_quantity']='0.25'
        self.adapter.recover('intent1',at=LATER)
        self.assertEqual(self.adapter.intent('intent1')['filled'],'0.5')
        self.assertIn('CUMULATIVE_REVISION_REQUIRES_RECONCILIATION',self.adapter.snapshot()['blockers'])
        self.provider.orders.append(dict(self.provider.orders[0],order_id='2'))
        self.assertEqual(self.adapter.recover('intent1',at=LATER)['intent']['state'],'RECONCILIATION_REQUIRED')
        self.assertEqual(self.adapter.intent('intent1')['reserve_cash'],'11.00')

    def test_cancel_request_is_not_confirmation_stale_reject_no_resurrection(self):
        self.submit(); result=self.adapter.cancel('intent1',at=AT)
        self.assertEqual((result['state'],result['cancel_state'],result['reserve_cash']),('OPEN','REQUEST_ACCEPTED','11.00'))
        self.adapter.cancel('intent1',at=AT); self.assertEqual(len(self.provider.cancelled),1)
        self.provider.orders[0]['status']='CANCELLED_ALL'
        self.adapter.recover('intent1',at=LATER)
        self.adapter.cancel_response('intent1',1,{'request_accepted':False},at=LATER)
        self.assertEqual(self.adapter.intent('intent1')['state'],'TERMINAL')
        self.assertEqual(self.adapter.intent('intent1')['reserve_cash'],'0')
        self.provider.orders[0]['status']='SUBMITTED'
        self.adapter.recover('intent1',at=LATER)
        self.assertEqual(self.adapter.intent('intent1')['state'],'TERMINAL')

    def test_fill_during_cancel_confirmation_retains_economic_unknown(self):
        self.submit(); self.adapter.cancel('intent1',at=AT)
        self.provider.orders[0].update(status='FILLED_ALL',dealt_quantity='1',dealt_avg_price='10')
        self.adapter.recover('intent1',at=LATER)
        self.assertEqual(self.adapter.intent('intent1')['state'],'TERMINAL')
        self.assertIn('PAPER_FILL_FEES_AND_EXECUTIONS_UNVERIFIED',self.adapter.snapshot()['blockers'])
        self.provider.state['observed_at']=LATER
        with self.assertRaisesRegex(ValueError,'UNRESOLVED'): self.adapter.submit('next',PAYLOAD,at=LATER,fee_bound=FEE)

    def test_unknown_sellability_and_existing_provider_pending(self):
        for available in [None,0,True]:
            self.provider.state['sellable']={'US.TEST':available}
            with self.assertRaises(ValueError): self.adapter.submit('s',dict(PAYLOAD,side='SELL'),at=AT,fee_bound=FEE)
        self.provider.state['open_orders']=[{'order_id':'external','side':'BUY','reserved_cash_usd':'90'}]
        with self.assertRaisesRegex(ValueError,'PENDING'): self.submit()

    def test_two_connections_serialize_pending_reservation(self):
        a2=FutuPaperAdapter(self.path,self.provider,binding=binding(),envelope=envelope(max_pending_orders=1),clock=lambda:AT)
        self.submit()
        with self.assertRaisesRegex(ValueError,'PENDING'): a2.submit('other',PAYLOAD,at=AT,fee_bound=FEE)
        a2.close(); self.assertEqual(len(self.provider.sent),1)

    def test_account_subtype_status_duplicate_rows_and_status_fill_conflicts(self):
        original=copy.deepcopy(self.provider.accounts)
        for field,value in [('sim_acc_type','FUTURE'),('acc_status','DISABLED')]:
            self.provider.accounts=copy.deepcopy(original); self.provider.accounts[0][field]=value
            with self.assertRaises(ValueError): self.submit()
        self.provider.accounts=original; self.submit()
        self.provider.orders[0].update(status='FILLED_ALL',dealt_quantity='0',dealt_avg_price='0')
        self.adapter.reconcile('intent1',at=LATER)
        self.assertEqual(self.adapter.intent('intent1')['state'],'OPEN')
        self.assertIn('PROVIDER_STATUS_FILL_CONFLICT',self.adapter.snapshot()['blockers'])
        self.provider.orders=[dict(self.provider.orders[0],status='SUBMITTED'),dict(self.provider.orders[0],status='CANCELLED_ALL')]
        self.adapter.recover('intent1',at=LATER)
        self.assertIn('NON_ATOMIC_DUPLICATE_ORDER_CONFLICT',self.adapter.snapshot()['blockers'])
        self.assertEqual(self.adapter.intent('intent1')['reserve_cash'],'11.00')

    def test_request_duration_actual_clock_and_pause_guard(self):
        self.provider.state['request_started_at']='2030-01-02T14:59:49Z'
        with self.assertRaisesRegex(ValueError,'STALE'): self.submit()
        self.provider.state['request_started_at']=AT
        self.adapter.pause('MANUAL',at=AT)
        with self.assertRaisesRegex(ValueError,'UNRESOLVED'): self.submit()
        self.assertEqual(self.provider.sent,[])

    def test_simultaneous_connections_send_once(self):
        barrier=threading.Barrier(2); outcomes=[]
        original=self.provider.snapshot
        def synchronized(**kwargs):
            result=original(**kwargs); barrier.wait(timeout=5); return result
        self.provider.snapshot=synchronized
        def send(intent):
            adapter=FutuPaperAdapter(self.path,self.provider,binding=binding(),envelope=envelope(),clock=lambda:AT)
            try:
                outcomes.append(adapter.submit(intent,PAYLOAD,at=AT,fee_bound=FEE)['state'])
            except ValueError:
                outcomes.append('BLOCKED')
            finally: adapter.close()
        threads=[threading.Thread(target=send,args=(name,)) for name in ('one','two')]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(outcomes),['BLOCKED','OPEN'])
        self.assertEqual(len(self.provider.sent),1)

    def test_raw_records_have_verifiable_hashes(self):
        import hashlib
        self.submit(); self.adapter.recover('intent1',at=LATER)
        for row in self.adapter.snapshot()['records']:
            self.assertEqual(row['sha256'],hashlib.sha256(row['payload'].encode()).hexdigest())
            json.loads(row['payload'])


class InventedSDKContext:
    def __init__(self): self.calls=[]
    def get_acc_list(self): return 0,[{'acc_id':123,'trd_env':'SIMULATE','trdmarket_auth':['US'],'sim_acc_type':'STOCK_AND_OPTION','acc_status':'ACTIVE'}]
    def place_order(self,**kwargs):
        self.calls.append(('place',kwargs)); return 0,[self.row(remark=kwargs['remark'])]
    @staticmethod
    def row(**changes):
        return dict({'order_id':'12345','code':'US.TEST','trd_side':'BUY','qty':1,'price':10, 'order_status':'SUBMITTED','dealt_qty':0,'dealt_avg_price':0,'remark':'tag','updated_time':'2030-01-02 15:00:00'},**changes)
    def order_list_query(self,**kwargs): self.calls.append(('orders',kwargs)); return 0,[self.row()]
    def history_order_list_query(self,**kwargs): self.calls.append(('history',kwargs)); return 0,[]
    def accinfo_query(self,**kwargs): self.calls.append(('cash',kwargs)); return 0,[{'buying_power':1000,'cash':100}]
    def position_list_query(self,**kwargs): self.calls.append(('positions',kwargs)); return 0,[]
    def modify_order(self,*args,**kwargs): self.calls.append(('cancel',dict(kwargs,args=args))); return 0,'request received'


class SDKMappingTests(unittest.TestCase):
    def test_history_queries_cover_new_york_dates_in_winter_and_summer(self):
        sdk=SimpleNamespace(RET_OK=0,TrdEnv=SimpleNamespace(SIMULATE='SIMULATE'))
        cases=[
            ('2030-01-02T04:30:00Z','2030-01-05T04:30:00Z','2030-01-01','2030-01-04'),
            ('2030-07-02T03:30:00Z','2030-07-05T03:30:00Z','2030-07-01','2030-07-04'),
            ('2026-10-06T02:00:00Z','2026-10-09T02:00:00Z','2026-10-05','2026-10-08'),
        ]
        for since,at,expected_start,expected_end in cases:
            with self.subTest(since=since):
                context=InventedSDKContext()
                context.order_list_query=lambda **kwargs:(0,[])  # Beyond the current-order retention window.
                raw_created=expected_start+' 22:00:00'
                def history(**kwargs):
                    context.calls.append(('history',kwargs))
                    covered=kwargs['start'] <= expected_start <= kwargs['end']
                    return 0,[context.row(create_time=raw_created)] if covered else []
                context.history_order_list_query=history
                transport=OpenDTransport(context,sdk,peer_identity=lambda:('127.0.0.1',11111))
                orders=transport.query_orders(acc_id=123,trd_env='SIMULATE',remark='tag',order_id=None,since=since,at=at)
                self.assertEqual(context.calls,[('history',{'start':expected_start,'end':expected_end,'trd_env':'SIMULATE','acc_id':123})])
                self.assertEqual([order['order_id'] for order in orders],['12345'])
                self.assertEqual(orders[0]['raw']['create_time'],raw_created)

    def test_raw_account_ids_never_coerce_into_whitelist(self):
        sdk=SimpleNamespace(RET_OK=0)
        context=InventedSDKContext()
        for raw_id in [123.9,'123',True]:
            context.get_acc_list=lambda: (0,[{'acc_id':raw_id,'trd_env':'SIMULATE','trdmarket_auth':['US'],'sim_acc_type':'STOCK_AND_OPTION','acc_status':'ACTIVE'}])
            transport=OpenDTransport(context,sdk,peer_identity=lambda:('127.0.0.1',11111))
            self.assertIs(type(transport.get_accounts()[0]['acc_id']),type(raw_id))
            with tempfile.TemporaryDirectory() as tmp:
                adapter=FutuPaperAdapter(Path(tmp)/'paper.sqlite',transport,binding=binding(),envelope=envelope(),clock=lambda:AT)
                try:
                    with self.assertRaisesRegex(ValueError,'NOT_BOUND'): adapter.preflight(at=AT)
                finally: adapter.close()

    def test_explicit_sdk_arguments_and_unknown_fees_cash(self):
        sdk=SimpleNamespace(RET_OK=0,Currency=SimpleNamespace(USD='USD'),TrdEnv=SimpleNamespace(SIMULATE='SIMULATE'),TrdSide=SimpleNamespace(BUY='BUY',SELL='SELL'),OrderType=SimpleNamespace(NORMAL='NORMAL'),TimeInForce=SimpleNamespace(DAY='DAY'),Session=SimpleNamespace(RTH='RTH'),ModifyOrderOp=SimpleNamespace(CANCEL='CANCEL'))
        context=InventedSDKContext(); transport=OpenDTransport(context,sdk,peer_identity=lambda:('127.0.0.1',11111))
        transport.get_accounts(); transport.place_order(acc_id=123,trd_env='SIMULATE',remark='tag',**PAYLOAD)
        transport.query_orders(acc_id=123,trd_env='SIMULATE',remark='tag',order_id=None,since=AT,at=LATER)
        result=transport.snapshot(acc_id=123,trd_env='SIMULATE',at=AT)
        transport.cancel_order(acc_id=123,trd_env='SIMULATE',order_id='12345')
        self.assertIsNone(result['settled_cash_usd']); self.assertIsNone(result['actual_fee_usd']); self.assertFalse(transport.query_deals()['supported'])
        for kind,args in context.calls:
            self.assertEqual((args['acc_id'],args['trd_env']),(123,'SIMULATE'))
            if kind in {'orders','cash','positions'}: self.assertIs(args['refresh_cache'],True)
            if kind == 'cash': self.assertEqual(args['currency'],sdk.Currency.USD)
        place=context.calls[0][1]
        self.assertEqual((place['time_in_force'],place['session'],place['adjust_limit']),('DAY','RTH',0))
        with self.assertRaises(ValueError): transport.cancel_order(acc_id=123,trd_env='REAL',order_id='12345')


if __name__=='__main__': unittest.main()
