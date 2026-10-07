import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class FundingDemoTests(unittest.TestCase):
    def test_cli_closed_cash_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve() / "new-run"
            command = [sys.executable, "-B", "-m", "research_base.funding_demo",
                       "--output", str(target)]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            receipt = json.loads(result.stdout)
            report = json.loads((target / "report.json").read_bytes())
            self.assertTrue(receipt["checks_passed"])
            self.assertEqual(report["reconciliation"]["state"]["nav"], "1997.824")
            self.assertTrue(report["reconciliation"]["closed_reconciliation"])
            self.assertFalse(report["actual_cash_posting_verified"])
            self.assertEqual(report["network_calls"], 0)
            previous = (target / "report.json").read_bytes()
            rejected = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual((target / "report.json").read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
