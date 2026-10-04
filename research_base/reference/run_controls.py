"""Run literal known-answer fixtures through the accounting kernel and an
independent Fraction-based event replay. Never run market history or broker APIs.
"""
import copy
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from fractions import Fraction as F
from pathlib import Path
try:
    from . import low_frequency_engine as kernel
except ImportError:
    import low_frequency_engine as kernel

HERE = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def fixture(days=None, symbols=None, cash="100", prices=None):
    days = days or ["2024-10-07", "2024-10-08", "2024-10-09", "2024-10-10", "2024-10-11"]
    symbols = symbols or ["SPY"]
    prices = prices or {s: "10" for s in symbols}
    return {"data_kind": "synthetic_fixture", "account_currency":"USD", "symbols": symbols, "start": days[0], "end": days[-1],
        "calendar": {"sessions": days + ["2024-10-14", "2024-10-15"] if days[-1] < "2024-10-14" else days,
                     "month_end_sessions": [], "source": "invented control calendar, not exchange-certified"},
        "initial": {"settled_cash": cash, "holdings": {s: 0 for s in symbols}, "marks": prices.copy()},
        "instruments": {s: {"lot_size": 1, "price_tick": "0.01", "currency":"USD"} for s in symbols},
        "seed_volume_history": {s: ["100000"] * 20 for s in symbols},
        "fees": {"commission_rate": "0", "minimum_commission": "0", "platform_per_order": "0",
                 "buy_tax_rate": "0", "sell_tax_rate": "0", "slippage_rate": "0",
                 "annual_cash_rate": "0", "fee_quantum": "0.01", "evidence": "explicit synthetic constants"},
        "settlement": {"cash_sessions": 1, "share_sessions": 1,
                       "reuse_unsettled_proceeds": False, "resell_unsettled_shares": False,
                       "evidence": "invented one-session settlement control, not actual broker terms"},
        "bars": [{"date": d, "symbol": s, "open": prices[s], "high": prices[s], "low": prices[s],
                  "close": prices[s], "volume": "100000", "open_at": d+"T13:30:00Z",
                  "close_at": d+"T20:00:00Z", "available_at": d+"T20:00:00Z",
                  "open_quote_known_at": d+"T13:30:00Z", "open_status": "tradable",
                  "open_capacity_shares": 100000}
                 for d in days for s in symbols], "actions": [], "control_intents": []}


def bar(data, d, s="SPY", **changes):
    b = next(b for b in data["bars"] if b["date"] == d and b["symbol"] == s)
    b.update(changes)


def price(data, d, value, s="SPY"):
    bar(data, d, s, open=value, high=value, low=value, close=value)


def intent(data, decision, **weights):
    data["control_intents"].append({"decision_date": decision,
                                  "weights": {s: str(weights.get(s, 0)) for s in data["symbols"]}})


def dividend(aid, ex, pay, amount="1", tax="0"):
    return {"id": aid, "type": "dividend", "symbol": "SPY", "effective_date": ex,
            "pay_date": pay, "known_at": ex+"T12:00:00Z", "gross_per_share": amount,
            "withholding_rate": tax, "share_basis": "post_split"}


