"""Portable independent fake BTC dispatch audit, using three exact source files.

The ten controls retain the frozen local auditor semantics. Runtime inputs are
only this file and its same-directory bridge v2 / adapter v4, with hard source
pins. No producer test helpers, legacy reports, credentials or network are used.
Default output is stdout; explicit report paths are exclusively created.
Original eight-file preservation remains a separate local provenance result.
"""
from __future__ import annotations

import hashlib
import json
import copy
import contextlib
import importlib
import os
import subprocess
import sys
import tempfile
from collections import Counter
from fractions import Fraction
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = Path(__file__).resolve().parent
METHOD_LIST = [('independent_instruments_good_wallet_and_actual_boundary_origins', 'bound_scope_and_good_wallet'), ('strict_initial_latest_risk_and_identity_mutations', 'initial_risk_mutations'), ('current_and_projected_fee_inclusive_floor_Fraction_edges', 'current_and_projected_floor'), ('risk_wallet_quote_clock_boundaries', 'observation_clocks'), ('latest_quote_maker_drift_and_total_position_exposure', 'latest_quote_and_total_exposure'), ('late_sign_guard_risk_changes_local_GuardError', 'late_sign_guard_rechecks'), ('actual_wallet_evidence_refusals', 'wallet_evidence_refusals'), ('spent_state_restart_and_fresh_child_GET_only', 'spent_restart_and_fresh_child'), ('initial_stop_journal_gap_and_expiry_boundaries', 'initial_gap_recovery_and_expiry'), ('legal_latest_changes_actual_v4_sign_guard_risk_POST_pipeline', 'legal_latest_changes_and_pipeline_trace')]
AST_COMPARISON = {'base_source_sha256': 'da4c8d6b8f73fee6530136d8dfdc2e6ec0e972112caa33d1a0d41e7e81c6d12b', 'control_methods': 10, 'exact_AST_unchanged_methods': ['initial_risk_mutations', 'current_and_projected_floor', 'observation_clocks', 'latest_quote_and_total_exposure', 'late_sign_guard_rechecks', 'wallet_evidence_refusals', 'initial_gap_recovery_and_expiry', 'legal_latest_changes_and_pipeline_trace'], 'source_import_child_plumbing_only_methods': ['bound_scope_and_good_wallet', 'spent_restart_and_fresh_child'], 'all_ten_normalized_AST_equal': True, 'normalized_control_AST_sha256': {'bound_scope_and_good_wallet': '497983ed554a816dcfdcfccea71c53b332d7cade83a035150d7efb1415fcc245', 'initial_risk_mutations': '6c6a07f29b8b1698ad15a4661010e03ff812f7dc283a2f6048094e32e7742bec', 'current_and_projected_floor': '9a2c1e3b91e863dce6c1ab7a06b5b417b0e15dcad15b38ecc7ed64e459bc5182', 'observation_clocks': 'acfda5b49e16341e38cbe13a8bd21df0887400b8feb2c999c90c6a52e994dffa', 'latest_quote_and_total_exposure': 'b464d034e792d76ac255c34b27c3816904c72aece06c7f2beb08c32be6911d3b', 'late_sign_guard_rechecks': '44dfa16a9f3e3e0f82e6f58506b513a080186c35fa1c89d95af1ddb20b396ff1', 'wallet_evidence_refusals': '5dab61e87cf05e85997c86fbd8000269593e605deb4b188fa59f45bc78d5db20', 'spent_restart_and_fresh_child': '2d6dce4cc57c0ffee30a94b6ef8d3faf2b329ff64ec14aab2dd057e4594e6473', 'initial_gap_recovery_and_expiry': 'b8e0cbbc30eb09d65a9ba38f4d812b095f4477e6f9ca02ea51063783ef5da3c3', 'legal_latest_changes_and_pipeline_trace': 'd21628e73031f81592ad80a7d1993099e02f9c3299d393bdb805415eceb5b342'}, 'core_v2_v4_pin_values_unchanged': True, 'new_main_only_same_directory_source_and_explicit_output': True}
PINNED_EXISTING = {'work/binance_spot_testnet_adapter_20261008_v4.py': '64189cdeaaaccf52ac5e8446e2233c1c63a5d8a5d6da025577df923d3d734891'}
EXPECTED_V2 = {'work/binance_signal_order_bridge_20261008_v2.py': 'd6878ee61787c973edb851f2a061b0ddbecea1c79cd023230e8ae2496133952a'}
NOW = 1_700_000_000_000
MODE = "FAKE_TRANSPORT_HISTORY_CONTROL"
PROTOCOL = "7d95ab4548665e96006775372307cf818cb12737c1a91a710a445afd9fbe9040"
ASSERTIONS = 0
RESULTS = []
MATRIX = {
    "schema": "binance-signal-dispatch-independent-attack-matrix/2",
    "eligibility": "Only fake BTC BUY, frozen plan and retained owned registry; no real CNY-to-USDT inference.",
    "new_observation": ["halt after compile", "floor crossed after compile", "risk state unknown",
        "account identity changed", "portfolio identity changed", "pair changed", "observation stale/future",
        "cash below fee-inclusive debit", "quantity/current holding changes", "pending state unknown",
        "quote changed/off-grid/nonfinite", "quote/capital/risk currency or units mismatch", "exposure cap exceeded"],
    "root_required_specific_edges": ["equity at floor30 blocks; one finite decimal unit above allows if all other facts fit",
        "projected_equity after frozen fee reserve and embedded limit slippage must strictly exceed30",
        "strict known/halted booleans and missing/null/string/int third states reject",
        "total BTC mark exposure rather than only new order notional",
        "maker price must stay below latest ask; drift equality and exceeded boundary",
        "positive nonBTC/unknown-asset state cannot be silently dropped",
        "USDT fee denomination is an artificial model assumption, not broker commission proof"],
    "time_of_change": ["before dispatch", "during order-signing", "after order guard", "immediately before closed fake POST"],
    "late_block": "After adapter reservation, fake POST count remains zero and retained budget remains one.",
    "error_category": "Local latest-risk refusal inside v4 transport must raise GuardError, not an ambiguous APIError or fake completed reconciliation.",
    "recovery": "Real close/reopen and fresh Python child only query the same spent identity, even under latest STOP/unknown state; zero reissue.",
    "instrument": "Self-made Fraction/unit known answers plus a deliberately wrong-unit refusal; no producer helper as outcome oracle.",
    "boundary_proof": "Actual v4 class/function file and SHA, actual exact POST business parameters and hooks after sign/guard are observed.",
    "count_scope": "Methods/control bundles and executed assertions; neither a market sample count nor independent strategy experiments.",
    "remaining_limits": ["Fixed synthetic observation source does not authenticate real account/risk evidence.",
        "No real CNY FX conversion or original capital-loss STOP validation.",
        "SELL, ETH, rotation, continuous strategy, broker eligibility and strategy/returns certification remain absent.",
        "Local fake transport boundary is not exchange arrival or fill protection."],
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    global ASSERTIONS
    ASSERTIONS += 1
    if not condition:
        raise AssertionError(message)


def decimal_text(value):
    value = Fraction(value)
    whole, remainder = divmod(abs(value.numerator), value.denominator)
    digits = []
    while remainder:
        digit, remainder = divmod(remainder * 10, value.denominator)
        digits.append(str(digit))
        if len(digits) > 100:
            raise AssertionError("nonfinite independent decimal")
    return ("-" if value < 0 else "") + str(whole) + ("." + "".join(digits) if digits else "")


def inputs(*, cash="50", held="0", weight="0.3"):
    return ({"mode": MODE, "protocol_sha256": PROTOCOL, "source_intent_id": "independent-v2-risk-intent",
        "source_row_sha256": "f" * 64, "cutoff_ms": NOW - 40000, "eligible_ms": NOW - 10000,
        "schedule_rebalance": True, "planned_weights": {"BTC": weight, "ETH": "0"},
        "source_sell_assets": [], "source_buy_assets": ["BTCUSDT"], "historical_PIT_verified": False,
        "actual_known_at": None},
      {"mode": MODE, "portfolio_id": "independent-v2-artificial-portfolio", "asof_ms": NOW,
        "fixture_equity_USDT": "50", "available_cash_USDT": cash, "positions": {"BTC": held, "ETH": "0"},
        "pending_orders": []},
      {"mode": MODE, "symbol": "BTCUSDT", "reference_price": "97.31", "bidPrice": "97", "askPrice": "99",
        "asof_ms": NOW, "provenance": "ARTIFICIAL_FAKE"},
      {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "isSpotTradingAllowed": True,
        "filters": [{"filterType": "PRICE_FILTER", "minPrice": "0.03", "maxPrice": "10000", "tickSize": "0.03"},
          {"filterType": "LOT_SIZE", "minQty": "0.007", "maxQty": "1", "stepSize": "0.007"},
          {"filterType": "NOTIONAL", "minNotional": "1", "maxNotional": "25"}]}]},
      {"mode": MODE, "requested_pair": "BTCUSDT", "gross_quote_cap_USDT": "25", "capital_is_artificial": True,
        "fee_rate": "0.0012", "slippage_rate": "0.0018", "combined_friction_rate": "0.003", "ttl_ms": 60000},
      {"halted": False, "account_state_known": True})


