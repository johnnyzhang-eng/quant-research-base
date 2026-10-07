"""Reproduce one invented, closed two-wallet cash ledger without market access."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from . import funding_account, funding_events


MARKET = "SYNTHETIC"
SYMBOL = "BASE_QUOTE"
START_MS = 946684800000


def event(identifier, kind, offset_ms, **fields):
    stamp = funding_events.iso_ms(START_MS + offset_ms)
    return {"id": identifier, "type": kind, "event_time": stamp,
            "applied_at": stamp, "known_at": stamp, "sequence": 0, **fields}


def run_demo():
    """Invented inputs and exact expected cash; no observed data are qualified."""
    bridge = funding_events.SyntheticFundingBridge(
        "1000", "1000", market=MARKET, symbol=SYMBOL)
    actions = [
        event("initial-mark", "mark", 0, spot_mark="100", perp_mark="101"),
        event("open-spot", "spot_fill", 1, side="buy", qty="2",
              reference_price="100", slippage_rate="0", fee_rate="0.01"),
        event("open-short", "perp_fill", 2, side="sell", qty="2",
              reference_price="101", slippage_rate="0", fee_rate="0.01"),
    ]
    for action in actions:
        bridge.apply_action(action)
    opened = bridge.pre_event_view()
    settlement_time = START_MS + 1000
    observations = funding_events.compile_observations(
        [{"calc_time": str(settlement_time), "funding_interval_hours": "8",
          "last_funding_rate": "0.001", "source_line": 2}],
        [{"open_time_ms": START_MS, "close_time_ms": START_MS + 59999,
          "open": "101", "high": "103", "low": "100", "close": "102"}],
        market=MARKET, symbol=SYMBOL)
    contract = funding_events.make_synthetic_contract(
        market=MARKET, symbol=SYMBOL, settlement_time_ms=settlement_time,
        payment_time_ms=START_MS + 2000, rate="0.001",
        eligible_perp_qty_signed="-2", settlement_mark="102",
        pre_event_account_view_sha256=bridge.pre_event_view_sha256(),
        rate_known_at_ms=settlement_time, eligibility_known_at_ms=settlement_time,
        mark_known_at_ms=settlement_time)
    bridge.capture(contract)
    captured = bridge.pre_event_view()
    restart = bridge.snapshot()
    restored = funding_events.SyntheticFundingBridge.restore(restart)
    close_actions = [
        event("exit-mark", "mark", 1099, spot_mark="110", perp_mark="108"),
        event("close-spot", "spot_fill", 1100, side="sell", qty="2",
              reference_price="110", slippage_rate="0", fee_rate="0.01"),
        event("close-short", "perp_fill", 1101, side="buy", qty="2",
              reference_price="108", slippage_rate="0", fee_rate="0.01"),
    ]
    for instance in (bridge, restored):
        for action in close_actions:
            instance.apply_action(action)
        instance.post(contract["event_id"])
    reconciliation = bridge.account.reconciliation()
    expected = {"spot_cash": "1015.8", "deriv_wallet_cash_signed": "982.024",
                "fees_total": "8.38", "funding_cash_posted": "0.204",
                "nav": "1997.824", "spot_qty": "0", "perp_qty_signed": "0"}
    actual = reconciliation["state"]
    checks = {
        "exact_expected_cash_and_nav": all(actual[key] == value for key, value in expected.items()),
        "capture_does_not_increase_cash_or_nav": (
            captured["nav"] == opened["nav"] and
            captured["deriv_wallet_cash_signed"] == opened["deriv_wallet_cash_signed"]),
        "restart_equals_uninterrupted_run": bridge.snapshot() == restored.snapshot(),
        "closed_exact_reconciliation": reconciliation["closed_reconciliation"],
        "observations_never_post_actual_cash": observations["actual_cash_postings"] == [],
    }
    if not all(checks.values()):
        raise AssertionError("Invented cash-ledger control differs: " + repr(checks))
    return {
        "schema": "funding-offline-demo/1", "purpose": "INVENTED_KNOWN_ANSWER_CONTROL",
        "expected": expected, "checks": checks, "reconciliation": reconciliation,
        "observations": observations, "synthetic_contract": contract,
        "final_bridge_snapshot": bridge.snapshot(),
        "source_sha256": {Path(module.__file__).name: hashlib.sha256(
            Path(module.__file__).read_bytes()).hexdigest()
            for module in (funding_account, funding_events)},
        "historical_backtest": False, "venue_liquidation_replica": False,
        "actual_cash_posting_verified": False, "orders": 0, "network_calls": 0,
    }


def save_report(output, report):
    output = Path(output).absolute()
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("Output directory must not use symlinks")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    raw = (json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2,
                      allow_nan=False) + "\n").encode()
    fd = os.open(output / "report.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    return {"report": str(output / "report.json"),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "scope": report["purpose"], "checks_passed": all(report["checks"].values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="New local output directory; existing runs are never overwritten")
    args = parser.parse_args()
    print(json.dumps(save_report(args.output, run_demo()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
