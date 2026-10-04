import copy
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from research_base import vibe_controls
from research_base.evidence import ContractError, digest
from research_base.path_controls import allocation_cases, execute_case, full_fixture, run_controls
from research_base.reference import low_frequency_engine as reference
from research_base.reference import run_controls as oracle
from research_base.strategy_path import ASSETS, generate
from research_base.vibe_accounting import accounting_class

PACKAGE = Path(__file__).resolve().parents[1] / "research_base"
HAS_VIBE = importlib.util.find_spec("backtest") is not None


class ProtocolTests(unittest.TestCase):
    def test_hand_answers_and_reference_prefix_controls(self):
        with tempfile.TemporaryDirectory() as t:
            result = run_controls(Path(t))
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["cases_passed"], 11)
        self.assertTrue(all(c["passed"] for c in result["causal_controls"]))

    def test_warmup_strict_equality_next_open_and_fixed_universe(self):
        data = full_fixture()
        prepared, features = generate(data, "S", trade_start="2023-11-01")
        self.assertEqual(len(features["skipped"]), 9)
        first = features["decisions"][0]
        self.assertEqual(first["date"], "2023-10-31")
        self.assertEqual(first["execution_date"], "2023-11-01")
        self.assertEqual(first["index_fraction"]["SPY"], "200")
        self.assertEqual(first["sma_fraction"]["SPY"], "110")
        self.assertEqual(first["weights"], {"SPY": "0.25", "EFA": "0", "IEF": "0", "GLD": "0.25"})
        self.assertEqual(prepared["calendar"]["month_end_sessions"], [])
        data["symbols"] = ["SPY"]
        with self.assertRaises(ValueError):
            generate(data, "S", trade_start="2023-11-01")

    def test_current_month_included_and_oldest_month_dropped(self):
        data = full_fixture()
        for b in data["bars"]:
            if b["symbol"] == "SPY":
                price = "20" if b["date"] <= "2023-02-01" else "10.5" if b["date"] >= "2023-11-30" else "10"
                b.update(open=price, high=price, low=price, close=price)
        _, f = generate(data, "S", trade_start="2023-11-01")
        self.assertEqual(f["decisions"][-1]["index_fraction"]["SPY"], "105")
        self.assertEqual(f["decisions"][-1]["sma_fraction"]["SPY"], "201/2")
        self.assertEqual(f["decisions"][-1]["weights"]["SPY"], "0.25")

    def test_feature_pipeline_matches_independent_reference_signal(self):
        data = full_fixture()
        _, f = generate(data, "S", trade_start="2023-11-01")
        result = reference.run(data, "synthetic")
        self.assertEqual([s["weights"] for s in result["signals"]], [s["weights"] for s in f["decisions"]])

    def test_missing_close_breaks_trend_chain_without_weakening_static_baseline(self):
        data = full_fixture()
        oracle.bar(data, "2023-09-30", "GLD", close=None)
        _, trend = generate(data, "S", trade_start="2023-11-01")
        _, static = generate(data, "B0", trade_start="2023-11-01")
        self.assertFalse(trend["intents"])
        self.assertTrue(any(s["reason"] == "BROKEN_TOTAL_RETURN_CHAIN" for s in trend["skipped"]))
        self.assertEqual(len(static["intents"]), 2)

    def test_late_close_and_late_action_rejected(self):
        for kind in ("close", "action"):
            d = full_fixture()
            if kind == "close":
                oracle.bar(d, "2023-10-31", available_at="2023-11-01T14:00:00Z")
            else:
                d["actions"] = [oracle.dividend("late", "2023-10-31", "2023-11-01")]
                d["actions"][0]["known_at"] = "2023-10-31T21:00:00Z"
            with self.subTest(kind=kind), self.assertRaises(ContractError):
                generate(d, "S", trade_start="2023-11-01")

    def test_br_fixed_grid_and_unselected_k_rejected(self):
        for variant, k in (("BR", None), ("BR", ".333"), ("BR", "1.01"), ("S", ".5")):
            with self.subTest(variant=variant, k=k), self.assertRaises(ContractError):
                generate(full_fixture(), variant, trade_start="2023-11-01", k=k)

    def test_gross_signal_is_distinct_from_net_wallet(self):
        from research_base.path_controls import full_cases
        _, data, variant, k, _ = next(c for c in full_cases() if c[0] == "F02_gross_signal_net_wallet")
        _, f, result, actual, replay = execute_case(data, variant, k)
        self.assertEqual(f["decisions"][0]["index_fraction"]["SPY"], "200")
        self.assertEqual(Decimal(actual["equity"]), Decimal("1080"))
        self.assertTrue(replay["passed"])

    def test_tolerance_is_bounded_money_only(self):
        _, data, _ = next(c for c in allocation_cases() if c[0] == "A04_cent_quotes_binary_state_tolerance")
        valid = reference.run(data, "synthetic")
        for amount, expected in ((".000000005", True), (".000001", False)):
            changed = copy.deepcopy(valid)
            changed["snapshots"][-1]["equity"] = str(Decimal(changed["snapshots"][-1]["equity"])+Decimal(amount))
            self.assertEqual(oracle.independent_replay(data, changed, monetary_tolerance=".00000001")["passed"], expected)
        with self.assertRaises(ValueError):
            oracle.independent_replay(data, valid, monetary_tolerance=".01")

    def test_wrong_source_equality_rule_produces_semantic_failure(self):
        original = PACKAGE / "strategy_path.py"; before = digest(original)
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            shutil.copytree(PACKAGE, root / "research_base", ignore=shutil.ignore_patterns("__pycache__"))
            p = root / "research_base/strategy_path.py"
            body = p.read_text(); anchor = "index[s] > average[s]"
            self.assertEqual(body.count(anchor), 1)
            p.write_text(body.replace(anchor, "index[s] >= average[s]"))
            try:
                code = 'import json; from pathlib import Path; from research_base.path_controls import run_controls; print(json.dumps(run_controls(Path("evidence"))))'
                process = subprocess.run([sys.executable, "-B", "-c", code], cwd=root, text=True, capture_output=True, timeout=30)
                self.assertEqual(process.returncode, 0, process.stderr)
                result = json.loads(process.stdout)
                r = next(r for r in result["reports"] if r["id"] == "F01_S_whole_path")
                self.assertFalse(r["passed"])
                self.assertTrue(r["differences"])
            finally:
                self.assertEqual(digest(original), before)


