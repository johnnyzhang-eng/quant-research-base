"""Explicit conditional stock-account layout. No synthetic relabelling.

The adapter computes entry FX once and binds all three inputs. The execution
bridge must reconstruct the wallet and use dated fee/settlement hooks; no static
fee placeholders or default capacity are supplied here.
"""
import copy
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from decimal import Decimal

from . import cash_account as cash
from .evidence import ContractError, canonical_hash
from .historical_input import VERSION as INPUT_VERSION, _day, _integer, _stamp
from .historical_signals import ASSETS, generate
from .trading_terms import TradingTerms, SCENARIOS, _evidence, control_document

VERSION = "historical-stock/1"
MODEL_VERSION = "historical-account-model/1"
MODEL_FIELDS = {"schema_version", "scenario", "start", "end", "initial", "seed_volume_history",
                "instruments", "terms", "cash_rules", "fx", "execution", "withholding", "performance_boundary", "dividend_posting"}


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ContractError(f"{label}: exact versioned fields required")


def _number(value, label, minimum=0):
    try:
        number = cash.number(value, label)
    except cash.CashContractError as exc:
        raise ContractError(str(exc)) from exc
    if number < Decimal(str(minimum)):
        raise ContractError(f"{label}: value below minimum")
    return number


def _quote_price_clock(quote, use_at):
    if _stamp(quote["quoted_at"]) > _stamp(use_at):
        raise ContractError("future FX price cannot value or fund an earlier account")


