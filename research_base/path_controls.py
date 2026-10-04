"""Predeclared four-asset order and full protocol acceptance cases."""
import calendar
import copy
from datetime import date, timedelta
from decimal import Decimal

from .evidence import ContractError, write_json
from .reference import low_frequency_engine as reference
from .reference import run_controls as oracle
from .strategy_path import ASSETS, generate
from .vibe_controls import differences, execute_vibe, offline_connections, reference_projection


def allocation_cases():
    cases = []
    d = oracle.fixture(symbols=ASSETS.copy(), cash="104", prices={s: "13" for s in ASSETS})
    d["settlement"].update(cash_sessions=0, share_sessions=0); d["fees"]["minimum_commission"] = "2"
    oracle.intent(d, "2024-10-07", **{s: ".25" for s in ASSETS})
    cases.append(("A01_sequential_basket_fees", d, {"cash": "5", "equity": "96",
        "holdings": {"SPY": 2, "EFA": 2, "IEF": 2, "GLD": 1},
        "fills": [{"date": "2024-10-08", "qty": q, "price": "13", "fee": "2"} for q in (2, 2, 2, 1)]}))
    d = oracle.fixture(symbols=ASSETS.copy(), cash="100"); d["initial"]["holdings"]["GLD"] = 10
    d["settlement"].update(cash_sessions=0, share_sessions=0)
    oracle.intent(d, "2024-10-07", SPY=".25", GLD=".5")
    oracle.bar(d, "2024-10-08", "GLD", open=None, high="1000", low="10", close="1000")
    for day in ("2024-10-09", "2024-10-10", "2024-10-11"):
        oracle.price(d, day, "1000", "GLD")
    cases.append(("A02_missing_open_no_close_lookahead", d, {"cash": "50", "equity": "10100",
        "holdings": {"SPY": 5, "EFA": 0, "IEF": 0, "GLD": 10},
        "fills": [{"date": "2024-10-08", "qty": 5, "price": "10", "fee": "0"}]}))
    d = oracle.fixture(); d["settlement"].update(cash_sessions=0, share_sessions=0)
    d["seed_volume_history"]["SPY"] = ["10"]*20
    oracle.bar(d, "2024-10-07", volume="10")
    oracle.bar(d, "2024-10-08", volume="1000000000")
    oracle.intent(d, "2024-10-07", SPY=1)
    cases.append(("A03_prior_volume_not_future_day_volume", d, {"cash": "100", "equity": "100", "holdings": {"SPY": 0}, "fills": []}))
    d = oracle.fixture(); d["settlement"].update(cash_sessions=0, share_sessions=0)
    d["fees"].update(slippage_rate=".01", commission_rate=".01")
    oracle.intent(d, "2024-10-07", SPY=1)
    cases.append(("A04_cent_quotes_binary_state_tolerance", d, {"cash": "8.19", "equity": "98.19", "holdings": {"SPY": 9},
        "fills": [{"date": "2024-10-08", "qty": 9, "price": "10.1", "fee": ".91"}]}))
    d = oracle.fixture(cash="0", prices={"SPY": "1"}); d["initial"]["holdings"]["SPY"] = 1
    d["fees"]["minimum_commission"] = "2"; oracle.intent(d, "2024-10-07", SPY=0)
    cases.append(("A05_negative_net_sale_not_committed", d, {"cash": "0", "equity": "1", "holdings": {"SPY": 1}, "fills": []}))
    for _, data, _ in cases:
        data["performance_boundary_date"] = "2024-10-04"
    return cases


def full_fixture():
    ends = [date(2023, m, calendar.monthrange(2023, m)[1]) for m in range(1, 12)]
    days = sorted({d.isoformat() for e in ends for d in (e, e+timedelta(days=1))})
    prices = {"SPY": "10", "EFA": "10", "IEF": "30", "GLD": "10"}
    data = oracle.fixture(days=days, symbols=ASSETS.copy(), cash="1000", prices=prices)
    data["calendar"]["sessions"] = days + ["2023-12-29", "2024-01-02"]
    data["calendar"]["month_end_sessions"] = [d.isoformat() for d in ends]
    data["settlement"].update(cash_sessions=0, share_sessions=0)
    data["performance_boundary_date"] = "2023-01-30"
    for d in days:
        if d >= "2023-10-31":
            for s, p in (("SPY", "20"), ("IEF", "20"), ("GLD", "30")):
                oracle.price(data, d, p, s)
    return data


