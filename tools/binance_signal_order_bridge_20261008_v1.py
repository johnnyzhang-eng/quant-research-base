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
SCHEMA = "binance-signal-order-bridge/1"
ADAPTER_SHA = "64189cdeaaaccf52ac5e8446e2233c1c63a5d8a5d6da025577df923d3d734891"
PROTOCOL_SHA = "7d95ab4548665e96006775372307cf818cb12737c1a91a710a445afd9fbe9040"
MAX_ELIGIBILITY_DELAY_MS = 60_000
REGISTRY_MARKER = "signal-bridge-fake-only/1"

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
    "CHECK(fake_dispatch_used IN (0,1)))")


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
        for key, source_sha, raw, plan_sha, spent in self.db.execute(
                "SELECT business_key,source_packet_sha256,plan_json,plan_sha256,fake_dispatch_used FROM signals"):
            try:
                plan = json.loads(raw)
            except (ValueError, TypeError):
                raise BridgeError("fake-registry-plan-json") from None
            if (not re.fullmatch(r"[a-f0-9]{64}", key) or not re.fullmatch(r"[a-f0-9]{64}", source_sha)
                    or not isinstance(plan, dict) or plan_sha != digest(plan)
                    or plan.get("business_key") != key or plan.get("source_packet_sha256") != source_sha
                    or spent not in (0, 1)):
                raise BridgeError("fake-registry-plan-integrity")
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
                self.db.execute("INSERT INTO signals VALUES (?,?,?,?,0)",
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


def execute_fake(plan, registry, scenario, *, recover=False):
    """Only closed FakeScenario plus a retained bound registry; never HTTP.

    Dispatch stamp is committed before creating a NEW adapter fake journal.
    Crash in that gap consumes permission; recovery cannot manufacture a POST.
    """
    if (type(registry) is not FakeSignalRegistry or type(scenario) is not FakeScenario
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
    client = adapter.GuardedClient(adapter.TESTNET_SCOPE,
        adapter.Credentials("CLOSED_PUBLIC_FAKE", b"CLOSED_PUBLIC_FAKE_SECRET", adapter.TESTNET_SCOPE),
        transport=scenario.transport, now_ms=lambda: scenario.now)
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
            "actual_probe_journal_used": False, "recovery_mode": recover}
    finally:
        journal.db.close()
