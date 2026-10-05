import copy
from pathlib import Path
import tempfile
import unittest

from research_base.evidence import ContractError
from research_base.trading_terms import TradingTerms, run_controls, control_report_accepted, EXPECTED_CONTROL_IDS


def document():
    evidence = {"reference": "invented rule control", "sha256": "a"*64, "basis": "invented_control"}
    def fee(name, category, basis, rate, minimum="0", sides=None, maximum=None):
        return {"id": name, "category": category, "sides": sides or ["BUY", "SELL"],
                "basis": basis, "rate": rate, "minimum": minimum, "maximum": maximum,
                "quantum": "0.01", "rounding": "half_up"}
    rule = {"id": "before", "from": "2024-01-01", "through": "2024-05-27",
            "known_at": "2023-12-01T00:00:00Z", "evidence": evidence,
            "fees": [fee("commission", "broker", "notional", "0.001", "2"),
                     fee("platform", "broker", "order", "1"),
                     fee("sell_tax", "tax", "notional", "0.01", sides=["SELL"])],
            "execution": {"friction_rate": "0.002", "price_tick": "0.01", "evidence": evidence},
            "settlement": {"cash_sessions": 2, "share_sessions": 2,
                           "reuse_unsettled_proceeds": False, "resell_unsettled_shares": False,
                           "evidence": evidence}}
    later = copy.deepcopy(rule)
    later.update(id="after", **{"from": "2024-05-28", "through": "2024-12-31"})
    later["settlement"].update(cash_sessions=1, share_sessions=1)
    return {"schema_version": "trading-terms/1", "data_kind": "invented_control", "currency": "USD",
            "frozen_at": "2024-10-01T00:00:00Z", "rules": [rule, later]}


