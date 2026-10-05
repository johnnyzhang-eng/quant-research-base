import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research_base import cash_account as cash
from research_base import historical_contract as account
from research_base import historical_signals as signals
from research_base.evidence import ContractError, canonical_hash
from research_base.reference.low_frequency_engine import validate, ContractError as OldError


def model_for(artifact, scenario="C0", **kwargs):
    return account.control_model(artifact, scenario, **kwargs)

class HistoricalAccountInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifact = signals.write_control_fixture(Path(self.temp.name)/"fixture")
        self.model, self.features = model_for(self.artifact)

    def prepare(self, model=None, artifact=None, features=None):
        return account.prepare(artifact or self.artifact, features or self.features, model or self.model)

    def test_modelled_payment_posting_is_explicit_and_cannot_be_claimed_observed(self):
        for mode in ("absent", "observed", "timezone", "aware_local_time"):
            model = copy.deepcopy(self.model)
            if mode == "absent": model.pop("dividend_posting")
            if mode == "observed": model["dividend_posting"]["basis"] = "observed_receipt"
            if mode == "timezone": model["dividend_posting"]["timezone"] = "Unknown/Place"
            if mode == "aware_local_time": model["dividend_posting"]["local_time"] = "16:30:00+00:00"
            with self.assertRaises(ContractError, msg=mode): self.prepare(model=model)

    def test_known_cash_entry_and_boundary_interest(self):
        data = self.prepare()
        self.assertEqual("invented_control", data["data_kind"])
        self.assertEqual("conditional_account_model", data["classification"])
        self.assertEqual("1000.00", data["initial"]["settled_cash"])
        self.assertEqual("923.80", data["cash_entry_balances"]["CNY"]["settled"])
        self.assertEqual("8000", data["opening_cny_snapshot"]["marked_equity_cny"])
        accrued = [e for e in data["cash_entry_journal"] if e["type"] == "CASH_INTEREST" and e["currency"] == "CNY"]
        self.assertEqual(["0.80"], [e["credited"] for e in accrued])
        self.assertEqual(["2020-09-30"], [e["day"] for e in accrued])
        self.assertNotIn("fees", data)
        self.assertNotIn("settlement", data)
        self.assertEqual(canonical_hash(self.artifact), data["lineage"]["artifact_sha256"])
        account.validate_account_input(data)
        with self.assertRaisesRegex(OldError, "refuses market/history"):
            validate(data, "synthetic")

    def test_c3_actual_execution_dates_and_fx_stress(self):
        model, features = model_for(self.artifact, "C3")
        data = self.prepare(model=model, features=features)
        self.assertEqual("997.50", data["initial"]["settled_cash"])
        self.assertEqual("2020-10-02", data["control_intents"][0]["execution_date"])
        self.assertEqual("2020-11-03", data["control_intents"][1]["execution_date"])
        fx = next(e for e in data["cash_entry_journal"] if e["type"] == "FX_EXECUTION")
        self.assertEqual("0.0025", fx["extra_spread_rate"])
        mismatch = copy.deepcopy(model)
        mismatch["scenario"] = "C2"
        with self.assertRaisesRegex(ContractError, "delay"):
            self.prepare(model=mismatch, features=features)

    def test_prepared_mutations_cannot_self_update_hash_to_pass(self):
        original = self.prepare()
        for mode in ("capacity", "cash", "intent", "lineage", "extra", "clock"):
            data = copy.deepcopy(original)
            if mode == "capacity": data["bars"][0]["open_capacity_shares"] = 999999
            if mode == "cash": data["initial"]["settled_cash"] = "999999"
            if mode == "intent": data["control_intents"][0]["execution_date"] = "2020-10-05"
            if mode == "lineage": data["lineage"]["model_sha256"] = "f"*64
            if mode == "extra": data["fees"] = {"commission_rate": "0"}
            if mode == "clock": data["bars"][0]["available_at"] = "2020-10-01T00:00:00Z"
            with self.assertRaisesRegex(ContractError, "differs"):
                account.validate_account_input(data)

    def test_no_opening_default_capacity_or_fee_interest_defaults(self):
        for mode in ("missing_open", "missing_capacity", "negative_capacity", "infinite_capacity", "float_capacity", "late_quote", "missing_fees", "missing_interest"):
            model = copy.deepcopy(self.model)
            if mode == "missing_open": model["execution"]["rows"].pop()
            if mode == "missing_capacity": model["execution"]["rows"][0].pop("open_capacity_shares")
            if mode == "negative_capacity": model["execution"]["rows"][0]["open_capacity_shares"] = -1
            if mode == "infinite_capacity": model["execution"]["rows"][0]["open_capacity_shares"] = "Infinity"
            if mode == "float_capacity": model["execution"]["rows"][0]["open_capacity_shares"] = 1.0
            if mode == "late_quote": model["execution"]["rows"][0]["open_quote_known_at"] = "2020-10-01T14:31:00Z"
            if mode == "missing_fees": model["terms"]["rules"][0]["fees"] = []
            if mode == "missing_interest": model["cash_rules"]["interest_schedules"] = []
            with self.assertRaises(ContractError, msg=mode): self.prepare(model=model)
        model = copy.deepcopy(self.model)
        model["execution"]["rows"][0].update(open_status="unknown", open_capacity_shares=None, open_quote_known_at="2020-10-01T21:05:00Z")
        data = self.prepare(model=model)
        self.assertIsNone(data["bars"][0]["open_capacity_shares"])
        self.assertEqual("unknown", data["bars"][0]["open_status"])

    def test_actual_prior_seed_and_marks_no_free_stocks(self):
        for mode in ("seed_value", "seed_size", "free_stocks", "late_seed", "wrong_mark", "binary_mark"):
            model, artifact = copy.deepcopy(self.model), copy.deepcopy(self.artifact)
            if mode == "seed_value": model["seed_volume_history"]["SPY"][0] = 999999
            if mode == "seed_size": model["seed_volume_history"]["SPY"].pop()
            if mode == "free_stocks": model["initial"]["holdings"]["SPY"] = 1
            if mode == "wrong_mark": model["initial"]["marks"]["SPY"] = "101"
            if mode == "binary_mark": model["initial"]["marks"]["SPY"] = 100.0
            if mode == "late_seed":
                for bar in artifact["bars"]:
                    if bar["trade_date"] == "2020-09-30": bar["available_at"] = "2020-10-01T14:30:00Z"
            features = signals.generate(artifact, "B0", trade_start=model["start"], end_date=model["end"])
            with self.assertRaises(ContractError, msg=mode): self.prepare(model=model, artifact=artifact, features=features)

    def test_scope_and_feature_provenance_not_silently_changed(self):
        for mode in ("start", "end", "delay", "target", "lineage", "classification"):
            model, features = copy.deepcopy(self.model), copy.deepcopy(self.features)
            if mode == "start": model["start"] = "2020-10-02"
            if mode == "end": model["end"] = "2020-11-02"
            if mode == "delay": features["delay_sessions"] = 1
            if mode == "target": features["intents"][0]["weights"]["SPY"] = "1"
            if mode == "lineage": features["normalized_artifact_sha256"] = "f"*64
            if mode == "classification": features["classification"] = "FORMAL_HISTORY_READY"
            with self.assertRaises(ContractError, msg=mode): self.prepare(model=model, features=features)

    def test_calendar_must_cover_dynamic_settlement_horizon(self):
        artifact = copy.deepcopy(self.artifact)
        artifact["calendar"] = [r for r in artifact["calendar"] if r["trade_date"] <= "2021-01-01"]
        artifact["bars"] = [r for r in artifact["bars"] if r["trade_date"] <= "2021-01-01"]
        model, features = model_for(artifact, start="2020-10-01", end="2020-12-31")
        # Invented calendar: Dec31 + two sessions needs Jan04, explicitly absent.
        with self.assertRaisesRegex(ContractError, "settlement lag"):
            self.prepare(model=model, features=features, artifact=artifact)

    def test_fx_and_performance_clocks_and_boundary(self):
        for mode in ("overfund", "entry_too_early", "entry_too_late", "wrong_boundary", "future_boundary_price", "missing_exit", "early_cutoff", "cash_start"):
            model = copy.deepcopy(self.model)
            if mode == "overfund": model["fx"]["entry"]["principal_cny"] = "8000"
            if mode == "entry_too_early": model["fx"]["entry"]["at"] = "2020-09-30T20:00:00Z"
            if mode == "entry_too_late": model["fx"]["entry"]["at"] = "2020-10-01T14:31:00Z"
            if mode == "wrong_boundary": model["performance_boundary"]["date"] = "2020-09-29"
            if mode == "future_boundary_price": model["performance_boundary"]["fx_mark"]["quoted_at"] = "2020-10-01T21:00:00Z"
            if mode == "missing_exit": model["fx"]["exit_quotes"].pop("2020-10-01")
            if mode == "early_cutoff": model["cash_rules"]["accrual_cutoff"] = "20:00:00"
            if mode == "cash_start": model["cash_rules"]["start_at"] = "2020-09-29T00:00:00+00:00"
            with self.assertRaises(ContractError, msg=mode): self.prepare(model=model)

    def test_late_close_remains_late_and_declares_carry_known_mark_policy(self):
        artifact = copy.deepcopy(self.artifact)
        for bar in artifact["bars"]:
            if bar["trade_date"] == "2020-10-01": bar["available_at"] = "2020-10-02T14:29:00Z"
        model, features = model_for(artifact)
        data = self.prepare(model=model, artifact=artifact, features=features)
        self.assertEqual("carry_last_known_close_at_cash_cutoff", data["valuation_policy"])
        self.assertEqual("2020-10-02T14:29:00Z", data["bars"][0]["available_at"])
        self.assertEqual("2020-10-01T21:00:00Z", data["bars"][0]["close_at"])

    def test_counterfactual_knowledge_does_not_allow_future_fx_price(self):
        model = copy.deepcopy(self.model)
        model["cash_rules"]["knowledge_clock"]["basis"] = "frozen_current_counterfactual"
        quote = model["fx"]["close_marks"]["2020-10-01"]
        quote.update(quoted_at="2021-01-01T21:00:00+00:00", known_at="2021-01-01T21:00:00+00:00")
        with self.assertRaisesRegex(ContractError, "future FX price"):
            self.prepare(model=model)
        quote["quoted_at"] = "2020-10-01T21:00:00+00:00"
        quote["evidence"]["known_at"] = "2021-01-01T21:00:00+00:00"
        # Original late retrieval remains explicit under the frozen model clock.
        data = self.prepare(model=model)
        self.assertEqual("2021-01-01T21:00:00+00:00", data["historical_model"]["fx"]["close_marks"]["2020-10-01"]["known_at"])

    def test_negative_boundary_interest_can_block_entry(self):
        model = copy.deepcopy(self.model)
        for rule in model["cash_rules"]["interest_schedules"]:
            if rule["currency"] == "CNY": rule["tiers"][0]["net_annual_rate"] = "-36.5"
        # One boundary day charges 800; principal 7500 + fee 7 exceeds 7200.
        model["fx"]["entry"]["principal_cny"] = "7500"
        with self.assertRaisesRegex(ContractError, "insufficient"):
            self.prepare(model=model)

    def test_explicit_net_dividend_and_late_action_gate(self):
        artifact = copy.deepcopy(self.artifact)
        action = {"id": "div-account", "symbol": "SPY", "source_id": "CONTROL", "type": "dividend",
            "effective_date": "2020-10-15", "pay_date": "2020-10-16", "gross_per_share": "1", "share_basis": "post_split", "currency": "USD",
            "available_at": "2020-10-14T21:05:00Z", "availability_basis": "modelled", "revision_status": "unknown"}
        artifact["actions"] = [action]
        model, features = model_for(artifact)
        model["withholding"] = {"div-account": "0.15"}
        data = self.prepare(model=model, artifact=artifact, features=features)
        self.assertEqual("0.15", data["actions"][0]["withholding_rate"])
        self.assertEqual("2020-10-14T21:05:00Z", data["actions"][0]["known_at"])
        weekend = copy.deepcopy(artifact)
        weekend["actions"][0]["pay_date"] = "2020-10-17"
        weekend_features = signals.generate(weekend, "B0", trade_start=model["start"], end_date=model["end"])
        weekend_data = self.prepare(model=model, artifact=weekend, features=weekend_features)
        self.assertEqual("2020-10-17T16:30:00-04:00", weekend_data["actions"][0]["payment_at"])
        self.assertEqual("modelled_at_declared_cutoff", weekend_data["actions"][0]["payment_basis"])
        model["withholding"] = {}
        with self.assertRaises(ContractError): self.prepare(model=model, artifact=artifact, features=features)
        model["withholding"] = {"div-account": "0.15"}
        artifact["actions"][0]["available_at"] = "2020-10-15T21:05:00Z"
        features = signals.generate(artifact, "B0", trade_start=model["start"], end_date=model["end"])
        with self.assertRaisesRegex(ContractError, "late effective"):
            self.prepare(model=model, artifact=artifact, features=features)


if __name__ == "__main__":
    unittest.main()
