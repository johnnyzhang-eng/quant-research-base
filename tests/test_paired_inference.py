import copy
import importlib.util
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

from research_base.evidence import ContractError, digest
from research_base.inference_controls import calculate, declared_sessions, monthly, months, run_controls
from research_base.paired_inference import block_indices, bootstrap, paired_monthly, quantile

PACKAGE = Path(__file__).resolve().parents[1]/"research_base"


class PairedTests(unittest.TestCase):
    def test_literal_controls(self):
        with tempfile.TemporaryDirectory() as t:
            r = run_controls(Path(t))
        self.assertTrue(r["accepted"],r)
        self.assertEqual(r["cases_passed"],6)

    def test_rational_wealth_point_not_arithmetic_average(self):
        r=calculate(monthly([".1","-.1","0"]),monthly(["0"]*3))
        # Whole-wealth ratio99/100 over3 months;4*log(99/100).
        self.assertAlmostEqual(r["annual_log_growth_difference"],4*math.log(float(Fraction(99,100))),places=14)
        self.assertEqual(r["same_window_drawdown"]["strategy"],.1)
        self.assertEqual(r["interval"]["status"],"NOT_RUN")

    def test_quantile_literal_type7(self):
        self.assertEqual(quantile([Decimal(0),Decimal(10),Decimal(20),Decimal(30)],Decimal(".05")),Decimal("1.5"))
        self.assertEqual(quantile([Decimal(0),Decimal(10),Decimal(20),Decimal(30)],Decimal(".95")),Decimal("28.5"))

    def test_circular_tail_and_fraction_replay_of_all_draws(self):
        indices=block_indices([60,0,12,24,36,48],61)
        self.assertEqual(indices[:13],[60,0,1,2,3,4,5,6,7,8,9,10,0])
        self.assertEqual(indices[-1],48);self.assertEqual(len(indices),61)
        a=bootstrap([Decimal(i)/100 for i in range(61)])
        self.assertEqual((a["seed"],a["replicates"],a["block_length"]),(20261003,2000,12))
        oracle=[]
        for starts in a["block_starts"]:
            self.assertEqual(len(starts),6)
            # Fraction arithmetic, explicit61 terms; no production sum/quantile helper.
            total=Fraction(0)
            for offset in range(61):
                total+=Fraction((starts[offset//12]+offset%12)%61,100)
            oracle.append(12*total/61)
        for exact,actual in zip(oracle,a["annual_log_growth_draws_decimal"]):
            self.assertAlmostEqual(float(exact),float(actual),places=13)
        sorted_oracle=sorted(oracle)
        lower=sorted_oracle[99]+Fraction(19,20)*(sorted_oracle[100]-sorted_oracle[99])
        upper=sorted_oracle[1899]+Fraction(1,20)*(sorted_oracle[1900]-sorted_oracle[1899])
        self.assertAlmostEqual(a["lower"],float(lower),places=13)
        self.assertAlmostEqual(a["upper"],float(upper),places=13)
        self.assertEqual(a["block_starts_sha256"],bootstrap([Decimal(0)]*61)["block_starts_sha256"])

    def test_currency_capital_context_and_calendar_source_must_match(self):
        for kind in ("currency","capital","context","source"):
            s,b=monthly([0]*60),monthly([0]*60)
            if kind=="currency":b["currency"]="CNY"
            elif kind=="capital":b["observations"][0]["equity"]="200"
            elif kind=="context":b["comparison_context"]["execution"]="f"*64
            else:b["calendar_source"]="another source"
            with self.subTest(kind=kind),self.assertRaises(ContractError):calculate(s,b)

    def test_shared_missing_month_still_rejected(self):
        s,b=monthly([0]*60),monthly([0]*60);sessions=declared_sessions(s)
        for d in (s,b):d["calendar"].pop(5);d["observations"].pop(5)
        with self.assertRaises(ContractError):
            paired_monthly(s,b,boundary_date=s["calendar"][0],end_date=s["calendar"][-1],
                           month_ends=s["calendar"][1:],session_calendar=sessions)

    def test_partial_month_and_incomplete_session_tail_rejected(self):
        s,b=monthly([0]*60),monthly([0]*60);sessions=declared_sessions(s)
        for d in (s,b):
            d["calendar"][-1]="2023-12-15";d["observations"][-1]["date"]="2023-12-15"
        with self.assertRaises(ContractError):
            paired_monthly(s,b,boundary_date=s["calendar"][0],end_date=s["calendar"][-1],
                           month_ends=s["calendar"][1:],session_calendar=sessions)
        s,b=monthly([0]*60),monthly([0]*60)
        with self.assertRaises(ContractError):
            paired_monthly(s,b,boundary_date=s["calendar"][0],end_date=s["calendar"][-1],
                           month_ends=s["calendar"][1:],session_calendar=sessions[:-1])

    def test_shared_missing_daily_valuation_rejected(self):
        from research_base.inference_controls import cases
        _,s,b,_=cases()[-1];sessions=declared_sessions(s)
        for d in (s,b):d["calendar"].pop(1);d["observations"].pop(1)
        with self.assertRaises(ContractError):
            paired_monthly(s,b,boundary_date=s["calendar"][0],end_date=s["calendar"][-1],
                           month_ends=months(60)[1:],session_calendar=sessions)

    def test_zero_net_flow_cannot_hide_contributions_and_withdrawals(self):
        s,b=monthly([0]*60),monthly([0]*60)
        b["observations"][1]["external_flow_end"]="10";b["observations"][2]["external_flow_end"]="-10"
        with self.assertRaises(ContractError):calculate(s,b)

    def test_insolvency_before_window_not_restarted(self):
        s,b=monthly([0]*3+[-1]+[0]*68),monthly([0]*72)
        r=paired_monthly(s,b,boundary_date="2019-12-31",end_date="2024-12-31",
                         month_ends=s["calendar"][13:],session_calendar=declared_sessions(s))
        self.assertEqual(r["paired_months"],60);self.assertEqual(len(r["monthly_pairs"]),60)
        self.assertLess(r["insolvencies"]["strategy"][0]["date"],r["boundary_date"])
        self.assertEqual(r["valid_log_months"],0);self.assertIsNone(r["annual_log_growth_difference"])

    def test_future_valuations_do_not_change_frozen_window(self):
        s,b=monthly([".01"]*72),monthly([0]*72)
        a=calculate(s,b,n=60)
        changed=copy.deepcopy(s)
        for o in changed["observations"][61:]:o["equity"]=str(Decimal(o["equity"])*2)
        c=calculate(changed,b,n=60)
        for key in ("monthly_pairs","annual_log_growth_difference_decimal","same_window_drawdown","interval"):
            self.assertEqual(a[key],c[key])
        self.assertNotEqual(a["source_ledger_sha256"],c["source_ledger_sha256"])

    def test_wrong_source_adding_benchmark_produces_semantic_mismatch(self):
        original=PACKAGE/"paired_inference.py";before=digest(original)
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);shutil.copytree(PACKAGE,root/"research_base",ignore=shutil.ignore_patterns("__pycache__"))
            p=root/"research_base/paired_inference.py";body=p.read_text()
            anchor="difference = (1 + sr).ln() - (1 + br).ln()"
            self.assertEqual(body.count(anchor),1)
            p.write_text(body.replace(anchor,"difference = (1 + sr).ln() + (1 + br).ln()"))
            try:
                code='import json;from pathlib import Path;from research_base.inference_controls import run_controls;print(json.dumps(run_controls(Path("evidence"))))'
                proc=subprocess.run([sys.executable,"-B","-c",code],cwd=root,text=True,capture_output=True,timeout=30)
                self.assertEqual(proc.returncode,0,proc.stderr)
                r=json.loads(proc.stdout)
                case=next(c for c in r["reports"] if c["id"]=="I01_identical_paired_zero")
                self.assertFalse(case["passed"]);self.assertTrue(case["differences"])
            finally:self.assertEqual(digest(original),before)


@unittest.skipUnless(importlib.util.find_spec("backtest") is not None,"Optional installed Vibe environment required")
class ActualPairedBridge(unittest.TestCase):
    def test_actual_vibe_accounts_reach_paired_api_without_promoting_one_month(self):
        from backtest.engines.global_equity import GlobalEquityEngine
        from research_base.path_controls import execute_case,full_fixture
        from research_base.performance_bridge import from_vibe
        from research_base.vibe_controls import offline_connections
        data=full_fixture();ledgers=[]
        with offline_connections():
            for variant,k in (("S",None),("BR",".50")):
                prepared,_,account,_,replay=execute_case(data,variant,k,GlobalEquityEngine)
                self.assertTrue(replay["passed"])
                ledger,_=from_vibe(prepared,account,opening_date=data["performance_boundary_date"])
                ledgers.append(ledger)
        result=paired_monthly(*ledgers,boundary_date="2023-10-31",end_date="2023-11-30",
                              month_ends=["2023-11-30"],session_calendar=[data["performance_boundary_date"],*data["calendar"]["sessions"]])
        self.assertEqual(result["paired_months"],1);self.assertEqual(result["annual_log_growth_difference"],0)
        self.assertFalse(result["sample_sufficient"]);self.assertEqual(result["interval"]["status"],"NOT_RUN")
