"""Offline controls with PUBLIC RFC/Binance example keys and synthetic accounts.

No credentials are discovered or read except freshly created fake tempfile
fixtures. No HTTPS/network calls are made: every client uses injected transport.
"""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from decimal import ROUND_DOWN, localcontext
from pathlib import Path
from unittest.mock import patch

from research_base import binance_spot_testnet as a
from tests.helpers import binance_spot_testnet_v3_control as old_v3

NOW = 1_700_000_000_000


def manifest(**kw):
    return a.OrderManifest(**({"price": "50000", "quantity": "0.0001", "expires_ms": NOW + 60_000} | kw))


def filters():
    return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "isSpotTradingAllowed": True, "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
        {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "9000000"},
        {"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "1.2", "avgPriceMins": 5},
    ]}]}


def quote():
    return {"symbol": "BTCUSDT", "bidPrice": "50000", "askPrice": "60000"}


def account(btc="0", usdt="1000", bnb="1", locked="0"):
    return {"balances": [{"asset": "BTC", "free": btc, "locked": "0"},
                         {"asset": "USDT", "free": usdt, "locked": locked},
                         {"asset": "BNB", "free": bnb, "locked": "0"}]}


def order(m=None, status="NEW", qty="0", cost="0", **kw):
    m = m or manifest()
    return {"symbol": m.pair, "orderId": 71, "clientOrderId": m.client_id, "status": status,
            "side": m.side, "type": m.order_type, "price": m.price, "origQty": m.quantity,
            "executedQty": qty, "cummulativeQuoteQty": cost} | kw


def trade(qty="0.0001", cost="5", fee="0.005", asset="USDT", tid=1):
    return {"symbol": "BTCUSDT", "id": tid, "orderId": 71, "isBuyer": True, "qty": qty,
            "quoteQty": cost, "price": "50000", "commission": fee, "commissionAsset": asset}


class Fake:
    def __init__(self):
        self.calls = []
        self.routes = {}
        self.hook = None

    def queue(self, method, path, *values):
        self.routes.setdefault((method, path), []).extend(values)
        return self

    def __call__(self, request):
        self.calls.append((request.method, request.path))
        if self.hook:
            self.hook(request)
        seq = self.routes.get((request.method, request.path))
        if seq:
            value = seq.pop(0)
        else:
            value = {
                ("GET", "/api/v3/time"): {"serverTime": NOW},
                ("GET", "/api/v3/ping"): {},
                ("GET", "/api/v3/exchangeInfo"): filters(),
                ("GET", "/api/v3/ticker/bookTicker"): quote(),
                ("GET", "/api/v3/account"): account(),
                ("POST", "/api/v3/order"): order(),
                ("POST", "/api/v3/order/test"): {},
                ("GET", "/api/v3/order"): order(),
                ("DELETE", "/api/v3/order"): order(status="CANCELED", origClientOrderId=manifest().client_id),
                ("GET", "/api/v3/myTrades"): [],
                ("GET", "/sapi/v1/account/apiRestrictions"): {"enableReading": True, "enableWithdrawals": False,
                    "enableInternalTransfer": False, "enableSpotAndMarginTrading": False,
                    "enableFutures": False, "permitsUniversalTransfer": False},
            }.get((request.method, request.path), {})
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, a.Response):
            return value
        return a.Response(200, json.dumps(value).encode())


def client(fake, scope=a.TESTNET_SCOPE, clock=True, now_ms=None):
    c = a.GuardedClient(scope, a.Credentials("FAKE_API_KEY", b"FAKE_SECRET_ONLY", scope),
                        transport=fake, now_ms=now_ms or (lambda: NOW))
    if clock:
        c.install_clock(a.ClockEvidence(c.base, NOW, NOW, NOW))
    return c


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        os.chmod(self.directory, 0o700)
        self.fake = Fake()
        self.m = manifest()
        self.c = client(self.fake)
        self.j = a.Journal(self.directory / "intent.db")
        self.addCleanup(self.j.close)
        self.r = a.FiniteRunner(self.c, self.j, self.m)

    def run_intent(self, **kw):
        return self.r.run(execute=True, approved_hash=self.m.sha256, **kw)


class SigningControls(unittest.TestCase):
    def test_utf8_wallet_coin_percent_bytes_known_answer(self):
        # Expected digest independently calculated with local OpenSSL HMAC;
        # all material is synthetic. Exact Unicode identity is not normalized.
        payload = "coin=%E6%B5%8B%E8%AF%95%E5%B8%81&network=BSC&recvWindow=5000&timestamp=1700000000000"
        expected = "229db4327175e9143f1ca874212241fb8710af8c03859d584eb895f008950022"
        params = {"coin": "测试币", "network": "BSC", "recvWindow": "5000", "timestamp": str(NOW)}
        self.assertEqual(a.encode(params), payload)
        self.assertEqual(a.sign(b"FAKE_SECRET_ONLY", payload), expected)
        f = Fake()
        captured = []
        f.hook = lambda req: captured.append(req)
        reader = a.WalletReader(client(f, a.WALLET_SCOPE))
        reader.address("测试币", "BSC")
        request = next(req for req in captured if req.path == "/sapi/v1/capital/deposit/address")
        unsigned, signature = request.query.rsplit("&signature=", 1)
        self.assertEqual(unsigned, payload)
        self.assertEqual(signature, expected)
        self.assertEqual(urllib.parse.parse_qs(unsigned)["coin"], ["测试币"])
        self.assertNotIn("测试币", repr(request))

    def test_rfc4231_known_answer(self):
        # Public RFC 4231 section 4.2 test case 1, not account credentials.
        import hmac
        import hashlib
        self.assertEqual(hmac.new(bytes.fromhex("0b" * 20), b"Hi There", hashlib.sha256).hexdigest(),
                         "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7")

    def test_binance_documented_ascii_fixture(self):
        # Public illustrative key from official REST signing docs; never real.
        public_example = b"NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
        data = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
        self.assertEqual(a.sign(public_example, data), "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")

    def test_binance_documented_unicode_fixture(self):
        public_example = b"NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
        values = {"symbol": "１２３４５６", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "1",
                  "price": "0.1", "recvWindow": "5000", "timestamp": "1499827319559"}
        self.assertEqual(a.sign(public_example, a.encode(values)),
                         "e1353ec6b14d888f1164ae9af8228a3dbd508bc82eb867db8ab6046442f33ef3")

    def test_exact_signed_channel_and_redacted_repr(self):
        f = Fake()
        requests = []
        f.hook = lambda r: requests.append(r)
        c = client(f)
        c.account()
        request = requests[0]
        payload, signature = request.query.rsplit("&signature=", 1)
        self.assertEqual(a.sign(b"FAKE_SECRET_ONLY", payload), signature)
        self.assertEqual(request.headers["X-MBX-APIKEY"], "FAKE_API_KEY")
        self.assertIsNone(request.body)
        for value in (c.credentials, request, a.Response(200, b"private content")):
            self.assertNotIn("FAKE_API_KEY", repr(value))
            self.assertNotIn(signature, repr(value))
            self.assertNotIn("private content", repr(value))


