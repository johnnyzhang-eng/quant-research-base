import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research_base import historical_input as h
from research_base.evidence import ContractError, digest, load_json
from research_base.reference.low_frequency_engine import validate, ContractError as EngineError


class HistoricalInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = h.write_control_fixture(self.root / "input")
        self.checked = "2020-01-10T00:00:00Z"

    def convert(self):
        return h.normalize(self.manifest, self.root, checked_at=self.checked)

    def rehash(self, name):
        contract = load_json(self.manifest)
        contract["files"][name]["sha256"] = digest(self.manifest.parent / name)
        self.manifest.write_text(json.dumps(contract))

    def test_actual_known_answers_and_adversarial_controls(self):
        output = self.root / "controls"
        report = h.run_controls(output)
        self.assertEqual(23, len(report["reports"]))
        self.assertTrue(report["accepted"], report)
        incomplete = copy.deepcopy(report)
        incomplete["reports"].pop()
        self.assertFalse(h.control_report_accepted(incomplete))
        forged = copy.deepcopy(report)
        forged["reports"][0]["actual"] = "wrong but flag still true"
        self.assertFalse(h.control_report_accepted(forged))
        self.assertEqual(17, sum(row["actual"] == "rejected" for row in report["reports"]))
        inventory = load_json(output / "manifest.json")["files"]
        self.assertTrue(all(digest(output / name) == checksum for name, checksum in inventory.items()))
        with self.assertRaises(FileExistsError):
            h.run_controls(output)

    def test_market_provenance_preserved_and_synthetic_engine_refuses(self):
        contract = load_json(self.manifest)
        contract["data_kind"] = "historical_market"
        rights_path = self.manifest.parent / "rights.json"
        rights = load_json(rights_path)
        rights["source_nature"] = "historical_market"
        rights_path.write_text(json.dumps(rights))
        contract["files"]["rights.json"]["sha256"] = digest(rights_path)
        self.manifest.write_text(json.dumps(contract))
        converted = self.convert()
        self.assertEqual("historical_market", converted["data_kind"])
        self.assertFalse(converted["historical_engine_ready"])
        with self.assertRaisesRegex(EngineError, "refuses market/history"):
            validate(converted, "synthetic")

    def test_mutation_during_parsing_detected(self):
        real_rows = h.rows
        def changed(path):
            table = real_rows(path)
            if path.name == "bars.csv":
                with path.open("a") as stream:
                    stream.write("mutation\n")
            return table
        with patch.object(h, "rows", side_effect=changed):
            with self.assertRaisesRegex(ContractError, "changed during"):
                self.convert()

    def test_manifest_mutation_during_parsing_detected(self):
        real_rows = h.rows
        def changed(path):
            table = real_rows(path)
            if path.name == "bars.csv":
                with self.manifest.open("a") as stream:
                    stream.write(" ")
            return table
        with patch.object(h, "rows", side_effect=changed):
            with self.assertRaisesRegex(ContractError, "changed during"):
                self.convert()

    def test_missing_field_not_implicitly_filled(self):
        table = h.rows(self.manifest.parent / "bars.csv")
        table[0]["volume"] = ""
        h._rewrite_control(self.manifest.parent, "bars.csv", table, h.BARS)
        with self.assertRaises(ContractError):
            self.convert()

    def test_symlink_input_refused(self):
        grant = self.manifest.parent / "grant.txt"
        grant.unlink()
        elsewhere = self.root / "elsewhere.txt"
        elsewhere.write_text("invented")
        grant.symlink_to(elsewhere)
        with self.assertRaisesRegex(ContractError, "Symlink"):
            self.convert()

    def test_output_private_and_exclusive(self):
        with TemporaryDirectory() as outside:
            with self.assertRaisesRegex(ContractError, "private_root"):
                h.import_history(self.manifest, Path(outside) / "out", self.root, checked_at=self.checked)
        out = self.root / "converted"
        report = h.import_history(self.manifest, out, self.root, checked_at=self.checked)
        self.assertEqual(digest(out / "normalized.json"), report["files"]["normalized.json"])
        with self.assertRaises(FileExistsError):
            h.import_history(self.manifest, out, self.root, checked_at=self.checked)

    def test_rights_expiry_boundary_no_grace_use(self):
        path = self.manifest.parent / "rights.json"
        rights = load_json(path)
        rights.update(retention="until_expiry", expires_at=self.checked,
                      delete_by="2020-02-01T00:00:00Z", deletion_conditions=["expiry", "cancellation"])
        path.write_text(json.dumps(rights))
        self.rehash("rights.json")
        with self.assertRaisesRegex(ContractError, "expired"):
            self.convert()

    def test_cancellation_requires_stop_even_before_expiry(self):
        path = self.manifest.parent / "rights.json"
        rights = load_json(path)
        rights["active_deletion_conditions"] = ["grant withdrawn"]
        path.write_text(json.dumps(rights))
        self.rehash("rights.json")
        with self.assertRaisesRegex(ContractError, "deletion conditions"):
            self.convert()

    def test_known_future_actions_not_ex_date_filtered(self):
        table = h.rows(self.manifest.parent / "actions.csv")
        for action in table:
            action["available_at"] = "2020-01-02T20:00:00Z"
        h._rewrite_control(self.manifest.parent, "actions.csv", table, h.ACTIONS)
        view = h.snapshot_as_of(self.convert(), "2020-01-02T21:05:00Z")
        self.assertEqual(1, len(view["bars"]))
        self.assertEqual(2, len(view["actions"]))
        self.assertEqual("2020-01-03", view["actions"][0]["effective_date"])

    def test_close_in_another_exchange_local_date_rejected(self):
        calendar = h.rows(self.manifest.parent / "calendar.csv")
        calendar[0]["close_at"] = "2020-01-03T06:00:00Z"
        h._rewrite_control(self.manifest.parent, "calendar.csv", calendar, h.CALENDAR)
        with self.assertRaisesRegex(ContractError, "trade date"):
            self.convert()


if __name__ == "__main__":
    unittest.main()
