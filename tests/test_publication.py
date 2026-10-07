import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("publication", Path(__file__).resolve().parents[1]/"tools/check_publication.py")
publication = importlib.util.module_from_spec(spec); spec.loader.exec_module(publication)


class PublicationGuardTests(unittest.TestCase):
    def test_allowed_method(self):
        self.assertEqual(publication.check_file("docs/method.md", b"Synthetic method"), [])
    def test_private_and_vendor_data_denied(self):
        for name in ("private/alpha.py", "data-private/quotes.csv", "runs-local/result.json", "examples/raw/prices.csv"):
            self.assertTrue(publication.check_file(name, b"private"))
    def test_key_denied_without_storing_a_key(self):
        fake = ("ghp_" + "A" * 36).encode()
        self.assertIn("credential pattern", publication.check_file("docs/x.md", fake))
    def test_binary_denied(self):
        self.assertTrue(publication.check_file("docs/account.png", b"image"))
    def test_reviewed_experiment_path_without_widening_directory(self):
        approved = "experiments/atlas20_inventory_bridge_controls.py"
        self.assertEqual(publication.check_file(approved, b"Synthetic source control"), [])
        for name in ("experiments/other.py", "experiments/atlas20_inventory_bridge_controls.py.bak",
                     "experiments/nested/atlas20_inventory_bridge_controls.py"):
            with self.subTest(name=name):
                self.assertIn("outside public allowlist", publication.check_file(name, b"unreviewed"))
    def test_reviewed_experiment_still_rejects_confidential_content(self):
        approved = "experiments/atlas20_inventory_bridge_controls.py"
        fake_key = ("ghp_" + "A" * 36).encode()
        self.assertIn("credential pattern", publication.check_file(approved, fake_key))
        personal_path = ("/" + "Users" + "/" + "example/private.txt").encode()
        self.assertIn("local personal path", publication.check_file(approved, personal_path))


if __name__ == "__main__":
    unittest.main()
