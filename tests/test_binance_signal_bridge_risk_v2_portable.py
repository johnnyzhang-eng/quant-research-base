"""Discover all 16 frozen producer controls from the public tools copy.

The producer, bridge and v4 adapter remain their original bytes. This shim
checks their hashes and import origins without running a report writer.
"""
import hashlib
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
EXPECTED = {
    "binance_signal_order_bridge_20261008_v2.py": "d6878ee61787c973edb851f2a061b0ddbecea1c79cd023230e8ae2496133952a",
    "binance_spot_testnet_adapter_20261008_v4.py": "64189cdeaaaccf52ac5e8446e2233c1c63a5d8a5d6da025577df923d3d734891",
    "test_binance_signal_order_bridge_20261008_v2.py": "e4f5e3d2933c65f34a8ff45393741f3a56603f0017136eb9430027be2d2dfdbc",
}


def load_tests(loader, standard_tests, pattern):
    for name, expected in EXPECTED.items():
        if hashlib.sha256((TOOLS / name).read_bytes()).hexdigest() != expected:
            raise ImportError("Frozen public v2 source or producer bytes differ")
    prior_path = list(sys.path)
    try:
        sys.path.insert(0, str(TOOLS))
        producer = importlib.import_module("test_binance_signal_order_bridge_20261008_v2")
    finally:
        sys.path[:] = prior_path
    for module, filename in [
        (producer, "test_binance_signal_order_bridge_20261008_v2.py"),
        (producer.b, "binance_signal_order_bridge_20261008_v2.py"),
        (producer.b.adapter, "binance_spot_testnet_adapter_20261008_v4.py"),
    ]:
        origin = Path(module.__file__).resolve()
        if origin != (TOOLS / filename).resolve() or hashlib.sha256(origin.read_bytes()).hexdigest() != EXPECTED[filename]:
            raise ImportError("Bridge imports must resolve to the exact public tools copy")
    suite = loader.loadTestsFromTestCase(producer.Controls)
    if suite.countTestCases() != 16:
        raise AssertionError("The frozen producer must discover all 16 semantic methods")
    return suite
