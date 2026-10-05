"""Execute frozen JSON fault fixtures through persistent OMS/venue paths.

Usage: python3 run_acceptance.py
Every run creates a NEW evidence directory; earlier runs are never overwritten.
"""
from __future__ import annotations
import concurrent.futures
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .manager import ACCOUNT, ENV, DEFAULT_CONFIG, FictionalVenue, OrderManager, SimulationBinding, TERMINAL, canonical
from ._expected import EXPECTED_CONTROL_IDS, EXPECTED_FROZEN_CASE_IDS
from .reporting import same_typed_value, validate_control_report

BINDING = SimulationBinding(ENV, ACCOUNT, (ACCOUNT,))

ROOT = Path(__file__).resolve().parent
NOW = "2026-10-08T10:00:00+08:00"  # Frozen synthetic market time, NOT live market data.


def block_network(*args,**kwargs):
    raise RuntimeError("OFFLINE_HARNESS_BLOCKS_NETWORK")




def path_get(value,path):
    for part in path.split("."):
        value=value[int(part)] if isinstance(value,list) else value[part]
    return value


def compare(observed,expected):
    differences=[]
    for path,want in expected.items():
        try:
            got=path_get(observed,path)
            if not same_typed_value(got,want):differences.append({"path":path,"expected":want,"actual":got})
        except (KeyError,IndexError,TypeError,ValueError):differences.append({"path":path,"expected":want,"actual":"MISSING"})
    return differences


def invariants(local,config):
    """Independent arithmetic oracle reconstructed from committed unique fills."""
    failures=[];orders={o["intent"]:o for o in local["orders"]}
    cash=config["initial_cash"]
    pos={s:p["qty"] for s,p in config["initial_positions"].items()}
    counts={};notionals={};fees={}
    for f in local["fills"]:
        o=orders[f["intent_id"]];p=o["payload"];sgn=1 if p["side"]=="BUY" else -1
        cash-=sgn*f["qty"]*f["price"]+f["fee"]
        pos[p["symbol"]]=pos.get(p["symbol"],0)+sgn*f["qty"]
        counts[o["intent"]]=counts.get(o["intent"],0)+f["qty"]
        notionals[o["intent"]]=notionals.get(o["intent"],0)+f["qty"]*f["price"]
        fees[o["intent"]]=fees.get(o["intent"],0)+f["fee"]
    if cash!=local["account"]["cash"]:failures.append("CASH_CONSERVATION")
    if {s:p for s,p in pos.items() if p}!={s:p["qty"] for s,p in local["positions"].items() if p["qty"]}:failures.append("POSITION_CONSERVATION")
    if local["account"]["cash"]<0 or local["reserved_cash"]>local["account"]["cash"]:failures.append("CASH_RESERVATION_BOUNDS")
    for o in local["orders"]:
        p=o["payload"];remaining=p["quantity"]-o["filled"]
        if not 0<=o["filled"]<=p["quantity"]:failures.append("FILLED_BOUNDS:"+o["intent"])
        if (o["filled"],o["notional"],o["fees"])!=(counts.get(o["intent"],0),notionals.get(o["intent"],0),fees.get(o["intent"],0)):failures.append("ORDER_FILL_AGREEMENT:"+o["intent"])
        expect_rc=(remaining*p["price"]+max(0,config["fee_cap"]-o["fees"])) if p["side"]=="BUY" and remaining and o["state"] not in TERMINAL else 0
        expect_rq=remaining if p["side"]=="SELL" and o["state"] not in TERMINAL else 0
        if (o["reserve_cash"],o["reserve_qty"])!=(expect_rc,expect_rq):failures.append("RESERVE_AGREEMENT:"+o["intent"])
    for symbol,p in local["positions"].items():
        rq=sum(o["reserve_qty"] for o in local["orders"] if o["payload"]["symbol"]==symbol)
        if p["qty"]<0 or p["sellable"]<0 or p["sellable"]>p["qty"] or rq>p["sellable"]:failures.append("QUANTITY_RESERVATION_BOUNDS:"+symbol)
    return failures


def order_args(step):
    return {"environment":ENV,"account":ACCOUNT,"symbol":"TEST.CNY","currency":"CNY","side":"BUY","quantity":100,"price":"10.00",
            "intent_id":step.get("intent","one"),**step.get("order",{})}


