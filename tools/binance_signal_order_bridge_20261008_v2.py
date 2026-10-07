# New v2 copies the frozen v1 source identified below. v1 and adapter v4 stay unchanged.
# All observations/capital are artificial. No account, HTTP, FX or risk feed exists.
# Checks are point-in-time; the last check and fake transport are not atomic against
# arbitrary concurrent mutation. This is not an account-global exactly-once claim.
"""Offline-only BTC BUY compiler with a retained fake signal registry.

MAX_ELIGIBILITY_DELAY_MS=60000 is an explicit small engineering assumption,
not a market/signal convention. Input clocks and portfolios are artificial.
The per-registry cutoff/portfolio identity is durable; deleting/copying/resetting
state or using another registry is outside this local integrity contract.
Only a closed in-process FakeScenario is executable here. No key loader,
HTTP transport, real/prospective journal, forward feed or live dispatcher.
Two SQLite files are not an atomic broker commit: uncertain/reserved dispatch
is never automatically reissued, and the existing journal alone governs recovery.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from urllib.parse import parse_qs
import binance_spot_testnet_adapter_20261008_v4 as adapter

MODE = "FAKE_TRANSPORT_HISTORY_CONTROL"
SCHEMA = "binance-signal-order-bridge/2"
PARENT_SOURCE_SHA = "07c7d706fd15ef937691e368e3074a953f6f890ee6f29ec8ee30c468a3ff2c20"
ADAPTER_SHA = "64189cdeaaaccf52ac5e8446e2233c1c63a5d8a5d6da025577df923d3d734891"
PROTOCOL_SHA = "7d95ab4548665e96006775372307cf818cb12737c1a91a710a445afd9fbe9040"
MAX_ELIGIBILITY_DELAY_MS = 60_000
REGISTRY_MARKER = "signal-bridge-fake-only/2"


def risk_contract():
    return {"version": "closed-artificial-dispatch-risk/1", "initial_capital_USDT": "50",
        "stop_floor_USDT": "30", "stop_at_or_below_floor": True,
        "max_observation_age_ms": 5000, "max_reference_drift_rate": "0.01",
        "max_BTC_exposure_USDT": "25", "fee_asset": "USDT",
        "valuation": "total_cash + BTC * latest reference",
        "projected_equity": "total_cash - fixed gross - frozen quote fee + (BTC + fixed quantity) * latest reference",
        "post_trade_exposure": "(BTC + fixed order quantity) * max(latest reference, fixed limit)",
        "capital_is_artificial": True}


def canonical(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(x):
    return hashlib.sha256(canonical(x).encode()).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class BridgeError(ValueError):
    pass


def rational(value, *, positive=False, nonnegative=False):
    """Bounded decimal external domain; subsequent arithmetic is exact Fraction."""
    if isinstance(value, bool) or not isinstance(value, (str, int)) or len(str(value)) > 100:
        raise BridgeError("numeric-domain")
    try:
        n = Decimal(value)
    except InvalidOperation:
        raise BridgeError("numeric-domain") from None
    if not n.is_finite() or n.copy_abs() > Decimal("1e30") or n.as_tuple().exponent < -18:
        raise BridgeError("numeric-domain")
    result = Fraction(n)
    if positive and result <= 0 or nonnegative and result < 0:
        raise BridgeError("numeric-sign")
    return result


def decimal_string(x):
    """Serialize a finite base-10 Fraction without ambient Decimal rounding."""
    x = Fraction(x)
    denominator, twos, fives = x.denominator, 0, 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise BridgeError("nonterminating-internal-decimal")
    scale = max(twos, fives)
    numerator = abs(x.numerator) * (2 ** (scale - twos)) * (5 ** (scale - fives))
    digits = str(numerator).zfill(scale + 1)
    text = digits if not scale else (digits[:-scale] + "." + digits[-scale:]).rstrip("0").rstrip(".")
    return ("-" if x < 0 else "") + text


def grid_floor(x, step):
    if step <= 0 or x < 0:
        raise BridgeError("grid-domain")
    return (x // step) * step


def ms(iso):
    parsed = datetime.fromisoformat(iso)
    if parsed.utcoffset() is None:
        raise BridgeError("timezone-required")
    return int(parsed.timestamp() * 1000)


def clock_int(x):
    if isinstance(x, bool) or not isinstance(x, int) or not 0 < x < 2**63:
        raise BridgeError("clock-domain")
    return x


def _result(status, registry, **kw):
    return {"schema": SCHEMA, "mode": MODE, "status": status,
            "new_order_permission": status == "READY_FAKE_BTC_PROJECTION",
            "live_order_permission": False, "next_registry": copy.deepcopy(registry), **kw}


def _compile_with_memory(packet, portfolio, quote, exchange, policy, risk, registry, *, now_ms):
    """Return an immutable BTC-only fake plan or explicit no-order result.

    Internal calculation only; the public entry uses a serialized retained
    fake-only SQLite registry. No production caller exists. Money operations use
    Fractions. Portfolio fixture equity is declared artificial capital, not NAV
    inferred from a historical candle or any actual market quote.
    """
    saved = copy.deepcopy(registry or {})
    try:
        now = clock_int(now_ms)
        if any(x.get("mode") != MODE for x in (packet, portfolio, quote, policy)):
            return _result("BLOCK_SCOPE", saved)
        if policy.get("requested_pair") != "BTCUSDT":
            return _result("BLOCK_UNSUPPORTED_ASSET", saved)
        if risk.get("halted") is not False or risk.get("account_state_known") is not True:
            return _result("BLOCK_RISK_OR_UNKNOWN_STATE", saved)
        if portfolio.get("pending_orders") != []:
            return _result("BLOCK_PENDING_ORDERS", saved)
        cutoff, eligible = clock_int(packet["cutoff_ms"]), clock_int(packet["eligible_ms"])
        if cutoff > now:
            return _result("BLOCK_FUTURE_CUTOFF", saved)
        if eligible <= cutoff or eligible > now:
            return _result("BLOCK_NOT_ELIGIBLE", saved)
        if eligible - cutoff > MAX_ELIGIBILITY_DELAY_MS:
            return _result("BLOCK_ELIGIBILITY_DELAY", saved)
        if packet.get("historical_PIT_verified") is not False or packet.get("actual_known_at") is not None:
            return _result("BLOCK_FALSE_HISTORICAL_RECEIPT", saved)
        if packet.get("schedule_rebalance") is not True:
            return _result("HOLD", saved)
        if packet.get("source_sell_assets"):
            mixed = bool(packet.get("source_buy_assets"))
            return _result("BLOCK_SELL_BEFORE_BUY_UNIMPLEMENTED" if mixed else "BLOCK_SELL_UNIMPLEMENTED", saved)
        if any(rational(v, nonnegative=True) for k, v in portfolio["positions"].items() if k != "BTC"):
            return _result("BLOCK_OTHER_ACTIVE_ASSET", saved)
        source_identity = {k: packet[k] for k in ("protocol_sha256", "source_intent_id", "cutoff_ms")}
        if (not isinstance(source_identity["source_intent_id"], str)
                or not source_identity["source_intent_id"]
                or source_identity["protocol_sha256"] != PROTOCOL_SHA
                or not isinstance(portfolio.get("portfolio_id"), str)
                or not portfolio["portfolio_id"]):
            return _result("BLOCK_IDENTITY", saved)
        # Intent renaming cannot mint a second plan at the same cutoff.
        business_key = digest({"protocol_sha256": source_identity["protocol_sha256"],
            "cutoff_ms": cutoff, "portfolio_id": portfolio["portfolio_id"], "pair": "BTCUSDT"})
        source_hash = digest(packet)
        if business_key in saved:
            old = saved[business_key]
            if old.get("source_packet_sha256") != source_hash:
                return _result("BLOCK_CONFLICTING_SIGNAL_IDENTITY", saved)
            plan = old.get("plan")
            if not isinstance(plan, dict) or old.get("plan_sha256") != digest(plan):
                return _result("BLOCK_REGISTRY_INTEGRITY", saved)
            return _result("REUSE_NO_NEW_ORDER", saved, business_key=business_key, plan=copy.deepcopy(plan))
        if now - eligible > 60_000:
            return _result("BLOCK_STALE_SIGNAL", saved)
        for observation in (portfolio, quote):
            age = now - clock_int(observation["asof_ms"])
            if not 0 <= age <= 5_000:
                return _result("BLOCK_STALE_OR_FUTURE_OBSERVATION", saved)
        ttl = clock_int(policy["ttl_ms"])
        if ttl > 60_000 or policy.get("capital_is_artificial") is not True:
            return _result("BLOCK_POLICY", saved)
        capital = rational(portfolio["fixture_equity_USDT"], positive=True)
        if capital != 50 or rational(policy["gross_quote_cap_USDT"], positive=True) != 25:
            return _result("BLOCK_ARTIFICIAL_CAPITAL_POLICY", saved)
        fee = rational(policy["fee_rate"], nonnegative=True)
        slip = rational(policy["slippage_rate"], nonnegative=True)
        if fee + slip != rational(policy["combined_friction_rate"], nonnegative=True) or fee + slip > Fraction(1, 100):
            return _result("BLOCK_FRICTION_POLICY", saved)
        weights = {k: rational(v, nonnegative=True) for k, v in packet["planned_weights"].items()}
        if set(weights) != {"BTC", "ETH"} or sum(weights.values()) > 1:
            return _result("BLOCK_TARGET_DOMAIN", saved)
        filters = exchange["symbols"][0]["filters"]
        index = {f["filterType"]: f for f in filters}
        if len(index) != len(filters):
            return _result("BLOCK_MARKET_FILTER", saved)
        tick = rational(index["PRICE_FILTER"]["tickSize"], positive=True)
        step = rational(index["LOT_SIZE"]["stepSize"], positive=True)
        if quote.get("provenance") != "ARTIFICIAL_FAKE" or quote.get("symbol") != "BTCUSDT":
            return _result("BLOCK_QUOTE_PROVENANCE", saved)
        price = grid_floor(rational(quote["reference_price"], positive=True) * (1 + slip), tick)
        target_qty = grid_floor(capital * weights["BTC"] / price, step)
        current_qty = rational(portfolio["positions"].get("BTC", "0"), nonnegative=True)
        if current_qty > target_qty:
            return _result("BLOCK_SELL_UNIMPLEMENTED", saved)
        quantity = grid_floor(target_qty - current_qty, step)
        if quantity == 0:
            return _result("HOLD_GRID_REMAINDER", saved)
        gross, fee_cost = quantity * price, quantity * price * fee
        # Slippage is already embedded once in price. Reserve quote + fee only.
        debit = gross + fee_cost
        if gross > 25:
            return _result("BLOCK_GROSS_CAP", saved)
        if debit > rational(portfolio["available_cash_USDT"], nonnegative=True):
            return _result("BLOCK_FEE_INCLUSIVE_CASH", saved)
        notional = index.get("NOTIONAL", index.get("MIN_NOTIONAL", {}))
        if gross < rational(notional["minNotional"], nonnegative=True):
            return _result("HOLD_MIN_NOTIONAL", saved)
        manifest = adapter.OrderManifest(price=decimal_string(price), quantity=decimal_string(quantity), expires_ms=now + ttl)
        adapter.validate_market(manifest, exchange, quote)
        plan = {"schema": SCHEMA, "mode": MODE, "business_key": business_key,
                "source_packet_sha256": source_hash, "source_row_sha256": packet["source_row_sha256"],
                "parent_source_sha256": PARENT_SOURCE_SHA, "protocol_sha256": PROTOCOL_SHA,
                "frozen_reference_price": decimal_string(rational(quote["reference_price"], positive=True)),
                "risk_contract": risk_contract(),
                "cutoff_ms": cutoff, "eligible_ms": eligible, "compiled_at_ms": now,
                "adapter_sha256": ADAPTER_SHA, "max_cutoff_eligibility_span_ms": MAX_ELIGIBILITY_DELAY_MS,
                "portfolio_id": portfolio["portfolio_id"],
                "order_manifest": manifest.document(), "order_manifest_sha256": manifest.sha256,
                "client_order_id": manifest.client_id, "quantity": decimal_string(quantity),
                "artificial_BTC_before": decimal_string(current_qty),
                "artificial_available_cash_USDT": decimal_string(rational(portfolio["available_cash_USDT"], nonnegative=True)),
                "gross_quote_USDT": decimal_string(gross), "fee_reserve_USDT": decimal_string(fee_cost),
                "maximum_cash_debit_USDT": decimal_string(debit), "fee_rate": decimal_string(fee),
                "slippage_embedded_once_in_limit_price": True, "capital_is_artificial": True,
                "projection_only": True, "omitted_target_asset": "ETH", "actual_known_at": None,
                "historical_PIT_verified": False, "live_order_permission": False}
        saved[business_key] = {"source_packet_sha256": source_hash, "plan_sha256": digest(plan), "plan": copy.deepcopy(plan)}
        return _result("READY_FAKE_BTC_PROJECTION", saved, business_key=business_key, plan=plan)
    except (BridgeError, adapter.GuardError, KeyError, TypeError, IndexError, AttributeError, ZeroDivisionError):
        return _result("BLOCK_INVALID_INPUT_OR_MARKET_FILTER", saved)


_META_SQL = "CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), marker TEXT NOT NULL, portfolio_id TEXT)"
_SIGNAL_SQL = ("CREATE TABLE signals (business_key TEXT PRIMARY KEY, source_packet_sha256 TEXT NOT NULL, "
    "plan_json TEXT NOT NULL, plan_sha256 TEXT NOT NULL, fake_dispatch_used INTEGER NOT NULL DEFAULT 0 "
    "CHECK(fake_dispatch_used IN (0,1)), risk_clocks_json TEXT NOT NULL DEFAULT '{}')")


class FakeSignalRegistry:
    """Per retained fake-only file; serialized creation, immutable plan, spent stamp.

    Local ownership/integrity is assumed. This does not resist arbitrary same-UID
    writers, copied/new registries or simultaneous external account activity.
    """
    def __init__(self, path):
        path = Path(path).absolute()
        if path.name != "fake-signal-registry.sqlite3" or not path.parent.name.startswith("bridge-fake-"):
            raise BridgeError("explicit-fake-registry-path-required")
        parent_fd = file_fd = None
        connection = None
        created = False
        try:
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parent_stat = os.fstat(parent_fd)
            if parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) != 0o700:
                raise BridgeError("fake-parent-owner-or-mode")
            try:
                file_fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o600, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                file_fd = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent_fd)
            checked = os.fstat(file_fd)
            if (not stat.S_ISREG(checked.st_mode) or checked.st_uid != os.getuid()
                    or stat.S_IMODE(checked.st_mode) != 0o600 or checked.st_size > 2_000_000):
                raise BridgeError("fake-file-owner-mode-or-size")
            connection = sqlite3.connect(str(path), timeout=2, isolation_level=None)
            for observed in (os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False),
                             os.stat(path, follow_symlinks=False)):
                if (observed.st_dev, observed.st_ino, observed.st_uid, stat.S_IMODE(observed.st_mode)) != (
                        checked.st_dev, checked.st_ino, os.getuid(), 0o600) or not stat.S_ISREG(observed.st_mode):
                    raise BridgeError("fake-file-changed-during-open")
            visible_parent = os.stat(path.parent, follow_symlinks=False)
            if (visible_parent.st_dev, visible_parent.st_ino, visible_parent.st_uid,
                    stat.S_IMODE(visible_parent.st_mode)) != (
                    parent_stat.st_dev, parent_stat.st_ino, os.getuid(), 0o700):
                raise BridgeError("fake-parent-changed-during-open")
            if created:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_META_SQL)
                connection.execute(_SIGNAL_SQL)
                connection.execute("INSERT INTO metadata VALUES (1,?,NULL)", (REGISTRY_MARKER,))
                connection.execute("COMMIT")
            self._validate_schema(connection)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            self.path, self.db = path, connection
            connection = None
        finally:
            if connection is not None:
                connection.close()
            if file_fd is not None:
                os.close(file_fd)
            if parent_fd is not None:
                os.close(parent_fd)

    @staticmethod
    def _validate_schema(db):
        rows = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        expected = [("index", "sqlite_autoindex_signals_1", None),
                    ("table", "metadata", _META_SQL), ("table", "signals", _SIGNAL_SQL)]
        if rows != expected:
            raise BridgeError("fake-registry-schema-or-trigger")
        meta = db.execute("SELECT singleton,marker,portfolio_id FROM metadata").fetchall()
        if len(meta) != 1 or meta[0][0] != 1 or meta[0][1] != REGISTRY_MARKER:
            raise BridgeError("fake-registry-marker")

    def close(self):
        self.db.close()

    def snapshot(self):
        saved = {}
        for key, source_sha, raw, plan_sha, spent, risk_raw in self.db.execute(
                "SELECT business_key,source_packet_sha256,plan_json,plan_sha256,fake_dispatch_used,risk_clocks_json FROM signals"):
            try:
                plan = json.loads(raw)
            except (ValueError, TypeError):
                raise BridgeError("fake-registry-plan-json") from None
            if (not re.fullmatch(r"[a-f0-9]{64}", key) or not re.fullmatch(r"[a-f0-9]{64}", source_sha)
                    or not isinstance(plan, dict) or plan_sha != digest(plan)
                    or plan.get("business_key") != key or plan.get("source_packet_sha256") != source_sha
                    or spent not in (0, 1)):
                raise BridgeError("fake-registry-plan-integrity")
            try:
                clocks = json.loads(risk_raw)
                if type(clocks) is not dict or clocks and set(clocks) != {"risk_ms", "wallet_ms", "quote_ms", "read_ms"}:
                    raise BridgeError("fake-risk-clock-shape")
                for value in clocks.values():
                    clock_int(value)
            except (ValueError, TypeError):
                raise BridgeError("fake-risk-clock-integrity") from None
            saved[key] = {"source_packet_sha256": source_sha, "plan_sha256": plan_sha, "plan": plan}
        return saved

    def compile(self, packet, portfolio, quote, exchange, policy, risk, *, now_ms):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            saved = self.snapshot()
            bound = self.db.execute("SELECT portfolio_id FROM metadata WHERE singleton=1").fetchone()[0]
            if bound is not None and portfolio.get("portfolio_id") != bound:
                result = _result("BLOCK_REGISTRY_PORTFOLIO_SCOPE", saved)
            else:
                result = _compile_with_memory(packet, portfolio, quote, exchange, policy, risk, saved, now_ms=now_ms)
            if result["status"] == "READY_FAKE_BTC_PROJECTION":
                key, plan = result["business_key"], result["plan"]
                if key in saved:
                    raise BridgeError("unexpected-registry-new-permission")
                self.db.execute("INSERT INTO signals VALUES (?,?,?,?,0,'{}')",
                    (key, plan["source_packet_sha256"], canonical(plan), digest(plan)))
                self.db.execute("UPDATE metadata SET portfolio_id=? WHERE singleton=1", (plan["portfolio_id"],))
            self.db.execute("COMMIT")
            return result
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def reserve_fake_dispatch(self, plan):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            saved = self.snapshot()
            record = saved.get(plan.get("business_key"))
            if record is None or record["plan_sha256"] != digest(plan):
                raise BridgeError("fake-dispatch-plan-binding")
            cursor = self.db.execute("UPDATE signals SET fake_dispatch_used=1 "
                "WHERE business_key=? AND fake_dispatch_used=0", (plan["business_key"],))
            self.db.execute("COMMIT")
            return cursor.rowcount == 1
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def require_spent(self, plan):
        saved = self.snapshot()
        record = saved.get(plan.get("business_key"))
        row = self.db.execute("SELECT fake_dispatch_used FROM signals WHERE business_key=?",
                              (plan.get("business_key"),)).fetchone()
        if record is None or record["plan_sha256"] != digest(plan) or row != (1,):
            raise BridgeError("fake-recovery-requires-spent-bound-plan")


    def commit_risk_clocks(self, plan, clocks):
        """Durable monotonic observations for an already spent fake grant."""
        if type(clocks) is not dict or set(clocks) != {"risk_ms", "wallet_ms", "quote_ms", "read_ms"}:
            raise RiskBlocked("risk-clock-shape")
        for value in clocks.values():
            clock_int(value)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.require_spent(plan)
            raw = self.db.execute("SELECT risk_clocks_json FROM signals WHERE business_key=?",
                (plan["business_key"],)).fetchone()[0]
            prior = json.loads(raw)
            if any(clocks[k] < prior.get(k, 0) for k in clocks):
                raise RiskBlocked("risk-observation-clock-regressed")
            self.db.execute("UPDATE signals SET risk_clocks_json=? WHERE business_key=?",
                (canonical(clocks), plan["business_key"]))
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise


def compile_position_deltas(packet, portfolio, quote, exchange, policy, risk, registry, *, now_ms):
    if type(registry) is not FakeSignalRegistry:
        return _result("BLOCK_DURABLE_FAKE_REGISTRY_REQUIRED", {})
    if any(type(item) is not dict for item in (packet, portfolio, quote, exchange, policy, risk)):
        return _result("BLOCK_INVALID_INPUT_OR_MARKET_FILTER", {})
    return registry.compile(packet, portfolio, quote, exchange, policy, risk, now_ms=now_ms)


class FakeScenario:
    """Closed in-memory endpoints: no URLs are opened, no callbacks injected."""
    def __init__(self, plan, *, expire_on=None, order_exists=False):
        if expire_on not in (None, "quote", "account") or type(order_exists) is not bool:
            raise BridgeError("fake-scenario-stage")
        self.plan = copy.deepcopy(plan)
        self.manifest = adapter.OrderManifest(**plan["order_manifest"])
        self.now = clock_int(plan["compiled_at_ms"])
        self.expire_on = expire_on
        self.recovery = False
        self.order_exists = order_exists
        self.calls = Counter()
        self.post_dispatch_ms = []
        self.business_parameters_bound = False
        price, qty, rate = map(rational, (self.manifest.price, self.manifest.quantity, plan["fee_rate"]))
        gross, fee = price * qty, price * qty * rate
        before_btc = rational(plan["artificial_BTC_before"], nonnegative=True)
        cash = rational(plan["artificial_available_cash_USDT"], nonnegative=True)
        self.baseline = {"balances": [{"asset": "BTC", "free": decimal_string(before_btc), "locked": "0"},
            {"asset": "USDT", "free": decimal_string(cash), "locked": "0"}]}
        self.current = {"balances": [{"asset": "BTC", "free": decimal_string(before_btc + qty), "locked": "0"},
            {"asset": "USDT", "free": decimal_string(cash - gross - fee), "locked": "0"}]}
        self.order = {"symbol": "BTCUSDT", "orderId": 913, "clientOrderId": self.manifest.client_id,
            "status": "FILLED", "side": "BUY", "type": "LIMIT_MAKER", "price": self.manifest.price,
            "origQty": self.manifest.quantity, "executedQty": self.manifest.quantity,
            "cummulativeQuoteQty": decimal_string(gross)}
        self.trade = {"symbol": "BTCUSDT", "id": 19, "orderId": 913, "isBuyer": True,
            "qty": self.manifest.quantity, "quoteQty": decimal_string(gross), "price": self.manifest.price,
            "commission": decimal_string(fee), "commissionAsset": "USDT"}
        self.exchange = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "isSpotTradingAllowed": True,
            "filters": [{"filterType": "PRICE_FILTER", "minPrice": "0.00000001", "maxPrice": "1000000000000", "tickSize": "0.00000001"},
                {"filterType": "LOT_SIZE", "minQty": "0.00000001", "maxQty": "1", "stepSize": "0.00000001"},
                {"filterType": "NOTIONAL", "minNotional": "0", "maxNotional": "25"}]}]}
        self.quote = {"symbol": "BTCUSDT", "bidPrice": self.manifest.price,
                      "askPrice": decimal_string(price + 1)}

    def transport(self, request):
        if request.base != adapter.TESTNET:
            raise BridgeError("fake-origin")
        route = (request.method, request.path)
        self.calls[" ".join(route)] += 1
        if route == ("POST", "/api/v3/order"):
            body = parse_qs(request.body.decode("ascii"), strict_parsing=True)
            expected = self.manifest.order_params() | {"newOrderRespType": "RESULT"}
            if any(body.get(k) != [v] for k, v in expected.items()) or set(body) != set(expected) | {
                    "timestamp", "recvWindow", "signature"}:
                raise BridgeError("fake-post-business-binding")
            self.business_parameters_bound = True
            self.post_dispatch_ms.append(self.now)
            self.order_exists = True
            response = self.order
        elif route == ("GET", "/api/v3/account"):
            response = self.current if self.recovery or self.calls["GET /api/v3/account"] > 1 else self.baseline
            if self.expire_on == "account":
                self.now = self.manifest.expires_ms + 1
        elif route == ("GET", "/api/v3/exchangeInfo"):
            response = self.exchange
        elif route == ("GET", "/api/v3/ticker/bookTicker"):
            response = self.quote
            if self.expire_on == "quote":
                self.now = self.manifest.expires_ms + 1
        elif route == ("GET", "/api/v3/order"):
            if not self.order_exists:
                return adapter.Response(400, b'{"code":-2013}')
            response = self.order
        elif route == ("GET", "/api/v3/myTrades"):
            response = [self.trade]
        else:
            raise BridgeError("unsupported-fake-route")
        return adapter.Response(200, canonical(response).encode())


class RiskBlocked(BridgeError, adapter.GuardError):
    """A local risk refusal; no fake POST is dispatched for this exception."""


def _wallet(account):
    """Only the closed two-asset fixture wallet, exact free/locked arithmetic."""
    if type(account) is not dict or type(account.get("balances")) is not list:
        raise RiskBlocked("unknown-wallet-shape")
    result = {}
    for row in account["balances"]:
        if (type(row) is not dict or type(row.get("asset")) is not str
                or row["asset"] not in {"BTC", "USDT"} or row["asset"] in result):
            raise RiskBlocked("extra-or-duplicate-wallet-asset")
        try:
            free = rational(row.get("free"), nonnegative=True)
            locked = rational(row.get("locked"), nonnegative=True)
        except BridgeError:
            raise RiskBlocked("invalid-wallet-numeric") from None
        result[row["asset"]] = (free, free + locked)
    if set(result) != {"BTC", "USDT"}:
        raise RiskBlocked("unknown-wallet-assets")
    return {"BTC": result["BTC"][1], "total_cash": result["USDT"][1],
            "available_cash": result["USDT"][0]}


class FakeRiskProvider:
    """Independent, explicitly artificial latest observations; no external feed.

    The exact class is required. publish() copies a synthetic snapshot, read()
    copies it again. Validation happens on every dispatch fence, not on publish.
    This object is not an account or an authenticated real-world risk authority.
    """
    def __init__(self, plan, snapshot):
        self.plan = copy.deepcopy(plan)
        self.reads = 0
        self.publish(snapshot)

    def publish(self, snapshot):
        if type(snapshot) is not dict:
            raise RiskBlocked("risk-provider-snapshot-required")
        self._latest = copy.deepcopy(snapshot)

    def read(self):
        self.reads += 1
        return copy.deepcopy(self._latest)


def fake_risk_snapshot(plan, scenario, *, asof_ms=None):
    """Convenience for synthetic controls only; no actual account is inspected."""
    if type(scenario) is not FakeScenario or scenario.plan != plan:
        raise RiskBlocked("snapshot-closed-fake-scope")
    wallet = _wallet(scenario.baseline)
    reference = rational(plan["frozen_reference_price"], positive=True)
    when = scenario.now if asof_ms is None else clock_int(asof_ms)
    return {"mode": MODE, "portfolio_id": plan["portfolio_id"], "pair": "BTCUSDT",
        "protocol_sha256": PROTOCOL_SHA, "cutoff_ms": plan["cutoff_ms"],
        "risk_asof_ms": when, "wallet_asof_ms": when, "quote_asof_ms": when,
        "account_state_known": True, "halted": False, "capital_is_artificial": True,
        "initial_capital_USDT": "50",
        "equity_USDT": decimal_string(wallet["total_cash"] + wallet["BTC"] * reference),
        "total_cash_USDT": decimal_string(wallet["total_cash"]),
        "available_cash_USDT": decimal_string(wallet["available_cash"]),
        "positions": {"BTC": decimal_string(wallet["BTC"]), "ETH": "0"}, "pending_orders": [],
        "quote": dict(scenario.quote, reference_price=decimal_string(reference), provenance="ARTIFICIAL_FAKE"),
        "fee_rate": plan["fee_rate"], "fee_asset": "USDT"}


class _RiskFence:
    def __init__(self, plan, registry, scenario, provider, *, recover):
        self.plan, self.registry, self.scenario, self.provider = plan, registry, scenario, provider
        self.recover, self.account_evidence, self.checks = recover, None, []
        self.manifest = adapter.OrderManifest(**plan["order_manifest"])

    def check(self, stage, *, account_required):
        """Re-read all inputs; never mutate the approved plan or order manifest."""
        try:
            self._check(stage, account_required=account_required)
        except (BridgeError, adapter.GuardError, KeyError, TypeError, AttributeError, ValueError) as exc:
            code = str(exc) if isinstance(exc, (RiskBlocked, BridgeError)) else "risk-input-or-market-invalid"
            self.checks.append({"stage": stage, "accepted": False, "reason": code})
            raise RiskBlocked(code) from None

    def _check(self, stage, *, account_required):
        plan, snapshot = self.plan, self.provider.read()
        expected = {"mode", "portfolio_id", "pair", "protocol_sha256", "cutoff_ms",
            "risk_asof_ms", "wallet_asof_ms", "quote_asof_ms", "account_state_known", "halted",
            "capital_is_artificial", "initial_capital_USDT", "equity_USDT", "total_cash_USDT",
            "available_cash_USDT", "positions", "pending_orders", "quote", "fee_rate", "fee_asset"}
        if type(snapshot) is not dict or set(snapshot) != expected:
            raise RiskBlocked("unknown-risk-snapshot-shape")
        if (plan.get("risk_contract") != risk_contract() or plan.get("parent_source_sha256") != PARENT_SOURCE_SHA
                or plan.get("protocol_sha256") != PROTOCOL_SHA):
            raise RiskBlocked("frozen-risk-contract-binding")
        if (snapshot["mode"], snapshot["portfolio_id"], snapshot["pair"], snapshot["protocol_sha256"],
                snapshot["cutoff_ms"]) != (MODE, plan["portfolio_id"], "BTCUSDT", PROTOCOL_SHA, plan["cutoff_ms"]):
            raise RiskBlocked("risk-identity-binding")
        clock_int(snapshot["cutoff_ms"])
        if snapshot["account_state_known"] is not True or snapshot["halted"] is not False:
            raise RiskBlocked("risk-halted-or-state-unknown")
        if snapshot["capital_is_artificial"] is not True or rational(snapshot["initial_capital_USDT"], positive=True) != 50:
            raise RiskBlocked("artificial-initial-capital-binding")
        now = clock_int(self.scenario.now)
        if now < plan["compiled_at_ms"] or now > self.manifest.expires_ms:
            raise RiskBlocked("risk-read-clock-or-expiry")
        clocks = {"risk_ms": clock_int(snapshot["risk_asof_ms"]),
            "wallet_ms": clock_int(snapshot["wallet_asof_ms"]),
            "quote_ms": clock_int(snapshot["quote_asof_ms"]), "read_ms": now}
        if any(not 0 <= now - clocks[k] <= 5000 for k in ("risk_ms", "wallet_ms", "quote_ms")):
            raise RiskBlocked("risk-observation-stale-or-future")
        positions, quote = snapshot["positions"], snapshot["quote"]
        if type(positions) is not dict or set(positions) != {"BTC", "ETH"}:
            raise RiskBlocked("unknown-or-extra-position-asset")
        btc, eth = (rational(positions[k], nonnegative=True) for k in ("BTC", "ETH"))
        if eth != 0 or btc != rational(plan["artificial_BTC_before"], nonnegative=True):
            raise RiskBlocked("position-changed-or-extra-active-asset")
        if type(snapshot["pending_orders"]) is not list or snapshot["pending_orders"] != []:
            raise RiskBlocked("pending-order-state-unknown")
        if (type(quote) is not dict or set(quote) != {"symbol", "bidPrice", "askPrice", "reference_price", "provenance"}
                or quote["symbol"] != "BTCUSDT" or quote["provenance"] != "ARTIFICIAL_FAKE"):
            raise RiskBlocked("quote-scope-or-provenance")
        reference = rational(quote["reference_price"], positive=True)
        frozen_reference = rational(plan["frozen_reference_price"], positive=True)
        if abs(reference / frozen_reference - 1) > Fraction(1, 100):
            raise RiskBlocked("quote-reference-drift")
        adapter.validate_market(self.manifest, self.scenario.exchange, quote)
        total = rational(snapshot["total_cash_USDT"], nonnegative=True)
        available = rational(snapshot["available_cash_USDT"], nonnegative=True)
        equity = rational(snapshot["equity_USDT"], nonnegative=True)
        if available > total or equity != total + btc * reference:
            raise RiskBlocked("wallet-or-equity-identity")
        if equity <= 30:
            raise RiskBlocked("current-equity-floor")
        if snapshot["fee_asset"] != "USDT":
            raise RiskBlocked("unsupported-fee-asset")
        latest_fee = rational(snapshot["fee_rate"], nonnegative=True)
        fee_bound = rational(plan["fee_rate"], nonnegative=True)
        if latest_fee > fee_bound:
            raise RiskBlocked("fee-exceeds-frozen-reserve")
        price, qty = rational(self.manifest.price, positive=True), rational(self.manifest.quantity, positive=True)
        gross, fee = price * qty, price * qty * fee_bound
        if (gross != rational(plan["gross_quote_USDT"], positive=True)
                or fee != rational(plan["fee_reserve_USDT"], nonnegative=True)
                or gross + fee != rational(plan["maximum_cash_debit_USDT"], positive=True)):
            raise RiskBlocked("frozen-budget-identity")
        if gross > 25 or (btc + qty) * max(reference, price) > 25:
            raise RiskBlocked("gross-or-latest-exposure-cap")
        if gross + fee > available:
            raise RiskBlocked("latest-fee-inclusive-cash")
        projected = total - gross - fee + (btc + qty) * reference
        if projected <= 30:
            raise RiskBlocked("projected-fee-inclusive-equity-floor")
        if account_required:
            if self.account_evidence is None or self.account_evidence != {
                    "BTC": btc, "total_cash": total, "available_cash": available}:
                raise RiskBlocked("latest-wallet-does-not-match-account-evidence")
        self.registry.commit_risk_clocks(plan, clocks)
        self.checks.append({"stage": stage, "accepted": True, "read_ms": now,
            "current_equity_USDT": decimal_string(equity), "projected_equity_USDT": decimal_string(projected),
            "latest_BTC_exposure_USDT": decimal_string((btc + qty) * max(reference, price))})

    def transport(self, request):
        if self.recover and request.method != "GET":
            raise RiskBlocked("recovery-get-only")
        if request.method == "POST":
            # This wrapper is reached after the unchanged v4 signature, origin/
            # endpoint guard and final deadline check. Re-read risk at this point.
            self.check("after-sign-guard-before-transport", account_required=True)
        response = self.scenario.transport(request)
        if not self.recover and request.method == "GET" and request.path == "/api/v3/account" and self.account_evidence is None:
            self.account_evidence = _wallet(json.loads(response.body))
        return response


class _RiskCheckedClient(adapter.GuardedClient):
    def __init__(self, *args, fence, **kwargs):
        self.fence = fence
        super().__init__(*args, **kwargs)

    def _request(self, method, path, params, *, deadline_ms=None):
        if self.fence.recover and method != "GET":
            raise RiskBlocked("recovery-get-only")
        if method == "POST":
            self.fence.check("request-before-sign", account_required=True)
        return super()._request(method, path, params, deadline_ms=deadline_ms)


def execute_fake(plan, registry, scenario, *, risk_provider, recover=False):
    """Only closed FakeScenario plus a retained bound registry; never HTTP.

    Dispatch stamp is committed before creating a NEW adapter fake journal.
    Crash in that gap consumes permission; recovery cannot manufacture a POST.
    """
    if (type(registry) is not FakeSignalRegistry or type(scenario) is not FakeScenario
            or type(risk_provider) is not FakeRiskProvider or risk_provider.plan != plan
            or plan.get("mode") != MODE or plan.get("live_order_permission") is not False
            or plan.get("projection_only") is not True or plan != scenario.plan
            or plan.get("adapter_sha256") != ADAPTER_SHA or file_sha(adapter.__file__) != ADAPTER_SHA):
        raise BridgeError("closed-fake-scope-or-adapter-binding")
    manifest = adapter.OrderManifest(**plan["order_manifest"])
    if plan["order_manifest_sha256"] != manifest.sha256 or plan["client_order_id"] != manifest.client_id:
        raise BridgeError("fake-manifest-identity-binding")
    path = registry.path.parent / ("fake-order-" + plan["business_key"] + ".sqlite3")
    if recover:
        registry.require_spent(plan)
        if not path.is_file() or path.is_symlink():
            raise BridgeError("fake-recovery-state-missing")
    elif not registry.reserve_fake_dispatch(plan):
        raise BridgeError("fake-dispatch-already-reserved")
    elif path.exists():
        raise BridgeError("fake-dispatch-state-preexists")
    fence = _RiskFence(plan, registry, scenario, risk_provider, recover=recover)
    if not recover:
        fence.check("initial-after-grant", account_required=False)
    client = _RiskCheckedClient(adapter.TESTNET_SCOPE,
        adapter.Credentials("CLOSED_PUBLIC_FAKE", b"CLOSED_PUBLIC_FAKE_SECRET", adapter.TESTNET_SCOPE),
        transport=fence.transport, now_ms=lambda: scenario.now, fence=fence)
    client.install_clock(adapter.ClockEvidence(adapter.TESTNET, scenario.now, scenario.now, scenario.now))
    journal = adapter.Journal(path)
    try:
        runner = adapter.FiniteRunner(client, journal, manifest)
        if recover:
            scenario.recovery = True
            result = runner.recover(execute=True, approved_hash=manifest.sha256)
        else:
            result = runner.run(execute=True, approved_hash=manifest.sha256)
        return {"mode": MODE, "result": result, "route_counts": dict(scenario.calls),
            "post_dispatch_ms": list(scenario.post_dispatch_ms),
            "manifest_expires_ms": manifest.expires_ms,
            "business_parameters_bound": scenario.business_parameters_bound,
            "real_network_calls": 0, "real_credentials_loaded": False,
            "actual_probe_journal_used": False, "recovery_mode": recover,
            "risk_checks": copy.deepcopy(fence.checks), "risk_snapshot_reads": risk_provider.reads,
            "risk_contract": copy.deepcopy(plan["risk_contract"])}
    finally:
        journal.db.close()
