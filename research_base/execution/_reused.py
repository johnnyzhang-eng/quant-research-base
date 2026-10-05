"""Reused first-phase project simulator (original evidence preserved unchanged).

Provenance: first-phase offline-simulator/order_simulator.py. This project-owned
source is adapted under the repository MIT license; no broker SDK is included.

Standard-library OFFLINE order fault simulator. No sockets, SDKs or credentials.

Two independent SQLite databases model an OMS and a fictional counterparty.
Prices/cash are integer cents; calendars/quotes/fees are synthetic fixtures.
This deliberately has no real-broker adapter and cannot submit a network order.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

ENV = "OFFLINE_SIMULATION"
ACCOUNT = "FICTIONAL-CNY-ONLY"
TERMINAL = {"FILLED", "CANCELLED", "EXPIRED", "REJECTED"}
DEFAULT_CONFIG = {
    "initial_cash": 1_000_000, "initial_positions": {},
    "allowed_symbols": ["TEST.CNY", "LOT100.CNY"],
    "fee_cap": 500, "per_order_cap": 1_000_000,
    "inflight_cap": 1_000_000, "max_open_orders": 10,
    "daily_submit_cap": 100, "daily_gross_cap": 10_000_000,
    "loss_limit": 100_000, "quote_max_age": 60,
    "calendar": {"2026-10-08": [["09:30", "11:30"], ["13:00", "15:00"]],
                 "2026-10-09": [["09:30", "11:30"]]},
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def price_cents(value):
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d <= 0 or d * 100 != (d * 100).to_integral_value():
            raise ValueError("PRICE_NOT_POSITIVE_CENT_ALIGNED")
        return int(d * 100)
    except (InvalidOperation, TypeError):
        raise ValueError("INVALID_PRICE") from None


def aware_time(value):
    d = datetime.fromisoformat(value)
    if d.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return d


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def tx(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()


class FictionalVenue(Store):
    """Durable, independent fake venue, explicitly driven by test events."""
    def __init__(self, path, config, now):
        super().__init__(path)
        with self.tx() as c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY,cash INTEGER);
              CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY,qty INTEGER,sellable INTEGER);
              CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,intent TEXT UNIQUE,payload TEXT,state TEXT,
                filled INTEGER DEFAULT 0,cancel_pending INTEGER DEFAULT 0);
              CREATE TABLE IF NOT EXISTS fills(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                exec_id TEXT UNIQUE,order_id TEXT,qty INTEGER,price INTEGER,fee INTEGER,payload TEXT);
              CREATE TABLE IF NOT EXISTS quotes(symbol TEXT PRIMARY KEY,price INTEGER,stamp TEXT,
                currency TEXT,lot INTEGER,tick INTEGER,status TEXT);
              CREATE TABLE IF NOT EXISTS log(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT);
            """)
            if not c.in_transaction:
                c.execute("BEGIN IMMEDIATE")
            fresh = not c.execute("SELECT 1 FROM account").fetchone()
            if fresh:
                c.execute("INSERT INTO account VALUES(1,?)", (config["initial_cash"],))
                for symbol, p in config["initial_positions"].items():
                    c.execute("INSERT INTO positions VALUES(?,?,?)", (symbol, p["qty"], p["sellable"]))
                for symbol, currency, lot in [("TEST.CNY", "CNY", 1), ("LOT100.CNY", "CNY", 100), ("TEST.USD", "USD", 1)]:
                    c.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?,?)", (symbol, 1000, now, currency, lot, 1, "TRADING"))

    def submit(self, intent, payload):
        if payload["environment"] != ENV or payload["account"] != ACCOUNT:
            raise ValueError("VENUE_ENVIRONMENT_REFUSED")
        with self.tx() as c:
            c.execute("INSERT INTO log(kind,payload) VALUES('SUBMIT_CALL',?)", (canonical({"intent":intent}),))
            prior = c.execute("SELECT * FROM orders WHERE intent=?", (intent,)).fetchone()
            if prior:
                if prior["payload"] != canonical(payload):
                    raise ValueError("VENUE_ID_CONFLICT")
                return prior["id"]
            oid = "FAKE-" + hashlib.sha256(intent.encode()).hexdigest()[:20]
            c.execute("INSERT INTO orders(id,intent,payload,state) VALUES(?,?,?,?)", (oid, intent, canonical(payload), "ACKNOWLEDGED"))
            c.execute("INSERT INTO log(kind,payload) VALUES('ACCEPT',?)", (canonical({"order_id": oid, "intent": intent}),))
            return oid

    def quote(self, symbol):
        with self.tx() as c:
            r = c.execute("SELECT * FROM quotes WHERE symbol=?", (symbol,)).fetchone()
            return dict(r) if r else None

    def set_quote(self, symbol, **updates):
        allowed = {"price", "stamp", "status", "tick"}
        if not set(updates) <= allowed:
            raise ValueError("UNSUPPORTED_QUOTE_FIELD")
        with self.tx() as c:
            for field, value in updates.items():
                c.execute(f"UPDATE quotes SET {field}=? WHERE symbol=?", (value, symbol))
            c.execute("INSERT INTO log(kind,payload) VALUES('QUOTE',?)", (canonical({"symbol": symbol, **updates}),))

    def fill(self, intent, exec_id, qty, price, fee=0):
        cents = price_cents(price)
        if type(qty) is not int or qty <= 0 or type(fee) is not int or fee < 0:
            raise ValueError("INVALID_FILL")
        with self.tx() as c:
            o = c.execute("SELECT * FROM orders WHERE intent=?", (intent,)).fetchone()
            if not o:
                raise ValueError("VENUE_ORDER_MISSING")
            p = json.loads(o["payload"])
            e = {"exec_id": exec_id, "order_id": o["id"], "intent_id": intent, "qty": qty,
                 "price": cents, "fee": fee, "environment": ENV, "account": ACCOUNT}
            previous = c.execute("SELECT payload FROM fills WHERE exec_id=?", (exec_id,)).fetchone()
            if previous:
                if previous[0] != canonical(e):
                    raise ValueError("VENUE_EXECUTION_CONFLICT")
                return e
            if o["state"] in TERMINAL or qty + o["filled"] > p["quantity"]:
                raise ValueError("VENUE_FILL_EXCEEDS_OPEN_ORDER")
            if (p["side"] == "BUY" and cents > p["price"]) or (p["side"] == "SELL" and cents < p["price"]):
                raise ValueError("VENUE_LIMIT_BREACH")
            pos = c.execute("SELECT * FROM positions WHERE symbol=?", (p["symbol"],)).fetchone()
            held, sellable = (pos["qty"], pos["sellable"]) if pos else (0, 0)
            sign = 1 if p["side"] == "BUY" else -1
            held += sign * qty
            sellable += qty if sign == 1 else -qty  # Synthetic same-day sellability only.
            c.execute("UPDATE account SET cash=cash-? WHERE id=1", (sign * qty * cents + fee,))
            c.execute("INSERT INTO positions VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty,sellable=excluded.sellable", (p["symbol"], held, sellable))
            filled = o["filled"] + qty
            state = "FILLED" if filled == p["quantity"] else "PARTIAL"
            c.execute("UPDATE orders SET filled=?,state=? WHERE id=?", (filled, state, o["id"]))
            c.execute("INSERT INTO fills(exec_id,order_id,qty,price,fee,payload) VALUES(?,?,?,?,?,?)", (exec_id, o["id"], qty, cents, fee, canonical(e)))
            c.execute("INSERT INTO log(kind,payload) VALUES('FILL',?)", (canonical(e),))
            return e

    def cancel(self, intent, confirm=False, expire=False, reject=False):
        with self.tx() as c:
            o = c.execute("SELECT * FROM orders WHERE intent=?", (intent,)).fetchone()
            if not o:
                raise ValueError("VENUE_ORDER_MISSING")
            if o["state"] in TERMINAL:
                state = o["state"]
            elif expire:
                state = "EXPIRED"
            elif confirm:
                state = "CANCELLED"
            else:
                state = o["state"]
            pending = 0 if (confirm or expire or reject) else 1
            c.execute("UPDATE orders SET state=?,cancel_pending=? WHERE intent=?", (state, pending, intent))
            c.execute("INSERT INTO log(kind,payload) VALUES('CANCEL',?)", (canonical({"intent": intent,"state":state,"pending":pending,"rejected":reject}),))
            return state

    def external_change(self, cash_delta=0, symbol=None, qty_delta=0):
        """Instrument-only fault injection; never called by the OMS."""
        with self.tx() as c:
            c.execute("UPDATE account SET cash=cash+? WHERE id=1", (cash_delta,))
            if symbol:
                c.execute("INSERT INTO positions VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET qty=qty+excluded.qty,sellable=sellable+excluded.sellable", (symbol,qty_delta,qty_delta))
            c.execute("INSERT INTO log(kind,payload) VALUES('EXTERNAL_CHANGE',?)", (canonical({"cash_delta":cash_delta,"symbol":symbol,"qty_delta":qty_delta}),))

    def snapshot(self):
        with self.tx() as c:
            return {"cash": c.execute("SELECT cash FROM account").fetchone()[0],
                    "positions": {r["symbol"]:{"qty":r["qty"],"sellable":r["sellable"]} for r in c.execute("SELECT * FROM positions ORDER BY symbol")},
                    "orders": [dict(r) for r in c.execute("SELECT * FROM orders ORDER BY intent")],
                    "fills": [json.loads(r[0]) for r in c.execute("SELECT payload FROM fills ORDER BY seq")],
                    "accept_count": c.execute("SELECT count(*) FROM log WHERE kind='ACCEPT'").fetchone()[0],
                    "submit_call_count": c.execute("SELECT count(*) FROM log WHERE kind='SUBMIT_CALL'").fetchone()[0]}


