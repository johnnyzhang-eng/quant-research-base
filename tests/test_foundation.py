import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from research_base.contracts import validate_spec
from research_base.data_audit import audit_daily
from research_base.evidence import (ContractError, append_event, digest, inside,
                                   load_json, seal_run, verify_run, write_json)
from research_base.runner import PACKAGE, run_validation

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.spec = load_json(ROOT / "examples/validation.json")
    def tearDown(self):
        self.temp.cleanup()
    def test_demo_spec(self):
        validate_spec(self.spec)
    def test_formal_label_cannot_unlock(self):
        self.spec["purpose"] = "historical_returns"
        with self.assertRaises(ContractError):
            validate_spec(self.spec)
    def test_unknown_cannot_disappear(self):
        del self.spec["assumptions"]["costs"]
        with self.assertRaises(ContractError):
            validate_spec(self.spec)
    def test_nonfinite_and_duplicate_keys(self):
        p = self.root / "bad.json"
        for text in ('{"x":NaN}', '{"x":Infinity}', '{"x":1,"x":2}'):
            p.write_text(text)
            with self.assertRaises(ContractError):
                load_json(p)
    def test_traversal_and_symlink(self):
        with self.assertRaises(ContractError):
            inside(self.root, "../outside")
        (self.root / "link").symlink_to(ROOT, target_is_directory=True)
        with self.assertRaises(ContractError):
            inside(self.root, "link/README.md")
    def test_evidence_not_overwritten(self):
        p = self.root / "a.json"
        write_json(p, {"value": 1})
        with self.assertRaises(FileExistsError):
            write_json(p, {"value": 2})
        self.assertEqual(load_json(p), {"value": 1})


class DataInstrumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "synthetic.csv"
        self.header = "trade_date,open,high,low,close,volume\n"
    def tearDown(self):
        self.temp.cleanup()
    def audit(self, text):
        self.path.write_text(self.header + text)
        return audit_daily(self.path, ["2024-10-07", "2024-10-08"], "nasdaq")
    def test_known_valid_and_unknown_volume(self):
        d = self.audit("2024-10-07,10,12,9,11,100\n2024-10-08,11,12,10,11,\n")
        self.assertEqual(d["error_count"], 0)
        self.assertEqual(d["missing_volume"], 1)
        self.assertEqual(d["missing_sessions_full_window"], 0)
    def test_bad_ohlc_and_nonfinite_rejected(self):
        for row in ("2024-10-07,10,9,8,11,100\n", "2024-10-07,NaN,12,9,11,100\n"):
            self.assertGreater(self.audit(row)["error_count"], 0)
    def test_duplicates_and_missing_sessions(self):
        d = self.audit("2024-10-07,10,12,9,11,100\n2024-10-07,10,12,9,11,100\n")
        self.assertGreater(d["error_count"], 0)
        self.assertEqual(d["missing_sessions_full_window"], 1)
    def test_ragged_csv_rejected(self):
        with self.assertRaises(ContractError):
            self.audit("2024-10-07,10,12,9,11,100,EXTRA\n")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "run-1"; self.run.mkdir()
        self.registry = self.root / "registry.jsonl"
    def tearDown(self):
        self.temp.cleanup()
    def make_seal(self):
        write_json(self.run / "result.json", {"value": 100})
        checksum = seal_run(self.run)
        append_event(self.registry, {"event": "FINISHED", "run_id": self.run.name, "seal_sha256": checksum})
    def test_good_and_tampered_output(self):
        self.make_seal()
        self.assertTrue(verify_run(self.run, self.registry)["verified"])
        (self.run / "result.json").write_text('{"value":999}')
        self.assertFalse(verify_run(self.run, self.registry)["verified"])
    def test_resealing_does_not_match_registry(self):
        self.make_seal()
        (self.run / "result.json").write_text('{"value":999}')
        (self.run / "seal.json").unlink()
        seal_run(self.run)
        self.assertFalse(verify_run(self.run, self.registry)["verified"])
    def test_added_file_rejected(self):
        self.make_seal(); (self.run / "extra.txt").write_text("extra")
        self.assertFalse(verify_run(self.run, self.registry)["verified"])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "output"
    def tearDown(self):
        self.temp.cleanup()
    def test_real_reference_path_and_reproduction(self):
        science = []
        keys = []
        for _ in range(2):
            code, run = run_validation(ROOT / "examples/validation.json", ROOT, self.output)
            self.assertEqual(code, 0)
            report = load_json(run / "result.json")
            self.assertEqual(report["reference_controls"]["passed"], 39)
            self.assertEqual(report["formal_history_trials"], 0)
            self.assertFalse(report["data_historical_ready"])
            self.assertTrue(verify_run(run, self.output / "registry.jsonl")["verified"])
            science.append(report["scientific_result_sha256"])
            keys.append(report["reproduction_key"])
        self.assertEqual(science[0], science[1])
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(len(list((self.output / "runs").iterdir())), 2)
    def test_failure_is_registered_and_sealed(self):
        p = Path(self.temp.name) / "bad.json"
        data = load_json(ROOT / "examples/validation.json"); data["purpose"] = "historical_returns"
        p.write_text(json.dumps(data))
        code, run = run_validation(p, ROOT, self.output)
        self.assertEqual(code, 1)
        self.assertEqual(load_json(run / "failure.json")["type"], "ContractError")
        self.assertTrue(verify_run(run, self.output / "registry.jsonl")["verified"])
        events = [json.loads(line) for line in (self.output / "registry.jsonl").read_text().splitlines()]
        self.assertEqual([e["event"] for e in events], ["STARTED", "FINISHED"])
    def test_wrong_source_copy_is_detected(self):
        # Mutate a disposable COPY, never the original or installed framework.
        original = PACKAGE / "reference/low_frequency_engine.py"
        before = digest(original)
        temp = Path(self.temp.name) / "mutation"; folder = temp / "round2"; folder.mkdir(parents=True)
        for name in ("low_frequency_engine.py", "run_controls.py"):
            shutil.copyfile(PACKAGE / "reference" / name, folder / name)
        anchor = 'if side == "BUY":\n                    self.cash += cash_change'
        text = (folder / "low_frequency_engine.py").read_text()
        self.assertEqual(text.count(anchor), 1)
        (folder / "low_frequency_engine.py").write_text(text.replace(anchor,
                            'if side == "BUY":\n                    self.cash -= cash_change'))
        (temp / "候选策略与首个实验协议.md").write_text("synthetic controls")
        (temp / "试验登记.csv").write_text("id\n")
        try:
            proc = subprocess.run([sys.executable, "-B", str(folder / "run_controls.py")],
                                  capture_output=True, timeout=30)
            self.assertNotEqual(proc.returncode, 0)
            rejected = load_json(folder / "controls_actual.json")
            self.assertLess(rejected["controls_passed"], rejected["controls_total"])
            self.assertTrue(any(r.get("differences") or r.get("independent_replay_errors")
                                for r in rejected["reports"] if not r["passed"]))
        finally:
            self.assertEqual(digest(original), before)


if __name__ == "__main__":
    unittest.main()
