"""Offline persistent OMS extensions; no network adapter or credentials.

Monetary quantities use integer cents and synthetic same-day sellability. The
reused implementation retains first-phase risk controls. Corrections replace an
execution's economic values, with revisioned evidence and independent venue
accounting. Cumulative status reports are never incremental execution events.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Protocol

from ._reused import (ACCOUNT, ENV, DEFAULT_CONFIG, TERMINAL, canonical,
                      FictionalVenue as ReusedVenue, OrderManager as ReusedOMS, aware_time)


@dataclass(frozen=True)
class SimulationBinding:
    environment: str
    account: str
    allowed_accounts: tuple[str, ...]

    def validate(self):
        # This finite implementation only supports this fictional offline venue.
        if self.environment != ENV or self.account != ACCOUNT:
            raise ValueError("OFFLINE_BINDING_REQUIRED")
        if not self.allowed_accounts or self.account not in self.allowed_accounts:
            raise ValueError("ACCOUNT_NOT_EXPLICITLY_WHITELISTED")
        if any(account != ACCOUNT for account in self.allowed_accounts):
            raise ValueError("ONLY_FICTIONAL_ACCOUNT_ALLOWED")
        return self

    def as_dict(self):
        return {"environment": self.environment, "account": self.account,
                "allowed_accounts": list(self.allowed_accounts)}


class OfflineAdapter(Protocol):
    """A local-only contract. It does not authorize implementing a broker sender."""
    binding: SimulationBinding
    def submit(self, intent: str, payload: dict) -> str: ...
    def query_intent(self, intent: str) -> dict: ...
    def snapshot(self) -> dict: ...
    def quote(self, symbol: str) -> dict: ...
    def cancel(self, intent: str): ...


def _execution_key(event):
    return canonical([event["environment"], event["account"], event["order_id"], event["exec_id"]])


def _validate_correction(event):
    if event.get("environment") != ENV or event.get("account") != ACCOUNT:
        raise ValueError("CORRECTION_BINDING_REFUSED")
    for key in ("adjustment_id", "intent_id", "order_id", "exec_id"):
        if not isinstance(event.get(key), str) or not event[key]:
            raise ValueError("INVALID_CORRECTION_ID")
    if any(type(event.get(key)) is not int for key in ("revision", "qty", "price", "fee")):
        raise ValueError("INVALID_CORRECTION_UNITS")
    if event["revision"] < 1 or event["qty"] < 0 or event["price"] <= 0 or event["fee"] < 0:
        raise ValueError("INVALID_CORRECTION_VALUES")


class FictionalVenue(ReusedVenue):
    def __init__(self, path, config, now, *, binding: SimulationBinding):
        self.binding = binding.validate()
        super().__init__(path, config, now)
        with self.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS binding(payload TEXT NOT NULL)")
            saved = c.execute("SELECT payload FROM binding").fetchone()
            if saved and saved[0] != canonical(binding.as_dict()):
                raise ValueError("VENUE_BINDING_CHANGED")
            if not saved:
                c.execute("INSERT INTO binding VALUES(?)", (canonical(binding.as_dict()),))
            c.execute("""CREATE TABLE IF NOT EXISTS corrections(
                adjustment_id TEXT PRIMARY KEY,exec_key TEXT,revision INTEGER,payload TEXT,
                UNIQUE(exec_key,revision))""")

    def query_intent(self, intent):
        with self.tx() as c:
            row = c.execute("SELECT * FROM orders WHERE intent=?", (intent,)).fetchone()
            c.execute("INSERT INTO log(kind,payload) VALUES('QUERY_INTENT',?)", (canonical({"intent": intent}),))
            return {"found": bool(row), "authoritative_absence": not row,
                    "order": dict(row) if row else None, **self.binding.as_dict()}

    def correct_execution(self, event):
        """Test-side authoritative revision: replacement values, never deltas."""
        _validate_correction(event)
        with self.tx() as c:
            prior = c.execute("SELECT payload FROM corrections WHERE adjustment_id=?", (event["adjustment_id"],)).fetchone()
            if prior:
                if prior[0] != canonical(event):
                    raise ValueError("CORRECTION_ID_CONFLICT")
                return event
            fill = c.execute("SELECT * FROM fills WHERE exec_id=?", (event["exec_id"],)).fetchone()
            order = c.execute("SELECT * FROM orders WHERE intent=?", (event["intent_id"],)).fetchone()
            if not fill or not order or fill["order_id"] != event["order_id"] or order["id"] != event["order_id"]:
                raise ValueError("CORRECTION_ORDER_UNBOUND")
            key = _execution_key(event)
            last = c.execute("SELECT revision,payload FROM corrections WHERE exec_key=? ORDER BY revision DESC LIMIT 1", (key,)).fetchone()
            if event["revision"] != (last["revision"] if last else 0) + 1:
                raise ValueError("CORRECTION_REVISION_GAP")
            old = json.loads(last["payload"] if last else fill["payload"])
            p = json.loads(order["payload"])
            qty_delta = event["qty"] - old["qty"]
            filled = order["filled"] + qty_delta
            if not 0 <= filled <= p["quantity"]:
                raise ValueError("CORRECTION_OVERFILL")
            # Independent venue formula uses a cash increment; OMS uses old/new debits.
            sign = 1 if p["side"] == "BUY" else -1
            cash_increment = sign * (old["qty"] * old["price"] - event["qty"] * event["price"]) + old["fee"] - event["fee"]
            c.execute("UPDATE account SET cash=cash+?", (cash_increment,))
            c.execute("UPDATE positions SET qty=qty+?,sellable=sellable+? WHERE symbol=?", (sign * qty_delta, sign * qty_delta, p["symbol"]))
            # A bust of a fully filled order restores an open quantity at this venue.
            state = "FILLED" if filled == p["quantity"] else ("PARTIAL" if order["state"] == "FILLED" else order["state"])
            c.execute("UPDATE orders SET filled=?,state=? WHERE id=?", (filled, state, order["id"]))
            c.execute("INSERT INTO corrections VALUES(?,?,?,?)", (event["adjustment_id"], key, event["revision"], canonical(event)))
            c.execute("INSERT INTO log(kind,payload) VALUES('CORRECTION',?)", (canonical(event),))
            return event

    def snapshot(self):
        with self.tx() as c:
            return {"cash": c.execute("SELECT cash FROM account").fetchone()[0],
                    "positions": {r["symbol"]: {"qty": r["qty"], "sellable": r["sellable"]}
                                  for r in c.execute("SELECT * FROM positions ORDER BY symbol")},
                    "orders": [dict(r) for r in c.execute("SELECT * FROM orders ORDER BY intent")],
                    "fills": [json.loads(r[0]) for r in c.execute("SELECT payload FROM fills ORDER BY seq")],
                    "accept_count": c.execute("SELECT count(*) FROM log WHERE kind='ACCEPT'").fetchone()[0],
                    "submit_call_count": c.execute("SELECT count(*) FROM log WHERE kind='SUBMIT_CALL'").fetchone()[0],
                    "corrections": [json.loads(row[0]) for row in c.execute("SELECT payload FROM corrections ORDER BY rowid")],
                    "binding": self.binding.as_dict()}


class OrderManager(ReusedOMS):
    def __init__(self, path, venue: OfflineAdapter, config=None, *, binding: SimulationBinding):
        self.binding = binding.validate()
        if getattr(venue, "binding", None) != binding:
            raise ValueError("ADAPTER_BINDING_MISMATCH")
        # Check persistent identity before the reused constructor mutates restart state.
        from pathlib import Path
        import sqlite3
        if Path(path).exists():
            with sqlite3.connect(path) as c:
                exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='settings'").fetchone()
                saved = c.execute("SELECT value FROM settings WHERE key='binding'").fetchone() if exists else None
                if not saved or saved[0] != canonical(binding.as_dict()):
                    raise ValueError("PERSISTED_BINDING_MISMATCH")
        super().__init__(path, venue, config)
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO settings VALUES('binding',?)", (canonical(binding.as_dict()),))
            c.execute("CREATE TABLE IF NOT EXISTS cancel_attempts(intent TEXT PRIMARY KEY,attempt INTEGER)")
            c.execute("""CREATE TABLE IF NOT EXISTS corrections(
                adjustment_id TEXT PRIMARY KEY,exec_key TEXT,revision INTEGER,payload TEXT,
                old_payload TEXT,UNIQUE(exec_key,revision))""")

    def _snapshot(self, c):
        result = super()._snapshot(c)
        result["binding"] = self.binding.as_dict()
        table = c.execute("SELECT 1 FROM sqlite_master WHERE name='corrections'").fetchone()
        result["corrections"] = [json.loads(r[0]) for r in c.execute("SELECT payload FROM corrections ORDER BY rowid")] if table else []
        return result

    def _fill(self, c, event):
        key = _execution_key(event)
        prior = c.execute("SELECT payload FROM fills WHERE exec_key=?", (key,)).fetchone()
        if prior and prior[0] != canonical(event):
            # Old execution reports after a revision must not undo that revision.
            old = c.execute("SELECT 1 FROM corrections WHERE exec_key=? AND old_payload=?", (key, canonical(event))).fetchone()
            if old:
                return {"applied": False, "duplicate": True, "stale_revision": True}
        order = c.execute("SELECT * FROM orders WHERE intent=?", (event["intent_id"],)).fetchone()
        state = order["state"] if order else None
        if state in {"CANCELLED", "EXPIRED"} and not prior:
            result = self._late_terminal_fill(c, event, order)
        else:
            result = super()._fill(c, event)
        reserved = c.execute("SELECT sum(reserve_cash) FROM orders").fetchone()[0] or 0
        cash = c.execute("SELECT cash FROM account").fetchone()[0]
        if reserved > cash:
            self._halt(c, "AUTHORITATIVE_FILL_RESERVATION_BREACH")
        for symbol, held, sellable in c.execute("SELECT symbol,qty,sellable FROM positions"):
            reserved_qty = c.execute("SELECT sum(reserve_qty) FROM orders WHERE json_extract(payload,'$.symbol')=?", (symbol,)).fetchone()[0] or 0
            if reserved_qty > sellable:
                self._halt(c, "AUTHORITATIVE_FILL_QUANTITY_RESERVATION_BREACH")
        return result

    def _late_terminal_fill(self, c, event, order):
        """Book a valid delayed execution even after its reservation was released.

        Balance or limit breaches are recorded after economic booking and block
        automatic resume; they cannot erase a known authoritative execution.
        """
        if event.get("environment") != ENV or event.get("account") != ACCOUNT:
            raise ValueError("FILL_ENVIRONMENT_REFUSED")
        if event["order_id"] != order["broker_id"]:
            raise ValueError("FILL_ORDER_UNBOUND")
        qty, price, fee = event["qty"], event["price"], event["fee"]
        if any(type(v) is not int for v in (qty, price, fee)) or qty <= 0 or price <= 0 or fee < 0:
            raise ValueError("INVALID_FILL")
        p = json.loads(order["payload"])
        filled = order["filled"] + qty
        if filled > p["quantity"]:
            raise ValueError("OVERFILL")
        sign = 1 if p["side"] == "BUY" else -1
        pos = c.execute("SELECT qty,sellable FROM positions WHERE symbol=?", (p["symbol"],)).fetchone()
        held, sellable = (pos[0], pos[1]) if pos else (0, 0)
        held += sign * qty
        sellable += sign * qty
        cash = c.execute("SELECT cash FROM account").fetchone()[0] - sign * qty * price - fee
        fees = order["fees"] + fee
        state = "FILLED" if filled == p["quantity"] else order["state"]
        c.execute("UPDATE account SET cash=?", (cash,))
        c.execute("INSERT INTO positions VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty,sellable=excluded.sellable", (p["symbol"], held, sellable))
        c.execute("UPDATE orders SET filled=?,notional=notional+?,fees=?,state=?,reserve_cash=0,reserve_qty=0 WHERE intent=?", (filled, qty * price, fees, state, order["intent"]))
        c.execute("INSERT INTO fills VALUES(?,?)", (_execution_key(event), canonical(event)))
        if cash < 0 or held < 0 or sellable < 0:
            self._halt(c, "AUTHORITATIVE_FILL_NEGATIVE_BALANCE")
        if fees > self.config["fee_cap"]:
            self._halt(c, "AUTHORITATIVE_FILL_FEE_CAP_EXCEEDED")
        if (p["side"] == "BUY" and price > p["price"]) or (p["side"] == "SELL" and price < p["price"]):
            self._halt(c, "AUTHORITATIVE_FILL_LIMIT_BREACH")
        return {"applied": True, "duplicate": False, "late_after_terminal": True}

    def recover_submission(self, intent, *, retry_if_absent=False, now=None):
        """Query first. Only final offline absence permits an explicit retry."""
        with self.tx() as c:
            order = c.execute("SELECT * FROM orders WHERE intent=?", (intent,)).fetchone()
            if not order or order["state"] not in {"UNKNOWN", "SUBMITTING"}:
                raise ValueError("NO_UNKNOWN_SUBMISSION")
            payload = json.loads(order["payload"])
        query = self.venue.query_intent(intent)
        if query.get("environment") != self.binding.environment or query.get("account") != self.binding.account:
            self.pause("QUERY_BINDING_MISMATCH")
            return {"resolved": False, "reason": "QUERY_BINDING_MISMATCH"}
        with self.tx() as c:
            before = self._snapshot(c)
            self._record(c, "QUERY_BEFORE_RETRY", {"intent": intent}, query, before)
        if query["found"]:
            report = self.reconcile(resume=True)
            return {"resolved": report["matched"], "resent": False, "reconciliation": report}
        if not retry_if_absent or not query.get("authoritative_absence"):
            return {"resolved": False, "resent": False, "reason": "AUTHORITATIVE_ABSENCE_AND_EXPLICIT_RETRY_REQUIRED"}
        with self.tx() as c:
            before = self._snapshot(c)
            row = c.execute("SELECT state FROM orders WHERE intent=?", (intent,)).fetchone()
            if row[0] != "UNKNOWN":
                return {"resolved": False, "resent": False, "reason": "STATE_CHANGED_DURING_QUERY"}
            reasons = {r[0] for r in c.execute("SELECT reason FROM halt_reasons")}
            blockers = sorted(reasons - {"RESTART_RECONCILE", "REPLY_LOST"})
            if blockers:
                result = {"resolved": False, "resent": False, "reason": "INDEPENDENT_PAUSE_BLOCKS_RETRY", "blocking_pause_reasons": blockers}
                self._record(c, "RETRY_BLOCKED", {"intent": intent}, result, before)
                return result
            try:
                self._validate_retry(c, intent, payload, now)
            except (ValueError, KeyError, TypeError) as error:
                result = {"resolved": False, "resent": False, "reason": str(error)}
                self._record(c, "RETRY_BLOCKED", {"intent": intent}, result, before)
                return result
            c.execute("UPDATE orders SET state='SUBMITTING' WHERE intent=?", (intent,))
            self._record(c, "RETRY_PREPARE", {"intent": intent}, {"query_confirmed_absent": True}, before)
        try:
            broker_id = self.venue.submit(intent, payload)
        except Exception as error:
            with self.tx() as c:
                before = self._snapshot(c)
                c.execute("UPDATE orders SET state='UNKNOWN' WHERE intent=?", (intent,))
                self._halt(c, "REPLY_LOST")
                self._record(c, "RETRY_UNKNOWN", {"intent": intent}, {"error": type(error).__name__}, before)
            return {"resolved": False, "resent": True, "reason": "UNKNOWN"}
        with self.tx() as c:
            before = self._snapshot(c)
            c.execute("UPDATE orders SET broker_id=?,state='ACKNOWLEDGED' WHERE intent=?", (broker_id, intent))
            self._record(c, "RETRY_ACK", {"intent": intent}, {"broker_id": broker_id}, before)
        report = self.reconcile(resume=True)
        return {"resolved": report["matched"], "resent": True, "reconciliation": report}

    def _validate_retry(self, c, intent, payload, now):
        if now is None:
            raise ValueError("RETRY_TIME_REQUIRED")
        from zoneinfo import ZoneInfo
        t = aware_time(now)
        quote = self.venue.quote(payload["symbol"])
        age = (t - aware_time(quote["stamp"])).total_seconds()
        if age < 0 or age > self.config["quote_max_age"]:
            raise ValueError("RETRY_QUOTE_TIME_INVALID")
        if quote["status"] != "TRADING":
            raise ValueError("RETRY_SYMBOL_NOT_TRADING")
        if payload["quantity"] % quote["lot"] or payload["price"] % quote["tick"]:
            raise ValueError("RETRY_LOT_OR_TICK_INVALID")
        if abs(payload["price"] - quote["price"]) * 100 > quote["price"]:
            raise ValueError("RETRY_PRICE_OUTSIDE_TEST_BOUND")
        local = t.astimezone(ZoneInfo("Asia/Shanghai"))
        day, clock = local.date().isoformat(), local.strftime("%H:%M:%S")
        if not any(start + ":00" <= clock < end + ":00" for start, end in self.config["calendar"].get(day, [])):
            raise ValueError("RETRY_OUTSIDE_SYNTHETIC_SESSION")
        order = c.execute("SELECT day,reserve_cash,reserve_qty FROM orders WHERE intent=?", (intent,)).fetchone()
        if order["day"] != day:
            raise ValueError("RETRY_DAY_CHANGED_NEW_INTENT_REQUIRED")
        snapshot = self._snapshot(c)
        if snapshot["reserved_cash"] > snapshot["account"]["cash"]:
            raise ValueError("RETRY_INSUFFICIENT_RESERVED_CASH")
        position = snapshot["positions"].get(payload["symbol"], {"sellable": 0})
        qty_reserved = sum(o["reserve_qty"] for o in snapshot["orders"] if o["payload"]["symbol"] == payload["symbol"])
        if qty_reserved > position["sellable"]:
            raise ValueError("RETRY_INSUFFICIENT_RESERVED_SELLABLE")

    def request_cancel(self, intent):
        with self.tx() as c:
            before = self._snapshot(c)
            order = c.execute("SELECT * FROM orders WHERE intent=?", (intent,)).fetchone()
            if not order or not order["broker_id"]:
                result = {"accepted": False, "reason": "ORDER_UNBOUND"}
            elif order["state"] in TERMINAL:
                result = {"accepted": True, "terminal": True}
            elif order["cancel_pending"]:
                result = {"accepted": None, "duplicate": True, "cancel_confirmed": False, "query_required": True}
            else:
                prior = c.execute("SELECT attempt FROM cancel_attempts WHERE intent=?", (intent,)).fetchone()
                attempt = (prior[0] if prior else 0) + 1
                c.execute("INSERT INTO cancel_attempts VALUES(?,?) ON CONFLICT(intent) DO UPDATE SET attempt=excluded.attempt", (intent, attempt))
                c.execute("UPDATE orders SET cancel_pending=1,state='CANCEL_PENDING' WHERE intent=?", (intent,))
                result = {"accepted": True, "request_id": intent + ":cancel:" + str(attempt)}
            self._record(c, "CANCEL_REQUEST", {"intent": intent}, result, before)
        if result["accepted"] is True and not result.get("terminal"):
            try:
                self.venue.cancel(intent)
            except Exception as error:
                self.pause("CANCEL_REPLY_LOST")
                return {"accepted": None, "cancel_confirmed": False, "query_required": True,
                        "request_id": result["request_id"], "reason": type(error).__name__}
        return result

    def cancel_response(self, intent, state, *, request_id=None):
        # State guard and transition share one immediate transaction. Another
        # callback cannot confirm cancellation between guard and mutation.
        with self.tx() as c:
            before = self._snapshot(c)
            order = c.execute("SELECT * FROM orders WHERE intent=?", (intent,)).fetchone()
            if not order:
                result = {"accepted": False, "reason": "UNKNOWN_ORDER"}
            elif order["state"] in TERMINAL:
                result = {"accepted": True, "terminal": True, "preserved_terminal": order["state"],
                          "preserved_filled": order["state"] == "FILLED"}
            elif state in {"CANCELLED", "EXPIRED"}:
                c.execute("UPDATE orders SET state=?,reserve_cash=0,reserve_qty=0,cancel_pending=0 WHERE intent=?", (state, intent))
                result = {"accepted": True}
            elif state == "CANCEL_REJECTED":
                attempt = c.execute("SELECT attempt FROM cancel_attempts WHERE intent=?", (intent,)).fetchone()
                current = intent + ":cancel:" + str(attempt[0]) if attempt else None
                reason = None
                if not order["cancel_pending"]:
                    reason = "NO_PENDING_CANCEL"
                elif request_id is not None and request_id != current:
                    reason = "STALE_CANCEL_REQUEST_ID"
                elif attempt and attempt[0] > 1 and request_id is None:
                    reason = "CANCEL_CORRELATION_REQUIRED"
                if reason:
                    result = {"accepted": False, "reason": reason}
                else:
                    c.execute("UPDATE orders SET state=?,cancel_pending=0 WHERE intent=?", ("PARTIAL" if order["filled"] else "ACKNOWLEDGED", intent))
                    result = {"accepted": False, "reason": "CANCEL_REJECTED"}
            else:
                result = {"accepted": False, "reason": "CANCEL_UNCONFIRMED"}
            self._record(c, "CANCEL_RESPONSE", {"intent": intent, "state": state, "request_id": request_id}, result, before)
            return result

    def receive_cumulative(self, event):
        """Status-only totals: compare against executions; never add them as fills."""
        with self.tx() as c:
            before = self._snapshot(c)
            order = c.execute("SELECT * FROM orders WHERE intent=?", (event.get("intent_id"),)).fetchone()
            if (event.get("environment") != ENV or event.get("account") != ACCOUNT or
                not order or event.get("order_id") != order["broker_id"]):
                reason = "CUMULATIVE_REPORT_UNBOUND"
            elif type(event.get("cumulative_qty")) is not int or not 0 <= event["cumulative_qty"] <= json.loads(order["payload"])["quantity"]:
                reason = "INVALID_CUMULATIVE_QUANTITY"
            elif event["cumulative_qty"] != order["filled"]:
                reason = "CUMULATIVE_REQUIRES_EXECUTION_QUERY"
            else:
                reason = None
            if reason:
                self._halt(c, reason)
            result = {"matched": reason is None, "applied": False, "reason": reason}
            self._record(c, "CUMULATIVE_REPORT", event, result, before)
            return result

    def _correction(self, c, event):
        _validate_correction(event)
        prior = c.execute("SELECT payload FROM corrections WHERE adjustment_id=?", (event["adjustment_id"],)).fetchone()
        if prior:
            if prior[0] != canonical(event):
                raise ValueError("CORRECTION_ID_CONFLICT")
            return {"applied": False, "duplicate": True}
        key = _execution_key(event)
        fill = c.execute("SELECT payload FROM fills WHERE exec_key=?", (key,)).fetchone()
        order = c.execute("SELECT * FROM orders WHERE intent=?", (event["intent_id"],)).fetchone()
        if not fill or not order or order["broker_id"] != event["order_id"]:
            raise ValueError("CORRECTION_ORDER_UNBOUND")
        revision = c.execute("SELECT max(revision) FROM corrections WHERE exec_key=?", (key,)).fetchone()[0] or 0
        if event["revision"] != revision + 1:
            raise ValueError("CORRECTION_REVISION_GAP")
        old = json.loads(fill[0])
        p = json.loads(order["payload"])
        dq = event["qty"] - old["qty"]
        filled = order["filled"] + dq
        if not 0 <= filled <= p["quantity"]:
            raise ValueError("CORRECTION_OVERFILL")
        sign = 1 if p["side"] == "BUY" else -1
        old_debit = sign * old["qty"] * old["price"] + old["fee"]
        new_debit = sign * event["qty"] * event["price"] + event["fee"]
        cash = c.execute("SELECT cash FROM account").fetchone()[0] + old_debit - new_debit
        fees = order["fees"] + event["fee"] - old["fee"]
        notional = order["notional"] + event["qty"] * event["price"] - old["qty"] * old["price"]
        pos = c.execute("SELECT qty,sellable FROM positions WHERE symbol=?", (p["symbol"],)).fetchone()
        held, sellable = pos[0] + sign * dq, pos[1] + sign * dq
        state = "FILLED" if filled == p["quantity"] else ("PARTIAL" if order["state"] == "FILLED" else order["state"])
        remaining = p["quantity"] - filled
        rc = remaining * p["price"] + max(0, self.config["fee_cap"] - fees) if p["side"] == "BUY" and state not in TERMINAL else 0
        rq = remaining if p["side"] == "SELL" and state not in TERMINAL else 0
        new_fill = {**old, **{k: event[k] for k in ("qty", "price", "fee")}}
        c.execute("UPDATE account SET cash=?", (cash,))
        c.execute("UPDATE positions SET qty=?,sellable=? WHERE symbol=?", (held, sellable, p["symbol"]))
        c.execute("UPDATE orders SET filled=?,notional=?,fees=?,state=?,reserve_cash=?,reserve_qty=? WHERE intent=?", (filled, notional, fees, state, rc, rq, event["intent_id"]))
        c.execute("UPDATE fills SET payload=? WHERE exec_key=?", (canonical(new_fill), key))
        c.execute("INSERT INTO corrections VALUES(?,?,?,?,?)", (event["adjustment_id"], key, event["revision"], canonical(event), canonical(old)))
        # Authoritative economics are booked even when they breach risk limits.
        if cash < 0 or held < 0 or sellable < 0:
            self._halt(c, "CORRECTION_NEGATIVE_BALANCE")
        if fees > self.config["fee_cap"]:
            self._halt(c, "CORRECTION_FEE_CAP_EXCEEDED")
        if (p["side"] == "BUY" and event["price"] > p["price"]) or (p["side"] == "SELL" and event["price"] < p["price"]):
            self._halt(c, "CORRECTION_LIMIT_BREACH")
        total_reserved = c.execute("SELECT sum(reserve_cash) FROM orders").fetchone()[0] or 0
        if total_reserved > cash:
            self._halt(c, "CORRECTION_RESERVATION_BREACH")
        total_qty_reserved = sum(row["reserve_qty"] for row in c.execute("SELECT reserve_qty,payload FROM orders")
                                 if json.loads(row["payload"])["symbol"] == p["symbol"])
        if total_qty_reserved > sellable:
            self._halt(c, "CORRECTION_QUANTITY_RESERVATION_BREACH")
        return {"applied": True, "duplicate": False, "revision": event["revision"]}

    def receive_correction(self, event):
        with self.tx() as c:
            before = self._snapshot(c)
            try:
                result = self._correction(c, event)
            except (ValueError, KeyError, TypeError) as error:
                self._halt(c, str(error))
                result = {"applied": False, "reason": str(error)}
            self._record(c, "EXECUTION_CORRECTION", event, result, before)
            return result

    def reconcile(self, resume=False):
        # Revision ordering matters: original fills, then replacement revisions,
        # then cumulative/order/cash comparison in reused reconciliation.
        snapshot = self.venue.snapshot()
        if snapshot.get("binding") != self.binding.as_dict():
            self.pause("SNAPSHOT_BINDING_MISMATCH")
            return {"matched": False, "resumed": False, "differences": ["SNAPSHOT_BINDING_MISMATCH"]}
        with self.tx() as c:
            before = self._snapshot(c)
            for order in snapshot["orders"]:
                local = c.execute("SELECT payload FROM orders WHERE intent=?", (order["intent"],)).fetchone()
                if local and local[0] == order["payload"]:
                    c.execute("UPDATE orders SET broker_id=? WHERE intent=?", (order["id"], order["intent"]))
            for fill in snapshot["fills"]:
                try:
                    self._fill(c, fill)
                except (ValueError, KeyError, TypeError) as error:
                    self._halt(c, str(error))
            for correction in snapshot.get("corrections", []):
                try:
                    self._correction(c, correction)
                except (ValueError, KeyError, TypeError) as error:
                    self._halt(c, str(error))
            self._record(c, "RECONCILE_EXECUTIONS_AND_CORRECTIONS", {}, {"queried": True}, before)
        result = super().reconcile(resume=False)
        with self.tx() as c:
            before = self._snapshot(c)
            clearable = {"RESTART_RECONCILE", "DISCONNECTED", "SLEEP", "REPLY_LOST", "RECONCILE_DIFFERENCE", "CUMULATIVE_REQUIRES_EXECUTION_QUERY", "CANCEL_REPLY_LOST"}
            reasons = {r[0] for r in c.execute("SELECT reason FROM halt_reasons")}
            blockers = sorted(reasons - clearable)
            result["resumed"] = bool(resume and result["matched"] and not blockers)
            result["blocking_pause_reasons"] = blockers
            result["resume_blocked"] = bool(resume and result["matched"] and blockers)
            if result["resumed"]:
                c.execute("DELETE FROM halt_reasons")
                c.execute("UPDATE account SET halted=0,reason='' WHERE id=1")
            self._record(c, "RECONCILE_RESUME_DECISION", {"resume": resume}, result, before)
        return result
