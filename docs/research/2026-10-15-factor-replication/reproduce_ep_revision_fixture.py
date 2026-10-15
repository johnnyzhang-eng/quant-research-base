"""Replay one pinned synthetic version-loss fixture; no market data or application startup.
Requires an existing Python environment with polars==1.44.1. No automatic install.
Obtain WW-shan/hs300 source at the pinned commit separately; code is not embedded.
This portability check reuses the original fixture implementation, not an independent audit.
"""
from __future__ import annotations
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
try:
    import polars as pl
except ImportError:
    raise SystemExit("An existing polars==1.44.1 environment is required; nothing is installed automatically.")

PINNED_COMMIT = 'ec7ac49bf9f286c85f34fb14880c3f3da7260f5e'
ORIGINAL_PROTOCOL_SHA256 = 'df5fb1e0a93b83d310af8309e0e640d890ff09333a9c28e6ca38186cdba1f507'
EXPECTED_FILE_SHA256 = {'backend/app/services/financial_sync.py': '7e53ebe3e2ca421ee56fd0162fab90a2519b933b05830a88fa97a72a69a57c46', 'backend/app/backtest/fundamentals.py': '9441677e88b15a95be084b2732fa62609a931db8e4d879026860666aa74fd421'}
EXPECTED_NODE_SHA256 = {'_merge_report_history': '8ad1976deabc43008e0531d8268d52d27b9234863cd041f0596e267754089847', 'FUNDAMENTAL_FACTORS': '359c66cb29f088caea424a4c575c7627594975d305cde3b23777aa72c520519b', 'FUNDAMENTAL_FACTOR_NAMES': '97b7fcc772188548b7d99f9439d6d3f0804df55f9c800220508e4e68c063703b', 'attach_fundamental_factors': 'caf92d018d470a510632070ab38aee55d26d29a902c0f8d1191e2b767192f52e'}
p = {'commit': 'ec7ac49bf9f286c85f34fb14880c3f3da7260f5e', 'dates': ['2024-03-14', '2024-03-15', '2024-03-16', '2024-03-31', '2024-06-15', '2024-06-16'], 'cases': [{'name': 'changed_value_revision', 'records': [{'symbol': 'SYNTHETIC_A.SH', 'period_end': '2022-12-31', 'announce_date': '2023-03-15', 'roe': 10.0, 'net_income': 50.0}, {'symbol': 'SYNTHETIC_A.SH', 'period_end': '2023-12-31', 'announce_date': '2024-03-15', 'roe': 20.0, 'net_income': 100.0}, {'symbol': 'SYNTHETIC_A.SH', 'period_end': '2023-12-31', 'announce_date': '2024-06-15', 'roe': 33.0, 'net_income': 80.0}], 'archive_expected': [10, 10, 20, 20, 20, 33], 'compressed_expected': [10, 10, 10, 10, 10, 33]}, {'name': 'same_value_revision', 'records': [{'symbol': 'SYNTHETIC_A.SH', 'period_end': '2022-12-31', 'announce_date': '2023-03-15', 'roe': 10.0, 'net_income': 50.0}, {'symbol': 'SYNTHETIC_A.SH', 'period_end': '2023-12-31', 'announce_date': '2024-03-15', 'roe': 20.0, 'net_income': 100.0}, {'symbol': 'SYNTHETIC_A.SH', 'period_end': '2023-12-31', 'announce_date': '2024-06-15', 'roe': 20.0, 'net_income': 100.0}], 'archive_expected': [10, 10, 20, 20, 20, 20], 'compressed_expected': [10, 10, 10, 10, 10, 20], 'purpose': '即使数值未改，公告日期后移压缩仍可能丢失首次可知月份；不得预设无害。'}, {'name': 'unrelated_null_column', 'records': [{'symbol': 'SYNTHETIC_A.SH', 'period_end': '2022-12-31', 'announce_date': '2023-03-15', 'roe': 10.0, 'net_income': 50.0, 'unrelated_metric': 2.0}, {'symbol': 'SYNTHETIC_A.SH', 'period_end': '2023-12-31', 'announce_date': '2024-03-15', 'roe': 20.0, 'net_income': 100.0, 'unrelated_metric': 7.0}, {'symbol': 'SYNTHETIC_A.SH', 'period_end': '2023-12-31', 'announce_date': '2024-06-15', 'roe': 33.0, 'net_income': 80.0, 'unrelated_metric': None}], 'archive_expected': [10, 10, 20, 20, 20, 33], 'compressed_expected': [10, 10, 10, 10, 10, 33], 'merged_unrelated_metric_expected': 7.0, 'purpose': '无关列null不能改变ROE；逐列drop_nulls保留旧无关值，但不保留整条旧版本。'}], 'recorded_comparison_visibility': 'Executor has seen source predictions, DeepSeek critique, and all frozen oracle values; this is not a blind experiment.'}

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source-root", type=Path, required=True,
                    help="Root of separately obtained pinned hs300 source (or byte-identical two-file subset).")
