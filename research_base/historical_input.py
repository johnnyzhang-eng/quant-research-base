"""Versioned, offline historical conversion. Does not unlock a backtest engine.

Rights and point-in-time claims are evidence-bound declarations, not automated
legal or archival certification. No vendor fetching, inferred units, or filling.
"""
import copy
import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import decimal
from .data_audit import rows
from .evidence import ContractError, canonical_hash, digest, inside, load_json, write_json

VERSION = "historical-input/1"
EXPECTED_CONTROL_IDS = (
    "split_dividend_units", "prefix_visibility", "missing_bar", "duplicate_bar", "volume_unit",
    "price_basis", "fractional_volume", "naive_time", "before_close", "invented_observed",
    "dividend_basis", "zero_split", "duplicate_action", "wrong_session_date", "unknown_timezone",
    "rights_storage", "rights_expired", "rights_provenance", "hash_tamper", "future_perturbation",
    "future_mutation_has_effect", "future_truncation", "observed_control",
)
RIGHTS_FIELDS = {"source_id", "source_nature", "terms_evidence", "reviewer", "authority", "scope",
                "review_note", "purpose", "storage_scope", "raw_storage_allowed", "normalized_storage_allowed",
                "effective_at", "reviewed_at", "retention", "expires_at", "delete_by", "deletion_conditions", "active_deletion_conditions"}
CALENDAR = {"trade_date", "open_at", "close_at"}
TIMING = {"availability_basis", "available_at", "observed_received_at",
          "timing_evidence", "model_rule", "revision_status"}
BARS = {"trade_date", "symbol", "source_id", "open", "high", "low", "close", "volume",
        "currency", "price_basis", "price_unit", "volume_unit"} | TIMING
ACTIONS = {"id", "symbol", "source_id", "type", "effective_date", "pay_date",
           "currency", "numerator", "denominator", "gross_per_share", "share_basis"} | TIMING


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name}: nonempty text required")
    return value


def _stamp(value):
    try:
        out = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if out.utcoffset() is None:
            raise ValueError("timezone required")
        return out.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContractError("timezone-aware timestamp required") from exc


def _day(value):
    try:
        result = date.fromisoformat(value).isoformat()
        if result != value:
            raise ValueError("canonical ISO date required")
        return result
    except (ValueError, TypeError) as exc:
        raise ContractError("ISO session date required") from exc


def _integer(value, name, minimum=0):
    number = decimal(value, name, minimum)
    if number != number.to_integral_value():
        raise ContractError(f"{name}: integer required")
    return int(number)


def _table(path, fields):
    # Reuse the existing ragged/duplicate-column checks; reject unknown columns.
    with path.open(newline="", encoding="utf-8-sig") as stream:
        header = csv.DictReader(stream).fieldnames
    if not header or set(header) != fields or len(header) != len(fields):
        raise ContractError(f"CSV schema differs: {path.name}")
    return rows(path)


def _private(path, private_root):
    root, target = Path(private_root).absolute(), Path(path).absolute()
    if root.is_symlink() or not target.is_relative_to(root):
        raise ContractError("historical inputs and outputs must be inside private_root")
    return inside(root, str(target.relative_to(root)))