def full_cases():
    data = full_fixture()
    cases = []
    for name, k, cash, holdings in (("S", None, "520", [12, 0, 0, 8]),
                                    ("B0", None, "30", [12, 25, 12, 8]),
                                    ("BR", ".50", "520", [6, 12, 6, 4]),
                                    ("BC", None, "1000", [0, 0, 0, 0])):
        cases.append((f"F01_{name}_whole_path", copy.deepcopy(data), name, k,
                      {"cash": cash, "equity": "1000", "holdings": dict(zip(ASSETS, holdings))}))
    d = copy.deepcopy(data); d["initial"]["settled_cash"] = "900"; d["initial"]["holdings"]["SPY"] = 10
    d["actions"] = [oracle.dividend("gross-signal-net-wallet", "2023-10-31", "2023-11-01", amount="10", tax=".2")]
    for day in d["calendar"]["sessions"]:
        if "2023-10-31" <= day <= d["end"]:
            oracle.price(d, day, "10", "SPY")
    cases.append(("F02_gross_signal_net_wallet", d, "S", None,
                  {"cash": "540", "equity": "1080", "holdings": {"SPY": 27, "EFA": 0, "IEF": 0, "GLD": 9}}))
    d = copy.deepcopy(data)
    d["actions"] = [{"id": "split-signal", "type": "split", "symbol": "SPY", "effective_date": "2023-10-31",
                     "known_at": "2023-10-31T12:00:00Z", "numerator": 2, "denominator": 1}]
    for day in d["calendar"]["sessions"]:
        if "2023-10-31" <= day <= d["end"]:
            oracle.price(d, day, "5", "SPY")
    cases.append(("F03_split_not_momentum", d, "S", None,
                  {"cash": "760", "equity": "1000", "holdings": {"SPY": 0, "EFA": 0, "IEF": 0, "GLD": 8}}))
    return cases


def execute_case(data, variant, k, native=None):
    prepared, features = generate(data, variant, trade_start="2023-11-01", k=k)
    if native is None:
        result = reference.run(prepared, "synthetic")
        return prepared, features, result, reference_projection(prepared, result), oracle.independent_replay(prepared, result)
    projection, details = execute_vibe(prepared, native, True)
    return prepared, features, details["account_ledger"], projection, {"passed": not details["replay_errors"], "errors": details["replay_errors"]}


def run_controls(run, use_vibe=False):
    reports, causal = [], []
    native = None
    with offline_connections():
        if use_vibe:
            from backtest.engines.global_equity import GlobalEquityEngine
            native = GlobalEquityEngine
        for cid, data, expected in allocation_cases():
            write_json(run / "path_inputs" / f"{cid}.json", {"data": data, "expected": expected})
            if native is None:
                result = reference.run(data, "synthetic")
                actual, replay = reference_projection(data, result), oracle.independent_replay(data, result)
            else:
                actual, details = execute_vibe(data, native, True)
                result, replay = details, {"passed": not details["replay_errors"], "errors": details["replay_errors"]}
            errors = differences(expected, actual)
            write_json(run / "path_actual" / f"{cid}.json", result)
            reports.append({"id": cid, "passed": not errors and replay["passed"], "differences": errors, "replay": replay})
        for cid, data, variant, k, expected in full_cases():
            prepared, features, result, actual, replay = execute_case(data, variant, k, native)
            errors = differences(expected, {key: actual[key] for key in expected})
            write_json(run / "path_inputs" / f"{cid}.json", {"data": data, "variant": variant, "k": k, "expected": expected})
            write_json(run / "path_actual" / f"{cid}.json", {"features": features, "prepared_input": prepared, "result": result})
            reports.append({"id": cid, "passed": not errors and replay["passed"], "differences": errors, "replay": replay})
        data = full_fixture()
        prepared, features, result, _, _ = execute_case(data, "S", None, native)
        cutoff = "2023-11-01"
        for mutation in ("truncate", "future_price_action"):
            altered = copy.deepcopy(data)
            if mutation == "truncate":
                altered["end"] = cutoff
            else:
                for d in ("2023-11-30", "2023-12-01"):
                    oracle.price(altered, d, "40", "SPY")
                altered["actions"] = [oracle.dividend("future-only", "2023-11-30", "2023-12-01", amount="1")]
            changed_input, changed_features, changed, _, _ = execute_case(altered, "S", None, native)
            pick = lambda rows, key: [r for r in rows if r[key] <= cutoff]
            same = (features["decisions"][:1] == changed_features["decisions"][:1] and
                    pick(result["events"], "date") == pick(changed["events"], "date") and
                    pick(result["snapshots"], "date") == pick(changed["snapshots"], "date"))
            causal.append({"mutation": mutation, "cutoff": cutoff, "passed": same})
            write_json(run / "path_causal" / f"{mutation}.json", {"data": altered, "features": changed_features, "result": changed, "prefix_passed": same})
    return {"classification": "SYNTHETIC_FOUR_ASSET_FULL_PATH_NOT_HISTORY", "engine": "installed Vibe aligned" if native else "reference",
            "reports": reports, "cases_total": len(reports), "cases_passed": sum(r["passed"] for r in reports),
            "causal_controls": causal, "accepted": all(r["passed"] for r in reports) and all(c["passed"] for c in causal),
            "limitations": ["Invented sparse dates, prices, volumes, fees and opening marks; not actual market evidence",
                            "Protocol SMA pipeline feeds Vibe execution; no upstream native SMA implementation certification",
                            "BR .50 is a literal model control, not development-calibrated historical BR",
                            "BC zero-interest USD is a model control, not an attainable personal-account cash yield"]}