parser.add_argument("--output", type=Path, required=True,
                    help="New JSON output outside the source checkout; existing files are refused.")
args = parser.parse_args()
SOURCE = args.source_root.resolve()
if args.output.exists():
    raise SystemExit("Output already exists; refusing to overwrite.")
if pl.__version__ != "1.44.1":
    raise SystemExit("Polars version drift: exactly 1.44.1 is required.")
source_git_commit_checked = None
if (SOURCE / ".git").exists():
    actual = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip()
    if actual != PINNED_COMMIT:
        raise SystemExit("Source checkout commit differs from pinned commit.")
    source_git_commit_checked = actual
for relative, expected in EXPECTED_FILE_SHA256.items():
    if hashlib.sha256((SOURCE / relative).read_bytes()).hexdigest() != expected:
        raise SystemExit("Pinned source-file hash mismatch: " + relative)
ns = {"pl": pl}
executed = []
def extract(relative,name,node_type):
    f=SOURCE/relative
    raw=f.read_bytes()
    tree=ast.parse(raw.decode(),filename=str(f))
    matches=[n for n in tree.body if isinstance(n,node_type) and (
        getattr(n,'name',None)==name or
        isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name) and n.target.id==name or
        isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in n.targets)
    )]
    assert len(matches)==1,(relative,name)
    node=matches[0]
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module,str(f),'exec'),ns)
    original_segment=ast.get_source_segment(raw.decode(),node)
    if hashlib.sha256(original_segment.encode()).hexdigest() != EXPECTED_NODE_SHA256[name]:
        raise SystemExit("Pinned source-node hash mismatch: " + name)
    executed.append({'path':relative,'name':name,'file_sha256':hashlib.sha256(raw).hexdigest(),'node_source_sha256':hashlib.sha256(original_segment.encode()).hexdigest(),'lines':[node.lineno,node.end_lineno],'rewritten':False})

start=time.perf_counter()
extract('backend/app/services/financial_sync.py','_merge_report_history',ast.FunctionDef)
extract('backend/app/backtest/fundamentals.py','FUNDAMENTAL_FACTORS',ast.AnnAssign)
extract('backend/app/backtest/fundamentals.py','FUNDAMENTAL_FACTOR_NAMES',ast.Assign)
extract('backend/app/backtest/fundamentals.py','attach_fundamental_factors',ast.FunctionDef)

panel=pl.DataFrame({'symbol':['SYNTHETIC_A.SH']*len(p['dates']),'date':p['dates'],'close':[1.0]*len(p['dates'])}).with_columns(pl.col('date').str.to_date())