def execute_case(case,folder):
    folder.mkdir()
    config={**DEFAULT_CONFIG,**case.get("config",{})}
    (folder/"input.json").write_text(json.dumps(case,ensure_ascii=False,indent=2))
    venue=FictionalVenue(folder/"venue.sqlite3",config,NOW,binding=BINDING)
    oms=OrderManager(folder/"oms.sqlite3",venue,config,binding=BINDING)
    observed=[];execs={};failures=[]
    for number,step in enumerate(case["steps"],1):
        op=step["op"];result={}
        try:
            if op=="submit":result=oms.submit(order_args(step),step.get("now",NOW))
            elif op=="parallel_submit":
                raw=[order_args({**s,"op":"submit"}) for s in step["orders"]]
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(raw)) as pool:
                    results=list(pool.map(lambda x:oms.submit(x,step.get("now",NOW)),raw))
                result={"accepted_count":sum(r["accepted"] is not False for r in results),"duplicate_count":sum(r.get("duplicate",False) for r in results),"rejected_count":sum(r["accepted"] is False for r in results),"responses":results}
            elif op=="crash_submit":
                worker={"config":config,"raw":order_args(step),"now":step.get("now",NOW)}
                file=folder/"crash-input.json";file.write_text(json.dumps(worker,ensure_ascii=False))
                command=[sys.executable,"-m","research_base.execution.controls","--crash-worker",str(folder)]
                child=subprocess.run(command,capture_output=True,text=True,timeout=15)
                result={"exit_code":child.returncode,"stdout":child.stdout,"stderr":child.stderr,"command":command}
                if child.returncode!=77:failures.append({"step":number,"crash_exit_code":child.returncode})
            elif op=="restart":oms=OrderManager(folder/"oms.sqlite3",venue,config,binding=BINDING);result={"restarted":True}
            elif op=="fill":
                event=venue.fill(step.get("intent","one"),step["exec_id"],step["qty"],step.get("price","10.00"),step.get("fee",0));execs[step["exec_id"]]=event
                result=oms.receive_fill(event) if step.get("deliver",True) else {"delivery":"LOST"}
            elif op=="replay_fill":result=oms.receive_fill({**execs[step["exec_id"]],**step.get("change",{})})
            elif op=="cancel":result=oms.request_cancel(step.get("intent","one"))
            elif op=="cancel_first_order":result=oms.request_cancel(oms.snapshot()["orders"][0]["intent"])
            elif op=="cancel_first_confirm":
                intent=oms.snapshot()["orders"][0]["intent"]
                venue.cancel(intent,confirm=True);result=oms.cancel_response(intent,"CANCELLED")
            elif op=="cancel_response":
                state=step["state"]
                if state in {"CANCELLED","EXPIRED","CANCEL_REJECTED"}:venue.cancel(step.get("intent","one"),confirm=state=="CANCELLED",expire=state=="EXPIRED",reject=state=="CANCEL_REJECTED")
                result=oms.cancel_response(step.get("intent","one"),state)
            elif op=="pause":oms.pause(step["reason"]);result={"paused":True}
            elif op=="reconcile":result=oms.reconcile(step.get("resume",False))
            elif op=="quote":venue.set_quote(step.get("symbol","TEST.CNY"),**step["updates"]);result={"updated":True}
            elif op=="external_change":venue.external_change(**step["change"]);result={"injected":True}
            elif op=="external_order":result={"broker_id":venue.submit(step["intent"],{k:v for k,v in order_args(step).items() if k!="intent_id" and k!="price"}|{"price":1000})}
            elif op=="mark":result=oms.mark_risk(step["marks"])
            elif op=="adjustment":result=oms.unsupported_adjustment(step["event"])
            else:raise ValueError("UNKNOWN_TEST_OPERATION:"+op)
            view={"result":result,"local":oms.snapshot(),"venue":venue.snapshot()}
            view["local"]["position_qty"]=view["local"]["positions"].get("TEST.CNY",{}).get("qty",0)
            differences=compare(view,step.get("expect",{}));inv=invariants(view["local"],config)
            row={"step":number,"input":step,"expected":step.get("expect",{}),"actual":view,"differences":differences,"invariant_failures":inv}
            if differences or inv:failures.append({"step":number,"differences":differences,"invariants":inv})
            observed.append(row)
        except Exception:
            failures.append({"step":number,"exception":traceback.format_exc()})
            observed.append({"step":number,"input":step,"exception":traceback.format_exc()})
            break
    for filename,content in [("observations.json",observed),("oms-events.json",oms.audit()),("final-state.json",{"local":oms.snapshot(),"venue":venue.snapshot()})]:
        (folder/filename).write_text(json.dumps(content,ensure_ascii=False,indent=2))
    integrity={}
    for file in ["oms.sqlite3","venue.sqlite3"]:
        with sqlite3.connect(folder/file) as db:integrity[file]=db.execute("PRAGMA integrity_check").fetchone()[0]
    if set(integrity.values())!={"ok"}:failures.append({"sqlite_integrity":integrity})
    result={"id":case["id"],"title":case["title"],"coverage":case["coverage"],"steps_executed":len(observed),"expectation_count":sum(len(s.get("expect",{})) for s in case["steps"]),
            "status":"PASS" if not failures else "FAIL","failures":failures,"sqlite_integrity":integrity,"scope":"OFFLINE_ONLY","gaps":case.get("gaps",[])}
    (folder/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    return result


def run_offline_controls(output_dir):
    """Run frozen fixtures and additional explicit expected/actual controls.

    Every call creates a fresh private run directory. Socket monkeypatches are
    bounded to this call and restored even if a control raises.
    """
    old_socket, old_connection = socket.socket, socket.create_connection
    socket.socket, socket.create_connection = block_network, block_network
    try:
        return _run_offline_controls(output_dir)
    finally:
        socket.socket, socket.create_connection = old_socket, old_connection


def _run_offline_controls(output_dir):
    fixtures=ROOT/"fixtures.json";cases=json.loads(fixtures.read_text())
    timestamp=datetime.now(ZoneInfo("Asia/Shanghai"))
    run=Path(output_dir)/timestamp.strftime("offline-%Y%m%d-%H%M%S-%f");run.mkdir(parents=True)
    (run/"fixtures.json").write_bytes(fixtures.read_bytes())
    bad=compare({"cash":899500},{"cash":899600})
    missing=compare({}, {"cash":899500})
    controls={"wrong_answer_detected":len(bad)==1,"missing_value_detected":len(missing)==1,
              "wrong_answer_differences":bad,"missing_value_differences":missing}
    try:socket.socket();controls["network_guard"]=False
    except RuntimeError:controls["network_guard"]=True
    results=[execute_case(case,run/case["id"]) for case in cases]
    reports=[{"id":key,"passed":controls[key] is True,"expected":True,"actual":controls[key]}
             for key in ("wrong_answer_detected","missing_value_detected","network_guard")]
    for case in cases:
        observations=json.loads((run/case["id"]/"observations.json").read_text())
        for row in observations:
            for path,want in row.get("expected",{}).items():
                try:got=path_get(row["actual"],path)
                except (KeyError,IndexError,ValueError,TypeError):got="MISSING"
                reports.append({"id":case["id"]+":"+str(row["step"])+":"+path,
                    "passed":same_typed_value(got,want),"expected":want,"actual":got})
    from .extended_controls import run_extended_controls
    reports.extend(run_extended_controls(run/"extended"))
    accepted=all(r["passed"] for r in reports) and all(r["status"]=="PASS" for r in results)
    files=["_reused.py","manager.py","controls.py","extended_controls.py","reporting.py","_expected.py","fixtures.json"]
    summary={"classification":"OFFLINE_SYNTHETIC_ONLY","accepted":accepted,"reports":reports,
        "checked_at":timestamp.isoformat(),"timezone":"Asia/Shanghai","scope":"OFFLINE_SYNTHETIC_ONLY",
        "environment":ENV,"paid_calls":0,"broker_network_calls":0,"official_adapter_complete":False,
        "sources_sha256":{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files},
        "harness_controls":controls,"case_count":len(results),
        "pass_count":sum(r["status"]=="PASS" for r in results),"fail_count":sum(r["status"]=="FAIL" for r in results),
        "expected_control_ids":list(EXPECTED_CONTROL_IDS),"expected_case_ids":list(EXPECTED_FROZEN_CASE_IDS),
        "cases":results,"steps_executed":sum(r["steps_executed"] for r in results),
        "frozen_expectation_count":sum(r["expectation_count"] for r in results),"run_directory":str(run)}
    summary["completeness"] = validate_control_report(summary)
    summary["accepted"] = accepted and summary["completeness"]["accepted"]
    (run/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    return summary


def main():
    if len(sys.argv)>1 and sys.argv[1]=="--crash-worker":
        socket.socket = block_network
        socket.create_connection = block_network
        folder=Path(sys.argv[2]);data=json.loads((folder/"crash-input.json").read_text())
        venue=FictionalVenue(folder/"venue.sqlite3",data["config"],data["now"],binding=BINDING)
        oms=OrderManager(folder/"oms.sqlite3",venue,data["config"],binding=BINDING)
        if not oms.reconcile(resume=True)["matched"]:raise RuntimeError("CRASH_WORKER_PRECONDITION")
        oms.submit(data["raw"],data["now"],crash_after_accept=True)
        raise RuntimeError("CRASH_WAS_NOT_TRIGGERED")
    import argparse
    parser=argparse.ArgumentParser(description="Offline synthetic order controls only")
    parser.add_argument("--output-dir",required=True)
    args=parser.parse_args()
    summary=run_offline_controls(args.output_dir)
    print(json.dumps({k:summary[k] for k in ("classification","accepted","run_directory","case_count","pass_count","fail_count")}))
    return 0 if summary["accepted"] else 1


if __name__=="__main__":sys.exit(main())