class GuardControls(unittest.TestCase):
    def test_testnet_rejects_production_and_sapi(self):
        f, c = Fake(), client(Fake())
        for method, path in [("GET", "/sapi/v1/capital/deposit/hisrec"), ("POST", "/api/v3/order/cancelReplace"),
                             ("PUT", "/api/v3/order"), ("GET", "/api/v3/order?symbol=BTCUSDT"),
                             ("GET", "https://api.binance.com/api/v3/account")]:
            with self.subTest(method=method, path=path), self.assertRaises(a.GuardError):
                c._request(method, path, {})
        with self.assertRaises(a.GuardError):
            a.guard_request(a.TESTNET_SCOPE, a.Request("https://api.binance.com", "GET", "/api/v3/ping", "", None, {}))
        self.assertEqual(c._transport.calls, [])

    def test_wallet_only_five_signed_gets_plus_same_origin_clock(self):
        c = client(Fake(), a.WALLET_SCOPE)
        for method, path in [("POST", "/api/v3/order"), ("GET", "/api/v3/account"),
                             ("GET", "/sapi/v1/capital/withdraw/apply"), ("POST", "/sapi/v1/asset/transfer"),
                             ("POST", "/sapi/v1/capital/deposit/address")]:
            with self.subTest(method=method, path=path), self.assertRaises(a.GuardError):
                c._request(method, path, {})
        self.assertEqual(len(a.POLICY[a.WALLET_SCOPE]), 6)
        self.assertEqual(sum(private for private, _ in a.POLICY[a.WALLET_SCOPE].values()), 5)
        self.assertTrue(all(method == "GET" for method, _ in a.POLICY[a.WALLET_SCOPE]))
        with self.assertRaises(a.GuardError):
            a.WalletReader(client(Fake()))
        c.sync_clock()
        self.assertEqual(c._clock.base, a.PRODUCTION_WALLET)

    def test_scoped_credentials_cannot_mix(self):
        with self.assertRaises(a.GuardError):
            a.GuardedClient(a.WALLET_SCOPE, a.Credentials("FAKE_API_KEY", b"FAKE_SECRET", a.TESTNET_SCOPE), transport=Fake())

    def test_reserved_parameters_and_mixed_channels_rejected(self):
        c = client(Fake())
        for key in ("signature", "timestamp", "recvWindow", "url", "network"):
            with self.subTest(key=key), self.assertRaises(a.GuardError):
                c._request("GET", "/api/v3/account", {key: "x"})
        with self.assertRaises(a.GuardError):
            a.guard_request(a.TESTNET_SCOPE, a.Request(a.TESTNET, "GET", "/api/v3/ping", "", b"x", {}))
        self.assertEqual(c._transport.calls, [])

    def test_clock_required_origin_freshness_and_bounds(self):
        f = Fake()
        c = client(f, clock=False)
        with self.assertRaises(a.GuardError):
            c.account()
        for evidence in (a.ClockEvidence(a.PRODUCTION_WALLET, NOW, NOW, NOW),
                         a.ClockEvidence(a.TESTNET, NOW, NOW + 2001, NOW),
                         a.ClockEvidence(a.TESTNET, NOW, NOW, NOW + 5001)):
            with self.subTest(evidence=evidence), self.assertRaises(a.GuardError):
                c.install_clock(evidence)
        c.install_clock(a.ClockEvidence(a.TESTNET, NOW, NOW, NOW))
        c.now_ms = lambda: NOW + 30_001
        with self.assertRaises(a.GuardError):
            c.account()
        c.now_ms = lambda: NOW - 1
        with self.assertRaises(a.GuardError):
            c.account()
        self.assertEqual(f.calls, [])

    def test_public_operations_and_clock_sync(self):
        c = client(Fake(), clock=False)
        self.assertEqual(c.ping(), {})
        c.filters()
        c.quote()
        self.assertEqual(c.sync_clock().offset_ms, 0)
        c.account()
        self.assertEqual(len(c._transport.calls), 5)

    def test_redirect_and_invalid_json_do_not_retry(self):
        for response in (a.Response(302, b"{}"), a.Response(200, b"private invalid json"),
                         a.Response(200, b"x" * (a.MAX_RESPONSE + 1))):
            f = Fake().queue("GET", "/api/v3/account", response)
            with self.subTest(status=response.status), self.assertRaises(a.APIError):
                client(f).account()
            self.assertEqual(len(f.calls), 1)
        self.assertIsNone(a.NoRedirect().redirect_request(None, None, 302, None, None, "https://api.binance.com"))

    def test_https_configuration_and_guard_without_network(self):
        t = a.HTTPS(a.TESTNET_SCOPE)
        with self.assertRaises(a.GuardError):
            t(a.Request(a.PRODUCTION_WALLET, "GET", "/api/v3/ping", "", None, {}))
        self.assertTrue(any(isinstance(handler, a.NoRedirect) for handler in t._opener.handlers))
        with self.assertRaises(a.GuardError):
            a.HTTPS(a.TESTNET_SCOPE, timeout=11)

    def test_transport_exception_text_sanitized(self):
        f = Fake().queue("GET", "/api/v3/account", RuntimeError("FAKE_API_KEY signature=SECRET_URL"))
        try:
            client(f).account()
            self.fail("must raise")
        except a.APIError as exc:
            self.assertNotIn("FAKE_API_KEY", str(exc))
            self.assertNotIn("SECRET_URL", str(exc))


class CredentialControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "fake-testnet.json"
        self.path.write_text(json.dumps({"api_key": "FAKE_API_KEY", "api_secret": "FAKE_SECRET_ONLY", "scope": a.TESTNET_SCOPE}))
        os.chmod(self.path, 0o600)

    def test_private_fixture_loads(self):
        self.assertEqual(a.load_credentials(self.path, a.TESTNET_SCOPE).scope, a.TESTNET_SCOPE)

    def test_bad_modes_and_wrong_scope_rejected(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(a.GuardError):
            a.load_credentials(self.path, a.TESTNET_SCOPE)
        os.chmod(self.path, 0o600)
        os.chmod(self.directory, 0o755)
        with self.assertRaises(a.GuardError):
            a.load_credentials(self.path, a.TESTNET_SCOPE)
        os.chmod(self.directory, 0o700)
        with self.assertRaises(a.GuardError):
            a.load_credentials(self.path, a.WALLET_SCOPE)

    def test_symlink_and_large_file_rejected(self):
        link = self.directory / "link.json"
        link.symlink_to(self.path)
        with self.assertRaises(a.GuardError):
            a.load_credentials(link, a.TESTNET_SCOPE)
        self.path.write_bytes(b"x" * 4097)
        with self.assertRaises(a.GuardError):
            a.load_credentials(self.path, a.TESTNET_SCOPE)


class ManifestControls(unittest.TestCase):
    def test_fixed_cap_and_hostile_decimal_context(self):
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 2, ROUND_DOWN
            with self.assertRaises(a.GuardError):
                manifest(price="25.1", quantity="1")
            m = manifest(price="25", quantity="1")
            self.assertEqual(m.quote_cap, "25")
            self.assertEqual(a.validate_market(manifest(), filters(), quote())["all_server_filters_proven"], False)
        self.assertEqual(manifest().client_id, manifest().client_id)
        self.assertNotEqual(manifest(price="50001").client_id, manifest().client_id)

    def test_nonfinite_floats_sign_and_policy_rejected(self):
        for kw in ({"price": 50000.0}, {"price": "NaN"}, {"price": "Infinity"}, {"quantity": "0"},
                   {"quantity": "-0.1"}, {"quantity": "1e-19"}, {"base": a.PRODUCTION_WALLET},
                   {"side": "SELL"}, {"order_type": "MARKET"}, {"pair": "ETHUSDT"}, {"quote_cap": "26"}):
            with self.subTest(kw=kw), self.assertRaises(a.GuardError):
                manifest(**kw)

    def test_price_quantity_notional_and_maker_guards(self):
        for m in (manifest(price="50000.001"), manifest(quantity="0.0001001"),
                  manifest(quantity="0.00001"), manifest(price="60000")):
            with self.subTest(m=m), self.assertRaises(a.GuardError):
                a.validate_market(m, filters(), quote())
        for mutate in (lambda f: f["symbols"][0].update(status="HALT"),
                       lambda f: f["symbols"][0].update(isSpotTradingAllowed=False),
                       lambda f: f["symbols"][0]["filters"].append(f["symbols"][0]["filters"][0])):
            f = filters()
            mutate(f)
            with self.assertRaises(a.GuardError):
                a.validate_market(manifest(), f, quote())

    def test_cli_offline_default_and_no_live_execute_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            a.main([])
        result = json.loads(out.getvalue())
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["network_calls"], 0)
        self.assertFalse(result["credentials_read"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            a.main(["--execute"])


class LifecycleControls(FixtureCase):
    def test_default_dry_run_and_exact_hash_approval(self):
        self.assertTrue(self.r.run()["dry_run"])
        self.assertIsNone(self.j.get())
        for approved in (None, "f" * 64):
            with self.subTest(approved=approved), self.assertRaises(a.GuardError):
                self.r.run(execute=True, approved_hash=approved)
        self.assertEqual(self.fake.calls, [])

    def test_intent_and_post_reservation_visible_before_http(self):
        observed = []
        def hook(req):
            if req.method == "POST" and req.path == "/api/v3/order":
                with contextlib.closing(sqlite3.connect(self.j.path)) as db:
                    row = db.execute("SELECT manifest_hash,post_used,state,baseline FROM intent").fetchone()
                observed.append(row)
        self.fake.hook = hook
        self.assertEqual(self.run_intent()["state"], "NEW")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][:3], (self.m.sha256, 1, "SUBMIT_UNKNOWN"))
        self.assertIsNotNone(observed[0][3])
        self.run_intent()
        self.assertEqual(self.fake.calls.count(("POST", "/api/v3/order")), 1)

    def test_timeout_then_minus2013_never_resubmits_after_restart(self):
        self.fake.queue("POST", "/api/v3/order", TimeoutError("private signed URL"))
        first = self.run_intent()
        self.assertEqual(first["state"], "SUBMIT_UNKNOWN")
        self.fake.queue("GET", "/api/v3/order", a.Response(400, b'{"code":-2013,"msg":"Order does not exist"}'))
        other = a.Journal(self.j.path)
        self.addCleanup(other.close)
        runner = a.FiniteRunner(self.c, other, self.m)
        second = runner.run(execute=True, approved_hash=self.m.sha256)
        self.assertEqual(second["state"], "SUBMIT_UNKNOWN")
        self.assertEqual(second["last_error"]["code"], -2013)
        self.assertEqual(runner.run(execute=True, approved_hash=self.m.sha256)["state"], "NEW")
        self.assertEqual(self.fake.calls.count(("POST", "/api/v3/order")), 1)

    def test_http500_and_api1007_remain_unknown(self):
        for response in (a.Response(500, b'{"code":-1000}'), a.Response(400, b'{"code":-1007}')):
            with self.subTest(response=response):
                with tempfile.TemporaryDirectory() as directory:
                    os.chmod(directory, 0o700)
                    j = a.Journal(Path(directory) / "i.db")
                    try:
                        f = Fake().queue("POST", "/api/v3/order", response)
                        r = a.FiniteRunner(client(f), j, self.m)
                        result = r.run(execute=True, approved_hash=self.m.sha256)
                        self.assertTrue(result["last_error"]["ambiguous"])
                        r.run(execute=True, approved_hash=self.m.sha256)
                        self.assertEqual(f.calls.count(("POST", "/api/v3/order")), 1)
                    finally:
                        j.close()

    def test_pre_http_crash_spends_reservation_and_expired_recovery(self):
        self.j.ensure(self.m)
        self.j.save_baseline(a.balances(account()))
        self.assertTrue(self.j.reserve("post"))
        self.c.now_ms = lambda: NOW + 120_000
        self.c.install_clock(a.ClockEvidence(a.TESTNET, NOW + 120_000, NOW + 120_000, NOW + 120_000))
        self.assertEqual(self.run_intent()["state"], "NEW")
        self.assertNotIn(("POST", "/api/v3/order"), self.fake.calls)

    def test_different_intent_and_expired_initial_submission_rejected(self):
        self.j.ensure(self.m)
        with self.assertRaises(a.GuardError):
            self.j.ensure(manifest(price="50001"))
        expired = manifest(expires_ms=NOW - 1)
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            j = a.Journal(Path(directory) / "i.db")
            try:
                r = a.FiniteRunner(self.c, j, expired)
                with self.assertRaises(a.GuardError):
                    r.run(execute=True, approved_hash=expired.sha256)
            finally:
                j.close()
        self.assertEqual(self.fake.calls, [])

    def test_concurrent_reservations_allow_only_one_sender(self):
        self.j.ensure(self.m)
        self.j.save_baseline(a.balances(account()))
        barrier = threading.Barrier(2)
        results, errors = [], []
        def compete():
            other = a.Journal(self.j.path)
            try:
                barrier.wait(timeout=5)
                results.append(other.reserve("post"))
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                other.close()
        threads = [threading.Thread(target=compete) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.j.get()["post_used"], 1)

    def test_order_test_separate_explicit_and_no_matching_engine_claim(self):
        with self.assertRaises(a.GuardError):
            self.r.validate_order_test()
        result = self.r.validate_order_test(execute=True, approved_hash=self.m.sha256)
        self.assertEqual(result["state"], "TEST_VALIDATED")
        self.assertEqual(result["new_order_post_reservations"], 0)
        self.assertIsNone(result["reconciliation"])
        self.r.validate_order_test(execute=True, approved_hash=self.m.sha256)
        self.assertEqual(self.fake.calls.count(("POST", "/api/v3/order/test")), 1)
        self.run_intent()
        self.assertEqual(self.fake.calls.count(("POST", "/api/v3/order")), 1)

    def test_cancel_reservation_durable_once_and_no_automatic_close(self):
        self.run_intent()
        observed = []
        def hook(req):
            if req.method == "DELETE":
                with contextlib.closing(sqlite3.connect(self.j.path)) as db:
                    observed.append(db.execute("SELECT cancel_used,state FROM intent").fetchone())
        self.fake.hook = hook
        self.fake.queue("DELETE", "/api/v3/order", TimeoutError())
        self.assertEqual(self.run_intent(cancel=True)["state"], "CANCEL_UNKNOWN")
        self.run_intent(cancel=True)
        self.assertEqual(observed, [(1, "CANCEL_UNKNOWN")])
        self.assertEqual(self.fake.calls.count(("DELETE", "/api/v3/order")), 1)
        self.assertEqual(self.fake.calls.count(("POST", "/api/v3/order")), 1)

    def test_canceled_partial_fills_not_flat_reconciled_and_cancel_id_recovery(self):
        self.fake.queue("POST", "/api/v3/order", order(status="PARTIALLY_FILLED", qty="0.00004", cost="2"))
        changed = order(status="CANCELED", qty="0.00004", cost="2", clientOrderId="cancel-replaced", origClientOrderId=self.m.client_id)
        self.fake.queue("DELETE", "/api/v3/order", changed)
        self.fake.queue("GET", "/api/v3/account", account(), account("0.00004", "997.998"), account("0.00004", "997.998"))
        self.fake.queue("GET", "/api/v3/myTrades", [trade("0.00004", "2", "0.002")], [trade("0.00004", "2", "0.002")])
        result = self.run_intent(cancel=True)
        self.assertEqual(result["state"], "CANCELED")
        self.assertTrue(result["reconciliation"]["passed"])
        self.assertTrue(result["reconciliation"]["residual_position_possible"])
        self.assertFalse(result["reconciliation"]["automatic_close"])
        changed.pop("origClientOrderId")
        self.fake.queue("GET", "/api/v3/order", changed)
        self.assertTrue(self.run_intent()["reconciliation"]["passed"])
        self.assertEqual(self.fake.calls.count(("DELETE", "/api/v3/order")), 1)

    def test_order_identity_unknown_status_and_backward_execution_rejected(self):
        self.run_intent()
        bad = [order(clientOrderId="some-other-intent"), order(orderId=99), order(status="MAGIC"),
               order(qty="0.0002", cost="10"), order(status="FILLED", qty="0.00004", cost="2")]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(a.GuardError):
                self.j.observe(self.m, value)
        self.j.observe(self.m, order(status="PARTIALLY_FILLED", qty="0.00004", cost="2"))
        with self.assertRaises(a.GuardError):
            self.j.observe(self.m, order())

    def test_order_cumulative_quote_cannot_exceed_approved_buy_limit(self):
        self.run_intent()
        with self.assertRaises(a.GuardError):
            self.j.observe(self.m, order(status="PARTIALLY_FILLED", qty="0.00004", cost="2.01"))

    def test_terminal_state_cannot_reopen_or_create_next_intent(self):
        self.run_intent()
        self.j.observe(self.m, order(status="CANCELED"))
        with self.assertRaises(a.GuardError):
            self.j.observe(self.m, order())
        with self.assertRaises(a.GuardError):
            self.j.ensure(manifest(expires_ms=NOW + 600_001))
        self.assertFalse(self.j.reserve("post"))

    def test_recover_requires_spent_post_and_cannot_submit_initial(self):
        with self.assertRaises(a.GuardError):
            self.r.recover(execute=True, approved_hash=self.m.sha256)
        self.j.ensure(self.m)
        with self.assertRaises(a.GuardError):
            self.r.recover(execute=True, approved_hash=self.m.sha256)
        self.assertEqual(self.fake.calls, [])

    def test_journal_trigger_and_weakened_schema_rejected(self):
        self.j.ensure(self.m)
        self.j.db.execute("CREATE TRIGGER reset_flag AFTER UPDATE ON intent BEGIN UPDATE intent SET post_used=0; END")
        with self.assertRaises(a.GuardError):
            self.j.reserve("post")
        with self.assertRaises(a.GuardError):
            a.Journal(self.j.path)
        alternate = self.directory / "weak.db"
        with contextlib.closing(sqlite3.connect(alternate)) as db:
            db.execute(a.JOURNAL_SQL.replace("CHECK(post_used IN (0,1))", ""))
            db.commit()
        os.chmod(alternate, 0o600)
        with self.assertRaises(a.GuardError):
            a.Journal(alternate)

    def test_connect_time_path_swap_rejected_and_connection_closed(self):
        candidate = self.directory / "candidate.db"
        candidate_journal = a.Journal(candidate)
        candidate_journal.close()
        replacement = self.directory / "replacement.db"
        replacement_journal = a.Journal(replacement)
        replacement_journal.close()
        os.chmod(replacement, 0o644)
        real_connect = sqlite3.connect
        opened = []
        def swapping_connect(path, *args, **kw):
            os.replace(replacement, candidate)
            conn = real_connect(path, *args, **kw)
            opened.append(conn)
            return conn
        with patch.object(a.sqlite3, "connect", swapping_connect), self.assertRaises(a.GuardError):
            a.Journal(candidate)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_same_mode_connect_time_inode_swap_also_rejected(self):
        candidate = self.directory / "candidate.db"
        old = a.Journal(candidate)
        old.close()
        replacement = self.directory / "replacement.db"
        new = a.Journal(replacement)
        new.close()
        real_connect = sqlite3.connect
        def swapping_connect(path, *args, **kw):
            os.replace(replacement, candidate)
            return real_connect(path, *args, **kw)
        with patch.object(a.sqlite3, "connect", swapping_connect), self.assertRaises(a.GuardError):
            a.Journal(candidate)


