"""Audit an existing S02 snapshot locally; never download or choose a source."""
import csv
from datetime import date, datetime
from decimal import Decimal
from .contracts import decimal
from .evidence import ContractError, digest, inside, load_json


def rows(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ContractError(f"Missing/duplicate CSV columns: {path.name}")
        values = list(reader)
        if any(None in r or any(v is None for v in r.values()) for r in values):
            raise ContractError(f"Ragged CSV rows: {path.name}")
        return values


def audit_daily(path, expected_sessions, kind):
    table = rows(path)
    columns = {"trade_date", "open", "high", "low", "close", "volume"}
    if not table or not columns <= set(table[0]):
        raise ContractError(f"Daily table missing required columns: {path.name}")
    dates, findings, missing_volume, missing_received = [], [], 0, 0
    for n, row in enumerate(table, 2):
        try:
            d = date.fromisoformat(row["trade_date"]).isoformat()
            dates.append(d)
            p = {k: decimal(row[k], k, "0.000000001") for k in ("open", "high", "low", "close")}
            if not p["low"] <= min(p["open"], p["close"]) <= max(p["open"], p["close"]) <= p["high"]:
                raise ContractError("OHLC ordering")
            if row["volume"] == "":
                missing_volume += 1
            else:
                decimal(row["volume"], "volume", 0)
            if kind == "yahoo":
                if row.get("currency") != "USD" or not row.get("price_basis"):
                    raise ContractError("Currency/price basis missing")
                decimal(row.get("adj_close_vendor"), "vendor adjusted close", "0.000000001")
                stamp = datetime.fromisoformat(row.get("source_bar_timestamp_utc", ""))
                if stamp.tzinfo is None:
                    raise ContractError("Naive source timestamp")
                received = row.get("historical_received_at_utc")
                if not received:
                    missing_received += 1
                elif datetime.fromisoformat(received).tzinfo is None:
                    raise ContractError("Naive received timestamp")
        except (ValueError, TypeError, ContractError) as exc:
            findings.append({"row": n, "reason": str(exc)})
    if dates != sorted(set(dates)):
        findings.append({"reason": "Dates not unique and strictly ascending"})
    covered = set(dates)
    return {"rows": len(table), "first": min(dates) if dates else None,
            "last": max(dates) if dates else None, "error_count": len(findings),
            "error_examples": findings[:20], "missing_volume": missing_volume,
            "missing_received_timestamps": missing_received,
            "missing_sessions_full_window": len(set(expected_sessions)-covered),
            "extra_sessions": sorted(covered-set(expected_sessions))[:20]}


def compare_daily(left, right):
    # A disagreement is recorded, never automatically averaged or filled.
    a = {r["trade_date"]: r for r in rows(left)}
    b = {r["trade_date"]: r for r in rows(right)}
    common = sorted(a.keys() & b.keys())
    counts = {k: 0 for k in ("open", "high", "low", "close", "volume")}
    comparable = counts.copy()
    for d in common:
        for k in counts:
            if not a[d][k] or not b[d][k]:
                continue
            delta = abs(decimal(a[d][k], k)-decimal(b[d][k], k))
            comparable[k] += 1
            counts[k] += int(delta > (Decimal(0) if k == "volume" else Decimal("0.005")))
    return {"overlap_rows": len(common), "comparable": comparable,
            "differences": counts, "ohlc_tolerance_usd": "0.005",
            "source_decision": "unresolved; no merge"}


def audit_snapshot(manifest_path):
    root = manifest_path.parent
    manifest = load_json(manifest_path)
    checks = []
    for section, prefix in (("raw_files", "raw"), ("normalized_files", "normalized"),
                            ("supplemental_files", ""), ("scripts_sha256", "")):
        declared = manifest.get(section)
        if not isinstance(declared, dict) or (section != "supplemental_files" and not declared):
            raise ContractError(f"Manifest inventory missing: {section}")
        for name, entry in declared.items():
            relative = f"{prefix}/{name}" if prefix else name
            p = inside(root, relative)
            checksum = entry if isinstance(entry, str) else entry["sha256"]
            same = p.is_file() and digest(p) == checksum
            length = not isinstance(entry, dict) or "bytes" not in entry or (
                p.is_file() and p.stat().st_size == entry["bytes"])
            checks.append({"file": relative, "hash_matches": same, "bytes_match": length})
    if not all(c["hash_matches"] and c["bytes_match"] for c in checks):
        return {"integrity_passed": False, "files_checked": len(checks), "files": checks,
                "daily": {}, "cross_source": {}, "historical_ready": False,
                "gaps": [{"id": "INTEGRITY", "status": "content_changed", "detail": "Stop before parsing data"}]}
    normalized = root / "normalized"
    calendar = rows(normalized / "session_calendar_rules.csv")
    key = "trade_date" if calendar and "trade_date" in calendar[0] else "session_date"
    sessions = [r[key] for r in calendar]
    if sessions != sorted(set(sessions)):
        raise ContractError("Calendar dates are not unique/ascending")
    for d in sessions:
        date.fromisoformat(d)
    daily, cross, actions = {}, {}, {}
    assets = manifest.get("assets", {})
    if set(assets) != {"SPY", "EFA", "IEF", "GLD"}:
        raise ContractError("This S02 adapter requires the four declared assets")
    for symbol in sorted(assets):
        yahoo = normalized / f"{symbol}.yahoo_source_daily.csv"
        nasdaq = normalized / f"{symbol}.nasdaq_daily.csv"
        for source, path in (("yahoo", yahoo), ("nasdaq", nasdaq)):
            daily[f"{symbol}.{source}"] = audit_daily(path, sessions, source)
        cross[symbol] = compare_daily(yahoo, nasdaq)
        distributions = normalized / f"{symbol}.issuer_distributions_in_window.csv"
        if distributions.exists():
            table = rows(distributions)
            bad = []
            late = []
            for n, row in enumerate(table, 2):
                try:
                    ex, pay = date.fromisoformat(row["ex_date"]), date.fromisoformat(row["pay_date"])
                    decimal(row["amount_issuer"], "distribution", 0)
                    if pay < ex:
                        raise ContractError("Payment before ex date")
                    if pay.isoformat() > manifest["requested_period"][1]:
                        late.append({"ex_date": ex.isoformat(), "pay_date": pay.isoformat(),
                                     "gross_per_share": row["amount_issuer"]})
                except (ValueError, ContractError) as exc:
                    bad.append({"row": n, "reason": str(exc)})
            actions[symbol] = {"count": len(table), "error_count": len(bad),
                               "error_examples": bad[:20], "pay_after_cutoff": late}
    gaps = manifest.get("remaining_conditions")
    if not isinstance(gaps, list) or not gaps:
        raise ContractError("S02 source gaps must be explicitly preserved")
    # Nothing in a byte/hash audit certifies a price vendor, PIT or market calendar.
    return {"integrity_passed": True, "files_checked": len(checks), "files": checks,
            "calendar_rows": len(sessions), "daily": daily, "cross_source": cross,
            "issuer_distributions": actions, "gaps": gaps,
            "historical_ready": False, "classification": "LOCAL_SNAPSHOT_AUDIT_ONLY",
            "limitations": ["Not a PIT archive", "Calendar intraday times not fully certified",
                            "No sustainable source permission or primary source selected",
                            "No historical strategy or account execution validated"]}