@contextlib.contextmanager
def fake_registry():
    with tempfile.TemporaryDirectory(prefix="bridge-fake-dispatch-independent-") as temp:
        directory = Path(temp)
        directory.chmod(0o700)
        registry = candidate.FakeSignalRegistry(directory / "fake-signal-registry.sqlite3")
        try:
            yield registry, directory
        finally:
            registry.close()


def compile_plan(registry, supplied=None):
    supplied = inputs() if supplied is None else supplied
    before = copy.deepcopy(supplied)
    result = candidate.compile_position_deltas(*supplied, registry, now_ms=NOW)
    require(supplied == before, "compiler mutated independent input")
    require(result["status"] == "READY_FAKE_BTC_PROJECTION", "independent plan fixture not admitted by compiler")
    require(result["live_order_permission"] is False, "fake plan granted live permission")
    return result["plan"]


def observe(plan, supplied=None):
    supplied = inputs() if supplied is None else supplied
    quote = copy.deepcopy(supplied[2])
    quote.pop("mode")
    quote.pop("asof_ms")
    cash = supplied[1]["available_cash_USDT"]
    held = supplied[1]["positions"]["BTC"]
    return {"mode": MODE, "portfolio_id": supplied[1]["portfolio_id"], "pair": "BTCUSDT",
        "protocol_sha256": PROTOCOL, "cutoff_ms": supplied[0]["cutoff_ms"],
        "risk_asof_ms": NOW, "wallet_asof_ms": NOW, "quote_asof_ms": NOW,
        "account_state_known": True, "halted": False, "capital_is_artificial": True, "initial_capital_USDT": "50",
        "equity_USDT": decimal_text(Fraction(cash) + Fraction(held) * Fraction(quote["reference_price"])),
        "total_cash_USDT": cash, "available_cash_USDT": cash,
        "positions": {"BTC": held, "ETH": "0"}, "pending_orders": [], "quote": quote,
        "fee_rate": "0.0012", "fee_asset": "USDT"}