class ReconciliationControls(unittest.TestCase):
    def test_one_character_assets_parse_and_commission_reconcile(self):
        baseline=account()
        baseline['balances'] += [{'asset':c,'free':'1.25','locked':'.75'} for c in ('S','W','T')]
        totals=a.balances(baseline)
        self.assertTrue(all(a.number(totals[c])==2 for c in ('S','W','T')))
        current=dict(totals)
        current.update(BTC='0.0001',USDT='995',S='1.99')
        result=a.reconcile(manifest(),totals,current,order(status='FILLED',qty='0.0001',cost='5'),[trade(fee='.01',asset='S')])
        self.assertTrue(result['passed'])

    def test_asset_invalid_empty_punctuation_length_and_pair_stays_min2(self):
        for value in ('','A-B','A B','A'*21,None,'\u0000','\n','🚀'):
            with self.subTest(value=value),self.assertRaises(a.GuardError):
                a.asset_code(value)
        self.assertEqual(a.asset_code('S'),'S')
        with self.assertRaises(a.GuardError):
            a.symbol('S')

    def test_unicode_asset_balances_and_commission_preserve_exact_identity(self):
        baseline = account()
        codes = ('测试币', 'é', '１２', 'btc', 'BTC')
        # BTC already has a distinct fixed inventory row; no case folding.
        baseline['balances'] += [{'asset':c,'free':'1.25','locked':'.75'} for c in codes if c!='BTC']
        totals = a.balances(baseline)
        self.assertTrue(all(a.number(totals[c])==2 for c in codes if c!='BTC'))
        self.assertEqual(a.number(totals['BTC']),0)
        self.assertEqual(a.asset_code('btc'),'btc')
        self.assertEqual(a.asset_code('１２'),'１２')
        current=dict(totals)
        current.update(BTC='0.0001',USDT='995')
        current['测试币']='1.99'
        result=a.reconcile(manifest(),totals,current,order(status='FILLED',qty='0.0001',cost='5'),
                           [trade(fee='.01',asset='测试币')])
        self.assertTrue(result['passed'])
        self.assertNotIn('测试币',json.dumps(result,ensure_ascii=False))
        with self.assertRaises(a.GuardError):
            a.symbol('测试币')

    def test_cash_identity_does_not_override_limit_price_contradiction(self):
        m = manifest(price="100", quantity="0.02")
        impossible_order = order(m, status="FILLED", qty="0.02", cost="2.02")
        impossible_trade = trade("0.02", "2.02", "0.002") | {"price": "101"}
        result = a.reconcile(m, a.balances(account()), a.balances(account("0.02", "997.978")),
                             impossible_order, [impossible_trade])
        self.assertFalse(result["passed"])
        self.assertIn("trade-price-exceeds-approved-buy-limit", result["gaps"])
        self.assertIn("order-execution-limit-mismatch", result["gaps"])

    def test_filled_quote_fee_balance_identity(self):
        result = a.reconcile(manifest(), a.balances(account()), a.balances(account("0.0001", "994.995")),
                             order(status="FILLED", qty="0.0001", cost="5"), [trade()])
        self.assertTrue(result["passed"])
        self.assertTrue(result["isolated_account_assumption"])

    def test_base_and_third_asset_commissions_and_locked_totals(self):
        for asset, fee, target in [("BTC", "0.0000001", account("0.0000999", "995")),
                                   ("BNB", "0.01", account("0.0001", "990", "0.99", "5"))]:
            with self.subTest(asset=asset):
                result = a.reconcile(manifest(), a.balances(account()), a.balances(target),
                                     order(status="FILLED", qty="0.0001", cost="5"), [trade(fee=fee, asset=asset)])
                self.assertTrue(result["passed"])

    def test_bad_balance_order_qty_identity_duplicate_and_page_gap(self):
        known_order = order(status="FILLED", qty="0.0001", cost="5")
        cases = [(account("0.0001", "995"), [trade()], "wallet-position-delta-mismatch"),
                 (account(), [], "fills-versus-order-mismatch"),
                 (account(), [trade() | {"orderId": 99}], "trade-identity-mismatch"),
                 (account(), [trade(), trade()], "duplicate-trade-id"),
                 (account(), [trade(tid=i) for i in range(1000)], "trade-page-coverage-unresolved")]
        for current, trades, gap in cases:
            with self.subTest(gap=gap):
                result = a.reconcile(manifest(), a.balances(account()), a.balances(current), known_order, trades)
                self.assertFalse(result["passed"])
                self.assertIn(gap, result["gaps"])

    def test_active_order_is_never_terminal_reconciliation(self):
        result = a.reconcile(manifest(), a.balances(account()), a.balances(account()), order(), [])
        self.assertFalse(result["passed"])
        self.assertIn("order-not-terminal", result["gaps"])


