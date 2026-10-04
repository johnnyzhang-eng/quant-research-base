import copy
import unittest

from research_base.historical_readiness import assess, case_evidence


def report(n, causal=False):
    d = {"accepted": True, "reports": [{"id": f"C{i}", "passed": True, "differences": []} for i in range(n)]}
    if causal:
        for row in d["reports"]:
            row["replay"] = {"passed": True, "errors": []}
        d["causal_controls"] = [{"mutation": m, "passed": True} for m in ("truncate", "future_price_action")]
    return d


class ReadinessTests(unittest.TestCase):
    def evaluate(self, audit=None, engine=None, live=None):
        return assess(audit, report(39), engine, report(12), report(11, True), live,
                      {"costs": {"status": "unknown"}})

    def test_missing_is_not_zero_failures_or_permission(self):
        r = self.evaluate()
        self.assertEqual([g["id"] for g in r["gates"]], [f"D{i:02}" for i in range(1, 7)])
        self.assertIsNone(r["controls"]["protocol_vibe"]["passed"])
        self.assertFalse(r["historical_run_permitted"])
        self.assertFalse(r["gates"][0]["current_evidence"]["license_grant_verified_by_this_run"])

    def test_summary_label_and_duplicate_cases_cannot_hide_bad_records(self):
        d = report(11, True)
        d["reports"][0]["passed"] = False
        self.assertFalse(case_evidence(d, 11, causal=True)["accepted"])
        d = report(11, True)
        d["reports"][1]["id"] = d["reports"][0]["id"]
        self.assertFalse(case_evidence(d, 11, causal=True)["accepted"])
        d = report(11, True); d["causal_controls"][0]["passed"] = False
        self.assertFalse(case_evidence(d, 11, causal=True)["accepted"])
        d = report(11, True); del d["reports"][0]["replay"]
        self.assertFalse(case_evidence(d, 11, causal=True)["accepted"])
        d = {"accepted": True, "reports": [{"id": "G01", "passed": True,
             "classification": "EXPECTED_REJECTION_OF_INVALID_OR_UNACCEPTED_INPUT",
             "expected": "STOP with fee required", "actual": "everything accepted"}]}
        self.assertFalse(case_evidence(d, 1)["accepted"])
        d["reports"][0]["actual"] = "fee required"
        self.assertTrue(case_evidence(d, 1)["accepted"])

    def test_stale_snapshot_is_preserved_without_overriding_current_synthetic_evidence(self):
        audit = {"integrity_passed": True, "gaps": [{"id": "D06", "status": "not_run"}],
                 "daily": {"EFA.yahoo": {"rows": 100, "missing_received_timestamps": 100}},
                 "cross_source": {"EFA": {"differences": {"volume": 20}}},
                 "source_metadata": {"EFA": {"volume_basis": "unconfirmed"}}}
        original = copy.deepcopy(audit)
        engine = {"instrument_calibrated": True, "aligned_supported_cases_accepted": True,
                  "reports": [{"id": str(i), "aligned": {"status": "MATCH", "differences": [], "replay_errors": []}} for i in range(21)]}
        r = self.evaluate(audit, engine, report(11, True))
        g = r["gates"][-1]
        self.assertEqual(g["snapshot_declaration"]["status"], "not_run")
        self.assertEqual(g["status"], "PARTIAL_SYNTHETIC_ACCEPTANCE")
        self.assertFalse(g["current_evidence"]["history_input_adapter_accepted"])
        self.assertEqual(r["gate_decision"], "NOT_ELIGIBLE")
        self.assertEqual(audit, original)
        changed = copy.deepcopy(audit); changed["cross_source"]["EFA"]["differences"]["volume"] += 1
        self.assertNotEqual(r["input_report_fingerprints"]["data_audit"], self.evaluate(changed)["input_report_fingerprints"]["data_audit"])

    def test_forged_ready_manifest_cannot_enable_unimplemented_history(self):
        r = self.evaluate({"integrity_passed": True, "historical_ready": True,
                           "gaps": [{"id": f"D{i:02}", "status": "passed"} for i in range(1, 7)]})
        self.assertEqual(r["gate_decision"], "NOT_ELIGIBLE")
        self.assertFalse(r["historical_run_permitted"])
        self.assertEqual(r["formal_history_trials"], 0)