def new_scenario(plan, observation, *, order_exists=False):
    return candidate.FakeScenario(plan, order_exists=order_exists), candidate.FakeRiskProvider(plan, observation)


def order_file(registry, plan):
    return registry.path.parent / ("fake-order-" + plan["business_key"] + ".sqlite3")


def get_budget(registry, plan):
    path = order_file(registry, plan)
    if not path.is_file():
        return None
    journal = candidate.adapter.Journal(path)
    try:
        row = journal.get()
        return None if row is None else row["post_used"]
    finally:
        journal.close()


def expect_guard_block(function, scenario):
    try:
        function()
    except candidate.adapter.GuardError:
        require(True, "local risk block has GuardError category")
    else:
        require(False, "unsafe latest observation was not blocked")
    require(scenario.calls["POST /api/v3/order"] == 0, "local block dispatched fake POST")


def group(name, function):
    before = ASSERTIONS
    try:
        evidence = function()
        RESULTS.append({"method": name, "passed": True, "assertions": ASSERTIONS - before, "evidence": evidence or {}})
    except Exception as exc:
        RESULTS.append({"method": name, "passed": False, "assertions": ASSERTIONS - before,
            "error_type": type(exc).__name__, "detail": str(exc) if isinstance(exc, AssertionError) else "raw candidate/harness details omitted"})


def fee_inclusive_debit(quantity, limit_price, fee_rate):
    quantity, limit_price, fee_rate = map(Fraction, (quantity, limit_price, fee_rate))
    if quantity <= 0 or limit_price <= 0 or not 0 <= fee_rate < 1:
        raise ValueError("independent numeric domain")
    gross = quantity * limit_price
    # Slippage embedded in the already frozen limit is not subtracted twice.
    return gross * (1 + fee_rate)


def same_unit_floor(cash, held, bid, floor, *, account_unit, floor_unit):
    if account_unit != "USDT" or floor_unit != account_unit:
        raise ValueError("unknown or mismatched artificial accounting unit")
    return Fraction(cash) + Fraction(held) * Fraction(bid) > Fraction(floor)


def independent_risk_numbers(quantity, limit_price, reference, fee_rate, total_cash, held):
    quantity, limit_price, reference, fee_rate, total_cash, held = map(
        Fraction, (quantity, limit_price, reference, fee_rate, total_cash, held))
    debit = fee_inclusive_debit(quantity, limit_price, fee_rate)
    current_equity = total_cash + held * reference
    return {"fee_inclusive_debit": debit, "current_equity": current_equity,
        "total_position_exposure": (held + quantity) * max(reference, limit_price),
        "projected_equity_at_frozen_reference": current_equity - debit + quantity * reference}


def own_instrument_known_answers():
    values = independent_risk_numbers("0.147", "97.47", "97.31", "0.0012", "50", "0")
    if values["fee_inclusive_debit"] != Fraction("14.345283708"):
        raise AssertionError("independent fee/slippage known answer wrong")
    if values["current_equity"] != 50 or values["total_position_exposure"] != Fraction("14.32809"):
        raise AssertionError("independent equity/exposure known answer wrong")
    if same_unit_floor("30", "0", "97.31", "30", account_unit="USDT", floor_unit="USDT"):
        raise AssertionError("strict artificial floor equality wrong")
    try:
        same_unit_floor("50", "0", "97.31", "30", account_unit="USDT", floor_unit="CNY")
    except ValueError:
        pass
    else:
        raise AssertionError("deliberately wrong artificial unit was not rejected")
    edge = independent_risk_numbers("0.147", "97.47", "97.31", "0.0012", "30.000000000000000001", "0")
    if not edge["current_equity"] > 30 or not edge["projected_equity_at_frozen_reference"] < 30:
        raise AssertionError("independent projected-floor counterexample incorrect")
    return {"self_owned_numeric_and_unit_instrument_checks": 5,
        "known_wrong_unit_refused": True, "candidate_methods_executed": 0,
        "current_vs_projected_floor_counterexample_available": True}


def execute(plan, registry, scenario, provider, *, recover=False):
    return candidate.execute_fake(plan, registry, scenario, risk_provider=provider, recover=recover)