def deposit(i, status=1, inserted=NOW, **kw):
    return {"id": str(i), "coin": "USDT", "network": "ETH", "amount": "5", "status": status,
            "insertTime": inserted, "address": "PRIVATE_FAKE_ADDRESS", "txId": "PRIVATE_FAKE_TXID"} | kw


class WalletControls(unittest.TestCase):
    def test_wallet_single_character_coin_does_not_relax_network(self):
        reader,f=self.reader([])
        reader.address('S','ETH')
        reader.deposit_window(NOW-1,NOW+1,coin='S')
        with self.assertRaises(a.GuardError):
            reader.address('S','E')

    def reader(self, *pages):
        f = Fake().queue("GET", "/sapi/v1/capital/deposit/hisrec", *pages)
        return a.WalletReader(client(f, a.WALLET_SCOPE)), f

    def window(self, reader, **kw):
        return reader.deposit_window(NOW - 1000, NOW + 1000, coin="USDT", **kw)

    def test_decode_all_statuses_and_no_identifiers_returned(self):
        reader, f = self.reader([deposit(i, status) for i, status in enumerate((0, 1, 2, 6, 7, 8))])
        result = self.window(reader)
        self.assertEqual(set(result["status_counts"]), set(a.DEPOSIT_STATUS.values()))
        self.assertTrue(result["pagination_terminal_observed"])
        self.assertFalse(result["coverage_complete"])
        self.assertFalse(result["balance_reconciled"])
        self.assertFalse(result["withdrawability_proven"])
        self.assertNotIn("PRIVATE_FAKE_ADDRESS", json.dumps(result))
        self.assertNotIn("PRIVATE_FAKE_TXID", json.dumps(result))
        self.assertEqual(f.calls, [("GET", "/sapi/v1/account/apiRestrictions"), ("GET", "/sapi/v1/capital/deposit/hisrec")])

    def test_duplicate_deposits_conflicts_and_no_progress(self):
        reader, _ = self.reader([deposit(1), deposit(2)], [deposit(1), deposit(2, status=6)])
        result = self.window(reader, limit=2, max_pages=3)
        self.assertEqual(result["unique_row_count"], 2)
        self.assertIn("duplicate-id-conflict", result["gaps"])
        self.assertIn("pagination-no-progress", result["gaps"])
        self.assertFalse(result["coverage_complete"])

    def test_full_page_budget_and_nonatomic_even_when_terminal(self):
        reader, _ = self.reader([deposit(1), deposit(2)])
        result = self.window(reader, limit=2, max_pages=1)
        self.assertIn("pagination-coverage-unresolved", result["gaps"])
        reader, _ = self.reader([deposit(1), deposit(2)], [])
        result = self.window(reader, limit=2)
        self.assertTrue(result["pagination_terminal_observed"])
        self.assertFalse(result["coverage_complete"])

    def test_explicit_windows_and_request_budgets(self):
        reader, f = self.reader([])
        for start, end, kw in [(NOW, NOW, {}), (NOW, NOW - 1, {}), (0, 90 * 86_400_000, {}),
                               (NOW, NOW + 1, {"limit": 1001}), (NOW, NOW + 1, {"max_pages": 11})]:
            with self.subTest(start=start, end=end, kw=kw), self.assertRaises(a.GuardError):
                reader.deposit_window(start, end, coin="USDT", **kw)
        self.assertEqual(f.calls, [])

    def test_unknown_missing_outside_window_and_request_error_gaps(self):
        missing = deposit(1)
        missing.pop("id")
        reader, _ = self.reader([missing, deposit(2, status=99), deposit(3, inserted=NOW + 2000)])
        result = self.window(reader)
        for gap in ("deposit-id-missing", "unknown-deposit-status", "row-outside-requested-window-or-coin"):
            self.assertIn(gap, result["gaps"])
        reader, _ = self.reader(TimeoutError())
        self.assertIn("page-request-unresolved", self.window(reader)["gaps"])

    def test_all_five_signed_endpoints_get_only_and_explicit_network(self):
        reader, f = self.reader([])
        reader.config()
        reader.address("USDT", "ETH")
        reader.account_info()
        reader.restrictions()
        self.window(reader)
        self.assertEqual(len(f.calls), 6)
        self.assertTrue(all(method == "GET" for method, _ in f.calls))
        with self.assertRaises(a.GuardError):
            reader.address("BTC", "LIGHTNING")
        with self.assertRaises(a.GuardError):
            reader.address("USDT", "")

    def test_wallet_write_privilege_and_missing_permission_evidence_stop_reads(self):
        for permissions in ({}, {"enableReading": False, "enableWithdrawals": False,
                                  "enableInternalTransfer": False, "enableSpotAndMarginTrading": False},
                            {"enableReading": True, "enableWithdrawals": True,
                             "enableInternalTransfer": False, "enableSpotAndMarginTrading": False},
                            {"enableReading": True, "enableWithdrawals": False,
                             "enableInternalTransfer": False, "enableSpotAndMarginTrading": False, "enableNewCapability": True}):
            f = Fake().queue("GET", "/sapi/v1/account/apiRestrictions", permissions)
            reader = a.WalletReader(client(f, a.WALLET_SCOPE))
            with self.subTest(permissions=permissions), self.assertRaises(a.GuardError):
                reader.config()
            self.assertEqual(f.calls, [("GET", "/sapi/v1/account/apiRestrictions")])