@unittest.skipUnless(HAS_VIBE, "Optional installed Vibe environment required")
class ActualProtocolPath(unittest.TestCase):
    def setUp(self):
        from backtest.engines.global_equity import GlobalEquityEngine
        self.native = GlobalEquityEngine

    def test_full_path_and_prefix_through_actual_engine(self):
        with tempfile.TemporaryDirectory() as t:
            result = run_controls(Path(t), use_vibe=True)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["cases_passed"], 11)

    def test_wrong_open_valuation_using_future_close_is_detected(self):
        def broken(base):
            good = accounting_class(base)
            class WrongOpen(good):
                def _calc_open_equity(self, data_map, close_df, ts):
                    return float(self.cash_for_planning()+self.assets_not_cash()) + sum(
                        self.held(s)*float(data_map[s].loc[ts,"close"]) for s in self.account_data["symbols"])
            return WrongOpen
        _, data, expected = allocation_cases()[1]
        with vibe_controls.offline_connections(), patch.object(vibe_controls, "accounting_class", broken):
            actual, _ = vibe_controls.execute_vibe(data, self.native, True)
        self.assertTrue(vibe_controls.differences(expected, actual))

    def test_post_close_volume_is_diagnostic_not_retroactive_cancel(self):
        _, data, _ = allocation_cases()[3]
        oracle.bar(data, "2024-10-08", volume=None)
        with vibe_controls.offline_connections():
            actual, details = vibe_controls.execute_vibe(data, self.native, True)
        self.assertEqual(actual["holdings"]["SPY"], 9)
        events = details["account_ledger"]["events"]
        self.assertTrue(any(e["type"] == "POST_CLOSE_FILL_DIAGNOSTIC" for e in events))


if __name__ == "__main__":
    unittest.main()