def bound_scope_and_good_wallet():
    own_instrument_known_answers()
    require(candidate.adapter.__file__ == str((SOURCE_DIR / "binance_spot_testnet_adapter_20261008_v4.py").resolve()), "actual v4 import origin differs")
    require(Path(candidate.adapter.GuardedClient._request.__code__.co_filename).resolve() == Path(candidate.adapter.__file__).resolve(), "actual v4 dispatch code origin differs")
    require(Path(candidate._RiskFence.transport.__code__.co_filename).resolve() == Path(candidate.__file__).resolve(), "risk final transport code origin differs")
    with fake_registry() as (registry, directory):
        plan = compile_plan(registry)
        snapshot = observe(plan)
        scenario, provider = new_scenario(plan, snapshot)
        snapshot["halted"] = True
        require(provider.read()["halted"] is False, "provider retained caller snapshot by reference")
        returned = provider.read()
        returned["quote"]["symbol"] = "ETHUSDT"
        require(provider.read()["quote"]["symbol"] == "BTCUSDT", "provider read leaked mutable internal state")
        price, qty, rate = map(Fraction, (plan["order_manifest"]["price"], plan["quantity"], plan["fee_rate"]))
        oracle = independent_risk_numbers(qty, price, "97.31", rate, "50", "0")
        require(price == Fraction("97.47") and qty == Fraction("0.147"), "own fixed grid plan differs")
        require(Fraction(plan["maximum_cash_debit_USDT"]) == oracle["fee_inclusive_debit"], "frozen cash duplicated slip/fee")
        original = candidate.FakeScenario.transport
        post_fields, wallets = [], []
        def observe_transport(self, request):
            if request.method == "POST":
                body = parse_qs(request.body.decode("ascii"), strict_parsing=True)
                require(body["quantity"] == [decimal_text(qty)] and body["price"] == [decimal_text(price)], "actual POST numeric business parameters changed")
                require(body["side"] == ["BUY"] and body["type"] == ["LIMIT_MAKER"], "actual POST side/type scope changed")
                post_fields.append(True)
            response = original(self, request)
            if request.path == "/api/v3/account":
                rows = {r["asset"]: Fraction(r["free"]) + Fraction(r["locked"]) for r in json.loads(response.body)["balances"]}
                wallets.append(rows)
            return response
        with patch.object(candidate.FakeScenario, "transport", observe_transport):
            result = execute(plan, registry, scenario, provider)
        require(len(post_fields) == 1 and scenario.calls["POST /api/v3/order"] == 1, "safe source did not dispatch exactly one fake POST")
        require(wallets == [{"BTC": Fraction(0), "USDT": Fraction(50)},
            {"BTC": qty, "USDT": Fraction(50) - oracle["fee_inclusive_debit"]}], "own Fraction wallet delta mismatch")
        require(result["result"]["reconciliation"]["passed"], "synthetic wallet not reconciled")
        require([c["stage"] for c in result["risk_checks"]] == ["initial-after-grant", "request-before-sign", "after-sign-guard-before-transport"], "three actual risk stages not covered")
        require(all(Fraction(c["projected_equity_USDT"]) == oracle["projected_equity_at_frozen_reference"] for c in result["risk_checks"]), "risk projected equity differs from independent Fraction")
        require(get_budget(registry, plan) == 1, "success did not retain budget")
    return {"known_wrong_unit_instrument_refused": True, "independent_actual_POST_and_wallet_Fraction_oracle": True,
        "all_three_actual_risk_call_sites_observed": True, "fixed_source_not_a_broker_proof": True}


def initial_risk_mutations():
    cases = [("halted", True), ("halted", 0), ("halted", None), ("account_state_known", False),
        ("account_state_known", 1), ("account_state_known", None), ("account_state_known", "true"),
        ("mode", "LIVE"), ("portfolio_id", "other-artificial-portfolio"), ("pair", "ETHUSDT"),
        ("protocol_sha256", "b" * 64), ("cutoff_ms", NOW - 40001),
        ("capital_is_artificial", False), ("initial_capital_USDT", "51"),
        ("fee_asset", "BTC"), ("fee_asset", "CNY"), ("fee_rate", "0.001200000000000001"),
        ("fee_rate", "-0.001"), ("pending_orders", None), ("pending_orders", ["fake-pending"]),
        ("available_cash_USDT", "1"), ("available_cash_USDT", "51"), ("equity_USDT", "51"),
        ("positions", {"BTC": "0", "ETH": "0.1"}), ("positions", {"BTC": "0", "ETH": "0", "BNB": "1"}),
        ("positions", {"BTC": "0.1", "ETH": "0"})]
    for key, value in cases:
        with fake_registry() as (registry, directory):
            plan = compile_plan(registry)
            observation = observe(plan)
            observation[key] = value
            scenario, provider = new_scenario(plan, observation)
            expect_guard_block(lambda: execute(plan, registry, scenario, provider), scenario)
            require(not order_file(registry, plan).exists(), "initial block created adapter journal")
            require(registry.db.execute("SELECT fake_dispatch_used FROM signals").fetchone() == (1,), "initial block refunded fake grant")
    return {"independent_initial_invalid_cases": len(cases), "local_GuardError_zero_POST": True,
        "initial_refusal_spends_grant_without_order_file": True, "unknown_boolean_fee_unit_and_extra_asset_checked": True}


def current_and_projected_floor():
    boundary = Fraction(30) + fee_inclusive_debit("0.147", "97.47", "0.0012") - Fraction("0.147") * Fraction("97.31")
    require(boundary == Fraction("30.040713708"), "own projected floor boundary wrong")
    for cash, allowed in ((Fraction(30), False), (boundary, False), (boundary + Fraction(1, 10**18), True)):
        with fake_registry() as (registry, directory):
            supplied = inputs(cash=decimal_text(cash))
            plan = compile_plan(registry, supplied)
            scenario, provider = new_scenario(plan, observe(plan, supplied))
            if allowed:
                result = execute(plan, registry, scenario, provider)
                require(scenario.calls["POST /api/v3/order"] == 1, "epsilon projected floor safe case blocked")
                require(Fraction(result["risk_checks"][-1]["projected_equity_USDT"]) == 30 + Fraction(1, 10**18), "epsilon projected floor rounded")
            else:
                expect_guard_block(lambda: execute(plan, registry, scenario, provider), scenario)
    with fake_registry() as (registry, directory):
        supplied = inputs(cash=decimal_text(boundary))
        plan = compile_plan(registry, supplied)
        observation = observe(plan, supplied)
        observation["fee_rate"] = "0"
        scenario, provider = new_scenario(plan, observation)
        expect_guard_block(lambda: execute(plan, registry, scenario, provider), scenario)
    return {"current_floor_equality_and_projected_cost_floor_equality_refused": True,
        "one_decimal_unit_above_projected_floor_allowed": True, "latest_lower_fee_cannot_reduce_frozen_reserve": True}


