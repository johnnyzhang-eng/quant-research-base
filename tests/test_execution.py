"""Persistent offline known answers and adversarial completeness/recovery checks."""
import copy
import concurrent.futures
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from research_base.execution import (ACCOUNT, ENV, EXPECTED_CONTROL_IDS,
    EXPECTED_FROZEN_CASE_IDS, OrderManager, SimulationBinding,
    run_offline_controls, validate_control_report)
from research_base.execution.extended_controls import BINDING, NOW, setup


class OfflineAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.original_socket = socket.socket
        cls.original_connection = socket.create_connection
        cls.report = run_offline_controls(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_all_frozen_and_revision_known_answers(self):
        self.assertTrue(self.report["accepted"], self.report["completeness"])
        self.assertEqual((self.report["case_count"], self.report["steps_executed"],
                          self.report["frozen_expectation_count"]), (19, 119, 290))
        self.assertEqual(len(self.report["reports"]), len(EXPECTED_CONTROL_IDS))
        self.assertEqual(len(EXPECTED_FROZEN_CASE_IDS), 19)
        self.assertFalse(self.report["official_adapter_complete"])
        self.assertEqual(self.report["broker_network_calls"], 0)

    def test_network_guard_restored_after_library_call(self):
        self.assertIs(socket.socket, type(self).original_socket)
        self.assertIs(socket.create_connection, type(self).original_connection)
        self.assertTrue(self.report["harness_controls"]["network_guard"])

    def test_drop_row_is_not_smaller_success(self):
        smaller = copy.deepcopy(self.report)
        missing = smaller["reports"].pop()
        result = validate_control_report(smaller)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["missing_control_ids"], [missing["id"]])

    def test_drop_case_with_unchanged_count_is_rejected(self):
        smaller = copy.deepcopy(self.report)
        missing = smaller["cases"].pop()
        result = validate_control_report(smaller)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["missing_case_ids"], [missing["id"]])

    def test_duplicate_row_is_rejected(self):
        duplicated = copy.deepcopy(self.report)
        duplicated["reports"].append(duplicated["reports"][0])
        result = validate_control_report(duplicated)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["duplicate_control_ids"], [duplicated["reports"][0]["id"]])

    def test_wrong_answer_cannot_relabel_expected(self):
        liar = copy.deepcopy(self.report)
        liar["reports"][0].update(passed=True, expected=False, actual=False)
        result = validate_control_report(liar)
        self.assertFalse(result["accepted"])
        self.assertIn(liar["reports"][0]["id"], result["wrong_answers"])

    def test_bool_integer_confusion_is_rejected_recursively(self):
        liar = copy.deepcopy(self.report)
        row = next(r for r in liar["reports"] if r["id"] == "accepted_unknown_query_never_resends")
        row["actual"] = [1, 0, 1]
        result = validate_control_report(liar)
        self.assertFalse(result["accepted"])
        self.assertIn(row["id"], result["wrong_answers"])

    def test_changed_fixture_hash_is_rejected(self):
        liar = copy.deepcopy(self.report)
        liar["sources_sha256"]["fixtures.json"] = "0" * 64
        self.assertFalse(validate_control_report(liar)["accepted"])

    def test_fake_failure_ignored_by_counts_is_rejected(self):
        liar = copy.deepcopy(self.report)
        liar["cases"][0]["failures"] = [{"invariant": "CASH_CONSERVATION"}]
        self.assertFalse(validate_control_report(liar)["accepted"])


class PersistentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "case"
        self.oms, self.venue, self.raw = setup(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def test_persisted_binding_rejected_before_restart_mutation(self):
        self.oms.submit(self.raw, NOW)
        with sqlite3.connect(self.oms.path) as db:
            db.execute("UPDATE settings SET value=? WHERE key='binding'", (json.dumps({"account": "OTHER"}),))
        with self.assertRaisesRegex(ValueError, "PERSISTED_BINDING_MISMATCH"):
            OrderManager(self.oms.path, self.venue, binding=BINDING)
        # Constructor must not introduce a restart pause into an unrelated DB.
        self.assertEqual(self.oms.snapshot()["account"]["halted"], 0)

    def test_concurrent_recovery_cannot_blind_resend_twice(self):
        original = self.venue.submit
        def unavailable(*args):
            raise TimeoutError("FICTIONAL_PRE_ACCEPT")
        self.venue.submit = unavailable
        self.oms.submit(self.raw, NOW)
        self.venue.submit = original
        original_query = self.venue.query_intent
        barrier = threading.Barrier(2)
        def simultaneous_query(intent):
            result = original_query(intent)
            barrier.wait(timeout=5)
            return result
        self.venue.query_intent = simultaneous_query
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.oms.recover_submission(
                "control", retry_if_absent=True, now=NOW), range(2)))
        self.assertEqual(self.venue.snapshot()["submit_call_count"], 1)
        self.assertEqual(sum(r["resent"] for r in results), 1)
        self.assertEqual(self.venue.snapshot()["accept_count"], 1)

    def test_terminal_guard_and_cancel_transition_are_atomic(self):
        self.oms.submit(self.raw, NOW)
        request = self.oms.request_cancel("control")
        # Concurrent confirmations/rejections can be serialized in either order;
        # confirmation must leave a terminal state and zero reservation.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(self.oms.cancel_response, "control", "CANCELLED"),
                     pool.submit(self.oms.cancel_response, "control", "CANCEL_REJECTED",
                                 request_id=request["request_id"])]
            for task in tasks:
                task.result()
        state = self.oms.snapshot()
        self.assertEqual(state["orders"][0]["state"], "CANCELLED")
        self.assertEqual(state["reserved_cash"], 0)
        self.oms.cancel_response("control", "CANCEL_REJECTED", request_id=request["request_id"])
        self.assertEqual(self.oms.snapshot()["orders"][0]["state"], "CANCELLED")

    def test_binding_constructor_needs_explicit_allowlist(self):
        with self.assertRaisesRegex(ValueError, "ACCOUNT_NOT_EXPLICITLY_WHITELISTED"):
            SimulationBinding(ENV, ACCOUNT, ()).validate()
        with self.assertRaisesRegex(ValueError, "OFFLINE_BINDING_REQUIRED"):
            SimulationBinding("REAL", ACCOUNT, (ACCOUNT,)).validate()


if __name__ == "__main__":
    unittest.main()