def rounded(v, step, how):
    q = v / step
    if how == "ceil":
        n = -((-q.numerator) // q.denominator)
    elif how == "floor":
        n = q.numerator // q.denominator
    else:
        n = (q + F(1, 2)).numerator // (q + F(1, 2)).denominator
    return n * step


def independent_replay(data, result):
    """Uses inputs + individual events, never kernel value/fee/settlement helpers
    or a kernel equity series to calculate the expected equity."""
    cash = F(data["initial"]["settled_cash"])
    holdings = dict(data["initial"]["holdings"])
    marks = {s: F(p) for s, p in data["initial"]["marks"].items()}
    pending_cash, locked_shares, receivables = {}, {}, {}
    quotes = {(b["date"], b["symbol"]): b for b in data["bars"]}
    fee = data["fees"]
    sessions = data["calendar"]["sessions"]
    replay = []
    errors = []
    def check(label, actual, expected):
        if isinstance(expected, (int, F)) and not isinstance(expected, bool):
            try:
                good = abs(F(str(actual)) - expected) <= F(1, 10**20)
            except (ValueError, TypeError):
                good = False
        else:
            good = actual == expected
        if not good:
            errors.append({"field": label, "actual": str(actual), "expected": str(expected)})
    previous = None
    for snap in result["snapshots"]:
        d = snap["date"]
        events = [e for e in result["events"] if e["date"] == d]
        by = lambda name: [e for e in events if e["type"] == name]
        if previous is not None:
            elapsed = (date.fromisoformat(d) - date.fromisoformat(previous)).days
            interest = cash * F(fee["annual_cash_rate"]) * elapsed / 365
            check(d+":interest count", len(by("INTEREST")), int(interest != 0))
            if interest:
                check(d+":interest", by("INTEREST")[0]["amount"], interest)
                cash += interest
        due_cash = {k for k, p in pending_cash.items() if p[1] <= d}
        check(d+":cash settlement IDs", {e["fill_sequence"] for e in by("CASH_SETTLEMENT")}, due_cash)
        for k in due_cash:
            match = [e for e in by("CASH_SETTLEMENT") if e["fill_sequence"] == k]
            if match:
                check(d+":settlement amount", match[0]["amount"], pending_cash[k][0])
            cash += pending_cash.pop(k)[0]
        due_shares = {k for k, p in locked_shares.items() if p[2] <= d}
        check(d+":share settlement IDs", {e["fill_sequence"] for e in by("SHARE_SETTLEMENT")}, due_shares)
        for k in due_shares:
            match = [e for e in by("SHARE_SETTLEMENT") if e["fill_sequence"] == k]
            if match:
                check(d+":settled qty", match[0]["qty"], locked_shares[k][1])
            del locked_shares[k]
        acts = [a for a in data["actions"] if a["effective_date"] == d]
        splits = [a for a in acts if a["type"] == "split"]
        check(d+":split IDs", {e["action_id"] for e in by("SPLIT")}, {a["id"] for a in splits})
        for a in splits:
            s = a["symbol"]
            ratio = F(a["numerator"], a["denominator"])
            holdings[s] = int(holdings[s] * ratio)
            marks[s] /= ratio
            for k, p in list(locked_shares.items()):
                if p[0] == s:
                    locked_shares[k] = (p[0], int(p[1] * ratio), p[2])
        divs = [a for a in acts if a["type"] == "dividend"]
        check(d+":div ex IDs", {e["action_id"] for e in by("DIV_EX")}, {a["id"] for a in divs})
        for a in divs:
            qty = holdings[a["symbol"]]
            net = qty * F(a["gross_per_share"]) * (1 - F(a["withholding_rate"]))
            evs = [e for e in by("DIV_EX") if e["action_id"] == a["id"]]
            if evs:
                check(d+":entitlement", evs[0]["entitled_qty"], qty)
                check(d+":div net", evs[0]["net_amount"], net)
            receivables[a["id"]] = (net, a["pay_date"])
        paid = {k for k, p in receivables.items() if p[1] == d}
        check(d+":div payment IDs", {e["action_id"] for e in by("DIV_PAY")}, paid)
        for k in paid:
            matches = [e for e in by("DIV_PAY") if e["action_id"] == k]
            if matches:
                check(d+":div paid amount", matches[0]["amount"], receivables[k][0])
            cash += receivables.pop(k)[0]
        for ev in by("FILL"):
            s, side, qty = ev["symbol"], ev["side"], ev["qty"]
            raw = F(quotes[d, s]["open"])
            check(d+":raw open", ev["raw_open"], raw)
            adverse = raw * (1 + F(fee["slippage_rate"]) if side == "BUY" else 1 - F(fee["slippage_rate"]))
            px = rounded(adverse, F(data["instruments"][s]["price_tick"]), "ceil" if side == "BUY" else "floor")
            check(d+":fill price", ev["price"], px)
            value = qty * px
            comm = rounded(max(value * F(fee["commission_rate"]), F(fee["minimum_commission"])), F(fee["fee_quantum"]), "half")
            platform = rounded(F(fee["platform_per_order"]), F(fee["fee_quantum"]), "half")
            tax = rounded(value * F(fee["buy_tax_rate" if side == "BUY" else "sell_tax_rate"]), F(fee["fee_quantum"]), "half")
            cost = comm + platform + tax
            check(d+":commission", ev["commission"], comm)
            check(d+":platform", ev["platform"], platform)
            check(d+":tax", ev["tax"], tax)
            check(d+":fee total", ev["fee"], cost)
            lag = data["settlement"]["share_sessions" if side == "BUY" else "cash_sessions"]
            due = sessions[sessions.index(d) + lag]
            check(d+":due date", ev["settlement_date"], due)
            check(d+":positive integer qty", isinstance(qty, int) and qty > 0, True)
            if side == "BUY":
                cash -= value + cost
                check(d+":nonnegative cash", cash >= 0, True)
                holdings[s] += qty
                if due > d:
                    locked_shares[ev["sequence"]] = (s, qty, due)
            else:
                available = holdings[s] - sum(p[1] for p in locked_shares.values() if p[0] == s)
                check(d+":sellable constraint", qty <= available, True)
                holdings[s] -= qty
                if due > d:
                    pending_cash[ev["sequence"]] = (value-cost, due)
                else:
                    cash += value-cost
        for s in holdings:
            close = quotes[d, s]["close"]
            if close is not None:
                marks[s] = F(close)
        unsettled = sum((p[0] for p in pending_cash.values()), F(0))
        receivable = sum((p[0] for p in receivables.values()), F(0))
        equity = cash + unsettled + receivable + sum(holdings[s] * marks[s] for s in holdings)
        check(d+":cash", snap["settled_cash"], cash)
        check(d+":unsettled", snap["unsettled_cash"], unsettled)
        check(d+":receivable", snap["dividend_receivable"], receivable)
        check(d+":holdings", snap["holdings"], holdings)
        check(d+":sellable", snap["sellable"], {s: holdings[s]-sum(p[1] for p in locked_shares.values() if p[0] == s) for s in holdings})
        check(d+":equity", snap["equity"], equity)
        replay.append({"date": d, "equity_exact_fraction": str(equity), "cash_exact_fraction": str(cash)})
        previous = d
    return {"method": "independent fractions; raw quotes, initial state and events", "errors": errors,
            "passed": not errors, "replayed": replay}


def metrics(result):
    fills = [e for e in result["events"] if e["type"] == "FILL"]
    return {"final_equity": result["snapshots"][-1]["equity"],
        "equities": [s["equity"] for s in result["snapshots"]],
        "final_cash": result["snapshots"][-1]["settled_cash"],
        "final_holdings": result["snapshots"][-1]["holdings"],
        "fills": [{k: e[k] for k in ("date", "symbol", "side", "qty", "price", "fee")} for e in fills],
        "fill_count": len(fills), "signal_count": len(result["signals"]),
        "final_signal_weights": result["signals"][-1]["weights"] if result["signals"] else {},
        "final_signal_index": result["signals"][-1]["index"] if result["signals"] else {},
        "final_sma10": result["signals"][-1]["sma10"] if result["signals"] else {},
        "cancel_reasons": [e["reason"] for e in result["events"] if e["type"] == "ORDER_CANCEL"]}


def build_cases():
    cases = []
    d = fixture(cash="50", prices={"SPY": "1"})
    d["initial"]["holdings"]["SPY"] = 50
    for x in d["calendar"]["sessions"][1:5]: price(d, x, "2")
    cases.append(("C01_cash_kept", d, {"final_equity": "150", "final_cash": "50", "fill_count": 0}))
    d = fixture(cash="0")
    d["initial"]["holdings"]["SPY"] = 10
    for x in d["calendar"]["sessions"][:5]: price(d, x, "9")
    d["actions"] = [dividend("DIV1", "2024-10-07", "2024-10-09")]
    cases.append(("C02_dividend_receivable_payment", d, {"equities": ["100"]*5, "final_cash": "10", "fill_count": 0}))
    d = copy.deepcopy(d); d["actions"][0]["withholding_rate"] = "0.2"
    cases.append(("C03_dividend_tax", d, {"equities": ["98.0"]*5, "final_cash": "8.0"}))
    d = fixture(cash="0"); d["initial"]["holdings"]["SPY"] = 10
    for x in d["calendar"]["sessions"][:5]: price(d, x, "5")
    d["actions"] = [{"id": "SPLIT1", "type": "split", "symbol": "SPY", "effective_date": "2024-10-07",
                     "known_at": "2024-10-07T12:00:00Z", "numerator": 2, "denominator": 1}]
    cases.append(("C04_split", d, {"equities": ["100"]*5, "final_holdings": {"SPY": 20}}))
    days=["2020-01-31","2020-02-28","2020-03-31","2020-04-30","2020-05-29",
          "2020-06-30","2020-07-31","2020-08-31","2020-09-30","2020-10-30","2020-11-02","2020-11-03","2020-11-04"]
    d = fixture(days, kernel.UNIVERSE, cash="1000")
    d["calendar"]["month_end_sessions"] = days[:10]
    for i, x in enumerate(days[:10]): price(d, x, str(10+i))
    for x in days[10:]:
        bar(d, x, open="30", low="30", high="31", close="31")
    cases.append(("C05_sma10_next_open_gap", d, {"signal_count": 1, "final_equity": "1008.00",
        "fills": [{"date":"2020-11-02","symbol":"SPY","side":"BUY","qty":8,"price":"30","fee":"0"}],
        "final_signal_weights": {"SPY":"0.25","EFA":"0","IEF":"0","GLD":"0"}}))
    flat = fixture(days, kernel.UNIVERSE, cash="1000")
    flat["calendar"]["month_end_sessions"] = days[:10]
    cases.append(("C06_constant_no_trade", flat, {"signal_count": 1, "fill_count": 0, "final_equity": "1000"}))
    d = fixture(cash="100", prices={"SPY":"7"})
    d["fees"].update(commission_rate="0.01", minimum_commission="2")
    intent(d,"2024-10-07",SPY=1); intent(d,"2024-10-08",SPY=0)
    for x in ["2024-10-09","2024-10-10","2024-10-11"]: price(d,x,"8")
    cases.append(("C07_fees_integer_cash_rebuild",d,{"final_equity":"110","final_cash":"110",
        "fills":[{"date":"2024-10-08","symbol":"SPY","side":"BUY","qty":14,"price":"7","fee":"2"},
                 {"date":"2024-10-09","symbol":"SPY","side":"SELL","qty":14,"price":"8","fee":"2"}]}))
    for cid, changes, reason in [("C08_zero_open_liquidity",{"open_capacity_shares":0,"volume":"0"},"ZERO_OPEN_LIQUIDITY"),
        ("C09_missing_open",{"open":None},"MISSING_OPEN"),("C10_unknown_open_liquidity",{"open_capacity_shares":None},"UNKNOWN_OPEN_LIQUIDITY")]:
        d=fixture(); intent(d,"2024-10-07",SPY=1); bar(d,"2024-10-08",**changes)
        cases.append((cid,d,{"fill_count":0,"final_cash":"100","cancel_reasons":[reason]}))
    d=fixture();d["fees"]["minimum_commission"]="5"
    intent(d,"2024-10-07",SPY=1)
    cases.append(("C11_fee_affordable_quantity",d,{"final_cash":"5","final_holdings":{"SPY":9},"final_equity":"95"}))
    d=fixture(cash="0",symbols=["SPY","EFA"])
    d["initial"]["holdings"]["SPY"]=10
    intent(d,"2024-10-07",EFA=1)
    cases.append(("C12_unsettled_cash_not_reused",d,{"final_cash":"100","final_holdings":{"SPY":0,"EFA":0},
        "fill_count":1,"cancel_reasons":["INSUFFICIENT_SETTLED_CASH"]}))
    d=fixture();d["settlement"]["share_sessions"]=2
    intent(d,"2024-10-07",SPY=1);intent(d,"2024-10-08",SPY=0)
    cases.append(("C13_unsettled_shares_not_sold",d,{"fill_count":1,"final_holdings":{"SPY":10},
        "cancel_reasons":["UNSETTLED_SHARES"]}))
    d=fixture(cash="0");d["initial"]["holdings"]["SPY"]=10
    for x in d["calendar"]["sessions"][:5]:price(d,x,"9")
    d["actions"]=[dividend("DIV_NOT_CASH","2024-10-07","2024-10-10")]
    intent(d,"2024-10-07",SPY=1)
    cases.append(("C14_dividend_receivable_not_spendable",d,{"fill_count":0,"final_cash":"10",
        "cancel_reasons":["INSUFFICIENT_SETTLED_CASH"]}))
    d=fixture(); d["fees"].update(slippage_rate="0.001",sell_tax_rate="0.01",platform_per_order="1")
    intent(d,"2024-10-07",SPY=1); intent(d,"2024-10-08",SPY=0)
    cases.append(("C15_adverse_tick_tax_fee",d,{"final_cash":"96.92","final_holdings":{"SPY":0},"fill_count":2}))
    d=fixture();d["fees"]["annual_cash_rate"]="0.0365"
    cases.append(("C16_explicit_cash_interest",d,{"final_equity":"100.0400060004000100","fill_count":0}))
    d=fixture();d["seed_volume_history"]["SPY"]=["1000"]*20
    intent(d,"2024-10-07",SPY=1)
    # Current first day is 100000; 19*1000 +100000 => avg5950 => max5 shares.
    cases.append(("C17_past_volume_capacity",d,{"final_holdings":{"SPY":5},"final_cash":"50"}))
    d=fixture();bar(d,"2024-10-07",volume=None);intent(d,"2024-10-07",SPY=1)
    cases.append(("C21_missing_adv_history",d,{"fill_count":0,"final_cash":"100",
        "cancel_reasons":["VOLUME_WARMUP_INCOMPLETE"]}))
    for cid,status,why in [("C22_unknown_open_status","unknown","UNKNOWN_OPEN_TRADABILITY"),
                           ("C23_halted_at_open","halted","HALTED_AT_OPEN")]:
        d=fixture();intent(d,"2024-10-07",SPY=1);bar(d,"2024-10-08",open_status=status)
        cases.append((cid,d,{"fill_count":0,"cancel_reasons":[why]}))
    d=fixture(days,kernel.UNIVERSE,cash="900")
    d["initial"]["holdings"]["SPY"]=10
    d["calendar"]["month_end_sessions"]=days[:10]
    for x in days[9:]:price(d,x,"9")
    d["actions"]=[dividend("TOTAL_RETURN_DIV",days[9],days[11],tax="0.2")]
    cases.append(("C24_gross_total_return_net_dividend",d,{"final_equity":"998.0","signal_count":1,
        "final_signal_index":{s:"100" for s in kernel.UNIVERSE},
        "final_sma10":{s:"100" for s in kernel.UNIVERSE},
        "final_signal_weights":{s:"0" for s in kernel.UNIVERSE}}))
    d=fixture(days,kernel.UNIVERSE,cash="900")
    d["initial"]["holdings"]["SPY"]=10
    d["calendar"]["month_end_sessions"]=days[:10]
    for x in days[9:]:price(d,x,"5")
    d["actions"]=[{"id":"TOTAL_RETURN_SPLIT","type":"split","symbol":"SPY","effective_date":days[9],
        "known_at":days[9]+"T12:00:00Z","numerator":2,"denominator":1}]
    cases.append(("C25_split_total_return_signal",d,{"final_equity":"1000","signal_count":1,
        "final_signal_index":{s:"100" for s in kernel.UNIVERSE},
        "final_sma10":{s:"100" for s in kernel.UNIVERSE}}))
    return cases


def same(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual,dict) and all(k in actual and same(v,actual[k]) for k,v in expected.items())
    if isinstance(expected,list):
        return isinstance(actual,list) and len(expected)==len(actual) and all(same(x,y) for x,y in zip(expected,actual))
    try:
        return F(str(expected))==F(str(actual))
    except (ValueError,TypeError):
        return expected==actual


def main():
    for name in ("inputs","actual"):(HERE/name).mkdir(exist_ok=True)
    frozen=[HERE.parent/"候选策略与首个实验协议.md",HERE.parent/"试验登记.csv"]
    before={str(p):sha(p) for p in frozen}
    cases=build_cases(); reports=[]; result_by={}
    for cid,data,expected in cases:
        input_path=HERE/"inputs"/(cid+".json");dump(input_path,data)
        result=kernel.run(data,"synthetic")
        actual=metrics(result)
        differences=[{"field":k,"expected":v,"actual":actual[k]} for k,v in expected.items() if not same(v,actual[k])]
        replay=independent_replay(data,result)
        dump(HERE/"actual"/(cid+".json"),{"result":result,"independent_replay":replay})
        reports.append({"id":cid,"passed":not differences and replay["passed"],"expected":expected,
                        "actual":{k:actual[k] for k in expected},"differences":differences,
                        "independent_replay_errors":replay["errors"],"input_sha256":sha(input_path)})
        result_by[cid]=(data,result)
    base,result=result_by["C05_sma10_next_open_gap"]
    cutoff="2020-10-30"
    prefix=copy.deepcopy(base);prefix["end"]=cutoff;prefix["bars"]=[b for b in prefix["bars"] if b["date"]<=cutoff]
    perturbed=copy.deepcopy(base)
    for b in perturbed["bars"]:
        if b["date"]>cutoff:b.update(open="100",high="100",low="100",close="100")
    perturbed["actions"].append(dividend("FUTURE_DIV","2020-11-03","2020-11-04",amount="20"))
    views=lambda r:{"snapshots":[s for s in r["snapshots"] if s["date"]<=cutoff],
                    "events":[e for e in r["events"] if e["date"]<=cutoff],
                    "signals":[s for s in r["signals"] if s["date"]<=cutoff]}
    runs={"full":result,"prefix":kernel.run(prefix,"synthetic"),"future_perturbed":kernel.run(perturbed,"synthetic")}
    prefix_ok=views(runs["full"])==views(runs["prefix"])==views(runs["future_perturbed"])
    dump(HERE/"inputs/C18_prefix.json",prefix);dump(HERE/"inputs/C18_future_perturbed.json",perturbed)
    dump(HERE/"actual/C18_prefix_future.json",{"cutoff":cutoff,"equal":prefix_ok,"runs":runs})
    reports.append({"id":"C18_prefix_future_information","passed":prefix_ok,"expected":True,"actual":prefix_ok,
                    "differences":[] if prefix_ok else ["past views changed"]})
    # The same execution session's future close and full-day volume cannot alter
    # morning order attempts/fills. They can affect close NAV/next month's signal.
    d=fixture();intent(d,"2024-10-07",SPY=1)
    variants={"baseline":d}
    for name,volume in [("day_zero_volume","0"),("day_unknown_volume",None)]:
        variant=copy.deepcopy(d)
        bar(variant,"2024-10-08",close="5",low="5",volume=volume)
        variants[name]=variant
    morning=lambda r:{"intents":[e for e in r["events"] if e["type"]=="INTENT" and e["date"]=="2024-10-07"],
        "opening_events":[e for e in r["events"] if e["date"]=="2024-10-08" and
            e["type"] in ("ORDER_ATTEMPT","FILL","ORDER_CANCEL","ORDER_REMAINDER_CANCEL")]}
    views20={};diag20={};actual20={}
    for name,fixture20 in variants.items():
        res=kernel.run(fixture20,"synthetic")
        views20[name]=morning(res)
        diag20[name]=[e for e in res["events"] if e["type"]=="POST_CLOSE_FILL_DIAGNOSTIC"]
        actual20[name]={"result":res,"independent_replay":independent_replay(fixture20,res)}
        dump(HERE/"inputs"/("C20_"+name+".json"),fixture20)
    morning_ok=all(v==views20["baseline"] for v in views20.values())
    diagnostic_ok=not diag20["baseline"] and all(len(diag20[n])==1 for n in ("day_zero_volume","day_unknown_volume"))
    dump(HERE/"actual/C20_same_session_future_information.json",actual20)
    reports.append({"id":"C20_same_session_future_information","passed":morning_ok and diagnostic_ok and
        all(r["independent_replay"]["passed"] for r in actual20.values()),
        "expected":{"same_open_intents_attempts_fills":True,"post_close_conflicts_flagged":True},
        "actual":{"same_open_intents_attempts_fills":morning_ok,"post_close_conflicts_flagged":diagnostic_ok}})
    bads=[]
    d=fixture();d["fees"]["commission_rate"]=None;bads.append(("G01_unknown_fee",d,"synthetic","commission_rate"))
    d=fixture();d["settlement"]["cash_sessions"]=None;bads.append(("G02_unknown_settlement",d,"synthetic","cash_sessions"))
    d=fixture();d["actions"]=[dividend("UNKNOWN_PAYMENT","2024-10-07","2024-10-09")];d["actions"][0]["pay_date"]=None
    bads.append(("G03_unknown_payment_date",d,"synthetic","pay_date"))
    d=fixture();d["data_kind"]="market_history";bads.append(("G04_real_data_in_synthetic",d,"synthetic","refuses market"))
    d=fixture();bads.append(("G05_formal_result_gate",d,"formal","FORMAL_RESULT_BLOCKED"))
    d=fixture();d["bars"].append(copy.deepcopy(d["bars"][0]));bads.append(("G06_duplicate_bar",d,"synthetic","duplicate"))
    d=fixture();d["bars"][0]["volume"]="NaN";bads.append(("G07_nonfinite_quote",d,"synthetic","nonfinite"))
    d=fixture();d["calendar"]["month_end_sessions"]=["2024-10-07"]
    bads.append(("G08_false_month_end",d,"synthetic","month end contradicts"))
    d=fixture();d["actions"]=[dividend("LATE_ACTION","2024-10-07","2024-10-09")];d["actions"][0]["known_at"]="2024-10-07T21:00:00Z"
    bads.append(("G09_late_action",d,"synthetic","late corporate action"))
    d=copy.deepcopy(base);bar(d,"2020-10-30",available_at="2020-10-30T20:01:00Z")
    bads.append(("G10_late_signal_bar",d,"synthetic","late close data"))
    d=fixture();d["calendar"]["sessions"]=["2024-10-07","2024-10-09","2024-10-10","2024-10-11","2024-10-14"]
    bads.append(("G11_non_session_bar",d,"synthetic","out-of-calendar"))
    d=fixture();d["bars"][0]["open_quote_known_at"]="2024-10-07T20:00:00Z"
    bads.append(("G12_late_opening_quote",d,"synthetic","opening quote information was late"))
    d=fixture();d["account_currency"]="CNY"
    bads.append(("G13_unimplemented_fx",d,"synthetic","cross-currency conversion not implemented"))
    d=fixture();d["initial"]["holdings"]["UNTRACKED_ASSET"]=1
    bads.append(("G14_untracked_opening_position",d,"synthetic","untracked position"))
    for cid,data,mode,expected in bads:
        dump(HERE/"inputs"/(cid+".json"),data)
        try:
            kernel.run(data,mode);message="UNEXPECTED_RUN"
        except kernel.ContractError as exc:message=str(exc)
        good=expected in message
        reports.append({"id":cid,"passed":good,"expected":"STOP with "+expected,"actual":message,
                        "classification":"EXPECTED_REJECTION_OF_INVALID_OR_UNACCEPTED_INPUT"})
    # Known-bad event tests the replay measurement system itself.
    data,good=result_by["C07_fees_integer_cash_rebuild"]
    altered=copy.deepcopy(good)
    next(e for e in altered["events"] if e["type"]=="FILL")["qty"]+=1
    attacked=independent_replay(data,altered)
    dump(HERE/"actual/C19_replay_injected_error.json",attacked)
    reports.append({"id":"C19_replay_detects_tampered_fill","passed":not attacked["passed"],
                    "expected":"independent replay rejects +1 injected share","actual_errors":attacked["errors"]})
    after={str(p):sha(p) for p in frozen}
    unchanged=before==after
    report={"run_at_utc":datetime.now(timezone.utc).isoformat(),"python":sys.version,
        "engine_version":kernel.VERSION,"classification":"SYNTHETIC_CONTROLS_NOT_STRATEGY_RETURNS",
        "controls_passed":sum(r["passed"] for r in reports),"controls_total":len(reports),
        "all_controls_passed":all(r["passed"] for r in reports),"reports":reports,
        "frozen_files_before":before,"frozen_files_after":after,"frozen_files_unchanged":unchanged,
        "formal_history_trials":0,"forward_decisions":0,"broker_orders":0,
        "source_sha256":{p.name:sha(p) for p in (HERE/"low_frequency_engine.py",Path(__file__))}}
    dump(HERE/"controls_actual.json",report)
    print(json.dumps({k:report[k] for k in ("controls_passed","controls_total","all_controls_passed","frozen_files_unchanged","formal_history_trials")},ensure_ascii=False))
    for r in reports:
        if not r["passed"]:print(json.dumps(r,ensure_ascii=False))
    return 0 if report["all_controls_passed"] and unchanged else 1


if __name__=="__main__":raise SystemExit(main())