def observation_clocks():
    cases = 0
    for key in ("risk_asof_ms", "wallet_asof_ms", "quote_asof_ms"):
        for value, allowed in ((NOW - 5000, True), (NOW - 5001, False), (NOW + 1, False), (True, False)):
            with fake_registry() as (registry, directory):
                plan = compile_plan(registry)
                observation = observe(plan)
                observation[key] = value
                scenario, provider = new_scenario(plan, observation)
                if allowed:
                    result = execute(plan, registry, scenario, provider)
                    require(result["result"]["new_order_post_reservations"] == 1, "5000ms inclusive clock rejected")
                else:
                    expect_guard_block(lambda: execute(plan, registry, scenario, provider), scenario)
                cases += 1
    return {"three_clock_domains_cases": cases, "inclusive_age5000_stale5001_future_and_bool_checked": True}


def latest_quote_and_total_exposure():
    for change, allowed in (("maker_equal", False), ("crossed_quote", False), ("drift_equal", True),
            ("drift_exceeded", False), ("fee_asset_unknown", False)):
        with fake_registry() as (registry, directory):
            plan = compile_plan(registry)
            observation = observe(plan)
            if change == "maker_equal":
                observation["quote"]["askPrice"] = plan["order_manifest"]["price"]
            elif change == "crossed_quote":
                observation["quote"]["bidPrice"], observation["quote"]["askPrice"] = "100", "99"
            elif change.startswith("drift"):
                ref = Fraction("97.31") * Fraction(101, 100)
                if change == "drift_exceeded":
                    ref += Fraction(1, 10**18)
                observation["quote"]["reference_price"] = decimal_text(ref)
            else:
                observation["fee_asset"] = None
            scenario, provider = new_scenario(plan, observation)
            if allowed:
                result = execute(plan, registry, scenario, provider)
                require(result["result"]["new_order_post_reservations"] == 1, "exact one-percent drift rejected")
            else:
                expect_guard_block(lambda: execute(plan, registry, scenario, provider), scenario)
    for kind in ("large_original_position", "latest_reference_pushes_total_above_cap"):
        with fake_registry() as (registry, directory):
            if kind == "large_original_position":
                supplied = inputs(cash="20", held="0.35", weight="0.9")
            else:
                supplied = list(copy.deepcopy(inputs(weight="0.5")))
                supplied[3]["symbols"][0]["filters"][1]["stepSize"] = "0.001"
                supplied[3]["symbols"][0]["filters"][1]["minQty"] = "0.001"
            plan = compile_plan(registry, supplied)
            observation = observe(plan, supplied)
            if kind.startswith("latest"):
                observation["quote"]["reference_price"] = decimal_text(Fraction("97.31") * Fraction(101, 100))
            oracle = independent_risk_numbers(plan["quantity"], plan["order_manifest"]["price"],
                observation["quote"]["reference_price"], plan["fee_rate"], observation["total_cash_USDT"], observation["positions"]["BTC"])
            require(Fraction(plan["gross_quote_USDT"]) <= 25 and oracle["total_position_exposure"] > 25, "own total exposure counterexample wrong")
            scenario, provider = new_scenario(plan, observation)
            expect_guard_block(lambda: execute(plan, registry, scenario, provider), scenario)
    return {"latest_maker_drift_fee_unit_cases": 5, "new_notional_under_cap_but_total_exposure_over_cap_cases": 2}


def late_sign_guard_rechecks():
    mutations = [("halted", True), ("account_state_known", False), ("fee_asset", "BNB"),
        ("fee_rate", "0.0013"), ("pending_orders", ["fake-pending"]),
        ("risk_asof_ms", NOW - 1), ("wallet_asof_ms", NOW - 5001),
        ("quote_asof_ms", NOW + 1), ("available_cash_USDT", "1"),
        ("total_cash_USDT", "1"), ("portfolio_id", "changed-artificial"),
        ("positions", {"BTC": "0", "ETH": "1"})]
    cases = 0
    for stage in ("sign", "guard"):
        for key, value in mutations:
            with fake_registry() as (registry, directory):
                plan = compile_plan(registry)
                scenario, provider = new_scenario(plan, observe(plan))
                def publish_bad():
                    altered = provider.read()
                    altered[key] = value
                    provider.publish(altered)
                if stage == "sign":
                    original = candidate.adapter.sign
                    def hook(secret, encoded):
                        signature = original(secret, encoded)
                        if "newClientOrderId=" in encoded:
                            publish_bad()
                        return signature
                    manager = patch.object(candidate.adapter, "sign", hook)
                else:
                    original = candidate.adapter.guard_request
                    def hook(scope, request):
                        result = original(scope, request)
                        if request.method == "POST":
                            publish_bad()
                        return result
                    manager = patch.object(candidate.adapter, "guard_request", hook)
                with manager:
                    expect_guard_block(lambda: execute(plan, registry, scenario, provider), scenario)
                require(get_budget(registry, plan) == 1, "late local refusal released adapter reservation")
                require(provider.reads >= 4, "late risk update did not reach final latest source read")
                require(scenario.calls["GET /api/v3/account"] == 1, "late block falsely completed wallet reconciliation")
                cases += 1
    return {"independent_sign_or_guard_latest_changes": cases, "late_local_GuardError_zero_POST": True,
        "adapter_budget_kept_one": True, "not_ambiguous_network_or_completed_reconciliation": True}