DEADLINE_EVIDENCE = []
DEADLINE_CRITERIA = {
    "schema": "binance-api-local-post-deadline-controls/1",
    "kind": "producer controls with old-v3 instrument sensitivity; not independent audit",
    "freeze_before_execution": True,
    "old_v3_sha256": "154fdb545de15b061314c0ddd02546bf1446b47d5e3ac1fb687f412f174a4442",
    "expiry_rule": "last local dispatch sample<=expires; equality allowed; not venue receipt/fill guarantee",
    "advancing_phases": ["filters", "quote", "account", "save_baseline", "reserve", "sign", "guard_request"],
    "pre_reservation_expiry": "GuardError; relevant budget remains0; no POST transport",
    "post_reservation_expiry": "GuardError; relevant budget remains1; no POST transport; query-only recovery for post",
    "normal_regression": "all54 preexisting controls retained under v4 import",
    "existing_limits_unchanged": ["clock RTT2000ms", "clock offset5000ms", "clock freshness30000ms", "hard domains",
                                  "BTCUSDT BUY LIMIT_MAKER grossnotional<=25", "one retained journal lifecycle"],
    "forbidden": ["real account/key/journal", "network", "paid calls", "old source edits"],
}


class AdvancingClock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value


class DeadlineFake:
    """One module's Response class, so old/new sensitivity controls are real."""
    def __init__(self, module, m, clock, slow_path=None):
        self.module, self.m, self.clock = module, m, clock
        self.slow_path = slow_path
        self.calls = []
        self.query_not_found = False

    def __call__(self, request):
        self.calls.append((request.method, request.path, self.clock.value))
        if request.path == self.slow_path:
            self.clock.value = self.m.expires_ms + 1
        value = {
            ("GET", "/api/v3/exchangeInfo"): filters(),
            ("GET", "/api/v3/ticker/bookTicker"): quote(),
            ("GET", "/api/v3/account"): account(),
            ("POST", "/api/v3/order/test"): {},
            ("POST", "/api/v3/order"): order(self.m),
            ("GET", "/api/v3/order"): order(self.m),
        }.get((request.method, request.path), {})
        status = 200
        if self.query_not_found and request.path == "/api/v3/order" and request.method == "GET":
            status, value = 400, {"code": -2013, "msg": "synthetic missing order"}
        return self.module.Response(status, json.dumps(value).encode())

    def posts(self):
        return [(method, path, clock) for method, path, clock in self.calls if method == "POST"]


