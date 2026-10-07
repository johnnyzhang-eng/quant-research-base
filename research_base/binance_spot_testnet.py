"""Bounded candidate: one Binance Spot Test Network intent; separate wallet GETs.

This module does not contact a network on import, contains no CLI with a live
execute option, and has no production order, transfer, or withdrawal operation.
Authentication and real order acceptance have NOT been verified. Callers must
retain the private journal and separately authorize the exact manifest hash.
Stdlib only. Keys, signed requests, account responses, and deposit identities
must not be printed or copied into public artifacts.

Deadline semantics: a new order-test/order POST may dispatch only when the last
local clock sample immediately before transport is <= manifest.expires_ms.
The runner also checks after slow preflight reads and before reservations.
Expiry after a durable reservation keeps that budget consumed, sends no POST,
and raises a local GuardError; recovery cannot create another order. Equality
is allowed, as in v3. This is a local dispatch deadline, not an exchange receipt
or fill-time guarantee. Queries/reconciliation and the existing cancel budget
remain available after expiry for a previously reserved intent.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Callable, Mapping

TESTNET = "https://testnet.binance.vision"
PRODUCTION_WALLET = "https://api.binance.com"
TESTNET_SCOPE = "spot-testnet-hmac-v1"
WALLET_SCOPE = "wallet-production-readonly-hmac-v1"
MAX_RESPONSE = 2_000_000
TERMINAL = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"})
ACTIVE = frozenset({"NEW", "PARTIALLY_FILLED", "PENDING_NEW", "PENDING_CANCEL"})
SOURCES = (
    "https://testnet.binance.vision/",
    "https://developers.binance.com/en/docs/products/spot/rest-api",
    "https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade",
    "https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account",
    "https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/capital",
    "https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/account",
    "https://www.rfc-editor.org/rfc/rfc4231",
)


class GuardError(ValueError):
    """A local policy violation; message deliberately excludes caller values."""


class APIError(RuntimeError):
    def __init__(self, category: str, *, status: int | None = None,
                 code: int | None = None, ambiguous: bool = False):
        self.category, self.status, self.code, self.ambiguous = category, status, code, ambiguous
        super().__init__(f"API {category}; status={status}; code={code}; ambiguous={ambiguous}")


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sign(secret: bytes, encoded: str) -> str:
    """HMAC of the exact already-percent-encoded request parameters."""
    return hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()


def encode(params: Mapping[str, str]) -> str:
    return urllib.parse.urlencode(list(params.items()), quote_via=urllib.parse.quote, safe="")


def number(value: object, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise GuardError("decimal string or integer required")
    if len(str(value)) > 100:
        raise GuardError("numeric input too long")
    try:
        n = Decimal(value)
    except InvalidOperation:
        raise GuardError("invalid decimal") from None
    if not n.is_finite() or n.copy_abs() > Decimal("1e30") or n.as_tuple().exponent < -18:
        raise GuardError("numeric input outside bounded domain")
    if positive and n <= 0 or nonnegative and n < 0:
        raise GuardError("numeric sign violation")
    return n


def integer(value: object, *, low: int = 0, high: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise GuardError("integer input outside bounded domain")
    return value


def symbol(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{2,20}", value):
        raise GuardError("invalid pair symbol or network")
    return value


def asset_code(value: str) -> str:
    # The account schema declares asset as a string, not uppercase ASCII.
    # Preserve exact Unicode identity/case: never uppercase or NFKC-normalize.
    # 1..20 alphanumeric code points is this candidate's observed-data bound,
    # not a claim that Binance universally excludes every other asset string.
    if not isinstance(value, str) or not 1 <= len(value) <= 20 or not all(c.isalnum() for c in value):
        raise GuardError("invalid asset code")
    return value


@dataclass(frozen=True, repr=False)
class Credentials:
    api_key: str
    api_secret: bytes
    scope: str

    def __post_init__(self):
        if self.scope not in (TESTNET_SCOPE, WALLET_SCOPE):
            raise GuardError("unsupported credential scope")
        if not isinstance(self.api_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", self.api_key):
            raise GuardError("invalid key format")
        if not isinstance(self.api_secret, bytes) or not 8 <= len(self.api_secret) <= 512:
            raise GuardError("invalid secret format")

    def __repr__(self):
        return f"Credentials(scope={self.scope!r}, material=<redacted>)"


def _secure_parent(path: Path) -> int:
    parent = path.absolute().parent
    try:
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise GuardError("private parent cannot be safely opened") from None
    st = os.fstat(fd)
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
        os.close(fd)
        raise GuardError("private parent must be owned and mode 0700")
    return fd


def load_credentials(path: Path, scope: str) -> Credentials:
    """Only an explicitly supplied 0600 file in an owned 0700 directory.

    No environment variables, home-directory discovery, or automatic fallback.
    """
    parent_fd = _secure_parent(path)
    fd = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise GuardError("credential file must be owned, regular, and mode 0600")
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise GuardError("credential file too large")
        try:
            data = json.loads(raw)
            if set(data) != {"api_key", "api_secret", "scope"} or data["scope"] != scope:
                raise GuardError("credential scope or schema mismatch")
            return Credentials(data["api_key"], data["api_secret"].encode("ascii"), scope)
        except (TypeError, UnicodeError, KeyError, json.JSONDecodeError, AttributeError):
            raise GuardError("invalid credential document") from None
    except OSError:
        raise GuardError("credential file cannot be safely read") from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


@dataclass(frozen=True, repr=False)
class Request:
    base: str
    method: str
    path: str
    query: str
    body: bytes | None
    headers: Mapping[str, str]

    def __repr__(self):
        return f"Request(method={self.method!r}, path={self.path!r}, private=<redacted>)"


@dataclass(frozen=True, repr=False)
class Response:
    status: int
    body: bytes

    def __repr__(self):
        return f"Response(status={self.status}, private=<redacted>)"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


POLICY = {
    TESTNET_SCOPE: {
        ("GET", "/api/v3/ping"): (False, set()),
        ("GET", "/api/v3/time"): (False, set()),
        ("GET", "/api/v3/exchangeInfo"): (False, {"symbol"}),
        ("GET", "/api/v3/ticker/bookTicker"): (False, {"symbol"}),
        ("GET", "/api/v3/account"): (True, set()),
        ("GET", "/api/v3/order"): (True, {"symbol", "origClientOrderId", "orderId"}),
        ("GET", "/api/v3/myTrades"): (True, {"symbol", "orderId", "limit"}),
        ("POST", "/api/v3/order/test"): (True, {"symbol", "side", "type", "quantity", "price", "newClientOrderId"}),
        ("POST", "/api/v3/order"): (True, {"symbol", "side", "type", "quantity", "price", "newClientOrderId", "newOrderRespType"}),
        ("DELETE", "/api/v3/order"): (True, {"symbol", "origClientOrderId", "orderId"}),
    },
    WALLET_SCOPE: {
        ("GET", "/api/v3/time"): (False, set()),
        ("GET", "/sapi/v1/capital/config/getall"): (True, set()),
        ("GET", "/sapi/v1/capital/deposit/address"): (True, {"coin", "network"}),
        ("GET", "/sapi/v1/capital/deposit/hisrec"): (True, {"coin", "startTime", "endTime", "offset", "limit"}),
        ("GET", "/sapi/v1/account/info"): (True, set()),
        ("GET", "/sapi/v1/account/apiRestrictions"): (True, set()),
    },
}


def guard_request(scope: str, request: Request) -> None:
    expected = TESTNET if scope == TESTNET_SCOPE else PRODUCTION_WALLET if scope == WALLET_SCOPE else None
    if request.base != expected or (request.method, request.path) not in POLICY.get(scope, {}):
        raise GuardError("endpoint outside hard allowlist")
    if request.method == "GET" and request.body is not None or request.method != "GET" and request.query:
        raise GuardError("mixed parameter channels prohibited")


class HTTPS:
    """Normal TLS, no proxy discovery, redirects, or HTTP retries."""
    def __init__(self, scope: str, *, timeout: float = 10.0):
        if scope not in POLICY or not 0 < timeout <= 10:
            raise GuardError("invalid HTTPS configuration")
        self.scope, self.timeout = scope, timeout
        context = ssl.create_default_context()
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise GuardError("TLS verification required")
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect())

    def __call__(self, request: Request) -> Response:
        guard_request(self.scope, request)
        url = request.base + request.path + ("?" + request.query if request.query else "")
        req = urllib.request.Request(url, data=request.body, headers=dict(request.headers), method=request.method)
        try:
            with self._opener.open(req, timeout=self.timeout) as result:
                body = result.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise APIError("response-size", ambiguous=request.method != "GET")
                return Response(result.status, body)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(MAX_RESPONSE + 1)
            finally:
                exc.close()
            if len(body) > MAX_RESPONSE:
                raise APIError("response-size", status=exc.code, ambiguous=request.method != "GET") from None
            return Response(exc.code, body)
        except APIError:
            raise
        except Exception:
            # urllib errors may contain a signed URL. Never propagate their text.
            raise APIError("transport-unknown", ambiguous=request.method != "GET") from None


@dataclass(frozen=True)
class ClockEvidence:
    base: str
    before_ms: int
    after_ms: int
    server_ms: int

    @property
    def offset_ms(self) -> int:
        return self.server_ms - (self.before_ms + self.after_ms) // 2


class GuardedClient:
    def __init__(self, scope: str, credentials: Credentials | None = None, *,
                 transport: Callable[[Request], Response] | None = None,
                 now_ms: Callable[[], int] | None = None):
        if scope not in POLICY or credentials is not None and credentials.scope != scope:
            raise GuardError("client scope mismatch")
        self.scope = scope
        self.base = TESTNET if scope == TESTNET_SCOPE else PRODUCTION_WALLET
        self.credentials = credentials
        self._transport = transport if transport is not None else HTTPS(scope)
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._clock: ClockEvidence | None = None

    def install_clock(self, evidence: ClockEvidence):
        if evidence.base != self.base:
            raise GuardError("clock origin mismatch")
        for n in (evidence.before_ms, evidence.after_ms, evidence.server_ms):
            integer(n)
        if not 0 <= evidence.after_ms - evidence.before_ms <= 2000 or abs(evidence.offset_ms) > 5000:
            raise GuardError("clock evidence outside tolerance")
        self._clock = evidence

    def sync_clock(self) -> ClockEvidence:
        before = self.now_ms()
        server = self._request("GET", "/api/v3/time", {})
        after = self.now_ms()
        evidence = ClockEvidence(self.base, before, after, integer(server.get("serverTime")))
        self.install_clock(evidence)
        return evidence

    def _request(self, method: str, path: str, params: Mapping[str, str], *, deadline_ms: int | None = None):
        endpoint = POLICY[self.scope].get((method, path))
        if endpoint is None or not isinstance(params, Mapping) or set(params) - endpoint[1]:
            raise GuardError("unsupported method, path, or parameters")
        if deadline_ms is not None:
            if self.scope != TESTNET_SCOPE or (method, path) not in {
                    ("POST", "/api/v3/order/test"), ("POST", "/api/v3/order")}:
                raise GuardError("dispatch deadline is limited to testnet order POSTs")
            integer(deadline_ms, low=1)
        if any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 128 for k, v in params.items()):
            raise GuardError("invalid request parameter")
        private, _ = endpoint
        payload = dict(params)
        headers = {"Accept": "application/json"}
        if private:
            if self.credentials is None or self._clock is None:
                raise GuardError("private request requires scoped credentials and clock evidence")
            now = integer(self.now_ms())
            if not 0 <= now - self._clock.after_ms <= 30_000:
                raise GuardError("clock evidence stale or clock moved backwards")
            payload.update(recvWindow="5000", timestamp=str(now + self._clock.offset_ms))
            unsigned = encode(payload)
            encoded = unsigned + "&signature=" + sign(self.credentials.api_secret, unsigned)
            headers["X-MBX-APIKEY"] = self.credentials.api_key
        else:
            encoded = encode(payload)
        if method == "GET":
            req = Request(self.base, method, path, encoded, None, headers)
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            req = Request(self.base, method, path, "", encoded.encode("ascii"), headers)
        guard_request(self.scope, req)
        # Signing samples the clock too; a runner-only precheck cannot fence
        # clock advancement while preparing the signed request.
        if deadline_ms is not None and integer(self.now_ms()) > deadline_ms:
            raise GuardError("manifest expired before POST transport dispatch")
        try:
            res = self._transport(req)
        except GuardError:
            raise
        except APIError:
            raise
        except Exception:
            raise APIError("transport-unknown", ambiguous=method != "GET") from None
        if not isinstance(res, Response) or not isinstance(res.body, bytes) or len(res.body) > MAX_RESPONSE:
            raise APIError("invalid-response", ambiguous=method != "GET")
        try:
            data = json.loads(res.body)
        except (ValueError, UnicodeError):
            raise APIError("invalid-json", status=res.status, ambiguous=method != "GET") from None
        code = data.get("code") if isinstance(data, dict) and isinstance(data.get("code"), int) else None
        if res.status >= 300 or code is not None and code < 0:
            raise APIError("remote", status=res.status, code=code,
                           ambiguous=res.status >= 500 or code in (-1007, -1006))
        if not 200 <= res.status < 300 or not isinstance(data, (dict, list)):
            raise APIError("invalid-response", status=res.status, ambiguous=method != "GET")
        return data

    def ping(self):
        return self._request("GET", "/api/v3/ping", {})

    def filters(self, pair: str = "BTCUSDT"):
        return self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol(pair)})

    def quote(self, pair: str = "BTCUSDT"):
        return self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol(pair)})

    def account(self):
        return self._request("GET", "/api/v3/account", {})

    def query_order(self, pair: str, client_id: str, order_id: int | None = None):
        params = {"symbol": symbol(pair)}
        if order_id is None:
            if not re.fullmatch(r"qr_[a-f0-9]{30}", client_id):
                raise GuardError("client order identity invalid")
            params["origClientOrderId"] = client_id
        else:
            params["orderId"] = str(integer(order_id, low=1))
        return self._request("GET", "/api/v3/order", params)

    def trades(self, pair: str, order_id: int):
        return self._request("GET", "/api/v3/myTrades", {
            "symbol": symbol(pair), "orderId": str(integer(order_id, low=1)), "limit": "1000"})


@dataclass(frozen=True)
class OrderManifest:
    price: str
    quantity: str
    expires_ms: int
    version: str = "finite-testnet-limit-maker/1"
    base: str = TESTNET
    pair: str = "BTCUSDT"
    side: str = "BUY"
    order_type: str = "LIMIT_MAKER"
    quote_cap: str = "25"

    def __post_init__(self):
        if (self.version, self.base, self.pair, self.side, self.order_type, self.quote_cap) != (
                "finite-testnet-limit-maker/1", TESTNET, "BTCUSDT", "BUY", "LIMIT_MAKER", "25"):
            raise GuardError("manifest policy is fixed")
        if not isinstance(self.price, str) or not isinstance(self.quantity, str):
            raise GuardError("manifest uses decimal strings")
        p, q = number(self.price, positive=True), number(self.quantity, positive=True)
        with localcontext() as ctx:
            ctx.prec = 100
            if p > Decimal("1e12") or q > 1 or p * q > 25:
                raise GuardError("manifest exceeds finite size cap")
        integer(self.expires_ms, low=1)

    def document(self):
        return dict(vars(self))

    @property
    def sha256(self):
        return digest(self.document())

    @property
    def client_id(self):
        return "qr_" + self.sha256[:30]

    def order_params(self):
        return {"symbol": self.pair, "side": self.side, "type": self.order_type,
                "quantity": self.quantity, "price": self.price, "newClientOrderId": self.client_id}


def validate_market(manifest: OrderManifest, exchange: dict, quote: dict):
    rows = exchange.get("symbols")
    if not isinstance(rows, list) or len(rows) != 1:
        raise GuardError("expected one exact exchange symbol")
    row = rows[0]
    if row.get("symbol") != manifest.pair or row.get("status") != "TRADING" or not row.get("isSpotTradingAllowed"):
        raise GuardError("symbol not confirmed spot trading")
    filters = row.get("filters")
    if not isinstance(filters, list):
        raise GuardError("filters missing")
    indexed = {f.get("filterType"): f for f in filters}
    if len(indexed) != len(filters) or not {"PRICE_FILTER", "LOT_SIZE"} <= set(indexed):
        raise GuardError("required filters missing or duplicated")
    p, q = number(manifest.price, positive=True), number(manifest.quantity, positive=True)
    with localcontext() as ctx:
        ctx.prec = 100
        for name, n, minimum, maximum, grid in (
            ("PRICE_FILTER", p, "minPrice", "maxPrice", "tickSize"),
            ("LOT_SIZE", q, "minQty", "maxQty", "stepSize")):
            f = indexed[name]
            low, high, step = [number(f.get(key), nonnegative=True) for key in (minimum, maximum, grid)]
            if low and n < low or high and n > high or step and n % step != 0:
                raise GuardError("price or quantity filter violation")
        if "NOTIONAL" in indexed:
            f = indexed["NOTIONAL"]
            if p * q < number(f.get("minNotional"), nonnegative=True) or p * q > number(f.get("maxNotional"), positive=True):
                raise GuardError("notional filter violation")
        elif "MIN_NOTIONAL" in indexed:
            if p * q < number(indexed["MIN_NOTIONAL"].get("minNotional"), nonnegative=True):
                raise GuardError("minimum notional filter violation")
        else:
            raise GuardError("notional filter missing")
        if quote.get("symbol") != manifest.pair:
            raise GuardError("quote symbol mismatch")
        bid, ask = number(quote.get("bidPrice"), positive=True), number(quote.get("askPrice"), positive=True)
        if bid > ask or p >= ask:
            raise GuardError("invalid quote or maker order would cross current ask")
    # Average-price, account open-order-count, and live races remain server rules.
    return {"grid_notional_quote_controls": True, "all_server_filters_proven": False}


JOURNAL_SQL = (
    "CREATE TABLE intent (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "manifest TEXT NOT NULL, manifest_hash TEXT NOT NULL, client_id TEXT NOT NULL, "
    "post_used INTEGER NOT NULL DEFAULT 0 CHECK(post_used IN (0,1)), "
    "cancel_used INTEGER NOT NULL DEFAULT 0 CHECK(cancel_used IN (0,1)), "
    "test_used INTEGER NOT NULL DEFAULT 0 CHECK(test_used IN (0,1)), "
    "state TEXT NOT NULL, order_id INTEGER, last_order TEXT, baseline TEXT, "
    "reconciliation TEXT, last_error TEXT)"
)


class Journal:
    """One intent per owned 0600 SQLite file, including after terminal states.

    A committed reservation is consumed even if the process dies before HTTP.
    This is at-most-once sending, not proof that a request was delivered.
    """
    def __init__(self, path: Path):
        self.path = path.absolute()
        pfd = _secure_parent(path)
        fd = None
        self.db = None
        try:
            fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=pfd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
                raise GuardError("journal must be owned regular mode 0600")
            # Keep the checked file and parent descriptors alive across SQLite's
            # separate pathname open. Reject an observed replacement before any
            # PRAGMA/schema/state mutation. The parent remains a trusted owned
            # namespace: this is not a sandbox against arbitrary same-UID code.
            self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            current = os.stat(path.name, dir_fd=pfd, follow_symlinks=False)
            absolute = os.stat(self.path, follow_symlinks=False)
            for observed in (current, absolute):
                if (not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.geteuid()
                        or stat.S_IMODE(observed.st_mode) != 0o600
                        or (observed.st_dev, observed.st_ino) != (st.st_dev, st.st_ino)):
                    raise GuardError("journal identity or permissions changed across SQLite open")
        except BaseException as exc:
            if self.db is not None:
                self.db.close()
            if isinstance(exc, OSError):
                raise GuardError("journal cannot be safely opened") from None
            raise
        finally:
            if fd is not None:
                os.close(fd)
            os.close(pfd)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA trusted_schema=OFF")
        self.db.execute(JOURNAL_SQL.replace("CREATE TABLE intent", "CREATE TABLE IF NOT EXISTS intent"))
        try:
            self._verify_schema()
        except BaseException:
            self.db.close()
            raise

    def _verify_schema(self):
        schema = [tuple(row) for row in self.db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name")]
        if schema != [("table", "intent", "intent", JOURNAL_SQL)]:
            raise GuardError("journal schema must exactly match; triggers and extra objects prohibited")

    def close(self):
        self.db.close()

    def get(self):
        self._verify_schema()
        self.db.row_factory = sqlite3.Row
        row = self.db.execute("SELECT * FROM intent WHERE singleton=1").fetchone()
        return dict(row) if row else None

    def ensure(self, manifest: OrderManifest):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.get()
            if row is None:
                self.db.execute("INSERT INTO intent(singleton,manifest,manifest_hash,client_id,state) VALUES(1,?,?,?,?)",
                                (canonical(manifest.document()), manifest.sha256, manifest.client_id, "INTENT_DURABLE"))
            elif row["manifest_hash"] != manifest.sha256 or row["manifest"] != canonical(manifest.document()) or row["client_id"] != manifest.client_id:
                raise GuardError("a different intent already owns this journal")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def save_baseline(self, balances: Mapping[str, str]):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.get()
            if row is None or row["post_used"]:
                raise GuardError("cannot alter baseline after post reservation")
            if row["baseline"] is None:
                self.db.execute("UPDATE intent SET baseline=? WHERE singleton=1", (canonical(balances),))
            elif row["baseline"] != canonical(balances):
                raise GuardError("baseline changed before submission; manual review required")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def reserve(self, kind: str) -> bool:
        if kind not in ("post", "cancel", "test"):
            raise GuardError("unsupported reservation")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.get()
            column = kind + "_used"
            if row is None:
                raise GuardError("intent required before reservation")
            if row[column]:
                self.db.execute("COMMIT")
                return False
            if kind == "post" and (row["baseline"] is None or row["state"] not in ("INTENT_DURABLE", "TEST_VALIDATED")):
                raise GuardError("baseline and unsubmitted intent required")
            if kind == "cancel" and (row["order_id"] is None or row["state"] not in ACTIVE):
                raise GuardError("only a known active order can be canceled")
            if kind == "test" and row["post_used"]:
                raise GuardError("order test must precede post reservation")
            state = {"post": "SUBMIT_UNKNOWN", "cancel": "CANCEL_UNKNOWN", "test": "TEST_UNKNOWN"}[kind]
            self.db.execute(f"UPDATE intent SET {column}=1,state=?,reconciliation=NULL WHERE singleton=1", (state,))
            self.db.execute("COMMIT")
            return True
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def error(self, exc: APIError):
        self._verify_schema()
        self.db.execute("UPDATE intent SET last_error=? WHERE singleton=1", (canonical({
            "category": exc.category, "status": exc.status, "code": exc.code, "ambiguous": exc.ambiguous}),))

    def test_validated(self):
        self._verify_schema()
        self.db.execute("UPDATE intent SET state='TEST_VALIDATED',last_error=NULL WHERE singleton=1 AND post_used=0")

    def observe(self, manifest: OrderManifest, order: Mapping):
        if not isinstance(order, Mapping):
            raise GuardError("order object required")
        oid = integer(order.get("orderId"), low=1)
        state = order.get("status")
        if (order.get("symbol") != manifest.pair or state not in ACTIVE | TERMINAL
                or order.get("side") != manifest.side or order.get("type") != manifest.order_type
                or number(order.get("origQty"), positive=True) != number(manifest.quantity)
                or number(order.get("price"), positive=True) != number(manifest.price)):
            raise GuardError("order identity, type, or quantities mismatch")
        executed = number(order.get("executedQty"), nonnegative=True)
        cost = number(order.get("cummulativeQuoteQty"), nonnegative=True)
        if executed > number(manifest.quantity) or cost > 25 or state == "FILLED" and executed != number(manifest.quantity):
            raise GuardError("order execution outside intent")
        with localcontext() as ctx:
            ctx.prec = 100
            if cost > number(manifest.price) * executed:
                raise GuardError("BUY cumulative quote exceeds the approved limit price")
        identity = order.get("origClientOrderId", order.get("clientOrderId"))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.get()
            if previous is None or not previous["post_used"]:
                raise GuardError("cannot observe an unreserved order")
            if previous["order_id"] is not None and previous["order_id"] != oid:
                raise GuardError("order id changed")
            # A successful cancellation may replace clientOrderId. The already
            # bound immutable orderId is then the recovery identity; an initial
            # response or any explicit origClientOrderId must still match.
            if identity != manifest.client_id and not (
                    previous["cancel_used"] and previous["order_id"] == oid
                    and "origClientOrderId" not in order):
                raise GuardError("order client identity mismatch")
            if previous["last_order"]:
                old = json.loads(previous["last_order"])
                if executed < number(old["executedQty"]) or cost < number(old["cummulativeQuoteQty"]) or old["status"] in TERMINAL and state != old["status"]:
                    raise GuardError("order state or execution moved backwards")
            safe = {"symbol": manifest.pair, "orderId": oid, "clientOrderId": manifest.client_id,
                    "status": state, "side": manifest.side, "type": manifest.order_type,
                    "price": manifest.price, "origQty": manifest.quantity,
                    "executedQty": str(executed), "cummulativeQuoteQty": str(cost)}
            self.db.execute("UPDATE intent SET state=?,order_id=?,last_order=?,last_error=NULL,reconciliation=NULL WHERE singleton=1",
                            (state, oid, canonical(safe)))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def save_reconciliation(self, result: Mapping):
        self._verify_schema()
        self.db.execute("UPDATE intent SET reconciliation=? WHERE singleton=1", (canonical(result),))

    def summary(self):
        row = self.get()
        if row is None:
            return {"intent_exists": False}
        return {"intent_exists": True, "manifest_sha256": row["manifest_hash"], "state": row["state"],
                "new_order_post_reservations": row["post_used"], "cancel_reservations": row["cancel_used"],
                "order_test_reservations": row["test_used"], "terminal_observed": row["state"] in TERMINAL,
                "reconciliation": json.loads(row["reconciliation"]) if row["reconciliation"] else None,
                "last_error": json.loads(row["last_error"]) if row["last_error"] else None}


def balances(account: Mapping) -> dict[str, str]:
    if not isinstance(account, Mapping) or not isinstance(account.get("balances"), list):
        raise GuardError("account balances missing")
    totals = {}
    with localcontext() as ctx:
        ctx.prec = 100
        for row in account["balances"]:
            asset = asset_code(row.get("asset"))
            if asset in totals:
                raise GuardError("duplicate account asset")
            totals[asset] = str(number(row.get("free"), nonnegative=True) + number(row.get("locked"), nonnegative=True))
    return totals


def reconcile(manifest: OrderManifest, baseline: Mapping[str, str], current: Mapping[str, str],
              order: Mapping, trades: list) -> dict:
    """An independent wallet/position identity check, not an exchange audit.

    Assumes no unrelated trades, deposits, withdrawals, transfers, reset, or
    other balance changes between snapshots. Always states that assumption.
    """
    gaps = []
    if (order.get("symbol") != manifest.pair or order.get("side") != manifest.side
            or order.get("type") != manifest.order_type or order.get("clientOrderId") != manifest.client_id
            or number(order.get("price"), positive=True) != number(manifest.price)
            or number(order.get("origQty"), positive=True) != number(manifest.quantity)):
        gaps.append("order-versus-manifest-mismatch")
    if order.get("status") not in TERMINAL:
        gaps.append("order-not-terminal")
    if not isinstance(trades, list) or len(trades) >= 1000:
        return {"passed": False, "gaps": ["trade-page-coverage-unresolved"], "isolated_account_assumption": True}
    seen = set()
    total_qty = Decimal(0)
    total_quote = Decimal(0)
    commissions = defaultdict(Decimal)
    with localcontext() as ctx:
        ctx.prec = 100
        if (number(order.get("executedQty"), nonnegative=True) > number(manifest.quantity)
                or number(order.get("cummulativeQuoteQty"), nonnegative=True) >
                number(manifest.price) * number(order.get("executedQty"), nonnegative=True)):
            gaps.append("order-execution-limit-mismatch")
        for trade in trades:
            tid = integer(trade.get("id"))
            if tid in seen:
                gaps.append("duplicate-trade-id")
                continue
            seen.add(tid)
            if (trade.get("symbol") != manifest.pair or trade.get("orderId") != order.get("orderId")
                    or trade.get("isBuyer") is not True):
                gaps.append("trade-identity-mismatch")
                continue
            qty, cost = number(trade.get("qty"), positive=True), number(trade.get("quoteQty"), nonnegative=True)
            price = number(trade.get("price"), positive=True)
            if price > number(manifest.price):
                gaps.append("trade-price-exceeds-approved-buy-limit")
            if cost != qty * price:
                gaps.append("trade-quote-product-mismatch")
            total_qty += qty
            total_quote += cost
            commissions[asset_code(trade.get("commissionAsset"))] += number(trade.get("commission"), nonnegative=True)
        if total_qty != number(order.get("executedQty")) or total_quote != number(order.get("cummulativeQuoteQty")):
            gaps.append("fills-versus-order-mismatch")
        expected = {a: number(n, nonnegative=True) for a, n in baseline.items()}
        expected["BTC"] = expected.get("BTC", Decimal(0)) + total_qty
        expected["USDT"] = expected.get("USDT", Decimal(0)) - total_quote
        for asset, amount in commissions.items():
            expected[asset] = expected.get(asset, Decimal(0)) - amount
        for asset in set(expected) | set(current):
            if expected.get(asset, Decimal(0)) != number(current.get(asset, "0"), nonnegative=True):
                gaps.append("wallet-position-delta-mismatch")
                break
    return {"passed": not gaps, "gaps": sorted(set(gaps)), "unique_trade_count": len(seen),
            "isolated_account_assumption": True, "external_audit": False,
            "residual_position_possible": total_qty > 0, "automatic_close": False}


class FiniteRunner:
    def __init__(self, client: GuardedClient, journal: Journal, manifest: OrderManifest):
        if client.scope != TESTNET_SCOPE or client.base != TESTNET:
            raise GuardError("finite runner requires hard testnet client")
        self.client, self.journal, self.manifest = client, journal, manifest

    def _approval(self, execute: bool, approved_hash: str | None):
        if execute is not True or approved_hash != self.manifest.sha256:
            raise GuardError("explicit execute and exact approved manifest hash required")

    def _check_deadline(self):
        if self.client.now_ms() > self.manifest.expires_ms:
            raise GuardError("manifest expired before next order stage")

    def validate_order_test(self, *, execute: bool = False, approved_hash: str | None = None):
        self._approval(execute, approved_hash)
        self._check_deadline()
        self.journal.ensure(self.manifest)
        validate_market(self.manifest, self.client.filters(), self.client.quote())
        self._check_deadline()
        if not self.journal.reserve("test"):
            return self.journal.summary()
        try:
            self._check_deadline()
            result = self.client._request("POST", "/api/v3/order/test", self.manifest.order_params(),
                                          deadline_ms=self.manifest.expires_ms)
            if result != {}:
                raise GuardError("unexpected order-test response")
            self.journal.test_validated()
        except APIError as exc:
            self.journal.error(exc)
        return self.journal.summary()

    def _recover(self):
        row = self.journal.get()
        try:
            order = self.client.query_order(self.manifest.pair, self.manifest.client_id, row["order_id"])
            self.journal.observe(self.manifest, order)
        except APIError as exc:
            self.journal.error(exc)
        # -2013 never authorizes resubmission, even after a Testnet reset.

    def recover(self, *, execute: bool = False, approved_hash: str | None = None, cancel: bool = False):
        """Recovery cannot create or send an initial intent, even if approved."""
        self._approval(execute, approved_hash)
        row = self.journal.get()
        if row is None or row["post_used"] != 1:
            raise GuardError("recovery requires an already consumed post reservation")
        return self.run(execute=True, approved_hash=approved_hash, cancel=cancel)

    def run(self, *, execute: bool = False, approved_hash: str | None = None, cancel: bool = False):
        if execute is False:
            return {"dry_run": True, "manifest_sha256": self.manifest.sha256, "network_calls": 0,
                    "quote_cap_usdt": "25", "journal": self.journal.summary()}
        self._approval(execute, approved_hash)
        self.journal.ensure(self.manifest)
        row = self.journal.get()
        if not row["post_used"]:
            self._check_deadline()
            validate_market(self.manifest, self.client.filters(), self.client.quote())
            self._check_deadline()
            baseline = balances(self.client.account())
            self._check_deadline()
            self.journal.save_baseline(baseline)
            self._check_deadline()
            if self.journal.reserve("post"):
                params = self.manifest.order_params()
                params["newOrderRespType"] = "RESULT"
                try:
                    self._check_deadline()
                    order = self.client._request("POST", "/api/v3/order", params,
                                                 deadline_ms=self.manifest.expires_ms)
                    self.journal.observe(self.manifest, order)
                except APIError as exc:
                    self.journal.error(exc)
        else:
            self._recover()
        row = self.journal.get()
        if cancel and row["state"] in ACTIVE and self.journal.reserve("cancel"):
            try:
                result = self.client._request("DELETE", "/api/v3/order", {
                    "symbol": self.manifest.pair, "orderId": str(row["order_id"])})
                self.journal.observe(self.manifest, result)
            except APIError as exc:
                self.journal.error(exc)
        row = self.journal.get()
        if row["state"] in TERMINAL:
            try:
                order = json.loads(row["last_order"])
                trades = self.client.trades(self.manifest.pair, row["order_id"])
                current = balances(self.client.account())
                result = reconcile(self.manifest, json.loads(row["baseline"]), current, order, trades)
                self.journal.save_reconciliation(result)
            except APIError as exc:
                self.journal.error(exc)
        return self.journal.summary()


DEPOSIT_STATUS = {0: "pending", 1: "success-reported", 2: "rejected", 6: "credited-not-withdrawable",
                  7: "wrong-deposit", 8: "waiting-user-confirmation"}


class WalletReader:
    """Production wallet reads ONLY; never an order or money-movement client."""
    def __init__(self, client: GuardedClient):
        if client.scope != WALLET_SCOPE or client.base != PRODUCTION_WALLET:
            raise GuardError("wallet reader requires separate production GET-only client")
        self.client = client
        self._readonly_checked_at: int | None = None

    def _require_readonly(self):
        now = self.client.now_ms()
        if self._readonly_checked_at is None or not 0 <= now - self._readonly_checked_at <= 30_000:
            self.restrictions()

    def config(self):
        self._require_readonly()
        return self.client._request("GET", "/sapi/v1/capital/config/getall", {})

    def address(self, coin: str, network: str):
        if network == "LIGHTNING":
            raise GuardError("Lightning amount-dependent address not implemented")
        asset_code(coin)
        symbol(network)
        self._require_readonly()
        return self.client._request("GET", "/sapi/v1/capital/deposit/address", {
            "coin": asset_code(coin), "network": symbol(network)})

    def account_info(self):
        self._require_readonly()
        return self.client._request("GET", "/sapi/v1/account/info", {})

    def restrictions(self):
        restrictions = self.client._request("GET", "/sapi/v1/account/apiRestrictions", {})
        required = {"enableReading", "enableWithdrawals", "enableInternalTransfer", "enableSpotAndMarginTrading"}
        if not isinstance(restrictions, dict) or not required <= set(restrictions) or any(
                not isinstance(restrictions[k], bool) for k in required) or restrictions["enableReading"] is not True:
            self._readonly_checked_at = None
            raise GuardError("wallet read-only permission evidence missing")
        for key, value in restrictions.items():
            if (key.startswith("enable") or key.startswith("permit")) and key != "enableReading" and value is not False:
                self._readonly_checked_at = None
                raise GuardError("wallet key has write or unknown capability; narrow permissions manually")
        self._readonly_checked_at = self.client.now_ms()
        return restrictions

    def deposit_window(self, start_ms: int, end_ms: int, *, coin: str, limit: int = 1000, max_pages: int = 3):
        integer(start_ms)
        integer(end_ms)
        integer(limit, low=1, high=1000)
        integer(max_pages, low=1, high=10)
        if not 0 < end_ms - start_ms < 90 * 24 * 60 * 60 * 1000:
            raise GuardError("explicit deposit window must be positive and less than 90 days")
        asset_code(coin)
        self._require_readonly()
        seen: dict[str, str] = {}
        decoded = Counter()
        gaps = set()
        pages = 0
        pagination_terminal = False
        for page in range(max_pages):
            try:
                rows = self.client._request("GET", "/sapi/v1/capital/deposit/hisrec", {
                    "coin": coin, "startTime": str(start_ms), "endTime": str(end_ms),
                    "offset": str(page * limit), "limit": str(limit)})
            except APIError:
                gaps.add("page-request-unresolved")
                break
            pages += 1
            if not isinstance(rows, list) or len(rows) > limit:
                gaps.add("page-schema-unresolved")
                break
            new = 0
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), (str, int)) or isinstance(row.get("id"), bool):
                    gaps.add("deposit-id-missing")
                    continue
                identity = str(row["id"])
                if not identity or len(identity) > 200:
                    gaps.add("deposit-id-invalid")
                    continue
                # Private identity and complete row stay in memory only. No address/txId
                # or raw row is returned, journaled, or logged by this reader.
                fingerprint = digest(row)
                if identity in seen:
                    if seen[identity] != fingerprint:
                        gaps.add("duplicate-id-conflict")
                    continue
                seen[identity] = fingerprint
                new += 1
                try:
                    inserted = integer(row.get("insertTime"))
                    number(row.get("amount"), nonnegative=True)
                    if row.get("coin") != coin or not start_ms <= inserted <= end_ms:
                        gaps.add("row-outside-requested-window-or-coin")
                    status = integer(row.get("status"), high=1000)
                    label = DEPOSIT_STATUS.get(status, "unknown-status")
                    if label == "unknown-status":
                        gaps.add("unknown-deposit-status")
                    decoded[label] += 1
                except GuardError:
                    gaps.add("deposit-row-schema-unresolved")
            if len(rows) < limit:
                pagination_terminal = True
                break
            if new == 0:
                gaps.add("pagination-no-progress")
                break
        if not pagination_terminal:
            gaps.add("pagination-coverage-unresolved")
        return {"requested_start_ms": start_ms, "requested_end_ms": end_ms, "page_requests": pages,
                "unique_row_count": len(seen), "status_counts": dict(sorted(decoded.items())),
                "pagination_terminal_observed": pagination_terminal, "gaps": sorted(gaps),
                "coverage_complete": False, "balance_reconciled": False, "withdrawability_proven": False,
                "limitations": ["endpoint-lag-and-reset-or-retention-not-proven", "offset-pages-not-atomic-snapshot",
                                "status-is-not-an-account-balance-reconciliation"],
                "private_identifiers_returned": False, "automatic_transfer_or_trade": False}


def main(argv=None):
    """Offline manifest preview. Live execute is intentionally not a CLI flag."""
    import argparse
    parser = argparse.ArgumentParser(description="Offline Binance Testnet finite-intent candidate preview")
    parser.add_argument("--dry-run", action="store_true", help="Offline preview (also the default)")
    parser.add_argument("--price", default="50000")
    parser.add_argument("--quantity", default="0.0001")
    parser.add_argument("--expires-ms", type=int, default=1)
    args = parser.parse_args(argv)
    manifest = OrderManifest(args.price, args.quantity, args.expires_ms)
    print(canonical({"dry_run": True, "network_calls": 0, "credentials_read": False,
                     "manifest_sha256": manifest.sha256, "notional_quote_cap_usdt": "25",
                     "fee_inclusive_debit_cap_proven": False, "authenticated_connection": False,
                     "live_execution_cli": False}))


if __name__ == "__main__":
    main()