def recheck_use_rights(artifact, *, checked_at):
    """Recheck declared storage/use conditions at execution, not conversion time.

    This checks a bound review record; it cannot discover a later revocation or
    independently establish a legal grant. Those remain source-review duties.
    """
    checked = _stamp(checked_at)
    kind = artifact.get("data_kind")
    if kind not in {"historical_market", "invented_control"}:
        raise ContractError("explicit market/control provenance required")
    sources = artifact.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ContractError("source use review absent")
    for source, record in sources.items():
        if not isinstance(record, dict) or set(record) != RIGHTS_FIELDS:
            raise ContractError("complete rights review required before execution")
        if record["source_id"] != source or record["source_nature"] != kind:
            raise ContractError("source use/provenance binding differs")
        for field in ("reviewer", "authority", "scope", "review_note", "terms_evidence"):
            _text(record[field], "rights " + field)
        if not _stamp(record["effective_at"]) <= _stamp(record["reviewed_at"]) <= checked:
            raise ContractError("source use review/effective clocks invalid")
        if (record["purpose"] != "local_research" or record["storage_scope"] != "private"
                or record["raw_storage_allowed"] is not True
                or record["normalized_storage_allowed"] is not True):
            raise ContractError("raw/normalized private research storage grant absent")
        conditions = record["deletion_conditions"]
        if not isinstance(conditions, list) or not conditions or any(
                not isinstance(c, str) or not c.strip() for c in conditions):
            raise ContractError("explicit source deletion conditions required")
        if record["active_deletion_conditions"] != []:
            raise ContractError("active/unknown source deletion condition prevents use")
        if record["retention"] == "until_expiry":
            expiry, deletion = _stamp(record["expires_at"]), _stamp(record["delete_by"])
            if not checked < expiry <= deletion or "expiry" not in conditions:
                raise ContractError("source use/storage grant expired or inconsistent")
        elif record["retention"] == "perpetual_grant":
            if record["expires_at"] is not None or record["delete_by"] is not None:
                raise ContractError("perpetual source grant requires null expiry/deadline")
        else:
            raise ContractError("unsupported source retention")
    return {"checked_at": checked_at, "source_ids": sorted(sources),
            "status": "DECLARED_CONDITIONS_CHECKED_NOT_LEGAL_CERTIFICATION"}


def _timing(row, evidence, retrieved_at, not_before=None):
    available = _stamp(row["available_at"])
    if available > retrieved_at or (not_before and available < not_before):
        raise ContractError("availability outside observation window")
    revision = row["revision_status"]
    if revision not in {"unknown", "as_of_archive", "unrevised_source_claim"}:
        raise ContractError("explicit revision status required")
    basis = row["availability_basis"]
    if basis == "observed":
        if _stamp(row["observed_received_at"]) != available or row["model_rule"]:
            raise ContractError("observed timing requires matching received time and no model rule")
        if row["timing_evidence"] not in evidence:
            raise ContractError("observed timing requires hashed archival evidence")
    elif basis == "modelled":
        if row["observed_received_at"] or row["timing_evidence"]:
            raise ContractError("modelled timing cannot claim observed evidence")
        _text(row["model_rule"], "availability model")
    else:
        raise ContractError("availability must be observed or modelled")
    return {k: row[k] for k in sorted(TIMING)}