def apply(frame):
    # This in-memory preparation matches load_fundamental_snapshot's relevant
    # selection/parsing/sorting; it bypasses all file loading and missing-column
    # checks for the unrelated six financial fields, not the original asof function.
    snapshot=frame.select('symbol','roe',pl.col('announce_date').str.to_date().alias('_announce')).sort(['symbol','_announce'])
    return ns['attach_fundamental_factors'](panel,snapshot,['roe_latest']).sort('date')['roe_latest'].to_list()

cases=[]
checks=[]
for case in p['cases']:
    archive=pl.DataFrame(case['records'],infer_schema_length=None)
    old=archive.head(2)
    revision=archive.tail(1)
    compressed=ns['_merge_report_history'](old,revision)
    archive_values=apply(archive)
    compressed_values=apply(compressed)
    case_checks={
        'archive_matches_frozen_oracle':archive_values==case['archive_expected'],
        'compressed_matches_frozen_prediction':compressed_values==case['compressed_expected'],
        'compressed_two_report_periods':compressed.height==2,
        'no_future_value_before_first_announcement':archive_values[0]==10.0 and compressed_values[0]==10.0,
        'same_day_first_report_not_early':archive_values[1]==10.0 and compressed_values[1]==10.0,
        'strict_revision_day_gate':archive_values[4]==20.0 and compressed_values[4]==10.0,
        'both_accept_revision_day_plus_one':archive_values[5]==case['records'][2]['roe'] and compressed_values[5]==case['records'][2]['roe'],
        'pre_revision_information_loss_observed':archive_values[2]!=compressed_values[2] and archive_values[3]!=compressed_values[3],
    }
    if 'merged_unrelated_metric_expected' in case:
        actual=compressed.filter(pl.col('period_end')=='2023-12-31')['unrelated_metric'].item()
        case_checks['unrelated_null_column_keeps_old_nonnull']=actual==case['merged_unrelated_metric_expected']
    checks.extend({'case':case['name'],'name':k,'passed':v} for k,v in case_checks.items())
    cases.append({'name':case['name'],'archive_rows':archive.to_dicts(),'compressed_rows':compressed.to_dicts(),'cutoff_dates':p['dates'],'archive_actual_roe':archive_values,'compressed_actual_roe':compressed_values,'archive_expected_roe':case['archive_expected'],'compressed_expected_roe':case['compressed_expected'],'checks':case_checks})


result = {
    "id": "EP-PIT-REV-02-PUBLIC-PORT-CHECK",
    "executed_at_utc": datetime.now(timezone.utc).isoformat(),
    "elapsed_seconds": time.perf_counter() - start,
    "original_protocol_sha256": ORIGINAL_PROTOCOL_SHA256,
    "pinned_source_commit": PINNED_COMMIT,
    "source_git_commit_checked": source_git_commit_checked,
    "source_subset_note": "If no .git exists, commit provenance is the declared public pin plus exact file hashes; no Git checkout identity is claimed.",
    "python_version": sys.version,
    "polars_version": pl.__version__,
    "public_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    "source_nodes_executed": executed,
    "cases": cases,
    "checks": checks,
    "all_checks_passed": all(item["passed"] for item in checks),
    "scope": "Synthetic ROE availability/version-loss fixture, not EP/stock-market data, full application, look-ahead leakage or profitability validation.",
    "execution_boundary": "Unchanged AST-extracted two functions and two constants; only in-memory selected ROE snapshot preparation. Original disk loader and seven-field completeness check bypassed.",
    "comparison_visibility": p["recorded_comparison_visibility"],
    "independence": "Portability rerun reuses original fixture logic and expected values; not independent review.",
    "calendar_note": "Weekend cutoff dates test date comparison only, not valid exchange sessions.",
    "network_data_requests": 0,
    "strategy_backtests": 0,
    "application_changes": 0,
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"checks": len(checks), "all_checks_passed": result["all_checks_passed"], "elapsed_seconds": result["elapsed_seconds"]}))
if not result["all_checks_passed"]:
    raise SystemExit(1)