class OrderManager(Store):
    def __init__(self, path, venue, config=None):
        super().__init__(path)
        self.venue = venue
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        with self.tx() as c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY,cash INTEGER,halted INTEGER,reason TEXT);
              CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY,qty INTEGER,sellable INTEGER);
              CREATE TABLE IF NOT EXISTS orders(intent TEXT PRIMARY KEY,payload TEXT,state TEXT,broker_id TEXT,
                filled INTEGER DEFAULT 0,notional INTEGER DEFAULT 0,fees INTEGER DEFAULT 0,
                reserve_cash INTEGER,reserve_qty INTEGER,cancel_pending INTEGER DEFAULT 0,day TEXT);
              CREATE TABLE IF NOT EXISTS fills(exec_key TEXT PRIMARY KEY,payload TEXT);
              CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,input TEXT,
                result TEXT,before_state TEXT,after_state TEXT);
              CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
              CREATE TABLE IF NOT EXISTS halt_reasons(reason TEXT PRIMARY KEY);
            """)
            if not c.in_transaction:
                c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT * FROM account").fetchone()
            if existing:
                saved = json.loads(c.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0])
                if saved != self.config:
                    raise ValueError("CONFIG_CHANGED_REQUIRES_NEW_ISOLATED_DB")
                before = self._snapshot(c)
                c.execute("UPDATE orders SET state='UNKNOWN' WHERE state='SUBMITTING'")
                if not existing["halted"]:
                    self._halt(c, "RESTART_RECONCILE")
                self._record(c,"RESTART",{}, {"paused": True},before)
            else:
                c.execute("INSERT INTO account VALUES(1,?,0,'')", (self.config["initial_cash"],))
                c.execute("INSERT INTO settings VALUES('config',?)", (canonical(self.config),))
                for symbol,p in self.config["initial_positions"].items():
                    c.execute("INSERT INTO positions VALUES(?,?,?)", (symbol,p["qty"],p["sellable"]))
                self._record(c,"INIT",self.config,{"initialized":True},{})

    def _snapshot(self,c):
        a=dict(c.execute("SELECT * FROM account WHERE id=1").fetchone())
        orders=[]
        for r in c.execute("SELECT * FROM orders ORDER BY intent"):
            d=dict(r);d["payload"]=json.loads(d["payload"]);orders.append(d)
        return {"account":a,"active_pauses":[r[0] for r in c.execute("SELECT reason FROM halt_reasons ORDER BY reason")],"positions":{r["symbol"]:{"qty":r["qty"],"sellable":r["sellable"]} for r in c.execute("SELECT * FROM positions ORDER BY symbol")},
                "orders":orders,"fills":[json.loads(r[0]) for r in c.execute("SELECT payload FROM fills ORDER BY exec_key")],
                "reserved_cash":sum(o["reserve_cash"] for o in orders),"reserved_quantity":sum(o["reserve_qty"] for o in orders)}

    def snapshot(self):
        with self.tx() as c:return self._snapshot(c)

    def _record(self,c,kind,data,result,before):
        c.execute("INSERT INTO events(kind,input,result,before_state,after_state) VALUES(?,?,?,?,?)",
                  (kind,canonical(data),canonical(result),canonical(before),canonical(self._snapshot(c))))

    def _halt(self,c,reason):
        c.execute("INSERT OR IGNORE INTO halt_reasons VALUES(?)",(reason,))
        c.execute("UPDATE account SET halted=1,reason=? WHERE id=1", (reason,))

    def pause(self,reason):
        with self.tx() as c:
            before=self._snapshot(c);self._halt(c,reason)
            self._record(c,"PAUSE",{"reason":reason},{"paused":True},before)

    def _normal_order(self, raw):
        if raw.get("environment") != ENV or raw.get("account") != ACCOUNT:
            raise ValueError("ENVIRONMENT_OR_ACCOUNT_REFUSED")
        if not isinstance(raw.get("intent_id"),str) or not raw["intent_id"] or len(raw["intent_id"])>100:
            raise ValueError("INVALID_INTENT_ID")
        if raw.get("symbol") not in self.config["allowed_symbols"] or raw.get("currency") != "CNY":
            raise ValueError("SYMBOL_OR_CURRENCY_REFUSED")
        if raw.get("side") not in {"BUY","SELL"} or type(raw.get("quantity")) is not int or raw["quantity"]<=0:
            raise ValueError("INVALID_SIDE_OR_QUANTITY")
        return {k:raw[k] for k in ("environment","account","symbol","currency","side","quantity")} | {"price":price_cents(raw.get("price"))}

    def submit(self,raw,now,crash_after_accept=False):
        """Persist SUBMITTING before accepting at separate venue; crash is a test-only hook."""
        with self.tx() as c:
            before=self._snapshot(c)
            try:
                p=self._normal_order(raw)
                prior=c.execute("SELECT * FROM orders WHERE intent=?",(raw["intent_id"],)).fetchone()
                if prior:
                    if prior["payload"]!=canonical(p):raise ValueError("INTENT_PARAMETER_CONFLICT")
                    result={"accepted":None if prior["state"] in {"SUBMITTING","UNKNOWN"} else True,"duplicate":True,"state":prior["state"],"broker_id":prior["broker_id"],"query_required":prior["state"] in {"SUBMITTING","UNKNOWN"}}
                    self._record(c,"SUBMIT",raw,result,before);return result
                account=c.execute("SELECT * FROM account").fetchone()
                if account["halted"]:raise ValueError("PAUSED:"+account["reason"])
                t=aware_time(now);q=self.venue.quote(p["symbol"])
                age=(t-aware_time(q["stamp"])).total_seconds()
                if age<0 or age>self.config["quote_max_age"]:raise ValueError("QUOTE_TIME_INVALID")
                if q["status"]!="TRADING":raise ValueError("SYMBOL_NOT_TRADING")
                if p["quantity"]%q["lot"] or p["price"]%q["tick"]:raise ValueError("LOT_OR_TICK_INVALID")
                if abs(p["price"]-q["price"])*100>q["price"]:raise ValueError("PRICE_OUTSIDE_TEST_BOUND")
                local=t.astimezone(ZoneInfo("Asia/Shanghai"));day=local.date().isoformat();clock=local.strftime("%H:%M:%S")
                if not any(start+":00"<=clock<end+":00" for start,end in self.config["calendar"].get(day,[])):
                    raise ValueError("OUTSIDE_SYNTHETIC_SESSION")
                orders=before["orders"];notional=p["price"]*p["quantity"]
                if notional>self.config["per_order_cap"]:raise ValueError("PER_ORDER_CAP")
                pending=sum((o["payload"]["quantity"]-o["filled"])*o["payload"]["price"] for o in orders if o["state"] not in TERMINAL)
                if pending+notional>self.config["inflight_cap"]:raise ValueError("INFLIGHT_CAP")
                if sum(o["state"] not in TERMINAL for o in orders)>=self.config["max_open_orders"]:raise ValueError("OPEN_ORDER_CAP")
                daily=[o for o in orders if o["day"]==day]
                if len(daily)>=self.config["daily_submit_cap"]:raise ValueError("DAILY_SUBMIT_CAP")
                gross=sum(o["notional"]+(0 if o["state"] in TERMINAL else (o["payload"]["quantity"]-o["filled"])*o["payload"]["price"]) for o in daily)
                if gross+notional>self.config["daily_gross_cap"]:raise ValueError("DAILY_GROSS_CAP")
                rc=notional+self.config["fee_cap"] if p["side"]=="BUY" else 0
                rq=p["quantity"] if p["side"]=="SELL" else 0
                if rc+before["reserved_cash"]>account["cash"]:raise ValueError("INSUFFICIENT_UNRESERVED_CASH")
                pos=before["positions"].get(p["symbol"],{"qty":0,"sellable":0})
                reserved=sum(o["reserve_qty"] for o in orders if o["payload"]["symbol"]==p["symbol"])
                if rq+reserved>pos["sellable"]:raise ValueError("INSUFFICIENT_UNRESERVED_SELLABLE")
                c.execute("INSERT INTO orders(intent,payload,state,reserve_cash,reserve_qty,day) VALUES(?,?,'SUBMITTING',?,?,?)",(raw["intent_id"],canonical(p),rc,rq,day))
                self._record(c,"SUBMIT_PREPARE",raw,{"accepted":True,"state":"SUBMITTING"},before)
            except (ValueError,TypeError,KeyError) as e:
                result={"accepted":False,"reason":str(e)};self._record(c,"SUBMIT",raw,result,before);return result
        try:
            oid=self.venue.submit(raw["intent_id"],p)
        except Exception as error:
            # Transport failure is ambiguous, including failure before venue acceptance.
            # Persist uncertainty and reserve funds; another submit must not retry it.
            with self.tx() as c:
                before=self._snapshot(c)
                c.execute("UPDATE orders SET state='UNKNOWN' WHERE intent=?",(raw["intent_id"],))
                self._halt(c,"REPLY_LOST")
                result={"accepted":None,"state":"UNKNOWN","query_required":True,
                        "reason":type(error).__name__}
                self._record(c,"SEND_UNKNOWN",{"intent_id":raw["intent_id"]},result,before)
            return result
        if crash_after_accept:
            os._exit(77)  # Deliberate process termination AFTER both durable commits.
        with self.tx() as c:
            before=self._snapshot(c)
            c.execute("UPDATE orders SET broker_id=?,state='ACKNOWLEDGED' WHERE intent=? AND state='SUBMITTING'",(oid,raw["intent_id"]))
            result={"accepted":True,"duplicate":False,"state":"ACKNOWLEDGED","broker_id":oid}
            self._record(c,"ACK",{"intent_id":raw["intent_id"]},result,before);return result

    def _fill(self,c,event):
        if event.get("environment")!=ENV or event.get("account")!=ACCOUNT:raise ValueError("FILL_ENVIRONMENT_REFUSED")
        key=canonical([event["environment"],event["account"],event["order_id"],event["exec_id"]])
        prior=c.execute("SELECT payload FROM fills WHERE exec_key=?",(key,)).fetchone()
        if prior:
            if prior[0]!=canonical(event):raise ValueError("EXECUTION_PARAMETER_CONFLICT")
            return {"applied":False,"duplicate":True}
        o=c.execute("SELECT * FROM orders WHERE intent=?",(event["intent_id"],)).fetchone()
        if not o or o["broker_id"]!=event["order_id"]:raise ValueError("FILL_ORDER_UNBOUND")
        p=json.loads(o["payload"]);qty,price,fee=event["qty"],event["price"],event["fee"]
        if any(type(v) is not int for v in (qty,price,fee)) or qty<=0 or price<=0 or fee<0:raise ValueError("INVALID_FILL")
        if o["state"] in TERMINAL:raise ValueError("NEW_FILL_AFTER_TERMINAL_REQUIRES_REVIEW")
        if o["filled"]+qty>p["quantity"]:raise ValueError("OVERFILL")
        if (p["side"]=="BUY" and price>p["price"]) or (p["side"]=="SELL" and price<p["price"]):raise ValueError("LIMIT_PRICE_BREACH")
        if o["fees"]+fee>self.config["fee_cap"]:raise ValueError("FEE_CAP_EXCEEDED")
        pos=c.execute("SELECT * FROM positions WHERE symbol=?",(p["symbol"],)).fetchone()
        held,sellable=(pos["qty"],pos["sellable"]) if pos else (0,0)
        sign=1 if p["side"]=="BUY" else -1
        cash=c.execute("SELECT cash FROM account").fetchone()[0]-sign*qty*price-fee
        held+=sign*qty;sellable+=sign*qty
        filled=o["filled"]+qty;remaining=p["quantity"]-filled;fees=o["fees"]+fee
        if cash<0 or held<0 or sellable<0:raise ValueError("NEGATIVE_BALANCE")
        rc=remaining*p["price"]+max(0,self.config["fee_cap"]-fees) if p["side"]=="BUY" and remaining else 0
        rq=remaining if p["side"]=="SELL" else 0
        # CANCEL_PENDING survives partial fills: cancellation is a separate flag.
        state="FILLED" if not remaining else ("CANCEL_PENDING" if o["cancel_pending"] else "PARTIAL")
        c.execute("UPDATE account SET cash=? WHERE id=1",(cash,))
        c.execute("INSERT INTO positions VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty,sellable=excluded.sellable",(p["symbol"],held,sellable))
        c.execute("UPDATE orders SET filled=?,notional=notional+?,fees=?,reserve_cash=?,reserve_qty=?,state=? WHERE intent=?",(filled,qty*price,fees,rc,rq,state,o["intent"]))
        c.execute("INSERT INTO fills VALUES(?,?)",(key,canonical(event)))
        return {"applied":True,"duplicate":False}

    def receive_fill(self,event):
        with self.tx() as c:
            before=self._snapshot(c)
            try:result=self._fill(c,event)
            except (ValueError,KeyError,TypeError) as e:
                self._halt(c,str(e));result={"applied":False,"reason":str(e)}
            self._record(c,"FILL",event,result,before);return result

    def request_cancel(self,intent):
        with self.tx() as c:
            before=self._snapshot(c);o=c.execute("SELECT * FROM orders WHERE intent=?",(intent,)).fetchone()
            if not o or not o["broker_id"]:result={"accepted":False,"reason":"ORDER_UNBOUND"}
            elif o["state"] in TERMINAL:result={"accepted":True,"terminal":True}
            else:
                c.execute("UPDATE orders SET cancel_pending=1,state='CANCEL_PENDING' WHERE intent=?",(intent,));result={"accepted":True}
            self._record(c,"CANCEL_REQUEST",{"intent":intent},result,before)
        if result["accepted"] and not result.get("terminal"):self.venue.cancel(intent)
        return result

    def cancel_response(self,intent,state):
        with self.tx() as c:
            before=self._snapshot(c);o=c.execute("SELECT * FROM orders WHERE intent=?",(intent,)).fetchone()
            if not o:result={"accepted":False,"reason":"UNKNOWN_ORDER"}
            elif o["state"]=="FILLED":result={"accepted":True,"preserved_filled":True}
            elif state in {"CANCELLED","EXPIRED"}:
                c.execute("UPDATE orders SET state=?,reserve_cash=0,reserve_qty=0,cancel_pending=0 WHERE intent=?",(state,intent));result={"accepted":True}
            elif state=="CANCEL_REJECTED":
                c.execute("UPDATE orders SET state=?,cancel_pending=0 WHERE intent=?",("PARTIAL" if o["filled"] else "ACKNOWLEDGED",intent));result={"accepted":False,"reason":"CANCEL_REJECTED"}
            else:result={"accepted":False,"reason":"CANCEL_UNCONFIRMED"}
            self._record(c,"CANCEL_RESPONSE",{"intent":intent,"state":state},result,before);return result

    def reconcile(self,resume=False):
        v=self.venue.snapshot()
        with self.tx() as c:
            before=self._snapshot(c);diff=[]
            local={r["intent"]:r for r in c.execute("SELECT * FROM orders")}
            for vo in v["orders"]:
                lo=local.get(vo["intent"])
                if not lo:diff.append("EXTRA_VENUE_ORDER:"+vo["intent"]);continue
                if lo["payload"]!=vo["payload"]:diff.append("ORDER_PAYLOAD_MISMATCH:"+vo["intent"]);continue
                c.execute("UPDATE orders SET broker_id=? WHERE intent=?",(vo["id"],vo["intent"]))
            for event in v["fills"]:
                try:self._fill(c,event)
                except (ValueError,KeyError,TypeError) as e:diff.append(str(e))
            venue_by={o["intent"]:o for o in v["orders"]}
            for lo in list(c.execute("SELECT * FROM orders")):
                vo=venue_by.get(lo["intent"])
                if not vo:diff.append("UNRESOLVED_SUBMISSION:"+lo["intent"]);continue
                if lo["filled"]!=vo["filled"]:diff.append("CUMULATIVE_FILL_MISMATCH:"+lo["intent"]);continue
                if lo["payload"]!=vo["payload"]:continue
                state=vo["state"]
                if state not in TERMINAL and vo["cancel_pending"]:state="CANCEL_PENDING"
                rc,rq=lo["reserve_cash"],lo["reserve_qty"]
                if state in TERMINAL:rc,rq=0,0
                c.execute("UPDATE orders SET state=?,cancel_pending=?,reserve_cash=?,reserve_qty=? WHERE intent=?",(state,vo["cancel_pending"],rc,rq,lo["intent"]))
            after=self._snapshot(c)
            if after["account"]["cash"]!=v["cash"]:diff.append("CASH_MISMATCH")
            # Zero rows are not economically different from absent positions.
            lpos={s:p for s,p in after["positions"].items() if p["qty"] or p["sellable"]}
            vpos={s:p for s,p in v["positions"].items() if p["qty"] or p["sellable"]}
            if lpos!=vpos:diff.append("POSITION_MISMATCH")
            # A matching snapshot clears only evidence-backed recovery causes.
            # MANUAL, TEST_LOSS_LIMIT and quarantined adjustments are independent.
            allowed_resume={"RESTART_RECONCILE", "DISCONNECTED", "SLEEP", "REPLY_LOST", "RECONCILE_DIFFERENCE"}
            reasons={r[0] for r in c.execute("SELECT reason FROM halt_reasons")}
            blockers=sorted(reasons-allowed_resume)
            resumed=resume and not diff and not blockers
            if diff:self._halt(c,"RECONCILE_DIFFERENCE")
            elif resumed:
                c.execute("DELETE FROM halt_reasons")
                c.execute("UPDATE account SET halted=0,reason='' WHERE id=1")
            result={"matched":not diff,"differences":diff,"resumed":resumed,"resume_blocked":bool(resume and not diff and not resumed),"blocking_pause_reasons":blockers}
            self._record(c,"RECONCILE",{"venue_snapshot":v,"resume":resume},result,before);return result

    def mark_risk(self,marks):
        with self.tx() as c:
            before=self._snapshot(c);equity=before["account"]["cash"]
            for symbol,p in before["positions"].items():
                if p["qty"]:
                    if symbol not in marks:
                        self._halt(c,"MISSING_SYNTHETIC_MARK");self._record(c,"MARK_RISK",marks,{"paused":True},before);return
                    equity+=p["qty"]*price_cents(marks[symbol])
            loss=self.config["initial_cash"]-equity
            if loss>=self.config["loss_limit"]:self._halt(c,"TEST_LOSS_LIMIT")
            result={"equity":equity,"loss":loss,"paused":bool(c.execute("SELECT halted FROM account").fetchone()[0])}
            self._record(c,"MARK_RISK",marks,result,before);return result

    def unsupported_adjustment(self,event):
        with self.tx() as c:
            before=self._snapshot(c);self._halt(c,"UNSUPPORTED_FEE_OR_FILL_ADJUSTMENT")
            result={"applied":False,"quarantined":True,"reason":"NOT_IMPLEMENTED"}
            self._record(c,"ADJUSTMENT_QUARANTINE",event,result,before);return result

    def audit(self):
        with self.tx() as c:
            return [{"seq":r["seq"],"kind":r["kind"],"input":json.loads(r["input"]),"result":json.loads(r["result"]),
                     "before":json.loads(r["before_state"]),"after":json.loads(r["after_state"])} for r in c.execute("SELECT * FROM events ORDER BY seq")]
