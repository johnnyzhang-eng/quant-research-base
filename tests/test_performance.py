import copy
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from research_base.evidence import ContractError, digest
from research_base.performance import calibrate_risk, report
from research_base.performance_controls import cases, fixture, risk_fixture, run_controls

PACKAGE = Path(__file__).resolve().parents[1] / "research_base"


class PerformanceTests(unittest.TestCase):
    def test_all_predeclared_metric_controls(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_controls(Path(temp))
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["cases_passed"], 12)
        self.assertTrue(all(r["detected"] for r in result["bad_inputs"]))

    def test_independent_fraction_return_and_sample_variance(self):
        values = [100, 105, 100, 115]
        returns = [Fraction(b, a)-1 for a, b in zip(values, values[1:])]
        mean = sum(returns)/len(returns)
        variance = sum((r-mean)**2 for r in returns)/(len(returns)-1)
        result = report(fixture(values, rf=[0, 0, 0]))
        self.assertAlmostEqual(result["period_net_return"]["value"], .15)
        self.assertAlmostEqual(result["volatility_annual"]["value"], math.sqrt(float(variance)*252))
        self.assertAlmostEqual(result["sharpe_annual"]["value"], float(mean)/math.sqrt(float(variance))*math.sqrt(252))

    def test_initial_securities_count_in_whole_account_denominator(self):
        # Cash is0, securities100; a100->110 mark is a10% return.
        result = report(fixture([100, 110]))
        self.assertEqual(result["initial_equity"], "100")
        self.assertEqual(result["period_net_return"]["value"], .1)

    def test_external_in_and_out_do_not_net_away_flow_timing(self):
        result = report(fixture([100, 250, 125], flows=[0, 150, -150]))
        self.assertEqual(result["external_net_flow"], "0")
        self.assertEqual(result["period_net_return"]["value"], .1)

    def test_bankruptcy_row_and_recapitalization_remain_visible(self):
        r = report(fixture([100, 0, 20], flows=[0, 0, 20]))
        self.assertEqual(len(r["timeline"]), 2)
        self.assertEqual(r["timeline"][0]["net_return"], "-1")
        self.assertEqual(len(r["insolvencies"]), 1)
        self.assertIsNone(r["timeline"][1]["net_return"])
        self.assertIsNone(r["sharpe_annual"]["value"])

    def test_nonfinite_zero_initial_and_overwithdrawal_rejected(self):
        for data in (fixture([0, 10]), fixture([100, "NaN"]), fixture([100, -10], flows=[0, -110])):
            with self.subTest(data=data), self.assertRaises(ContractError):
                report(data)

    def test_costs_disclosed_and_not_deducted_twice(self):
        d = fixture([100, 95]); d["cost_events"] = [{"id": "server", "date": "2024-10-08", "category": "fixed", "amount": "5"}]
        r = report(d)
        self.assertEqual(r["period_net_return"]["value"], -.05)
        self.assertEqual(r["cost_total"], "5")

    def test_risk_free_alignment_currency_and_source_required(self):
        for change in ("currency", "source", "date"):
            d = fixture([100, 110, 100], rf=[0, 0])
            if change == "date":
                d["risk_free"]["period_returns"][0]["date"] = "2024-10-09"
            else:
                d["risk_free"][change] = "CNY" if change == "currency" else ""
            with self.assertRaises(ContractError):
                report(d)

    def test_two_sided_turnover_and_fee_components(self):
        d = copy.deepcopy(cases()[1][1])
        other = copy.deepcopy(d["fills"][0]); other.update(id="sell", signed_quantity="-5")
        d["fills"].append(other)
        r = report(d)
        self.assertAlmostEqual(r["turnover_two_sided"]["value"], 100/99)
        d["fills"][0]["fee"] = "999"
        with self.assertRaises(ContractError):
            report(d)

    def test_calibration_exact_grid_units_dates_and_no_future(self):
        candidates = {f"{i/100:.2f}": risk_fixture(i/100) for i in range(101)}
        kwargs = {"boundary_date": "2024-10-07", "end_date": "2024-10-09"}
        for mutation in ("grid", "currency", "future", "flow", "cost_model"):
            altered = copy.deepcopy(candidates)
            if mutation == "grid":
                del altered["0.50"]
            elif mutation == "currency":
                altered["0.50"]["currency"] = "CNY"
            elif mutation == "future":
                altered["0.50"]["calendar"][-1] = "2024-10-10"
                altered["0.50"]["observations"][-1]["date"] = "2024-10-10"
            elif mutation == "flow":
                altered["0.50"]["observations"][1]["external_flow_end"] = "1"
            else:
                altered["0.50"]["comparison_context"]["costs"] = "0"*64
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                calibrate_risk(risk_fixture(.5), altered, **kwargs)

    def test_calibration_tie_uses_lower_k_and_does_not_select_return(self):
        # Every candidate has the same risk, despite different mean returns.
        # Lower k is selected even though larger k has far higher final equity.
        candidates = {}
        for i in range(101):
            from decimal import Decimal
            mean = Decimal(i)/1000
            a, b = 1+mean+Decimal(".01"), 1+mean-Decimal(".01")
            candidates[f"{i/100:.2f}"] = fixture(["100", str(100*a), str(100*a*b)])
        r = calibrate_risk(candidates["0.00"], candidates, boundary_date="2024-10-07", end_date="2024-10-09")
        self.assertEqual(r["selected_k"], "0.00")
        self.assertEqual(len(r["candidates"]), 101)

    def test_zero_strategy_risk_does_not_pass(self):
        candidates = {f"{i/100:.2f}": risk_fixture(i/100) for i in range(101)}
        r = calibrate_risk(fixture([100, 100, 100]), candidates, boundary_date="2024-10-07", end_date="2024-10-09")
        self.assertFalse(r["risk_matched"])
        self.assertIsNone(r["selected_k"])

    def test_negative_growth_keeps_negative_calmar(self):
        r = report(fixture([100, 110, 90], dates=["2023-01-01", "2023-07-01", "2024-01-01"]))
        self.assertAlmostEqual(r["cagr_net"]["value"], -.1)
        self.assertAlmostEqual(r["calmar_full_window"]["value"], -.55)

    def test_bridge_requires_opening_boundary_and_preserves_receivables(self):
        from research_base.performance_bridge import from_vibe
        from research_base.reference import low_frequency_engine as reference
        from research_base.vibe_controls import shared_cases
        data = next(d for cid, d, _ in shared_cases() if cid == "P17_final_receivable_not_cash")
        ledger = reference.run(data, "synthetic")
        with self.assertRaises(ContractError):
            from_vibe(data, ledger, opening_date="2024-10-07")
        normalized, result = from_vibe(data, ledger, opening_date="2024-10-04")
        self.assertEqual(normalized["observations"][-1]["equity"], "100")
        self.assertEqual(result["period_net_return"]["value"], 0)
        self.assertEqual(result["initial_equity"], "100")

    def test_wrong_source_flow_mutation_semantically_detected(self):
        original = PACKAGE / "performance.py"
        before = digest(original)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(PACKAGE, root / "research_base", ignore=shutil.ignore_patterns("__pycache__"))
            p = root / "research_base/performance.py"
            text = p.read_text(); anchor = "end_before_flow = equities[i] - flows[i]"
            self.assertEqual(text.count(anchor), 1)
            p.write_text(text.replace(anchor, "end_before_flow = equities[i]"))
            try:
                script = 'import json; from research_base.performance_controls import run_controls; from pathlib import Path; r=run_controls(Path("evidence")); print(json.dumps(r))'
                proc = subprocess.run([sys.executable, "-B", "-c", script], cwd=root, text=True, capture_output=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(proc.stdout)
                bad = next(r for r in result["reports"] if r["id"] == "M03_deposit_is_not_profit")
                self.assertFalse(bad["passed"])
                self.assertTrue(bad["differences"])
            finally:
                self.assertEqual(digest(original), before)


if __name__ == "__main__":
    unittest.main()