def normalize(manifest_path, private_root, *, checked_at):
    """Convert an authorized, hashed local bundle; never fetch or relabel it."""
    manifest_path = _private(manifest_path, private_root)
    manifest_hash = digest(manifest_path)
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != VERSION:
        raise ContractError("unsupported historical input version")
    kind = manifest.get("data_kind")
    if kind not in {"historical_market", "invented_control"}:
        raise ContractError("explicit historical_market or invented_control provenance required")
    checked, retrieved = _stamp(checked_at), _stamp(manifest.get("retrieved_at"))
    if retrieved > checked:
        raise ContractError("retrieval after permission check")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ContractError("hashed file inventory required")
    paths = {}
    for name, meta in files.items():
        if not isinstance(meta, dict):
            raise ContractError("file metadata must be an object")
        path = inside(manifest_path.parent, name)
        if not path.is_file() or digest(path) != meta.get("sha256"):
            raise ContractError(f"input content/hash mismatch: {name}")
        if meta.get("role") not in {"calendar", "bars", "actions", "evidence", "rights"}:
            raise ContractError("unknown file role")
        paths[name] = path
    def single(role):
        matches = [n for n, meta in files.items() if meta["role"] == role]
        if len(matches) != 1:
            raise ContractError(f"exactly one {role} CSV required")
        return paths[matches[0]]
    evidence = {n for n, meta in files.items() if meta["role"] == "evidence"}
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ContractError("source/permission bindings required")
    rights = {}
    for source, rights_name in sources.items():
        _text(source, "source ID")
        _text(rights_name, "rights file")
        if rights_name not in files or files[rights_name]["role"] != "rights":
            raise ContractError("source rights must reference hashed review record")
        record = load_json(paths[rights_name])
        if not isinstance(record, dict) or set(record) != RIGHTS_FIELDS:
            raise ContractError("rights review fields differ; explicit expiry and deletion terms required")
        if record.get("source_id") != source or record.get("source_nature") != kind:
            raise ContractError("source binding/provenance differs; relabelling forbidden")
        if record.get("terms_evidence") not in evidence:
            raise ContractError("rights review must bind hashed terms/grant evidence")
        for field in ("reviewer", "authority", "scope", "review_note"):
            _text(record.get(field), f"rights {field}")
        if not _stamp(record.get("effective_at")) <= _stamp(record.get("reviewed_at")) <= checked:
            raise ContractError("rights review/effective times invalid")
        if not _stamp(record["effective_at"]) <= retrieved:
            raise ContractError("retrieval preceded grant")
        if (record.get("purpose") != "local_research" or record.get("storage_scope") != "private"
                or record.get("raw_storage_allowed") is not True
                or record.get("normalized_storage_allowed") is not True):
            raise ContractError("raw/normalized local research storage permission absent")
        conditions = record.get("deletion_conditions")
        if not isinstance(conditions, list) or not conditions or any(
                not isinstance(c, str) or not c.strip() for c in conditions):
            raise ContractError("explicit deletion conditions required")
        if record.get("active_deletion_conditions") != []:
            raise ContractError("active/unknown deletion conditions prevent conversion")
        if record.get("retention") == "until_expiry":
            expiry, deletion = _stamp(record.get("expires_at")), _stamp(record.get("delete_by"))
            if not checked < expiry <= deletion or "expiry" not in conditions:
                raise ContractError("expired/inconsistent retention or missing expiry deletion condition")
        elif record.get("retention") == "perpetual_grant":
            if record.get("expires_at") is not None or record.get("delete_by") is not None:
                raise ContractError("perpetual grant must explicitly have null expiry/deadline")
        else:
            raise ContractError("unsupported/unknown retention")
        rights[source] = record
    calendar = _table(single("calendar"), CALENDAR)
    if not calendar:
        raise ContractError("nonempty calendar required")
    sessions = {}
    for row in calendar:
        day = _day(row["trade_date"])
        opened, closed = _stamp(row["open_at"]), _stamp(row["close_at"])
        if day in sessions or opened >= closed or (sessions and opened <= _stamp(next(reversed(sessions.values()))["close_at"])):
            raise ContractError("duplicate session or invalid session hours")
        sessions[day] = row
    if list(sessions) != sorted(sessions):
        raise ContractError("calendar must be strictly ascending")
    if manifest.get("calendar_evidence") not in evidence:
        raise ContractError("calendar must bind hashed evidence; times cannot be guessed")
    instruments = manifest.get("instruments")
    if not isinstance(instruments, dict) or not instruments:
        raise ContractError("instrument metadata required")
    for symbol, meta in instruments.items():
        _text(symbol, "symbol")
        if not isinstance(meta, dict):
            raise ContractError("instrument metadata must be an object")
        if (meta.get("source_id") not in sources or meta.get("price_unit") != "currency_per_share"
                or meta.get("volume_unit") != "shares" or meta.get("price_basis") != "raw_unadjusted"):
            raise ContractError("explicit raw price/share volume units and source required")
        _text(meta.get("currency"), "currency")
        try:
            zone = ZoneInfo(_text(meta.get("timezone"), "exchange timezone"))
        except ZoneInfoNotFoundError as exc:
            raise ContractError("unknown exchange timezone") from exc
        if any(_stamp(session[k]).astimezone(zone).date().isoformat() != day
               for day, session in sessions.items() for k in ("open_at", "close_at")):
            raise ContractError("session timestamps differ from exchange-local trade date")
    bars, seen = [], set()
    for row in _table(single("bars"), BARS):
        symbol, day = row["symbol"], _day(row["trade_date"])
        if symbol not in instruments or day not in sessions or (day, symbol) in seen:
            raise ContractError("unknown symbol/session or duplicate bar")
        seen.add((day, symbol))
        meta = instruments[symbol]
        if any(row[k] != meta[k] for k in ("currency", "source_id", "price_unit", "volume_unit", "price_basis")):
            raise ContractError("bar source/unit/basis conflicts with instrument")
        prices = {k: decimal(row[k], k, "0.000000001") for k in ("open", "high", "low", "close")}
        if not prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]:
            raise ContractError("invalid OHLC ordering")
        timing = _timing(row, evidence, retrieved, _stamp(sessions[day]["close_at"]))
        bars.append({**row, **{k: str(v) for k, v in prices.items()},
                     "volume": _integer(row["volume"], "actual share volume"), **timing,
                     "open_at": sessions[day]["open_at"], "close_at": sessions[day]["close_at"]})
    expected = {(day, symbol) for day in sessions for symbol in instruments}
    if seen != expected:
        raise ContractError("missing entire bar; conversion does not fill gaps")
    actions, ids = [], set()
    for row in _table(single("actions"), ACTIONS):
        symbol = row["symbol"]
        if symbol not in instruments or row["source_id"] not in sources:
            raise ContractError("action source/symbol unknown")
        aid = _text(row["id"], "action ID")
        if aid in ids:
            raise ContractError("duplicate action ID")
        ids.add(aid)
        if row["currency"] != instruments[symbol]["currency"] or row["effective_date"] not in sessions:
            raise ContractError("action currency/session differs")
        timing = _timing(row, evidence, retrieved)
        if row["type"] == "split":
            numerator = _integer(row["numerator"], "split numerator", 1)
            denominator = _integer(row["denominator"], "split denominator", 1)
            if any(row[k] for k in ("pay_date", "gross_per_share", "share_basis")):
                raise ContractError("split contains dividend fields")
            payload = {"numerator": numerator, "denominator": denominator}
        elif row["type"] == "dividend":
            if row["numerator"] or row["denominator"] or row["share_basis"] != "post_split":
                raise ContractError("dividend must explicitly use post-split shares")
            if _day(row["pay_date"]) < row["effective_date"]:
                raise ContractError("payment before ex-date")
            payload = {"gross_per_share": str(decimal(row["gross_per_share"], "gross dividend", 0))}
        else:
            raise ContractError("unsupported action type")
        actions.append({**row, **payload, **timing})
    if digest(manifest_path) != manifest_hash or any(digest(paths[n]) != m["sha256"] for n, m in files.items()):
        raise ContractError("bundle changed during normalization")
    return {"schema_version": VERSION, "data_kind": kind,
            "classification": "NORMALIZED_CONVERSION_ONLY", "historical_engine_ready": False,
            "checked_at": checked_at, "retrieved_at": manifest["retrieved_at"],
            "input_manifest_sha256": manifest_hash, "input_files": files,
            "sources": rights, "instruments": copy.deepcopy(instruments),
            "calendar": calendar, "bars": sorted(bars, key=lambda b: (b["trade_date"], b["symbol"])),
            "actions": sorted(actions, key=lambda a: (a["effective_date"], a["id"])),
            "limitations": ["Review records do not independently certify legal rights or PIT archives",
                            "Modelled availability remains an assumption; revisions may be unknown",
                            "Raw conversion only: no return, cash-interest, FX, costs or execution acceptance"]}