def wallet_evidence_refusals():
    for kind, expected_budget in (("extra_asset", 0), ("duplicate_asset", 0), ("negative_locked", 0),
            ("missing_asset", 0), ("cash_evidence_changed", 1), ("unknown_free_value", 0)):
        with fake_registry() as (registry, directory):
            plan = compile_plan(registry)
            scenario, provider = new_scenario(plan, observe(plan))
            rows = scenario.baseline["balances"]
            if kind == "extra_asset":
                rows.append({"asset": "BNB", "free": "1", "locked": "0"})
            elif kind == "duplicate_asset":
                rows.append(copy.deepcopy(rows[0]))
            elif kind == "negative_locked":
                rows[1]["locked"] = "-1"
            elif kind == "missing_asset":
                del rows[0]
            elif kind == "cash_evidence_changed":
                rows[1]["free"] = "49"
            else:
                rows[1]["free"] = None
            expect_guard_block(lambda: execute(plan, registry, scenario, provider), scenario)
            require(get_budget(registry, plan) == expected_budget, "wallet evidence block at wrong reservation stage")
    return {"actual_GET_account_evidence_invalid_cases": 6, "extra_assets_not_silently_dropped": True,
        "latest_provider_declaration_cannot_replace_GET_wallet_evidence": True}


def spent_restart_and_fresh_child():
    with fake_registry() as (registry, directory):
        plan = compile_plan(registry)
        scenario, provider = new_scenario(plan, observe(plan))
        original = candidate.adapter.sign
        def hook(secret, encoded):
            signature = original(secret, encoded)
            if "newClientOrderId=" in encoded:
                altered = provider.read()
                altered["halted"] = True
                provider.publish(altered)
            return signature
        with patch.object(candidate.adapter, "sign", hook):
            expect_guard_block(lambda: execute(plan, registry, scenario, provider), scenario)
        require(get_budget(registry, plan) == 1, "fresh process starting budget not consumed")
        path = registry.path
        registry.close()
        child = (
            "import sys,json,hashlib,pathlib; import binance_signal_order_bridge_20261008_v2 as b; "
            "x=json.load(sys.stdin); r=b.FakeSignalRegistry(x['path']); p=x['plan']; "
            "s=b.FakeScenario(p,order_exists=False); v=b.FakeRiskProvider(p,x['snapshot']); "
            "a=b.compile_position_deltas(*x['inputs'],r,now_ms=x['now']); "
            "z=b.execute_fake(p,r,s,risk_provider=v,recover=True); "
            "r.close(); print(json.dumps({'compile':a['status'],'permission':a['new_order_permission'],"
            "'plan_equal':a['plan']==p,'routes':dict(s.calls),'budget':z['result']['new_order_post_reservations'],"
            "'unknown':z['result']['last_error']['code'],'reads':v.reads,"
            "'source_sha':hashlib.sha256(pathlib.Path(b.__file__).read_bytes()).hexdigest(),"
            "'adapter_sha':hashlib.sha256(pathlib.Path(b.adapter.__file__).read_bytes()).hexdigest()}))"
        )
        stopped = observe(plan)
        stopped["account_state_known"], stopped["halted"] = False, True
        done = subprocess.run([sys.executable, "-B", "-c", child], input=canonical({"path": str(path),
            "plan": plan, "snapshot": stopped, "inputs": inputs(), "now": NOW}), cwd=ROOT,
            env={"PYTHONPATH": str(SOURCE_DIR), "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=15, check=False)
        require(done.returncode == 0, "fresh child recovery failed to load retained synthetic state")
        observed = json.loads(done.stdout)
        require(observed["compile"] == "REUSE_NO_NEW_ORDER" and observed["permission"] is False and observed["plan_equal"], "fresh child minted changed or new permission")
        require(observed["routes"] == {"GET /api/v3/order": 1} and observed["budget"] == 1 and observed["unknown"] == -2013, "fresh child did not retain query-only unknown reservation")
        require(observed["reads"] == 0, "GET-only recovery incorrectly required new risk permission")
        require(observed["source_sha"] == EXPECTED_V2["work/binance_signal_order_bridge_20261008_v2.py"] and observed["adapter_sha"] == PINNED_EXISTING["work/binance_spot_testnet_adapter_20261008_v4.py"], "fresh child tested other code bytes")
        reopened = candidate.FakeSignalRegistry(path)
        try:
            clocks = json.loads(reopened.db.execute("SELECT risk_clocks_json FROM signals").fetchone()[0])
            require(clocks == {"risk_ms": NOW, "wallet_ms": NOW, "quote_ms": NOW, "read_ms": NOW}, "risk clocks not durable before late block")
            for _ in range(2):
                s, p = new_scenario(plan, stopped)
                result = execute(plan, reopened, s, p, recover=True)
                require(s.calls == Counter({"GET /api/v3/order": 1}) and result["result"]["new_order_post_reservations"] == 1, "repeat recovery reopened POST budget")
        finally:
            reopened.close()
    return {"actual_fresh_Python_process": True, "same_plan_identity_and_spent_budget": True,
        "unknown_or_halted_new_source_not_required_for_GET_only_recovery": True, "three_unknown_recoveries_zero_reissue": True}


def initial_gap_recovery_and_expiry():
    with fake_registry() as (registry, directory):
        plan = compile_plan(registry)
        bad = observe(plan)
        bad["halted"] = True
        scenario, provider = new_scenario(plan, bad)
        expect_guard_block(lambda: execute(plan, registry, scenario, provider), scenario)
        path = registry.path
        registry.close()
        reopened = candidate.FakeSignalRegistry(path)
        try:
            for recover in (False, True):
                s, p = new_scenario(plan, observe(plan))
                try:
                    execute(plan, reopened, s, p, recover=recover)
                except candidate.BridgeError:
                    require(True, "conservative missing order state refusal")
                else:
                    require(False, "initial block regained eligibility")
                require(not s.calls and not order_file(reopened, plan).exists(), "gap recovery created initial state or traffic")
        finally:
            reopened.close()
    for delta, allowed in ((0, True), (1, False)):
        with fake_registry() as (registry, directory):
            plan = compile_plan(registry)
            observation = observe(plan)
            when = plan["order_manifest"]["expires_ms"] + delta
            for key in ("risk_asof_ms", "wallet_asof_ms", "quote_asof_ms"):
                observation[key] = when
            scenario, provider = new_scenario(plan, observation)
            scenario.now = when
            if allowed:
                result = execute(plan, registry, scenario, provider)
                require(result["post_dispatch_ms"] == [when], "expiry equality semantics changed")
            else:
                expect_guard_block(lambda: execute(plan, registry, scenario, provider), scenario)
    return {"initial_stop_then_real_close_reopen_cannot_create_order_file": True,
        "expiry_equality_allowed_expiry_plus_one_refused": True}


def legal_latest_changes_and_pipeline_trace():
    with fake_registry() as (registry, directory):
        plan = compile_plan(registry)
        frozen_plan = copy.deepcopy(plan)
        observation = observe(plan)
        scenario, provider = new_scenario(plan, observation)
        changed = copy.deepcopy(observation)
        changed.update(total_cash_USDT="49", available_cash_USDT="49", equity_USDT="49", fee_rate="0.001")
        changed["quote"].update(reference_price="98", bidPrice="97.5", askPrice="98.8")
        provider.publish(changed)
        qty, price, rate = map(Fraction, (plan["quantity"], plan["order_manifest"]["price"], plan["fee_rate"]))
        expected = independent_risk_numbers(qty, price, "98", rate, "49", "0")
        scenario.baseline["balances"][1]["free"] = "49"
        scenario.current["balances"][1]["free"] = decimal_text(Fraction(49) - expected["fee_inclusive_debit"])
        scenario.quote.update(bidPrice="97.5", askPrice="98.8")
        events = []
        a = candidate.adapter
        old_request, old_sign, old_guard = a.GuardedClient._request, a.sign, a.guard_request
        old_check, old_transport = candidate._RiskFence.check, candidate.FakeScenario.transport
        def request(client, method, path, params, *, deadline_ms=None):
            if method == "POST":
                events.append("v4-request")
                require(deadline_ms == plan["order_manifest"]["expires_ms"], "actual v4 deadline parameter changed")
            return old_request(client, method, path, params, deadline_ms=deadline_ms)
        def sign(secret, encoded):
            signature = old_sign(secret, encoded)
            if "newClientOrderId=" in encoded:
                events.append("v4-sign")
            return signature
        def guard(scope, req):
            result = old_guard(scope, req)
            if req.method == "POST":
                events.append("v4-guard")
            return result
        def risk(fence, stage, *, account_required):
            if stage != "initial-after-grant":
                events.append("risk-entry" if stage == "request-before-sign" else "risk-final")
            return old_check(fence, stage, account_required=account_required)
        def transport(self, req):
            if req.method == "POST":
                events.append("closed-fake-POST")
                body = parse_qs(req.body.decode(), strict_parsing=True)
                require(body["newClientOrderId"] == [plan["client_order_id"]], "legal updates changed client intent identity")
                require(body["price"] == [decimal_text(price)] and body["quantity"] == [decimal_text(qty)], "legal updates resized frozen order")
            return old_transport(self, req)
        with patch.object(a.GuardedClient, "_request", request), patch.object(a, "sign", sign), \
                patch.object(a, "guard_request", guard), patch.object(candidate._RiskFence, "check", risk), \
                patch.object(candidate.FakeScenario, "transport", transport):
            result = execute(plan, registry, scenario, provider)
        require(events == ["risk-entry", "v4-request", "v4-sign", "v4-guard", "risk-final", "closed-fake-POST"], "actual POST call order did not include final after-guard fence")
        require(plan == frozen_plan, "legal latest source updates mutated plan or expiry")
        require(result["result"]["reconciliation"]["passed"], "legal latest wallet change failed synthetic reconciliation")
        require(Fraction(result["risk_checks"][-1]["projected_equity_USDT"]) == expected["projected_equity_at_frozen_reference"], "latest lower fee incorrectly replaced frozen fee reserve")
        require(Path(candidate._RiskCheckedClient._request.__code__.co_filename).resolve() == Path(candidate.__file__).resolve(), "actual risk entry subclass origin differs")
    return {"valid_changed_cash_quote_and_lower_fee_allowed": True, "actual_ordered_pipeline": events,
        "frozen_quantity_price_ID_expiry_preserved": True, "latest_fee_lower_but_frozen_upper_reserve_used": True,
        "call_sites_real_v2_subclass_and_v4_code_bytes": True}


def actual_function_binding(function):
    path = Path(function.__code__.co_filename).resolve()
    return {"path": path.relative_to(ROOT).as_posix(), "line": function.__code__.co_firstlineno,
        "source_sha256": digest_file(path)}


def main():
    import argparse
    global candidate
    parser = argparse.ArgumentParser(description="Independent offline fake BTC dispatch controls; stdout by default.")
    parser.add_argument("--output", type=Path, help="Exclusive-create a new explicitly selected report; never overwrites.")
    parser.add_argument("--expected-source-sha", help="Optional external SHA pin for this auditor's exact bytes.")
    args = parser.parse_args()
    source_before = digest_file(Path(__file__))
    if args.expected_source_sha is not None and args.expected_source_sha != source_before:
        raise AssertionError("auditor external source pin differs")
    core_pins = {
        "binance_signal_order_bridge_20261008_v2.py": EXPECTED_V2["work/binance_signal_order_bridge_20261008_v2.py"],
        "binance_spot_testnet_adapter_20261008_v4.py": PINNED_EXISTING["work/binance_spot_testnet_adapter_20261008_v4.py"],
    }
    observed = {name: digest_file(SOURCE_DIR / name) for name in core_pins}
    require(observed == core_pins, "exact frozen bridge v2 and adapter v4 source binding mismatch")
    sys.path.insert(0, str(SOURCE_DIR))
    candidate = importlib.import_module("binance_signal_order_bridge_20261008_v2")
    if Path(candidate.__file__).resolve() != (SOURCE_DIR / "binance_signal_order_bridge_20261008_v2.py").resolve():
        raise AssertionError("actual bridge import came from another directory")
    for name, function_name in METHOD_LIST:
        group(name, globals()[function_name])
    require({name: digest_file(SOURCE_DIR / name) for name in core_pins} == observed, "frozen core files changed during controls")
    require(digest_file(Path(__file__)) == source_before, "auditor bytes changed during controls")
    report = {
        "schema": "binance-signal-order-bridge-dispatch-portable-independent-audit/2",
        "version": "20261008-v2-portable",
        "status": "LIMITED_OFFLINE_PORTABLE_DISPATCH_RECHECK_ACCEPTANCE" if all(c["passed"] for c in RESULTS) else "INDEPENDENT_PORTABLE_DISPATCH_CONTROL_FAILURE",
        "core_source_bindings": observed,
        "auditor_source_sha256": source_before,
        "all_three_execution_sources_unchanged": True,
        "source_directory_only_dependencies": True,
        "portable_execution_context": {
            "same_directory_file_count": len(list(SOURCE_DIR.iterdir())),
            "exact_three_source_file_directory": sorted(p.name for p in SOURCE_DIR.iterdir()) == sorted(list(core_pins) + [Path(__file__).name]),
            "python_isolated_mode": bool(sys.flags.isolated),
            "bytecode_output_disabled": bool(sys.dont_write_bytecode),
            "runtime_requirement": "Only three same-directory source files and Python standard library; no work aliases, legacy helper artifacts or producer tests.",
        },
        "source_origin": {
            "bridge": actual_function_binding(candidate.compile_position_deltas),
            "adapter": actual_function_binding(candidate.adapter.GuardedClient._request),
            "auditor": Path(__file__).resolve().relative_to(ROOT).as_posix(),
            "fresh_child_import_search_path": SOURCE_DIR.relative_to(ROOT).as_posix(),
        },
        "original_legacy_preservation_provenance": {
            "original_local_auditor_sha256": "da4c8d6b8f73fee6530136d8dfdc2e6ec0e972112caa33d1a0d41e7e81c6d12b",
            "original_local_report_sha256": "893f17472f58a3a6ee4e43a86c12c5e86b232d49d5fc174d91904f9c3e8139ac",
            "legacy_eight_file_preservation_reaudited_in_this_portable_run": False,
            "scope": "The earlier local report covers the original eight-file closure. This portable run needs only bridge v2, adapter v4 and this auditor; it neither reads old reports nor repeats legacy preservation. Original local closure auditor source is outside this portable bundle.",
        },
        "ast_control_comparison": AST_COMPARISON,
        "attack_matrix": MATRIX,
        "method_count": len(RESULTS),
        "assertion_calls_executed": ASSERTIONS,
        "passed_methods": sum(c["passed"] for c in RESULTS),
        "failed_methods": sum(not c["passed"] for c in RESULTS),
        "count_scope": "Actual require calls for ten independent control bundles plus three exact source checks. Looped faults are neither market samples nor independent strategy experiments. Five known-answer instrument checks are separate, not require-call counts.",
        "producer_tests_imported_or_run": 0,
        "producer_risk_snapshot_helper_calls": 0,
        "actual_call_sites": {
            "v4_request": actual_function_binding(candidate.adapter.GuardedClient._request),
            "v4_runner": actual_function_binding(candidate.adapter.FiniteRunner.run),
            "v2_request_entry": actual_function_binding(candidate._RiskCheckedClient._request),
            "v2_transport_fence": actual_function_binding(candidate._RiskFence.transport),
            "v2_risk_check": actual_function_binding(candidate._RiskFence.check),
        },
        "decision_check": {
            "P1": "Independent Fraction/grid/wallet known answers and a deliberately wrong USDT/CNY unit refusal validate the instrument.",
            "P2": "Latest-risk refusal at the v4 final fake transport fence is distinct from expiry; local GuardError retains spent reservations without ambiguous network submission.",
            "P3": "Valid latest cash/quote/lower fee updates execute, so blanket refusal cannot manufacture all-pass safety. Late sign/guard faults and fresh-child unknown recovery attack the favorable result.",
        },
        "controls": RESULTS,
        "limits": MATRIX["remaining_limits"] + [
            "No portfolio-global sticky breach, continuous execution, new partial-fill model or cancel-flow admission.",
            "Equities are conditional artificial reference calculations; future gaps, real broker fees and real NAV are unproven.",
            "Registry/order-journal commits are separate; a spent grant without an order journal loses eligibility conservatively.",
            "Same-UID concurrent mutation and copied/reset/fresh registries are outside this per-retained-file contract.",
            "This executable does not repeat the earlier eight-file preservation or verify the full public parent tree.",
        ],
        "actual_actions": {"network_calls": 0, "real_credentials_read": 0, "actual_canonical_probe_journal_access": 0, "real_orders": 0, "new_forward_signals": 0, "paid_calls": 0, "GitHub_writes": 0},
        "output_policy": "No file output by default. An explicit --output exclusively creates a new report; choose an owned runs-local or new report path.",
    }
    if args.output is None:
        print(canonical(report))
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n")
        print(canonical({"status": report["status"], "report": args.output.name, "sha256": digest_file(args.output), "methods": len(RESULTS), "assertions": ASSERTIONS, "failures": report["failed_methods"]}))


if __name__ == "__main__":
    main()
