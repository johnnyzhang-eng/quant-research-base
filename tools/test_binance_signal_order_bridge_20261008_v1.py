"""Producer offline fault controls; this is not the independent peer audit."""
import copy
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Inexact, ROUND_DOWN, ROUND_UP, localcontext
from fractions import Fraction as F
from pathlib import Path
from unittest.mock import patch

import binance_signal_order_bridge_20261008_v1 as b

NOW = 9_000_000
ASSERTIONS = 0


def fixture():
    packet = {"mode": b.MODE, "protocol_sha256": b.PROTOCOL_SHA, "source_intent_id": "own-fake-A",
        "source_row_sha256": "f" * 64, "cutoff_ms": NOW - 2000, "eligible_ms": NOW - 1000,
        "schedule_rebalance": True, "planned_weights": {"BTC": "0.4", "ETH": "0.3"},
        "source_sell_assets": [], "source_buy_assets": ["BTCUSDT"], "historical_PIT_verified": False,
        "actual_known_at": None}
    portfolio = {"mode": b.MODE, "portfolio_id": "explicit-artificial-50", "asof_ms": NOW,
        "fixture_equity_USDT": "50", "available_cash_USDT": "50",
        "positions": {"BTC": "0", "ETH": "0"}, "pending_orders": []}
    quote = {"mode": b.MODE, "symbol": "BTCUSDT", "reference_price": "100.17", "bidPrice": "100",
        "askPrice": "101.2", "asof_ms": NOW, "provenance": "ARTIFICIAL_FAKE"}
    exchange = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "isSpotTradingAllowed": True,
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1", "stepSize": "0.001"},
            {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "25"}]}]}
    policy = {"mode": b.MODE, "requested_pair": "BTCUSDT", "gross_quote_cap_USDT": "25",
        "capital_is_artificial": True, "fee_rate": "0.004", "slippage_rate": "0.003",
        "combined_friction_rate": "0.007", "ttl_ms": 1000}
    risk = {"halted": False, "account_state_known": True}
    return packet, portfolio, quote, exchange, policy, risk