class TradingTermsTests(unittest.TestCase):
    def order(self, scenario="C0", side="SELL", **changes):
        args = dict(day="2024-05-24", use_at="2024-05-24T13:30:00Z", side=side,
                    shares=10, raw_open="10", scenario=scenario)
        args.update(changes)
        return TradingTerms(document()).order(**args)

    def test_known_answer_scenarios_and_tax_distinction(self):
        baseline = self.order()
        self.assertEqual((baseline["execution_price"], baseline["fee_total"], baseline["cash_delta"]),
                         ("9.98", "4.00", "95.80"))
        stressed = self.order("C1")
        self.assertEqual((stressed["execution_price"], stressed["fee_total"], stressed["cash_delta"]),
                         ("9.96", "7.00", "92.60"))
        self.assertEqual(stressed["fees"][-1]["amount"], "1.00")
        c2, c3 = self.order("C2"), self.order("C3")
        self.assertEqual((c2["execution_price"], c2["fee_total"], c2["cash_delta"]),
                         ("9.97", "4.00", "95.70"))
        self.assertEqual(c2["extra_entry_exit_fx_rate"], "0.0025")
        self.assertEqual(c2["extra_delay_sessions"], 0)
        self.assertEqual(c3["extra_delay_sessions"], 1)
        self.assertEqual(c3["execution_price"], c2["execution_price"])

    def test_buy_fee_and_away_from_anchor_rounding(self):
        row = self.order(side="BUY", raw_open="10.003")
        self.assertEqual((row["execution_price"], row["fee_total"]), ("10.03", "3"))
        sell = self.order(raw_open="10.003")
        self.assertEqual(sell["execution_price"], "9.98")

    def test_share_fee_maximum_and_noncent_quantum(self):
        data = document()
        component = data["rules"][0]["fees"][0]
        component.update(basis="shares", rate="0.7", maximum="3.1", quantum="0.05", rounding="ceiling")
        model = TradingTerms(data)
        row = model.order(day="2024-05-24", use_at="2024-05-24T13:30:00Z",
                          side="BUY", shares=10, raw_open="10", scenario="C1")
        self.assertEqual(row["fees"][0]["amount"], "6.20")

    def test_effective_settlement_and_calendar_holiday(self):
        model = TradingTerms(document())
        sessions = ["2024-05-24", "2024-05-28", "2024-05-29", "2024-05-30"]
        self.assertEqual(model.settlement_date(trade_date=sessions[0], side="SELL", sessions=sessions,
                                              use_at="2024-05-24T13:30:00Z"), sessions[2])
        self.assertEqual(model.settlement_date(trade_date=sessions[1], side="BUY", sessions=sessions,
                                              use_at="2024-05-28T13:30:00Z"), sessions[2])
        with self.assertRaises(ContractError):
            model.settlement_date(trade_date=sessions[-1], side="BUY", sessions=sessions,
                                  use_at="2024-05-30T13:30:00Z")

    def test_mutations_and_model_snapshot(self):
        changes = [lambda d: d["rules"][0]["fees"][0].update(rate=True),
                   lambda d: d["rules"][0]["fees"][0].update(maximum="1"),
                   lambda d: d["rules"][1].update(**{"from": "2024-05-27"}),
                   lambda d: d["rules"][0]["settlement"].update(cash_sessions=True),
                   lambda d: d["rules"][0]["settlement"].update(reuse_unsettled_proceeds=True),
                   lambda d: d["rules"][0]["execution"].update(friction_rate="NaN"),
                   lambda d: d["rules"][0].update(known_at="2025-01-01T00:00:00Z"),
                   lambda d: d.update(frozen_at="2024-01-01"),
                   lambda d: d.update(ignored_field="unimplemented"),
                   lambda d: d["rules"][1].update(id="before")]
        for mutation in changes:
            data = document()
            mutation(data)
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                TradingTerms(data)
        data = document()
        model = TradingTerms(data)
        identity = model.sha256
        data["rules"][0]["fees"][0]["rate"] = "0.9"
        self.assertEqual(model.sha256, identity)
        row = model.rule("2024-05-24", use_at="2024-05-24T13:30:00Z")
        row["fees"][0]["rate"] = "0.9"
        self.assertEqual(model.rule("2024-05-24", use_at="2024-05-24T13:30:00Z")["fees"][0]["rate"], "0.001")

    def test_late_and_counterfactual_clocks_are_distinct(self):
        data = document()
        data["rules"][0]["known_at"] = "2024-09-01T00:00:00Z"
        with self.assertRaises(ContractError):
            TradingTerms(data).rule("2024-05-24", use_at="2024-05-24T13:30:00Z")
        data["data_kind"] = "reviewed_model"
        for rule in data["rules"]:
            for evidence in [rule["evidence"], rule["execution"]["evidence"], rule["settlement"]["evidence"]]:
                evidence["basis"] = "current_counterfactual"
        row = TradingTerms(data).rule("2024-05-24", use_at="2024-05-24T13:30:00Z")
        self.assertEqual(row["known_at"], "2024-09-01T00:00:00Z")
        data["frozen_at"] = "2024-08-01T00:00:00Z"
        with self.assertRaises(ContractError):
            TradingTerms(data)

    def test_unknown_dates_zero_orders_and_unregistered_scenarios(self):
        for change in [{"shares": 0}, {"shares": True}, {"raw_open": "0"},
                       {"scenario": "cheapest"}, {"day": "2025-01-01"}, {"side": "SHORT"},
                       {"use_at": "2024-05-24"}]:
            with self.subTest(change=change), self.assertRaises(ContractError):
                self.order(**change)

    def test_c1_doubles_rounded_actual_broker_charge(self):
        data = document()
        data["rules"][0]["fees"] = [{"id": "fixed", "category": "broker", "sides": ["BUY", "SELL"],
                                    "basis": "order", "rate": "0.006", "minimum": "0", "maximum": None,
                                    "quantum": "0.01", "rounding": "half_up"}]
        model = TradingTerms(data)
        args = dict(day="2024-05-24", use_at="2024-05-24T13:30:00Z", side="BUY", shares=1, raw_open="10")
        self.assertEqual(model.order(scenario="C0", **args)["fee_total"], "0.01")
        self.assertEqual(model.order(scenario="C1", **args)["fee_total"], "0.02")

    def test_iso_aliases_and_incompatible_fee_cap_rejected(self):
        model = TradingTerms(document())
        with self.assertRaises(ContractError):
            model.settlement_date(trade_date="2024-05-28", side="BUY", use_at="2024-05-28T13:30:00Z",
                                  sessions=["2024-05-28", "20240528", "20240529"])
        for field, amount in [("maximum", "3.11"), ("minimum", "0.006")]:
            data = document()
            data["rules"][0]["fees"][0].update(**{field: amount, "quantum": "0.05", "rounding": "ceiling"})
            with self.subTest(field=field), self.assertRaises(ContractError):
                TradingTerms(data)

    def test_fixed_report_rejects_forged_and_incomplete_answers(self):
        with tempfile.TemporaryDirectory() as folder:
            report = run_controls(Path(folder) / "controls")
            self.assertTrue(control_report_accepted(report))
            self.assertEqual(len(report["reports"]), len(EXPECTED_CONTROL_IDS))
        for mutation in [lambda r: r["reports"].pop(),
                         lambda r: r["reports"].append(copy.deepcopy(r["reports"][0])),
                         lambda r: r["reports"][0].update(actual="false", expected="false", passed=True),
                         lambda r: r.update(accepted=False)]:
            changed = copy.deepcopy(report)
            mutation(changed)
            self.assertFalse(control_report_accepted(changed))
