"""Discover the exact producer's 18 offline cases without its report writer.

The three source files are copied byte-for-byte into tools. No import rewrite,
alias, credential loader, author strategy execution or new finite CLI is used.
"""
import hashlib
import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
EXPECTED = {
    "binance_signal_order_bridge_20261008_v1.py": "07c7d706fd15ef937691e368e3074a953f6f890ee6f29ec8ee30c468a3ff2c20",
    "binance_spot_testnet_adapter_20261008_v4.py": "64189cdeaaaccf52ac5e8446e2233c1c63a5d8a5d6da025577df923d3d734891",
    "test_binance_signal_order_bridge_20261008_v1.py": "c65b282644d0dd639572d5bd47370dead7c5593f7d30c091f59658954052cce4",
}


def load_tests(loader, standard_tests, pattern):
    for name, expected in EXPECTED.items():
        if hashlib.sha256((TOOLS / name).read_bytes()).hexdigest() != expected:
            raise ImportError("Frozen public bridge source or producer bytes differ")
    prior_path = list(sys.path)
    try:
        sys.path.insert(0, str(TOOLS))
        producer = importlib.import_module("test_binance_signal_order_bridge_20261008_v1")
    finally:
        sys.path[:] = prior_path
    for module, filename in [
        (producer, "test_binance_signal_order_bridge_20261008_v1.py"),
        (producer.b, "binance_signal_order_bridge_20261008_v1.py"),
        (producer.b.adapter, "binance_spot_testnet_adapter_20261008_v4.py"),
    ]:
        origin = Path(module.__file__).resolve()
        if origin != (TOOLS / filename).resolve() or hashlib.sha256(origin.read_bytes()).hexdigest() != EXPECTED[filename]:
            raise ImportError("Bridge imports must resolve to the exact public tools copy")
    suite = loader.loadTestsFromTestCase(producer.Controls)
    if suite.countTestCases() != 18:
        raise AssertionError("The exact producer must discover 18 semantic methods")
    return suite