def snapshot_as_of(artifact, cutoff):
    """Information visible by a cutoff, preserving its observed/modelled labels.

    Future effective corporate actions can be known already. This view contains
    announcements, not applied entitlements; payment can fall outside the bars.
    """
    if artifact.get("schema_version") != VERSION:
        raise ContractError("unsupported artifact version")
    stamp = _stamp(cutoff)
    return {"cutoff": cutoff, "data_kind": artifact["data_kind"],
            "bars": [copy.deepcopy(b) for b in artifact["bars"] if _stamp(b["available_at"]) <= stamp],
            "actions": [copy.deepcopy(a) for a in artifact["actions"] if _stamp(a["available_at"]) <= stamp]}


def import_history(manifest_path, output_dir, private_root, *, checked_at):
    artifact = normalize(manifest_path, private_root, checked_at=checked_at)
    output = _private(output_dir, private_root)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "normalized.json", artifact)
    manifest = {"schema_version": VERSION, "classification": artifact["classification"],
                "data_kind": artifact["data_kind"], "historical_engine_ready": False,
                "files": {"normalized.json": digest(output / "normalized.json")},
                "input_manifest_sha256": artifact["input_manifest_sha256"],
                "rights_recheck_required_before_use": True}
    write_json(output / "manifest.json", manifest)
    return manifest


