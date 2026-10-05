"""Known callback instruments; these numbers are not market conclusions."""
import copy
from decimal import Decimal
import unittest

from research_base.evidence import ContractError, canonical_hash
from research_base.historical_friction import scan_friction, DEFAULT_GRID_BP, VERSION
from research_base.trading_terms import control_document


def instrument():
    terms = control_document()
    terms["rules"][0].update(**{"from":"2026-01-01","through":"2026-01-05"})
    terms["rules"][1].update(**{"from":"2026-01-06","through":"2026-01-07"})
    context = {"schema_version": VERSION, "capital_cny": "100", "frozen_k": "0.50",
        "calendar": ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"],
        "comparison_context": {k:"0"*64 for k in ("data", "account", "costs", "execution", "fx")},
        "terms": terms, "scenario": "C0", "fixed_expenses": [], "expense_review": {"schema_version":"fixed-expense-review/1","reviewer":"invented instrument author", "scope":"literal zero schedule",
            "reviewed_at":"2024-10-01T00:00:00Z", "coverage_start_date":"2026-01-01", "coverage_end_date":"2026-01-07", "explicit_zero":True,
            "schedule_sha256":canonical_hash([]), "evidence":{"reference":"invented control", "sha256":"0"*64, "known_at":"2024-10-01T00:00:00Z", "classification":"synthetic_control"}},
        "data_kind": "invented_control"}
    context["comparison_context"]["costs"] = canonical_hash({k:context[k] for k in ("terms", "scenario", "fixed_expenses", "expense_review")})
    windows = {"validation": {"boundary_date":"2026-01-02", "end":"2026-01-05"},
               "confirmation": {"boundary_date":"2026-01-05", "end":"2026-01-07"}}
    return context, windows


def known_callback(context, growth, visits=None, mutate=None):
    def run(bp):
        if visits is not None: visits.append(bp)
        terms = copy.deepcopy(context["terms"])
        for rule in terms["rules"]:
            rule["execution"]["friction_rate"] = str(Decimal(rule["execution"]["friction_rate"])+Decimal(bp)/10000)
        scopes = copy.deepcopy(context["comparison_context"])
        scopes["costs"] = canonical_hash({"terms":terms, **{k:context[k] for k in ("scenario", "fixed_expenses", "expense_review")}})
        accounts = {}
        for variant in ("S", "BR"):
            multiplier = Decimal(growth[bp]) if variant == "S" else Decimal(1)
            navs = [Decimal(100), Decimal(100)]
            for _ in range(3): navs.append(navs[-1]*multiplier)
            # Literal quantity jump at2bp: path changes, even though the same
            # full calendar/capital/intent k and comparison identities remain.
            qty = "2" if Decimal(bp) < 2 else "1"
            fills = [{"id":"fill-1", "date":context["calendar"][2], "symbol":"TEST", "signed_quantity":qty,
                      "price":"10", "commission":"0", "platform":"0", "tax":"0", "fee":"0"}]
            ledger = {"schema_version":"1", "currency":"CNY", "sampling":"daily_session_close", "calendar":context["calendar"],
                "calendar_source":"invented accounting instrument", "periods_per_year":252, "year_day_basis":365,
                "flow_policy":"end_boundary_only", "observations":[{"date":d,"equity":str(n),"external_flow_end":"0"} for d,n in zip(context["calendar"],navs)],
                "risk_free":{"source":"same explicit zero instrument", "currency":"CNY", "period_returns":[{"date":d,"return":"0"} for d in context["calendar"][1:]]},
                "fills":fills, "cost_events":[], "comparison_context":scopes}
            accounts[variant] = {"ledger":ledger, "reconciliation":{"passed":True,"errors":[],
                "source_trial_ref":"invented-known-answer/"+bp+"/"+variant,"account_sha256":"1"*64,"replay_sha256":"2"*64}}
        result = {"extra_per_side_bp":bp,"frozen_k":context["frozen_k"],"terms":terms,"accounts":accounts}
        if mutate: mutate(bp,result)
        return result
    return run


