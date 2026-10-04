"""Normalize explicit synthetic Vibe account evidence for metric diagnostics."""
from datetime import date
from decimal import Decimal

from .evidence import ContractError, canonical_hash
from .performance import report


def from_vibe(data, account_ledger, *, opening_date):
    if data["data_kind"] != "synthetic_fixture":
        raise ContractError("This normalization bridge has not accepted historical data")
    snapshots = account_ledger["snapshots"]
    if date.fromisoformat(opening_date) >= date.fromisoformat(snapshots[0]["date"]):
        raise ContractError("Explicit opening valuation boundary must precede close observations")
    initial = data["initial"]
    initial_equity = Decimal(initial["settled_cash"]) + sum(Decimal(q) * Decimal(initial["marks"][s]) for s, q in initial["holdings"].items())
    fills = []
    for e in account_ledger["events"]:
        if e["type"] == "FILL":
            fills.append({"id": f"fill-{e['sequence']}", "date": e["date"], "symbol": e["symbol"],
                          "signed_quantity": str(e["qty"] * (1 if e["side"] == "BUY" else -1)),
                          **{k: e[k] for k in ("price", "commission", "platform", "tax", "fee")}})
    context = {"data": {"bars": data["bars"], "actions": data["actions"], "calendar": data["calendar"]},
               "account": {"settlement": data["settlement"], "instruments": data["instruments"]},
               "costs": data["fees"], "execution": "synthetic fixed-target aligned-v2-accounting",
               "fx": "single USD, no conversion"}
    ledger = {"schema_version": "1", "currency": "USD", "sampling": "daily_session_close",
              "calendar": [opening_date] + [s["date"] for s in snapshots],
              "calendar_source": "explicit synthetic opening boundary and invented close calendar, not market certification",
              "periods_per_year": 252, "year_day_basis": 365, "flow_policy": "end_boundary_only",
              "observations": [{"date": opening_date, "equity": str(initial_equity), "external_flow_end": "0"}] +
                              [{"date": s["date"], "equity": s["equity"], "external_flow_end": "0"} for s in snapshots],
              "risk_free": None, "fills": fills, "cost_events": [],
              "comparison_context": {k: canonical_hash(v) for k, v in context.items()}}
    return ledger, report(ledger)