class Controls(unittest.TestCase):
    def assertEqual(self, *args, **kwargs):
        global ASSERTIONS; ASSERTIONS += 1
        return super().assertEqual(*args, **kwargs)

    def assertTrue(self, *args, **kwargs):
        global ASSERTIONS; ASSERTIONS += 1
        return super().assertTrue(*args, **kwargs)

    def assertFalse(self, *args, **kwargs):
        global ASSERTIONS; ASSERTIONS += 1
        return super().assertFalse(*args, **kwargs)

    def assertRaises(self, *args, **kwargs):
        global ASSERTIONS; ASSERTIONS += 1
        return super().assertRaises(*args, **kwargs)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-producer-fake-")
        self.directory = Path(self.temp.name) / "bridge-fake-state"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "fake-signal-registry.sqlite3"
        self.registry = b.FakeSignalRegistry(self.path)
        self.inputs = fixture()
        self.block_network = patch.object(b.adapter, "HTTPS", side_effect=AssertionError("HTTP construction forbidden"))
        self.block_network.start()

    def tearDown(self):
        self.block_network.stop()
        self.registry.close()
        self.temp.cleanup()

    def compile(self, inputs=None, now=NOW):
        return b.compile_position_deltas(*(inputs or self.inputs), self.registry, now_ms=now)

    def plan(self):
        result = self.compile()
        self.assertEqual(result["status"], "READY_FAKE_BTC_PROJECTION")
        return result["plan"]

    def test_fraction_cash_fee_grid_instrument_and_inputs_unchanged(self):
        before = copy.deepcopy(self.inputs)
        plan = self.plan()
        self.assertEqual(before, self.inputs)
        self.assertEqual(F(20) * F(401, 400), F("20.05"))
        price = F(10017, 100) * F(1003, 1000)
        price = (price // F(1, 100)) * F(1, 100)
        quantity = ((F(20) / price) // F(1, 1000)) * F(1, 1000)
        self.assertEqual(price, F("100.47")); self.assertEqual(quantity, F("0.199"))
        self.assertEqual(F(plan["order_manifest"]["price"]), price)
        self.assertEqual(F(plan["quantity"]), quantity)
        self.assertEqual(F(plan["maximum_cash_debit_USDT"]), quantity * price * F(251, 250))
        self.assertFalse(plan["live_order_permission"])

    def test_halt_hold_sell_ETH_rotation_no_permission_or_row(self):
        cases = [(0, "schedule_rebalance", False, "HOLD"),
            (5, "halted", True, "BLOCK_RISK_OR_UNKNOWN_STATE"),
            (5, "account_state_known", False, "BLOCK_RISK_OR_UNKNOWN_STATE"),
            (4, "requested_pair", "ETHUSDT", "BLOCK_UNSUPPORTED_ASSET")]
        for index, key, value, expected in cases:
            x = list(fixture()); x[index][key] = value
            result = self.compile(tuple(x)); self.assertEqual(result["status"], expected)
            self.assertFalse(result["new_order_permission"])
        for buys, expected in [([], "BLOCK_SELL_UNIMPLEMENTED"),
                                (["ETHUSDT"], "BLOCK_SELL_BEFORE_BUY_UNIMPLEMENTED")]:
            x = list(fixture()); x[0].update(source_sell_assets=["BTCUSDT"], source_buy_assets=buys)
            self.assertEqual(self.compile(tuple(x))["status"], expected)
        self.assertEqual(self.registry.snapshot(), {})

    def test_delay_span_boundary_and_future_stale_controls(self):
        x = list(fixture()); x[0].update(cutoff_ms=NOW - 61_000, eligible_ms=NOW - 1000)
        allowed = self.compile(tuple(x)); self.assertEqual(allowed["status"], "READY_FAKE_BTC_PROJECTION")
        x[0]["cutoff_ms"] -= 1
        self.assertEqual(self.compile(tuple(x))["status"], "BLOCK_ELIGIBILITY_DELAY")
        x[0].update(cutoff_ms=NOW + 1, eligible_ms=NOW + 2)
        self.assertEqual(self.compile(tuple(x))["status"], "BLOCK_FUTURE_CUTOFF")
        x[0].update(cutoff_ms=NOW - 1000, eligible_ms=NOW + 1)
        self.assertEqual(self.compile(tuple(x))["status"], "BLOCK_NOT_ELIGIBLE")
        x[0].update(cutoff_ms=NOW - 60_002, eligible_ms=NOW - 60_001)
        self.assertEqual(self.compile(tuple(x))["status"], "BLOCK_STALE_SIGNAL")
        x[0].update(cutoff_ms=NOW - 60_001, eligible_ms=NOW - 60_000)
        self.assertEqual(self.compile(tuple(x))["status"], "READY_FAKE_BTC_PROJECTION")
        x[0].update(cutoff_ms=NOW - 1000, eligible_ms=NOW - 1000)
        self.assertEqual(self.compile(tuple(x))["status"], "BLOCK_NOT_ELIGIBLE")

    def test_quote_cash_time_retry_reuses_without_permission_or_expiry_reset(self):
        plan = self.plan(); x = list(fixture())
        x[1]["available_cash_USDT"] = "0"; x[2]["reference_price"] = "200"
        repeat = self.compile(tuple(x), now=NOW + 120_000)
        self.assertEqual(repeat["status"], "REUSE_NO_NEW_ORDER")
        self.assertFalse(repeat["new_order_permission"])
        self.assertEqual(plan, repeat["plan"])
        self.assertEqual(plan["order_manifest"]["expires_ms"], NOW + 1000)

    def test_observation_clock_pending_false_receipt_and_invalid_shape(self):
        for index, key, value, expected in [
                (1, "asof_ms", NOW - 5001, "BLOCK_STALE_OR_FUTURE_OBSERVATION"),
                (2, "asof_ms", NOW + 1, "BLOCK_STALE_OR_FUTURE_OBSERVATION"),
                (1, "pending_orders", ["own-unknown"], "BLOCK_PENDING_ORDERS"),
                (0, "historical_PIT_verified", True, "BLOCK_FALSE_HISTORICAL_RECEIPT"),
                (0, "actual_known_at", NOW - 2000, "BLOCK_FALSE_HISTORICAL_RECEIPT")]:
            x = list(fixture()); x[index][key] = value
            result = self.compile(tuple(x)); self.assertEqual(result["status"], expected)
            self.assertFalse(result["new_order_permission"])
        x = list(fixture()); x[1] = None
        self.assertEqual(self.compile(tuple(x))["status"], "BLOCK_INVALID_INPUT_OR_MARKET_FILTER")
        self.assertEqual(self.registry.snapshot(), {})
        x = list(fixture()); x[1]["asof_ms"] = NOW - 5000; x[2]["asof_ms"] = NOW - 5000
        self.assertEqual(self.compile(tuple(x))["status"], "READY_FAKE_BTC_PROJECTION")

    def test_same_cutoff_renaming_target_and_portfolio_conflicts(self):
        self.plan()
        for key, value in [("source_intent_id", "renamed-same-cutoff"),
                           ("planned_weights", {"BTC": "0.3", "ETH": "0.3"}),
                           ("source_row_sha256", "e" * 64)]:
            x = list(fixture()); x[0][key] = value
            self.assertEqual(self.compile(tuple(x))["status"], "BLOCK_CONFLICTING_SIGNAL_IDENTITY")
        x = list(fixture()); x[1]["portfolio_id"] = "renamed-portfolio"
        self.assertEqual(self.compile(tuple(x))["status"], "BLOCK_REGISTRY_PORTFOLIO_SCOPE")
        x = list(fixture()); x[0]["protocol_sha256"] = "e" * 64
        self.assertEqual(self.compile(tuple(x))["status"], "BLOCK_IDENTITY")
        self.assertEqual(len(self.registry.snapshot()), 1)

    def test_true_fresh_process_restart_and_conflict(self):
        first = self.plan(); self.registry.close()
        code = """import json,sys
import binance_signal_order_bridge_20261008_v1 as b
inputs=json.loads(sys.argv[2]); r=b.FakeSignalRegistry(sys.argv[1])
try: print(json.dumps(b.compile_position_deltas(*inputs,r,now_ms=9000000),sort_keys=True))
finally:r.close()
"""
        for conflict in (False, True):
            x = list(fixture())
            if conflict: x[0]["source_intent_id"] = "renamed-after-process-exit"
            run = subprocess.run([sys.executable, "-B", "-c", code, str(self.path), json.dumps(x)],
                cwd=Path(b.__file__).parent, check=True, capture_output=True, text=True)
            result = json.loads(run.stdout)
            self.assertFalse(result["new_order_permission"])
            self.assertEqual(result["status"], "BLOCK_CONFLICTING_SIGNAL_IDENTITY" if conflict else "REUSE_NO_NEW_ORDER")
            if not conflict: self.assertEqual(result["plan"], first)
        self.registry = b.FakeSignalRegistry(self.path)

    def test_serialized_concurrent_compiles_only_one_new_plan(self):
        self.registry.close()
        def run(_):
            r = b.FakeSignalRegistry(self.path)
            try: return b.compile_position_deltas(*fixture(), r, now_ms=NOW)["status"]
            finally: r.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(run, range(2)))
        self.assertEqual(statuses.count("READY_FAKE_BTC_PROJECTION"), 1)
        self.assertEqual(statuses.count("REUSE_NO_NEW_ORDER"), 1)
        self.registry = b.FakeSignalRegistry(self.path)

    def test_fee_cash_notional_and_hostile_Decimal_do_not_bypass(self):
        x = list(fixture()); x[1]["available_cash_USDT"] = "19.99353"
        self.assertEqual(self.compile(tuple(x))["status"], "BLOCK_FEE_INCLUSIVE_CASH")
        x[1]["available_cash_USDT"] = "20.07350412"
        with localcontext() as ctx:
            ctx.prec = 2; ctx.rounding = ROUND_DOWN; ctx.traps[Inexact] = True
            plan = self.compile(tuple(x))["plan"]
        self.assertEqual(F(plan["maximum_cash_debit_USDT"]), F("20.07350412"))
        for rounding in (ROUND_UP, ROUND_DOWN):
            with localcontext() as ctx:
                ctx.prec = 2; ctx.rounding = rounding; ctx.traps[Inexact] = True
                retry = self.compile(tuple(x))
            self.assertEqual(retry["plan"], plan); self.assertFalse(retry["new_order_permission"])

    def test_registry_missing_path_permissions_marker_and_plan_corruption(self):
        result = b.compile_position_deltas(*fixture(), None, now_ms=NOW)
        self.assertEqual(result["status"], "BLOCK_DURABLE_FAKE_REGISTRY_REQUIRED")
        with self.assertRaises(b.BridgeError): b.FakeSignalRegistry(self.directory / "actual-intent.sqlite3")
        self.plan()
        self.registry.db.execute("UPDATE signals SET plan_json='{}'")
        with self.assertRaises(b.BridgeError): self.compile()
        self.registry.close(); os.chmod(self.path, 0o644)
        with self.assertRaises(b.BridgeError): b.FakeSignalRegistry(self.path)
        os.chmod(self.path, 0o600); self.registry = b.FakeSignalRegistry(self.path)

    def test_existing_trigger_rejected_before_registry_mutation(self):
        self.registry.db.execute("CREATE TRIGGER hostile AFTER INSERT ON signals BEGIN DELETE FROM signals; END")
        self.registry.close()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(b.BridgeError): b.FakeSignalRegistry(self.path)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        # Cleanup own intentional corruption for tearDown; no original files.
        c = sqlite3.connect(self.path); c.execute("DROP TRIGGER hostile"); c.close()
        self.registry = b.FakeSignalRegistry(self.path)

    def test_actual_v4_deadline_argument_business_parameters_and_recovery(self):
        plan = self.plan(); scenario = b.FakeScenario(plan); captured = []
        original = b.adapter.GuardedClient._request
        def spy(client, method, path, params, **kwargs):
            if method == "POST": captured.append((path, kwargs.get("deadline_ms")))
            return original(client, method, path, params, **kwargs)
        with patch.object(b.adapter.GuardedClient, "_request", spy):
            first = b.execute_fake(plan, self.registry, scenario)
        self.assertEqual(captured, [("/api/v3/order", NOW + 1000)])
        self.assertEqual(first["post_dispatch_ms"], [NOW])
        self.assertTrue(first["business_parameters_bound"])
        self.assertTrue(first["result"]["reconciliation"]["passed"])
        self.registry.close(); self.registry = b.FakeSignalRegistry(self.path)
        recovered = b.execute_fake(plan, self.registry, b.FakeScenario(plan, order_exists=True), recover=True)
        self.assertEqual(recovered["route_counts"].get("POST /api/v3/order", 0), 0)
        self.assertEqual(recovered["route_counts"]["GET /api/v3/order"], 1)
        self.assertTrue(recovered["result"]["reconciliation"]["passed"])
        with self.assertRaises(b.BridgeError): b.execute_fake(plan, self.registry, b.FakeScenario(plan))

    def test_topup_fake_baseline_matches_compiled_portfolio_and_Fraction(self):
        self.inputs[1]["positions"]["BTC"] = "0.037"
        self.inputs[1]["available_cash_USDT"] = "30"
        plan = self.plan(); scenario = b.FakeScenario(plan)
        self.assertEqual(F(plan["quantity"]), F("0.162"))
        self.assertEqual(F(scenario.baseline["balances"][0]["free"]), F("0.037"))
        result = b.execute_fake(plan, self.registry, scenario)
        self.assertTrue(result["result"]["reconciliation"]["passed"])
        self.assertEqual(F(scenario.current["balances"][0]["free"]), F("0.199"))
        self.assertEqual(F(scenario.current["balances"][1]["free"]), 30 - F("0.162") * F("100.47") * F(251, 250))

    def test_signing_cross_expiry_spent_reservation_restart_query_only(self):
        plan = self.plan(); scenario = b.FakeScenario(plan); original = b.adapter.sign
        def late(secret, unsigned):
            result = original(secret, unsigned)
            if "newOrderRespType=" in unsigned:
                scenario.now = NOW + 1001
            return result
        with patch.object(b.adapter, "sign", late):
            with self.assertRaises(b.adapter.GuardError): b.execute_fake(plan, self.registry, scenario)
        self.assertEqual(scenario.calls.get("POST /api/v3/order", 0), 0)
        self.registry.close(); self.registry = b.FakeSignalRegistry(self.path)
        retry = self.compile(); self.assertFalse(retry["new_order_permission"])
        with self.assertRaises(b.BridgeError): b.execute_fake(plan, self.registry, b.FakeScenario(plan))
        for _ in range(2):
            recovered = b.execute_fake(plan, self.registry, b.FakeScenario(plan), recover=True)
            self.assertEqual(recovered["route_counts"].get("POST /api/v3/order", 0), 0)
            self.assertEqual(recovered["route_counts"]["GET /api/v3/order"], 1)

    def test_guard_cross_expiry_last_transport_fence(self):
        plan = self.plan(); scenario = b.FakeScenario(plan); original = b.adapter.guard_request
        def late(scope, request):
            original(scope, request)
            if request.method == "POST": scenario.now = NOW + 1001
        with patch.object(b.adapter, "guard_request", late):
            with self.assertRaises(b.adapter.GuardError): b.execute_fake(plan, self.registry, scenario)
        self.assertEqual(scenario.calls.get("POST /api/v3/order", 0), 0)
        recovered = b.execute_fake(plan, self.registry, b.FakeScenario(plan), recover=True)
        self.assertEqual(recovered["route_counts"].get("POST /api/v3/order", 0), 0)

    def test_slow_preflight_has_no_order_reservation_or_recoverable_POST(self):
        plan = self.plan(); scenario = b.FakeScenario(plan, expire_on="quote")
        with self.assertRaises(b.adapter.GuardError): b.execute_fake(plan, self.registry, scenario)
        self.assertEqual(scenario.calls.get("POST /api/v3/order", 0), 0)
        with self.assertRaises(b.adapter.GuardError): b.execute_fake(plan, self.registry, b.FakeScenario(plan), recover=True)
        with self.assertRaises(b.BridgeError): b.execute_fake(plan, self.registry, b.FakeScenario(plan))

    def test_dispatch_stamp_before_missing_order_state_does_not_reissue(self):
        plan = self.plan(); self.assertTrue(self.registry.reserve_fake_dispatch(plan))
        self.registry.close(); self.registry = b.FakeSignalRegistry(self.path)
        self.assertEqual(self.compile()["status"], "REUSE_NO_NEW_ORDER")
        with self.assertRaises(b.BridgeError): b.execute_fake(plan, self.registry, b.FakeScenario(plan))
        with self.assertRaises(b.BridgeError): b.execute_fake(plan, self.registry, b.FakeScenario(plan), recover=True)
        self.assertEqual(len(list(self.directory.glob("fake-order-*"))), 0)

    def test_dispatch_exact_expiry_equality_allowed(self):
        plan = self.plan(); scenario = b.FakeScenario(plan); scenario.now = NOW + 1000
        result = b.execute_fake(plan, self.registry, scenario)
        self.assertEqual(result["post_dispatch_ms"], [plan["order_manifest"]["expires_ms"]])
        self.assertTrue(result["result"]["reconciliation"]["passed"])


def produce_report():
    root = Path(__file__).resolve().parents[1]
    original = {
        "work/binance_signal_order_bridge_20261008_v0.py": "cf7668fca7668e8004e8d6f312677b66143432ac7893dc44dd608d7c271cae93",
        "work/test_binance_signal_order_bridge_20261008_v0.py": "7ba1e3f7923d9e96ee2c67141ad4b595bcb1fd6dcafa6ef9a254b9f1d623ec02",
        "outputs/binance-signal-order-bridge-20261008-v0.json": "a046132e5e32e5dda160a68f1d33aec3d59a2cb31e73c49a7b52a95af98cbcb2",
        "work/binance_spot_testnet_adapter_20261008_v4.py": b.ADAPTER_SHA,
    }
    sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if any(sha(root / path) != expected for path, expected in original.items()):
        raise SystemExit("frozen prerequisite instrument mismatch")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Controls)
    captured = io.StringIO(); result = unittest.TextTestRunner(stream=captured, verbosity=2).run(suite)
    unchanged = all(sha(root / path) == expected for path, expected in original.items())
    report = {"schema": "binance-signal-order-bridge-report/1", "created_utc": datetime.now(timezone.utc).isoformat(),
        "mode": b.MODE, "status": "OFFLINE_FAKE_V4_DURABLE_BRIDGE_CANDIDATE" if result.wasSuccessful() else "PRODUCER_CONTROL_FAILURE",
        "draft_instrument_repair": {"initial_draft_report_sha256": "dd3b2c1a6e6929a4007a9c6b871b67ae8085f7e3033e35f86ac98911ad8081da",
            "initial_methods": 17, "initial_harness_errors": 1,
            "mechanism": "Sign hook advanced clock on account preflight instead of order signing; test wrongly expected a consumed POST reservation. Hook now targets outbound order business field only.",
            "draft_announced_as_frozen": False, "initial_draft_bytes_retained": False,
            "candidate_adapter_changed_to_fix_harness": False},
        "source_sha256": sha(b.__file__), "test_sha256": sha(__file__), "adapter_sha256": b.ADAPTER_SHA,
        "frozen_prerequisites_unchanged": unchanged,
        "controls": {"unittest_methods": result.testsRun, "assertion_calls": ASSERTIONS,
            "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
            "independent_audit": False, "failed_method_ids": [case.id() for case, _ in result.failures + result.errors]},
        "contract": {"max_cutoff_to_eligible_ms": 60_000, "assumption": "Explicit small engineering span, not a venue or daily strategy convention.",
            "eligible_age_max_ms": 60_000, "observation_age_max_ms": 5000,
            "ttl_max_ms": 60_000, "expiry_equality_allowed": True,
            "gross_notional_cap_USDT": "25", "fees_outside_gross_cap": True,
            "portfolio_capital_is_artificial": True, "BTC_BUY_projection_only": True,
            "registry_business_key": "fixed protocol + bound portfolio + BTCUSDT + cutoff; source intent renaming conflicts"},
        "observed_controls": ["Own Fraction price/grid/debit known answer and hostile Decimal context.",
            "Fresh child processes reopen same registry, reuse exact plan/ID and reject same-cutoff renaming.",
            "Concurrent separate connections grant one new plan for initialized retained registry.",
            "Quote/cash/time retries return frozen plan without new permission; target/source/protocol/portfolio conflicts reject.",
            "Cutoff-to-eligible 60,000ms equality accepted, 60,001ms rejected; future/stale/risk/unsupported actions rejected.",
            "Actual v4 request argument capture binds deadline_ms to compiled manifest; fake outbound business fields checked.",
            "Signature and guard processing crossing deadline send no POST, preserve consumed reservations; two recovery queries never reissue.",
            "Missing journal after fake dispatch stamp conservatively consumes permission; recovery does not create a new intent.",
            "Fresh reopened fake execution state reconciles fills/fees; topup baseline matches compiled artificial portfolio."],
        "remaining_boundaries": ["This producer report requires independent peer audit of its exact source/report.",
            "Per-retained local registry integrity only; deletion/copy/new registry is not account-wide deduplication.",
            "Compile registry and adapter journal are separate commits; crash after stamp but before journal loses send eligibility conservatively.",
            "No prospective source authentication/PIT, market data, actual account state or eligibility admission.",
            "No SELL/ETH execution, continuous strategy scheduler, live/profit acceptance or deposit integration.",
            "Deadline is last local transport dispatch sample, not exchange arrival or fill guarantee.",
            "Root's exhausted canonical probe budgets are not accessed or replenished; all states here are newly created temporary fakes."],
        "actions": {"real_keys_read": 0, "real_network_calls": 0, "testnet_HTTP_calls": 0,
            "canonical_probe_journal_access": 0, "new_forward_signals": 0, "paid_calls": 0, "github_writes": 0},
        "public_scope": "Synthetic control code/report only; original historical weights and private paths removed from this new version."}
    serialized = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if re.search(r"/(?:Users|home)/[^/\s\"]+|(?<!\w)\.env(?:\b|\.)", serialized):
        raise SystemExit("public report private-path/env scan rejected")
    path = root / "outputs/binance-signal-order-bridge-20261008-v1.json"
    with path.open("x", encoding="utf-8") as output: output.write(serialized)
    print(captured.getvalue())
    print(path.name, sha(path), "source", report["source_sha256"], "tests", report["test_sha256"])
    return 0 if result.wasSuccessful() and unchanged else 1


if __name__ == "__main__":
    raise SystemExit(produce_report())