class HistoricalFrictionTests(unittest.TestCase):
    def setUp(self):
        self.context,self.windows = instrument()
        self.growth = {"0":"1.1","1":"1.05","2":"0.99","5":"1.02","10":"0.98","20":"1","50":"0.95","100":"0.9"}

    def scan(self, callback=None, **kwargs):
        return scan_friction(callback or known_callback(self.context,self.growth), context=self.context, windows=self.windows, **kwargs)

    def test_known_nonmonotone_grid_share_jump_and_reentry(self):
        visits=[]
        result=self.scan(known_callback(self.context,self.growth,visits))
        self.assertEqual(visits,list(DEFAULT_GRID_BP))
        self.assertEqual(result["validated_trial_count"],8)
        self.assertEqual(result["trials"][2]["fill_quantity_path_changed_vs_baseline"], {"S":True,"BR":True})
        for name in ("full","validation","confirmation"):
            threshold=result["thresholds"][name]
            self.assertEqual(threshold["first_observed_nonpositive_interval"]["lower_bp"],"1")
            self.assertEqual(threshold["first_observed_nonpositive_interval"]["upper_bp"],"2")
            self.assertEqual([(i["lower_bp"],i["upper_bp"]) for i in threshold["reentry_positive_intervals"]],[("2","5")])
            self.assertEqual(threshold["zero_candidates_bp"],["20"])
            self.assertFalse(threshold["observed_nonincreasing"])
            self.assertFalse(threshold["global_unique_root_claim"])
        # Three periods of1.1 vs1, so ln gain3*ln1.1, not additive0.3.
        full=Decimal(result["trials"][0]["gains"]["full"]["log_gain"])
        self.assertAlmostEqual(float(full),0.285930539413,places=11)
        self.assertFalse(result["baseline_linear_approximation"]["full"]["formal_threshold"])

    def test_only_one_frozen_window_run_per_candidate_no_retuning(self):
        visits=[]
        result=self.scan(known_callback(self.context,self.growth,visits))
        self.assertEqual(len(visits),8)
        self.assertFalse(result["retuned_k"])
        self.assertEqual(result["frozen_k"],"0.50")
        self.assertEqual(result["trials"][0]["gains"]["validation"]["status"],"DEFINED")

    def test_missing_calendar_or_wrong_capital_is_instrument_error(self):
        for key in ("calendar","capital"):
            def mutate(bp,trial):
                if bp=="2":
                    ledger=trial["accounts"]["S"]["ledger"]
                    if key=="calendar": ledger["calendar"]=ledger["calendar"][:-1]
                    else: ledger["observations"][0]["equity"]="101"
            result=self.scan(known_callback(self.context,self.growth,mutate=mutate))
            self.assertEqual(result["trials"][2]["status"],"TRIAL_ERROR")
            self.assertFalse(result["trials"][2]["economic_failure"])
            self.assertFalse(result["all_trials_validated"])
            self.assertIsNone(result["thresholds"]["full"]["first_observed_nonpositive_interval"])

    def test_frozen_k_and_other_model_changes_rejected(self):
        mutations=[lambda t:t.update(frozen_k="0.51"),
                   lambda t:t["terms"]["rules"][0]["fees"][0].update(minimum="999"),
                   lambda t:t["accounts"]["S"]["ledger"]["comparison_context"].update(fx="f"*64)]
        for mutation in mutations:
            result=self.scan(known_callback(self.context,self.growth,mutate=lambda bp,t:mutation(t) if bp=="1" else None))
            self.assertEqual(result["trials"][1]["status"],"TRIAL_ERROR")

    def test_wrong_added_bp_and_false_reconciliation_rejected(self):
        def mutate(bp,trial):
            if bp=="5":trial["terms"]["rules"][0]["execution"]["friction_rate"]="0"
            if bp=="10":trial["accounts"]["BR"]["reconciliation"]["passed"]=1
        result=self.scan(known_callback(self.context,self.growth,mutate=mutate))
        self.assertEqual(result["trials"][3]["status"],"TRIAL_ERROR")
        self.assertEqual(result["trials"][4]["status"],"TRIAL_ERROR")

    def test_bankruptcy_is_not_a_reset_or_finite_log_root(self):
        growth=copy.deepcopy(self.growth);growth["2"]="0"
        result=self.scan(known_callback(self.context,growth))
        row=result["trials"][2]
        self.assertEqual(row["status"],"VALIDATED_FULL_ACCOUNT_PAIR")
        for name in ("full","validation","confirmation"):
            self.assertEqual(row["gains"][name]["status"],"UNDEFINED_NONPOSITIVE_EQUITY")
            self.assertIsNone(row["gains"][name]["log_gain"])
        self.assertIsNone(result["thresholds"]["full"]["first_observed_nonpositive_interval"])

    def test_zero_baseline_and_unreached_upper_bound(self):
        equal={bp:"1" for bp in DEFAULT_GRID_BP}
        result=self.scan(known_callback(self.context,equal))
        self.assertEqual(result["thresholds"]["full"]["status"],"BASELINE_NOT_POSITIVE")
        self.assertIsNone(result["thresholds"]["full"]["first_observed_nonpositive_interval"])
        positive={bp:"1.01" for bp in DEFAULT_GRID_BP}
        result=self.scan(known_callback(self.context,positive))
        self.assertEqual(result["thresholds"]["full"]["status"],"NOT_FOUND_ON_SCANNED_GRID")
        self.assertIsNone(result["thresholds"]["full"]["first_observed_nonpositive_interval"])

    def test_cash_background_and_external_capital_reset_rejected(self):
        def mutate(bp,trial):
            if bp=="1":
                for account in trial["accounts"].values(): account["ledger"]["risk_free"]["period_returns"][0]["return"]="0.01"
            if bp=="2":trial["accounts"]["S"]["ledger"]["observations"][2]["external_flow_end"]="10"
        result=self.scan(known_callback(self.context,self.growth,mutate=mutate))
        self.assertEqual(result["trials"][1]["status"],"TRIAL_ERROR")
        self.assertEqual(result["trials"][2]["status"],"TRIAL_ERROR")

    def test_callback_failures_keep_fixed_denominator(self):
        callback=known_callback(self.context,self.growth);visits=[]
        def broken(bp):
            visits.append(bp)
            if bp=="2":raise RuntimeError("instrument stopped")
            return callback(bp)
        result=self.scan(broken)
        self.assertEqual(visits,list(DEFAULT_GRID_BP))
        self.assertEqual(result["intended_trial_count"],8)
        self.assertEqual(result["attempted_trial_count"],8)
        self.assertEqual(result["validated_trial_count"],7)
        self.assertEqual(result["trials"][2]["status"],"TRIAL_ERROR")

    def test_exact_sign_has_no_implicit_economic_zero_band(self):
        equal={bp:"1" for bp in DEFAULT_GRID_BP}
        for terminal, expected in [("100.0000000000000000000000001","POSITIVE"),
                                   ("99.9999999999999999999999999","NEGATIVE")]:
            def mutate(bp,trial):
                trial["accounts"]["S"]["ledger"]["observations"][-1]["equity"]=terminal
            result=self.scan(known_callback(self.context,equal,mutate=mutate))
            self.assertEqual(result["trials"][0]["gains"]["full"]["sign"],expected)
            self.assertEqual(result["thresholds"]["full"]["zero_candidates_bp"],[])
            self.assertEqual(result["log_zero_tolerance"],"0")
        # This input difference is below the registered50digit log precision;
        # a displayed0 cannot overwrite the exact rational economic sign.
        def tiny(bp,trial):
            trial["accounts"]["S"]["ledger"]["observations"][-1]["equity"]="100."+"0"*80+"1"
        result=self.scan(known_callback(self.context,equal,mutate=tiny))
        gain=result["trials"][0]["gains"]["full"]
        self.assertEqual(gain["sign"],"POSITIVE")
        self.assertTrue(gain["log_gain_rounded_to_zero"])

    def test_linear_notional_count_disclosure_is_not_scanned_threshold(self):
        def turnover(bp,trial):
            trial["accounts"]["S"]["ledger"]["fills"][0]["signed_quantity"]="10"
            trial["accounts"]["BR"]["ledger"]["fills"][0]["signed_quantity"]="1"
        result=self.scan(known_callback(self.context,self.growth,mutate=turnover))
        approximation=result["baseline_linear_approximation"]["full"]
        self.assertEqual(approximation["variants"]["S"]["fill_count"],1)
        self.assertEqual(Decimal(approximation["variants"]["S"]["one_side_notional_cny"]),Decimal("100"))
        self.assertEqual(Decimal(approximation["variants"]["S"]["one_bp_average_cost_per_fill_cny"]),Decimal("0.01"))
        self.assertEqual(Decimal(approximation["variants"]["BR"]["one_side_notional_cny"]),Decimal("10"))
        self.assertGreater(Decimal(approximation["linear_approximation_bp"]),Decimal("100"))
        self.assertFalse(approximation["formal_threshold"])
        self.assertEqual(result["thresholds"]["full"]["first_observed_nonpositive_interval"]["upper_bp"],"2")

    def test_invalid_grid_scope_and_period_reset_rejected_before_callback(self):
        for grid in [("1","2"),("0","1","1"),("0","2","1")]:
            with self.assertRaises(ContractError):self.scan(grid_bp=grid)
        context=copy.deepcopy(self.context);context["scenario"]="C1"
        with self.assertRaises(ContractError):scan_friction(lambda bp:None,context=context,windows=self.windows)
        windows=copy.deepcopy(self.windows);windows["confirmation"]["boundary_date"]="2026-01-06"
        with self.assertRaises(ContractError):scan_friction(lambda bp:None,context=self.context,windows=windows)