def _build(artifact, features, model):
    version = model.get("schema_version") if isinstance(model, dict) else None
    expected_fields = MODEL_FIELDS | ({"expense_review", "fixed_expenses"} if version == "historical-account-model/2" else set())
    _keys(model, expected_fields, "historical account model")
    if version not in {MODEL_VERSION, "historical-account-model/2"} or model["scenario"] not in SCENARIOS:
        raise ContractError("explicit historical account model and C0-C3 scenario required")
    if (artifact.get("schema_version") != INPUT_VERSION
            or artifact.get("classification") != "NORMALIZED_CONVERSION_ONLY"
            or artifact.get("data_kind") not in {"historical_market", "invented_control"}):
        raise ContractError("explicit normalized market/control input required")
    kind = artifact["data_kind"]
    if not artifact.get("sources") or any(record.get("source_nature") != kind for record in artifact["sources"].values()):
        raise ContractError("source rights provenance differs from market/control label")
    if features.get("data_kind") != kind or features.get("normalized_artifact_sha256") != canonical_hash(artifact):
        raise ContractError("features do not bind this normalized artifact/provenance")
    if features.get("trade_start") != model["start"] or features.get("end_date") != model["end"]:
        raise ContractError("account window differs from explicit signal scope")
    delay = 1 if model["scenario"] == "C3" else 0
    if features.get("delay_sessions") != delay:
        raise ContractError("scenario delay differs from fixed signal execution schedule")
    reproduced = generate(artifact, features["variant"], trade_start=model["start"], end_date=model["end"],
                          k=features["k"], delay_sessions=delay)
    if canonical_hash(features) != canonical_hash(reproduced):
        raise ContractError("feature decisions/provenance were changed after generation")
    _day(model["start"])
    _day(model["end"])
    calendar = artifact["calendar"]
    sessions = [r["trade_date"] for r in calendar]
    lookup = {r["trade_date"]: r for r in calendar}
    start, end = model["start"], model["end"]
    first_index = sessions.index(start)
    if first_index < 20:
        raise ContractError("twenty actual prior sessions and opening boundary required")
    boundary = sessions[first_index-1]
    run_days = [d for d in sessions if start <= d <= end]
    first_open = lookup[start]["open_at"]
    _keys(model["initial"], {"holdings", "marks"}, "initial")
    _keys(model["initial"]["holdings"], ASSETS, "initial holdings")
    _keys(model["initial"]["marks"], ASSETS, "initial marks")
    _keys(model["seed_volume_history"], ASSETS, "prior volume history")
    _keys(model["instruments"], ASSETS, "account instruments")
    source_bars = {(b["trade_date"], b["symbol"]): b for b in artifact["bars"]}
    seed, seed_provenance, instruments = {}, {}, {}
    for symbol in ASSETS:
        if type(model["initial"]["holdings"][symbol]) is not int or model["initial"]["holdings"][symbol] != 0:
            raise ContractError("funded account starts with explicit zero stocks; transfers unsupported")
        anchor = source_bars[boundary, symbol]
        mark = _number(model["initial"]["marks"][symbol], "initial mark", "0.000000001")
        if mark != _number(anchor["close"], "boundary close") or _stamp(anchor["available_at"]) >= _stamp(first_open):
            raise ContractError("opening mark differs from timely immediately preceding raw close")
        values = model["seed_volume_history"][symbol]
        if not isinstance(values, list) or len(values) != 20:
            raise ContractError("exactly twenty explicit actual prior volumes required")
        records = [source_bars[d, symbol] for d in sessions[first_index-20:first_index]]
        if any(_number(v, "prior volume") != _number(row["volume"], "source volume") or
               _stamp(row["available_at"]) >= _stamp(first_open) for v, row in zip(values, records)):
            raise ContractError("seed volumes differ from actual timely prior-session source bars")
        seed[symbol] = [str(_number(v, "prior volume")) for v in values]
        seed_provenance[symbol] = [{"date": r["trade_date"], "volume": r["volume"], "available_at": r["available_at"],
                                    "availability_basis": r["availability_basis"], "record_sha256": canonical_hash(r)} for r in records]
        meta = model["instruments"][symbol]
        _keys(meta, {"lot_size", "price_tick"}, "account instrument")
        if type(meta["lot_size"]) is not int or meta["lot_size"] != 1:
            raise ContractError("this bridge accepts explicit one-share lots only")
        _number(meta["price_tick"], "price tick", "0.000000001")
        instruments[symbol] = {"currency": "USD", **copy.deepcopy(meta)}
    terms = TradingTerms(model["terms"])
    try:
        if version == "historical-account-model/2":
            from .historical_expenses import ExpenseWallet
            wallet = ExpenseWallet(model["cash_rules"], expense_review=model["expense_review"], fixed_expenses=model["fixed_expenses"])
            if model["expense_review"]["coverage_end_date"] < model["end"]:
                raise ContractError("fixed expense review does not cover the account window")
        else:
            wallet = cash.CashAccount(model["cash_rules"])
    except cash.CashContractError as exc:
        raise ContractError(str(exc)) from exc
    if kind == "invented_control":
        if model["terms"]["data_kind"] != "invented_control" or model["cash_rules"]["classification"] != "synthetic_control":
            raise ContractError("invented market controls require invented terms/cash provenance")
    elif model["terms"]["data_kind"] != "reviewed_model" or model["cash_rules"]["classification"] != "conditional_historical_model":
        raise ContractError("market data requires reviewed terms and conditional cash model; no synthetic relabelling")
    _keys(model["performance_boundary"], {"date", "fx_mark"}, "performance boundary")
    if model["performance_boundary"]["date"] != boundary:
        raise ContractError("performance boundary must be immediately preceding actual session")
    if wallet.start_at.astimezone(wallet.timezone).date().isoformat() != boundary:
        raise ContractError("cash funding must start at boundary-day midnight")
    for day in [boundary] + run_days:
        if _stamp(wallet.cutoff_at(day)) < _stamp(lookup[day]["close_at"]):
            raise ContractError("cash cutoff before regular close is unsupported by daily mark bridge")
    _keys(model["fx"], {"entry", "close_marks", "exit_quotes"}, "FX")
    entry = model["fx"]["entry"]
    _keys(entry, {"at", "principal_cny", "execution_quote"}, "entry FX")
    if not _stamp(lookup[boundary]["close_at"]) <= _stamp(entry["at"]) <= _stamp(first_open):
        raise ContractError("entry must occur after opening NAV boundary and before first trade open")
    _keys(model["fx"]["close_marks"], run_days, "daily close FX marks")
    _keys(model["fx"]["exit_quotes"], run_days, "daily explicit exit quotes/nulls")
    try:
        _quote_price_clock(model["performance_boundary"]["fx_mark"], lookup[boundary]["close_at"])
        opening_nav = wallet.snapshot(as_of=lookup[boundary]["close_at"], fx_mark=model["performance_boundary"]["fx_mark"], usd_assets="0")
        for day in run_days:
            at = wallet.cutoff_at(day)
            mark = model["fx"]["close_marks"][day]
            _quote_price_clock(mark, at)
            exit_quote = model["fx"]["exit_quotes"][day]
            if exit_quote is not None: _quote_price_clock(exit_quote, at)
            wallet.snapshot(as_of=at, fx_mark=mark, usd_assets="0", exit_quote=exit_quote)
        current_day = wallet.last_accrued_day + timedelta(days=1)
        while _stamp(wallet.cutoff_at(current_day.isoformat())) < _stamp(entry["at"]):
            if version == "historical-account-model/2":
                wallet.book_expenses(as_of=wallet.cutoff_at(current_day.isoformat()))
            wallet.accrue_day(current_day.isoformat(), as_of=wallet.cutoff_at(current_day.isoformat()))
            if version == "historical-account-model/2":
                wallet.book_expenses(as_of=wallet.cutoff_at(current_day.isoformat()))
            current_day += timedelta(days=1)
        _quote_price_clock(entry["execution_quote"], entry["at"])
        wallet.exchange("CNY_TO_USD", entry["principal_cny"], execution_quote=entry["execution_quote"],
                        as_of=entry["at"], event_id="entry", extra_spread_rate="0.0025" if model["scenario"] in {"C2", "C3"} else "0")
    except cash.CashContractError as exc:
        raise ContractError(str(exc)) from exc
    _keys(model["execution"], {"rows"}, "opening execution declarations")
    execution = model["execution"]["rows"]
    if not isinstance(execution, list):
        raise ContractError("explicit opening rows required")
    openings = {}
    for row in execution:
        _keys(row, {"date", "symbol", "open_quote_known_at", "open_status", "open_capacity_shares", "evidence"}, "opening declaration")
        key = row["date"], row["symbol"]
        if key in openings or key[0] not in run_days or key[1] not in ASSETS:
            raise ContractError("duplicate/out-of-window opening declaration")
        _evidence(row["evidence"])
        if kind == "invented_control" and row["evidence"]["basis"] != "invented_control":
            raise ContractError("opening control evidence provenance differs")
        if kind == "historical_market" and row["evidence"]["basis"] == "invented_control":
            raise ContractError("invented opening evidence cannot support market account model")
        known = _stamp(row["open_quote_known_at"])
        if row["open_status"] not in {"tradable", "halted", "unknown"}:
            raise ContractError("opening tradability must explicitly be tradable/halted/unknown")
        capacity = row["open_capacity_shares"]
        if capacity is not None and (type(capacity) is not int or capacity < 0):
            raise ContractError("opening capacity must be nonnegative integer shares or explicit null")
        if row["open_status"] == "tradable" and known > _stamp(lookup[key[0]]["open_at"]):
            raise ContractError("tradable opening quote was unavailable at modeled opening")
        openings[key] = row
    if set(openings) != {(d, s) for d in run_days for s in ASSETS}:
        raise ContractError("every account symbol/session needs an explicit opening declaration")
    bars, term_provenance = [], []
    for day in run_days:
        rule = terms.rule(day, use_at=lookup[day]["open_at"])
        # Cover every possible trade day, not only days eventually producing fills.
        for side in ("BUY", "SELL"):
            terms.settlement_date(trade_date=day, side=side, sessions=sessions, use_at=lookup[day]["open_at"])
        for symbol in ASSETS:
            if _number(instruments[symbol]["price_tick"], "instrument tick") != _number(rule["execution"]["price_tick"], "dated execution tick"):
                raise ContractError("declared instrument tick differs from dated model tick")
            original = source_bars[day, symbol]
            if original.get("source_id") != artifact["instruments"][symbol].get("source_id"):
                raise ContractError("account bar source differs from normalized instrument")
            values = {key: _number(original[key], key, "0.000000001") for key in ("open", "high", "low", "close")}
            if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
                raise ContractError("account raw OHLC ordering differs")
            _integer(original["volume"], "actual share volume", 0)
            opening = openings[day, symbol]
            bars.append({"date": day, "symbol": symbol, **{key: copy.deepcopy(original[key]) for key in
                         ("open", "high", "low", "close", "volume", "open_at", "close_at", "available_at")},
                         **{key: copy.deepcopy(opening[key]) for key in ("open_quote_known_at", "open_status", "open_capacity_shares")},
                         "execution_evidence": copy.deepcopy(opening["evidence"]),
                         "availability_basis": original["availability_basis"], "revision_status": original["revision_status"]})
        term_provenance.append({"date": day, "rule_id": rule["id"], "evidence": rule["evidence"]})
    posting = model["dividend_posting"]
    _keys(posting, {"local_time", "timezone", "basis", "evidence"}, "dividend posting model")
    try:
        posting_time = time.fromisoformat(posting["local_time"])
        posting_zone = ZoneInfo(posting["timezone"])
    except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        raise ContractError("explicit dividend local time/timezone required") from exc
    if posting_time.tzinfo is not None or posting["basis"] != "modelled_at_declared_cutoff":
        raise ContractError("explicit modelled dividend cutoff basis required")
    _evidence(posting["evidence"])
    if ((kind == "invented_control" and posting["evidence"]["basis"] != "invented_control")
            or (kind == "historical_market" and posting["evidence"]["basis"] == "invented_control")):
        raise ContractError("dividend posting model provenance differs")
    relevant = [a for a in artifact["actions"] if start <= a["effective_date"] <= end]
    dividends = {a["id"] for a in relevant if a["type"] == "dividend"}
    _keys(model["withholding"], dividends, "explicit account dividend withholding")
    actions = []
    for original in relevant:
        if _stamp(original["available_at"]) > _stamp(lookup[original["effective_date"]]["open_at"]):
            raise ContractError("late effective corporate action requires an unsupported correction path; no backfill")
        if original.get("source_id") not in artifact["sources"]:
            raise ContractError("account corporate action source lacks rights binding")
        if original.get("currency") != "USD":
            raise ContractError("account corporate actions require explicit USD units")
        action = {**copy.deepcopy(original), "known_at": original["available_at"]}
        if action["type"] == "dividend":
            rate = _number(model["withholding"][action["id"]], "withholding")
            if rate > 1: raise ContractError("withholding exceeds one")
            action["withholding_rate"] = str(rate)
            pay_date = _day(action["pay_date"])
            if pay_date < action["effective_date"]:
                raise ContractError("dividend payment before effective date")
            payment_at = datetime.combine(date.fromisoformat(pay_date), posting_time, posting_zone).isoformat()
            if _stamp(payment_at) < _stamp(original["available_at"]):
                raise ContractError("modelled dividend posting before payment information was available")
            action.update(payment_at=payment_at, payment_basis=posting["basis"],
                          payment_evidence=copy.deepcopy(posting["evidence"]))
        actions.append(action)
    intents = [{**copy.deepcopy(i), "source": f"S02_{features['variant']}_HISTORICAL_MODEL_TARGET"} for i in features["intents"]]
    return {"account_model_version": VERSION, "classification": "conditional_account_model", "data_kind": kind,
            "historical_engine_ready": False, "valuation_policy": "carry_last_known_close_at_cash_cutoff", "account_currency": "USD", "symbols": list(ASSETS),
            "start": start, "end": end, "instruments": instruments,
            "calendar": {"sessions": sessions, "month_end_sessions": [], "source": "reviewed inventory declaration; no independent certification"},
            "initial": {"holdings": copy.deepcopy(model["initial"]["holdings"]), "marks": copy.deepcopy(model["initial"]["marks"]),
                        "settled_cash": str(wallet.settled["USD"])}, "seed_volume_history": seed,
            "seed_volume_provenance": seed_provenance, "bars": bars, "actions": actions, "control_intents": intents,
            "historical_artifact": copy.deepcopy(artifact), "historical_features": copy.deepcopy(features), "historical_model": copy.deepcopy(model),
            "lineage": {"artifact_sha256": canonical_hash(artifact), "features_sha256": canonical_hash(features), "model_sha256": canonical_hash(model),
                        "terms_sha256": terms.sha256, "cash_rules_sha256": canonical_hash(model["cash_rules"])},
            "cash_entry_journal": copy.deepcopy(wallet.events), "cash_entry_balances": wallet.balances(),
            "opening_cny_snapshot": opening_nav, "dated_terms_provenance": term_provenance,
            "limitations": ["Conditional account model only; does not complete S02 or certify source/funding/PIT rights",
                            "Opening daily prices are model anchors, not evidence of actual opening fills",
                            "Dated terms and cash hooks are mandatory; no static fee/settlement fallback"]}