class DeadlineControls(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, module, *, slow_path=None):
        with tempfile.TemporaryDirectory(prefix="fake-local-deadline-") as temp:
            directory = Path(temp); directory.chmod(0o700)
            clock = AdvancingClock()
            m = module.OrderManifest("50000", "0.0001", NOW + 100)
            transport = DeadlineFake(module, m, clock, slow_path)
            c = module.GuardedClient(module.TESTNET_SCOPE,
                                     module.Credentials("FAKE_API_KEY", b"FAKE_SECRET_ONLY", module.TESTNET_SCOPE),
                                     transport=transport, now_ms=clock)
            c.install_clock(module.ClockEvidence(c.base, NOW, NOW, NOW))
            path = directory / "only-fake.db"
            j = module.Journal(path)
            try:
                yield clock, m, transport, c, j, module.FiniteRunner(c, j, m), path
            finally:
                j.close()

    @staticmethod
    def invoke(runner, m, kind):
        if kind == "test":
            return runner.validate_order_test(execute=True, approved_hash=m.sha256)
        return runner.run(execute=True, approved_hash=m.sha256)

    def record(self, module, phase, kind, clock, m, fake, journal, guarded):
        row = journal.get()
        evidence = {"implementation": "v3" if module is old_v3 else "v4", "phase": phase, "kind": kind,
                    "guarded": guarded, "post_transport_count": len(fake.posts()),
                    "post_clock_offsets_from_deadline": [t - m.expires_ms for _, _, t in fake.posts()],
                    "test_used": row["test_used"] if row else 0, "post_used": row["post_used"] if row else 0,
                    "state": row["state"] if row else "NO_INTENT"}
        DEADLINE_EVIDENCE.append(evidence)

    def test_slow_filters_and_quote_old_late_posts_new_unspent(self):
        for path in ("/api/v3/exchangeInfo", "/api/v3/ticker/bookTicker"):
            for kind in ("test", "post"):
                for module in (old_v3, a):
                    with self.subTest(path=path, kind=kind, module=module.__name__), self.fixture(module, slow_path=path) as f:
                        clock, m, fake, c, j, runner, _ = f
                        if module is old_v3:
                            self.invoke(runner, m, kind)
                            self.assertEqual(len(fake.posts()), 1)
                            self.assertGreater(fake.posts()[0][2], m.expires_ms)
                            guarded = False
                        else:
                            with self.assertRaises(module.GuardError): self.invoke(runner, m, kind)
                            self.assertEqual(fake.posts(), [])
                            self.assertEqual(j.get()[kind + "_used"], 0)
                            guarded = True
                        self.record(module, path, kind, clock, m, fake, j, guarded)

    def test_slow_account_old_late_post_new_unspent(self):
        for module in (old_v3, a):
            with self.subTest(module=module.__name__), self.fixture(module, slow_path="/api/v3/account") as f:
                clock, m, fake, c, j, runner, _ = f
                if module is old_v3:
                    self.invoke(runner, m, "post")
                    self.assertEqual(len(fake.posts()), 1)
                    self.assertGreater(fake.posts()[0][2], m.expires_ms)
                    guarded = False
                else:
                    with self.assertRaises(module.GuardError): self.invoke(runner, m, "post")
                    self.assertEqual(fake.posts(), []); self.assertEqual(j.get()["post_used"], 0)
                    guarded = True
                self.record(module, "account", "post", clock, m, fake, j, guarded)

    def test_slow_baseline_commit_old_late_post_new_unspent(self):
        for module in (old_v3, a):
            with self.subTest(module=module.__name__), self.fixture(module) as f:
                clock, m, fake, c, j, runner, _ = f
                original = j.save_baseline
                def slow_save(values):
                    original(values); clock.value = m.expires_ms + 1
                with patch.object(j, "save_baseline", slow_save):
                    if module is old_v3:
                        self.invoke(runner, m, "post")
                        self.assertEqual(len(fake.posts()), 1)
                        self.assertGreater(fake.posts()[0][2], m.expires_ms)
                        guarded = False
                    else:
                        with self.assertRaises(module.GuardError): self.invoke(runner, m, "post")
                        self.assertEqual(fake.posts(), []); self.assertEqual(j.get()["post_used"], 0)
                        guarded = True
                self.record(module, "save_baseline", "post", clock, m, fake, j, guarded)

    def test_expiry_during_reserve_keeps_budget_and_sends_no_post(self):
        for kind in ("test", "post"):
            for module in (old_v3, a):
                with self.subTest(kind=kind, module=module.__name__), self.fixture(module) as f:
                    clock, m, fake, c, j, runner, _ = f
                    original = j.reserve
                    def slow_reserve(which):
                        used = original(which)
                        if used: clock.value = m.expires_ms + 1
                        return used
                    with patch.object(j, "reserve", slow_reserve):
                        if module is old_v3:
                            self.invoke(runner, m, kind)
                            self.assertEqual(len(fake.posts()), 1)
                            self.assertGreater(fake.posts()[0][2], m.expires_ms)
                            guarded = False
                        else:
                            with self.assertRaises(module.GuardError): self.invoke(runner, m, kind)
                            self.assertEqual(fake.posts(), [])
                            guarded = True
                    self.assertEqual(j.get()[kind + "_used"], 1)
                    self.record(module, "reserve", kind, clock, m, fake, j, guarded)

    def test_expiry_during_sign_keeps_budget_and_final_transport_fence(self):
        for kind in ("test", "post"):
            for module in (old_v3, a):
                with self.subTest(kind=kind, module=module.__name__), self.fixture(module) as f:
                    clock, m, fake, c, j, runner, _ = f
                    original = module.sign
                    def slow_sign(secret, payload):
                        result = original(secret, payload)
                        if (kind == "test" and "newClientOrderId=" in payload) or (kind == "post" and "newOrderRespType=" in payload):
                            clock.value = m.expires_ms + 1
                        return result
                    with patch.object(module, "sign", slow_sign):
                        if module is old_v3:
                            self.invoke(runner, m, kind)
                            self.assertEqual(len(fake.posts()), 1)
                            self.assertGreater(fake.posts()[0][2], m.expires_ms)
                            guarded = False
                        else:
                            with self.assertRaises(module.GuardError): self.invoke(runner, m, kind)
                            self.assertEqual(fake.posts(), [])
                            guarded = True
                    self.assertEqual(j.get()[kind + "_used"], 1)
                    self.record(module, "sign", kind, clock, m, fake, j, guarded)

    def test_expiry_after_guard_request_fenced_before_transport(self):
        for kind in ("test", "post"):
            for module in (old_v3, a):
                with self.subTest(kind=kind, module=module.__name__), self.fixture(module) as f:
                    clock, m, fake, c, j, runner, _ = f
                    original = module.guard_request
                    def slow_guard(scope, request):
                        original(scope, request)
                        if request.method == "POST": clock.value = m.expires_ms + 1
                    with patch.object(module, "guard_request", slow_guard):
                        if module is old_v3:
                            self.invoke(runner, m, kind)
                            self.assertEqual(len(fake.posts()), 1)
                            self.assertGreater(fake.posts()[0][2], m.expires_ms)
                            guarded = False
                        else:
                            with self.assertRaises(module.GuardError): self.invoke(runner, m, kind)
                            self.assertEqual(fake.posts(), [])
                            guarded = True
                    self.assertEqual(j.get()[kind + "_used"], 1)
                    self.record(module, "guard_request", kind, clock, m, fake, j, guarded)

    def test_deadline_equality_allowed_and_already_expired_stops(self):
        for delta in (-1, 0, 1):
            for kind in ("test", "post"):
                for module in (old_v3, a):
                    with self.subTest(delta=delta, kind=kind, module=module.__name__), self.fixture(module) as f:
                        clock, m, fake, c, j, runner, _ = f
                        clock.value = m.expires_ms + delta
                        if delta > 0:
                            with self.assertRaises(module.GuardError): self.invoke(runner, m, kind)
                            self.assertEqual(fake.calls, [])
                            guarded = True
                        else:
                            self.invoke(runner, m, kind)
                            self.assertEqual(len(fake.posts()), 1)
                            self.assertLessEqual(fake.posts()[0][2], m.expires_ms)
                            guarded = False
                        self.record(module, "boundary" + str(delta), kind, clock, m, fake, j, guarded)

    def test_post_reserved_then_expired_restart_queries_never_resend(self):
        with self.fixture(a) as f:
            clock, m, fake, c, j, runner, path = f
            original = j.reserve
            def slow_reserve(which):
                used = original(which)
                if used: clock.value = m.expires_ms + 1
                return used
            with patch.object(j, "reserve", slow_reserve), self.assertRaises(a.GuardError):
                self.invoke(runner, m, "post")
            self.assertEqual(j.get()["post_used"], 1); self.assertEqual(fake.posts(), [])
            # Independent connection models process restart; first connection is
            # idle and receives no more state operations until fixture cleanup.
            restarted = a.Journal(path)
            try:
                fake.query_not_found = True
                r = a.FiniteRunner(c, restarted, m)
                for _ in range(2):
                    result = r.recover(execute=True, approved_hash=m.sha256)
                    self.assertEqual(result["new_order_post_reservations"], 1)
                    self.assertEqual(result["state"], "SUBMIT_UNKNOWN")
                    self.assertEqual(result["last_error"]["code"], -2013)
                self.assertEqual(fake.posts(), [])
                self.assertEqual(sum(method == "GET" and route == "/api/v3/order" for method, route, _ in fake.calls), 2)
                self.record(a, "restart-minus2013-query-only", "post", clock, m, fake, restarted, True)
            finally:
                restarted.close()

    def test_test_reserved_expired_cannot_recover_as_new_order(self):
        with self.fixture(a) as f:
            clock, m, fake, c, j, runner, _ = f
            original = j.reserve
            def slow_reserve(which):
                used = original(which)
                if used: clock.value = m.expires_ms + 1
                return used
            with patch.object(j, "reserve", slow_reserve), self.assertRaises(a.GuardError):
                self.invoke(runner, m, "test")
            self.assertEqual(j.get()["test_used"], 1)
            self.assertEqual(j.get()["post_used"], 0)
            with self.assertRaises(a.GuardError): runner.recover(execute=True, approved_hash=m.sha256)
            with self.assertRaises(a.GuardError): self.invoke(runner, m, "post")
            self.assertEqual(fake.posts(), [])
            self.record(a, "expired-test-no-new-order-recovery", "test", clock, m, fake, j, True)

    def test_lowlevel_deadline_cannot_expand_domains_or_operations(self):
        with self.fixture(a) as f:
            clock, m, fake, c, j, runner, _ = f
            for method, path, expiry in (("GET", "/api/v3/time", m.expires_ms),
                                        ("DELETE", "/api/v3/order", m.expires_ms),
                                        ("POST", "/api/v3/order", True),
                                        ("POST", "/api/v3/order", 0)):
                with self.subTest(method=method, path=path, expiry=expiry), self.assertRaises(a.GuardError):
                    c._request(method, path, {}, deadline_ms=expiry)
            self.assertEqual(fake.calls, [])




if __name__ == "__main__":
    unittest.main(verbosity=2)