def write_control_fixture(root):
    """Create invented unit controls only; never ingest or relabel market rows."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    def csv_file(name, fields, table):
        with (root / name).open("x", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=sorted(fields))
            writer.writeheader()
            writer.writerows(table)
    (root / "grant.txt").write_text("Invented controls authored for this converter; permission is not a vendor grant.\n")
    rights = {"source_id": "CONTROL", "source_nature": "invented_control",
              "terms_evidence": "grant.txt", "reviewer": "control author", "authority": "fixture authorship",
              "scope": "invented test rows only", "review_note": "No market data included",
              "purpose": "local_research", "storage_scope": "private", "raw_storage_allowed": True,
              "normalized_storage_allowed": True, "effective_at": "2020-01-01T00:00:00Z",
              "reviewed_at": "2020-01-01T00:00:00Z", "retention": "perpetual_grant",
              "expires_at": None, "delete_by": None, "deletion_conditions": ["grant withdrawn"],
              "active_deletion_conditions": []}
    write_json(root / "rights.json", rights)
    calendar = [{"trade_date": f"2020-01-0{i}", "open_at": f"2020-01-0{i}T14:30:00Z",
                 "close_at": f"2020-01-0{i}T21:00:00Z"} for i in (2, 3, 6)]
    timing = lambda day: {"availability_basis": "modelled", "available_at": day + "T21:05:00Z",
                          "observed_received_at": "", "timing_evidence": "", "model_rule": "invented close plus five minutes",
                          "revision_status": "unknown"}
    bars = []
    for day, price, volume in (("2020-01-02", "100", "1000"), ("2020-01-03", "49", "2000"),
                               ("2020-01-06", "50", "2200")):
        bars.append({"trade_date": day, "symbol": "TEST", "source_id": "CONTROL", "open": price,
                     "high": price, "low": price, "close": price, "volume": volume, "currency": "USD",
                     "price_basis": "raw_unadjusted", "price_unit": "currency_per_share",
                     "volume_unit": "shares", **timing(day)})
    common = {"symbol": "TEST", "source_id": "CONTROL", "currency": "USD",
              "effective_date": "2020-01-03", **timing("2020-01-03")}
    actions = [{**common, "id": "split-2-1", "type": "split", "numerator": "2", "denominator": "1",
                "pay_date": "", "gross_per_share": "", "share_basis": ""},
               {**common, "id": "div-1", "type": "dividend", "numerator": "", "denominator": "",
                "pay_date": "2020-01-08", "gross_per_share": "1", "share_basis": "post_split"}]
    csv_file("calendar.csv", CALENDAR, calendar)
    csv_file("bars.csv", BARS, bars)
    csv_file("actions.csv", ACTIONS, actions)
    files = {name: {"role": role, "sha256": digest(root / name)} for name, role in (
        ("calendar.csv", "calendar"), ("bars.csv", "bars"), ("actions.csv", "actions"),
        ("grant.txt", "evidence"), ("rights.json", "rights"))}
    write_json(root / "input.json", {"schema_version": VERSION, "data_kind": "invented_control",
               "retrieved_at": "2020-01-10T00:00:00Z", "files": files,
               "sources": {"CONTROL": "rights.json"}, "calendar_evidence": "grant.txt",
               "instruments": {"TEST": {"currency": "USD", "timezone": "America/New_York",
                   "source_id": "CONTROL", "price_basis": "raw_unadjusted",
                   "price_unit": "currency_per_share", "volume_unit": "shares"}}})
    return root / "input.json"


def _rewrite_control(root, name, table, fields):
    # For deliberate mutations of our own fixture only, never user input.
    with (root / name).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=sorted(fields))
        writer.writeheader()
        writer.writerows(table)
    manifest = load_json(root / "input.json")
    manifest["files"][name]["sha256"] = digest(root / name)
    (root / "input.json").write_text(json.dumps(manifest))


def run_controls(output_dir):
    """Immutable synthetic converter evidence, with known answers and mutants."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    checked_at = "2020-01-10T00:00:00Z"
    manifest = write_control_fixture(output / "fixture")
    base = normalize(manifest, output, checked_at=checked_at)
    write_json(output / "normalized-control.json", base)
    reports = []
    def record(name, expected, actual):
        reports.append({"id": name, "passed": expected == actual, "expected": expected, "actual": actual})
    split = next(a for a in base["actions"] if a["type"] == "split")
    dividend = next(a for a in base["actions"] if a["type"] == "dividend")
    # Test normalized values against independent hand-calculated unit answers.
    post_split_shares = 100 * split["numerator"] // split["denominator"]
    units = {"post_split_shares": post_split_shares,
             "gross_cash": str(decimal(dividend["gross_per_share"], "cash") * post_split_shares),
             "raw_ex_close": base["bars"][1]["close"], "raw_ex_volume": base["bars"][1]["volume"],
             "payment_outside_bar_window": dividend["pay_date"] > base["calendar"][-1]["trade_date"]}
    record("split_dividend_units", {"post_split_shares": 200, "gross_cash": "200", "raw_ex_close": "49",
                                   "raw_ex_volume": 2000, "payment_outside_bar_window": True}, units)
    cutoff = "2020-01-03T21:05:00Z"
    prefix = snapshot_as_of(base, cutoff)
    record("prefix_visibility", {"bars": 2, "actions": 2, "basis": "modelled"},
           {"bars": len(prefix["bars"]), "actions": len(prefix["actions"]),
            "basis": prefix["bars"][0]["availability_basis"]})
    mutants = ["missing_bar", "duplicate_bar", "volume_unit", "price_basis", "fractional_volume",
               "naive_time", "before_close", "invented_observed", "dividend_basis", "zero_split",
               "duplicate_action", "wrong_session_date", "unknown_timezone", "rights_storage", "rights_expired", "rights_provenance", "hash_tamper"]
    for mutation in mutants + ["future_perturbation", "future_truncation", "observed_control"]:
        with TemporaryDirectory(dir=output) as temp:
            root = Path(temp) / "fixture"
            candidate = write_control_fixture(root)
            bars, actions, calendar = rows(root / "bars.csv"), rows(root / "actions.csv"), rows(root / "calendar.csv")
            if mutation == "missing_bar": bars.pop(1)
            if mutation == "duplicate_bar": bars.append(bars[0].copy())
            if mutation == "volume_unit": bars[0]["volume_unit"] = "lots"
            if mutation == "price_basis": bars[0]["price_basis"] = "adjusted"
            if mutation == "fractional_volume": bars[0]["volume"] = "1.5"
            if mutation == "naive_time": bars[0]["available_at"] = "2020-01-02T21:05:00"
            if mutation == "before_close": bars[0]["available_at"] = "2020-01-02T20:00:00Z"
            if mutation == "invented_observed": bars[0]["availability_basis"] = "observed"
            if mutation == "dividend_basis": actions[1]["share_basis"] = "pre_split"
            if mutation == "zero_split": actions[0]["numerator"] = "0"
            if mutation == "duplicate_action": actions.append(actions[0].copy())
            if mutation == "wrong_session_date": calendar[0]["open_at"] = "2020-01-01T14:30:00Z"
            if mutation == "unknown_timezone":
                contract = load_json(candidate)
                contract["instruments"]["TEST"]["timezone"] = "Unknown/Invented"
                candidate.write_text(json.dumps(contract))
            if mutation.startswith("rights_"):
                rights = load_json(root / "rights.json")
                if mutation == "rights_storage": rights["raw_storage_allowed"] = False
                if mutation == "rights_provenance": rights["source_nature"] = "historical_market"
                if mutation == "rights_expired":
                    rights.update(retention="until_expiry", expires_at="2020-01-09T00:00:00Z",
                                  delete_by="2020-01-09T00:00:00Z", deletion_conditions=["expiry"])
                (root / "rights.json").write_text(json.dumps(rights))
                contract = load_json(candidate)
                contract["files"]["rights.json"]["sha256"] = digest(root / "rights.json")
                candidate.write_text(json.dumps(contract))
            if mutation == "future_perturbation":
                for k in ("open", "high", "low", "close"): bars[-1][k] = "999"
                bars[-1]["volume"] = "999999"
            if mutation == "future_truncation":
                bars.pop()
                calendar.pop()
            if mutation == "observed_control":
                bars[0].update(availability_basis="observed", observed_received_at=bars[0]["available_at"],
                               timing_evidence="grant.txt", model_rule="", revision_status="as_of_archive")
            _rewrite_control(root, "bars.csv", bars, BARS)
            _rewrite_control(root, "actions.csv", actions, ACTIONS)
            _rewrite_control(root, "calendar.csv", calendar, CALENDAR)
            if mutation == "hash_tamper":
                with (root / "bars.csv").open("a") as stream: stream.write("corruption\n")
            try:
                converted = normalize(candidate, output, checked_at=checked_at)
            except ContractError as exc:
                record(mutation, "rejected" if mutation in mutants else "converted", "rejected")
                reports[-1]["rejection_reason"] = str(exc)
            else:
                if mutation in mutants:
                    record(mutation, "rejected", "converted")
                elif mutation == "observed_control":
                    record(mutation, "observed", converted["bars"][0]["availability_basis"])
                else:
                    record(mutation, canonical_hash(prefix), canonical_hash(snapshot_as_of(converted, cutoff)))
                    if mutation == "future_perturbation":
                        record("future_mutation_has_effect", "999", converted["bars"][-1]["close"])
    report = {"schema_version": VERSION, "classification": "SYNTHETIC_IMPORT_CONTROLS_ONLY",
              "accepted": False, "reports": reports,
              "historical_engine_ready": False,
              "limitations": ["No historical market data or independently reviewed vendor rights used",
                              "Causal view controls validate conversion information filtering, not a strategy engine"]}
    report["accepted"] = control_report_accepted(report)
    write_json(output / "report.json", report)
    write_json(output / "manifest.json", {"schema_version": VERSION,
               "files": {str(p.relative_to(output)): digest(p) for p in sorted(output.rglob("*")) if p.is_file()}})
    return report


def control_report_accepted(report):
    """A missing/duplicated row or forged passed flag cannot shrink acceptance."""
    if (report.get("schema_version") != VERSION
            or report.get("classification") != "SYNTHETIC_IMPORT_CONTROLS_ONLY"):
        return False
    reports = report.get("reports")
    if not isinstance(reports, list) or any(not isinstance(r, dict) for r in reports):
        return False
    ids = [r.get("id") for r in reports]
    if (any(not isinstance(value, str) for value in ids)
            or len(ids) != len(EXPECTED_CONTROL_IDS) or set(ids) != set(EXPECTED_CONTROL_IDS)):
        return False
    return all("expected" in r and "actual" in r and r["expected"] == r["actual"]
               and r.get("passed") is True for r in reports)