def prepare(artifact, features, model):
    """Build a defensive, fully bound layout with independently computed entry FX."""
    try:
        return _build(artifact, features, model)
    except cash.CashContractError as exc:
        raise ContractError(str(exc)) from exc
    except (KeyError, IndexError, TypeError) as exc:
        raise ContractError("malformed bound historical account input") from exc


def validate_account_input(data):
    """Rebuild from all bound inputs; changed prepared fields cannot bypass gates."""
    if not isinstance(data, dict) or data.get("account_model_version") != VERSION:
        raise ContractError("explicit historical-stock/1 account input required")
    if any(name not in data for name in ("historical_artifact", "historical_features", "historical_model")):
        raise ContractError("all three bound historical account inputs required")
    expected = prepare(data["historical_artifact"], data["historical_features"], data["historical_model"])
    if canonical_hash(data) != canonical_hash(expected):
        raise ContractError("prepared account layout/lineage differs from bound source inputs")


def control_model(artifact, scenario="C0", *, start="2020-10-01", end="2020-11-03"):
    if artifact.get("data_kind") != "invented_control":
        raise ContractError("invented control model helper refuses market data")
    sessions = [r["trade_date"] for r in artifact["calendar"]]
    index = sessions.index(start)
    boundary = sessions[index-1]
    source = {(b["trade_date"], b["symbol"]): b for b in artifact["bars"]}
    rules = cash._control_rules(initial="8000", start=boundary, crediting="daily")
    rules["knowledge_clock"]["freeze_at"] = "2021-02-01T00:00:00+00:00"
    terms = control_document()
    terms["frozen_at"] = "2021-02-01T00:00:00Z"
    terms["rules"] = [terms["rules"][0]]
    terms["rules"][0].update(id="invented-2020", **{"from": "2020-01-01", "through": "2021-01-31", "known_at": "2019-12-01T00:00:00Z"})
    entry_at = start + "T14:00:00+00:00"
    days = [d for d in sessions if start <= d <= end]
    evidence = {"reference": "invented opening model, no actual fill evidence", "sha256": "b"*64, "basis": "invented_control"}
    model = {"schema_version": MODEL_VERSION, "scenario": scenario, "start": start, "end": end,
        "initial": {"holdings": {s: 0 for s in ASSETS}, "marks": {s: source[boundary,s]["close"] for s in ASSETS}},
        "seed_volume_history": {s: [source[d,s]["volume"] for d in sessions[index-20:index]] for s in ASSETS},
        "instruments": {s: {"lot_size": 1, "price_tick": "0.01"} for s in ASSETS},
        "terms": terms, "cash_rules": rules,
        "fx": {"entry": {"at": entry_at, "principal_cny": "7070", "execution_quote": cash._control_quote(entry_at, execution=True)},
               "close_marks": {d: cash._control_quote(d+"T21:00:00+00:00") for d in days},
               "exit_quotes": {d: None for d in days}},
        "execution": {"rows": [{"date": d, "symbol": s, "open_quote_known_at": d+"T14:30:00Z",
                 "open_status": "tradable", "open_capacity_shares": 100,
                 "evidence": copy.deepcopy(evidence)} for d in days for s in ASSETS]},
        "dividend_posting": {"local_time": "16:30:00", "timezone": "America/New_York",
                             "basis": "modelled_at_declared_cutoff", "evidence": copy.deepcopy(evidence)},
        "withholding": {}, "performance_boundary": {"date": boundary, "fx_mark": cash._control_quote(boundary+"T21:00:00+00:00")}}
    features = generate(artifact, "B0", trade_start=start, end_date=end, delay_sessions=1 if scenario == "C3" else 0)
    return model, features
