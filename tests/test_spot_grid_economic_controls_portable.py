"""Discover the original synthetic economic controls in ordinary offline CI."""
import importlib.util
from pathlib import Path


def load_tests(loader, tests, pattern):
    source = (Path(__file__).resolve().parents[1] / "tools" /
              "test_spot_grid_economic_diagnostic_20261008_v0.py")
    spec = importlib.util.spec_from_file_location("grid_economics_known_answers", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return loader.loadTestsFromTestCase(module.EconomicsControls)
