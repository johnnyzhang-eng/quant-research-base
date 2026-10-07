"""Fixed-source, synthetic-only K05/K06/K07 cash/inventory experiments.

No upstream module, loader, main, market data or provider API is executed.
Run using the bundled Python with pandas and numpy. This is not a market
backtest, author-return reproduction or strategy-profitability test.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import math
import sys
import types
from datetime import datetime, timezone
from fractions import Fraction as F
from pathlib import Path

import numpy as np
import pandas as pd

import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-dir', type=Path, required=True, help='Directory containing the three fixed source snapshots; no network fetch is performed.')
parser.add_argument('--output-dir', type=Path, required=True, help='Fresh private output directory; refuses to overwrite an existing run.')
parser.add_argument('--report-dir', type=Path, required=True, help='Existing directory for the new synthetic result JSON and TXT.')
args = parser.parse_args()
SOURCE_DIR = args.source_dir.resolve()
DEST = args.output_dir.resolve()
REPORT_DIR = args.report_dir.resolve()
assert REPORT_DIR.is_dir(), 'Create a fresh report directory first.'
FIXED_COMMIT = '46c1af3934a459252c8cea18c1c149fc75705505'
SOURCES = {
    'src__atlas20__backtest__engine.py': (
        '6f4b393839de07e1768ed887aef8d9035973591b6f50783753042aba7f20b9c2',
        ['BacktestResult', 'cap_and_normalize', 'cap_sector_weights',
         'aggregate_sector_exposure', 'run_backtest']),
    'scripts__run_vol_target_walk_forward.py': (
        '6f2abb14adc94cdb6ccd5cdbb759d18c7872b6545120c9070b79976fba1acdba',
        ['_metric_value', '_select_candidate', '_apply_switch_cost', '_walk_forward']),
    'scripts__run_strategy_evidence_audit.py': (
        'c4bf327e08b785d3380803a04d4835c12c868228416d59bc5ce61c1306ee67b8',
        ['_metrics_from_returns']),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    with path.open('x') as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    path.chmod(0o600)


def number(value):
    value = F(value)
    return {'fraction': str(value), 'decimal': float(value)}


@dataclasses.dataclass(frozen=True)
class FrictionFields:
    """Only fields actually read by the isolated source function, no stubs."""
    fee_bps: float = 20.0
    slippage_bps: float = 0.0
    max_weight_per_coin: float = 1.0
    max_weight_per_sector: float = 1.0
    missing_return_policy: str = 'error'
    missing_return_fill: float = 0.0


def source_namespace():
    module = types.ModuleType('atlas20_isolated_synthetic_controls')
    sys.modules[module.__name__] = module
    scope = module.__dict__
    scope.update(pd=pd, np=np, math=math, dataclass=dataclasses.dataclass,
                 FrictionConfig=FrictionFields)
    receipts = []
    forbidden = {'open', 'eval', 'exec', '__import__', 'compile', 'requests',
                 'subprocess', 'socket', 'read_csv', 'to_csv', 'read_parquet',
                 'to_parquet', 'write_text', 'write_bytes'}
    for filename, (expected, selected) in SOURCES.items():
        raw = (SOURCE_DIR / filename).read_bytes()
        assert sha(raw) == expected, f'Source drift: {filename}'
        parsed = ast.parse(raw.decode(), filename=filename)
        nodes = [n for n in parsed.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                 and n.name in selected]
        assert {n.name for n in nodes} == set(selected) and len(nodes) == len(selected)
        for node in nodes:
            for child in ast.walk(node):
                assert not isinstance(child, (ast.Import, ast.ImportFrom)), 'No upstream import'
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Name):
                        assert child.func.id not in forbidden
                    if isinstance(child.func, ast.Attribute):
                        assert child.func.attr not in forbidden
        # Preserve exact selected ASTs; only add future annotations at module level.
        extracted = ast.Module(body=[ast.ImportFrom(module='__future__',
                               names=[ast.alias(name='annotations')], level=0), *nodes],
                               type_ignores=[])
        ast.fix_missing_locations(extracted)
        code_text = ast.unparse(extracted) + '\n'
        path = DEST / f'isolated-{filename}'
        with path.open('x') as f:
            f.write(code_text)
        path.chmod(0o600)
        exec(compile(extracted, f'isolated:{filename}', 'exec'), scope)
        receipts.append({'source_filename': filename, 'fixed_source_sha256': expected,
                         'functions_or_classes': selected,
                         'extracted_AST_sha256': sha(ast.dump(extracted, include_attributes=False).encode()),
                         'saved_extracted_code_sha256': sha(path.read_bytes()),
                         'source_first_line': min(n.lineno for n in nodes),
                         'source_last_line': max(n.end_lineno for n in nodes)})
    return scope, receipts


class QuoteWallet:
    """Invented fractional spot fills, fees paid in quote currency, no debt."""
    def __init__(self, cash=100000, fee=F(1, 500)):
        self.cash = F(cash)
        self.fee = F(fee)
        assert self.cash >= 0 and 0 <= self.fee < 1
        self.holdings = {}
        self.rows = []

    def nav(self, prices):
        return self.cash + sum((q * F(prices[a]) for a, q in self.holdings.items()), F(0))

    def fill(self, asset, quantity, price, side, event):
        quantity, price = F(quantity), F(price)
        if quantity < 0 or price <= 0 or side not in {'buy', 'sell'}:
            raise ValueError('Invalid invented fill')
        notional = quantity * price
        fee = notional * self.fee
        if side == 'buy':
            if any(a != asset and q > 0 for a, q in self.holdings.items()):
                raise ValueError('Synthetic helper supports one held asset; liquidate the previous asset first.')
            if notional + fee > self.cash:
                raise ValueError('Insufficient quote cash including fee')
            new_cash = self.cash - notional - fee
            new_q = self.holdings.get(asset, F(0)) + quantity
        else:
            if quantity > self.holdings.get(asset, F(0)):
                raise ValueError('No borrowed inventory')
            new_cash = self.cash + notional - fee
            new_q = self.holdings[asset] - quantity
        assert new_cash >= 0 and new_q >= 0
        self.cash = new_cash
        self.holdings[asset] = new_q
        row = {'event': event, 'side': side, 'asset': asset,
               'price': number(price), 'quantity': number(quantity),
               'notional': number(notional), 'fee_quote': number(fee),
               'cash_after': number(self.cash),
               'holdings_after': {a: number(q) for a, q in self.holdings.items()},
               'NAV_after_at_declared_fixture_marks': number(self.nav({a: price for a in self.holdings}))}
        self.rows.append(row)

    def buy_all(self, asset, price, event):
        self.fill(asset, self.cash / (F(price) * (1 + self.fee)), price, 'buy', event)

    def sell_all(self, asset, price, event):
        self.fill(asset, self.holdings[asset], price, 'sell', event)


def engine(scope, frame, targets, fee=20):
    return scope['run_backtest']('synthetic', frame, targets,
                                pd.Series('invented', index=frame.columns),
                                FrictionFields(fee_bps=fee), 100000.0)


def main():
    DEST.mkdir(mode=0o700)
    scope, source_receipts = source_namespace()
    dates = pd.date_range('2000-01-01', periods=4, freq='D')
    records = []
    candidate = pd.DataFrame({'A': [.02, .02, 0, 0], 'B': [.01, .01, .03, 0]}, index=dates)
    returns, selections = scope['_walk_forward'](
        candidate, ['A', 'B'], train_days=2, test_days=1,
        selection_metric='multiple', switch_cost_bps=40)
    assert selections.selected_candidate.tolist() == ['A', 'B']
    assert selections.switched.tolist() == [False, True]
    assert np.allclose(returns.to_numpy(), [0, -.004], rtol=0, atol=1e-14)
    stitched = 100000 * float((1 + returns).prod())
    assert math.isclose(stitched, 99600, abs_tol=1e-8)
    wallet = QuoteWallet()
    wallet.buy_all('A', 100, 'first_OOS_entry_from_actual_cash')
    wallet.sell_all('A', 100, 'second_OOS_switch_sell_A')
    wallet.buy_all('B', 100, 'second_OOS_switch_buy_B')
    cash_bridge_nav = wallet.nav({'A': 100, 'B': 100})
    assert cash_bridge_nav == F(24950000000, 251001)
    records.append({'id': 'K05', 'source_function_executed': '_walk_forward',
                    'fixture_candidate_returns': candidate.reset_index().astype(str).to_dict('records'),
                    'source_chained_returns': [float(x) for x in returns],
                    'source_selection_rows': selections.astype(str).to_dict('records'),
                    'source_terminal_NAV': stitched,
                    'quote_fee_cash_bridge_terminal_mark_NAV': number(cash_bridge_nav),
                    'cash_bridge_fill_ledger': wallet.rows,
                    'limits': 'Prebuilt candidate returns include their own history. Fixture assumes their first OOS holdings already exist; cash bridge starts flat. No generic return-bias direction or actual author impact measured. Bridge ends holding B, not fully liquidated.'})

    two = dates[:2]
    frame = pd.DataFrame({'A': [0, .1]}, index=two)
    result = engine(scope, frame, {two[0]: pd.Series({'A': 1.0})}, fee=0)
    assert math.isclose(result.equity_curve.iloc[-1], 110000, abs_tol=1e-8)
    wallet = QuoteWallet(fee=0)
    wallet.buy_all('A', 110, 'eligible_open110_after_signal_close100')
    assert wallet.nav({'A': 110}) == 100000
    # Independent time admission guard; absent from the upstream source engine.
    def opening_eligible(open_time, receipt_time, intent_time):
        return max(receipt_time, intent_time) < open_time
    assert opening_eligible(10, 8, 9)
    assert not opening_eligible(10, 11, 12)
    assert not opening_eligible(10, 9, 10)  # tie ordering absent => conservative rejection
    records.append({'id': 'K06', 'source_function_executed': 'run_backtest',
                    'fixture': 'Signal close100; next-row close-to-close return10%; eligible next open110 and close110.',
                    'source_next_row_return_NAV': float(result.equity_curve.iloc[-1]),
                    'declared_open_fill_close_mark_NAV': number(wallet.nav({'A': 110})),
                    'cash_bridge_fill_ledger': wallet.rows,
                    'independent_timing_guard_cases': [
                        {'open': 10, 'receipt': 8, 'intent': 9, 'eligible': True},
                        {'open': 10, 'receipt': 11, 'intent': 12, 'eligible': False},
                        {'open': 10, 'receipt': 9, 'intent': 10, 'eligible': False}],
                    'limits': 'No actual market receipt/open quote. Timing guard is a local specified control, not an upstream feature or production calendar/latency certification.'})

    flat = pd.DataFrame({'A': [0.0]*4, 'B': [0.0]*4}, index=dates)
    targets = {dates[0]: pd.Series({'A': 1.0, 'B': 0.0}),
               dates[1]: pd.Series({'A': 0.0, 'B': 1.0}),
               dates[2]: pd.Series({'A': 0.0, 'B': 0.0})}
    result = engine(scope, flat, targets)
    assert result.turnover.tolist() == [0, 1, 2, 1]
    l1_expected = F(100000) * F(499, 500) * F(249, 250) * F(499, 500)
    assert math.isclose(result.equity_curve.iloc[-1], float(l1_expected), abs_tol=1e-8)
    wallet = QuoteWallet()
    wallet.buy_all('A', 100, 'buy_A')
    wallet.sell_all('A', 100, 'sell_A')
    wallet.buy_all('B', 100, 'buy_B')
    wallet.sell_all('B', 100, 'sell_B')
    assert wallet.cash == F(24900100000, 251001)
    assert all(q == 0 for q in wallet.holdings.values())
    assert 100000 - wallet.cash == sum(F(r['fee_quote']['fraction']) for r in wallet.rows)
    no_fee = engine(scope, flat, targets, fee=0)
    assert no_fee.equity_curve.iloc[-1] == 100000
    wallet_zero = QuoteWallet(fee=0)
    for side, asset in [('buy', 'A'), ('sell', 'A'), ('buy', 'B'), ('sell', 'B')]:
        getattr(wallet_zero, side+'_all')(asset, 100, side+'_'+asset)
    assert wallet_zero.cash == 100000
    rejected = QuoteWallet()
    prior = (rejected.cash, dict(rejected.holdings), list(rejected.rows))
    try:
        rejected.fill('A', 1000, 100, 'buy', 'unfunded_full_notional')
    except ValueError:
        pass
    else:
        raise AssertionError('Unfunded purchase was not rejected')
    assert (rejected.cash, rejected.holdings, rejected.rows) == prior
    single = QuoteWallet()
    single.fill('A', 1, 100, 'buy', 'single_asset_guard_setup')
    single_before = (single.cash, dict(single.holdings), list(single.rows))
    try:
        single.fill('B', 1, 200, 'buy', 'unsupported_multi_asset_purchase')
    except ValueError:
        pass
    else:
        raise AssertionError('Unsupported second held asset was not rejected')
    assert (single.cash, single.holdings, single.rows) == single_before
    records.append({'id': 'K07', 'source_function_executed': 'run_backtest',
                    'fixture': 'Four rows, zero returns/constant prices100; cash→A→B→cash; fee20bps, slippage0.',
                    'source_turnover': result.turnover.tolist(),
                    'source_daily_returns': result.daily_returns.tolist(),
                    'source_terminal_NAV': float(result.equity_curve.iloc[-1]),
                    'source_L1_fraction_expectation': number(l1_expected),
                    'quote_fee_terminal_cash_NAV': number(wallet.cash),
                    'cash_bridge_fill_ledger': wallet.rows,
                    'controls': {'zero_fee_source_and_wallet_return_to100000': True,
                                 'unfunded_buy_rejected_before_state_change': True,
                                 'closed_wallet_fee_sum_reconciles_PnL': True,
                                 'unsupported_multi_asset_buy_rejected_atomically': True},
                    'limits': 'L1-return and self-financing quote-fee models are different contracts. Fractional quantities only; no venue lot rounding, spread, borrowing, real execution or full source-engine reproduction.'})

    for filename, (expected, _) in SOURCES.items():
        assert sha((SOURCE_DIR / filename).read_bytes()) == expected
    report = {
        'schema': 'atlas20-inventory-bridge-controls/1',
        'completed_at_utc': datetime.now(timezone.utc).isoformat(),
        'fixed_source_commit': FIXED_COMMIT,
        'script_sha256': sha(Path(__file__).read_bytes()),
        'runtime': {'python': sys.version.split()[0], 'pandas': pd.__version__, 'numpy': np.__version__},
        'data_kind': 'Invented fixtures only; 100000 is a test numeraire, not user capital.',
        'source_extraction': source_receipts,
        'scope': 'Actual isolated source functions plus independent Fraction cash ledger. No upstream imports, module main/loaders or actual market backtest.',
        'fee_contract': 'K05/K07 per-fill20bps quote-asset fee; K06 zero fee to isolate timing. No slippage, fractional spot quantities, no borrowing, one nonzero held asset at a time enforced by the helper; not official venue fees or changes to frozen programme25bps.',
        'executed_control_specifications': ['K05', 'K06', 'K07'],
        'other_prior_control_specifications_not_executed_by_this_run': ['K01','K02','K03','K04','K08','K09','K10','K11','K12','K13'],
        'control_results': records,
        'source_bytes_unchanged_after_run': True,
        'operations': {'isolated_upstream_engine_calls': 3, 'isolated_upstream_WF_calls': 1,
                       'whole_upstream_repository_import_or_main_runs': 0,
                       'market_network_or_downloads': 0, 'paid_calls': 0, 'orders': 0,
                       'frozen_strategy_parameters_or_journal_changes': 0},
        'independent_review_pending': True,
        'economic_profitability_or_author_return_impact_verified': False,
        'decision': 'THREE_SYNTHETIC_SOURCE_FUNCTION_CONTROLS_EXECUTED_ECONOMIC_REPRODUCTION_OPEN',
    }
    save(DEST / 'report.json', report)
    save(REPORT_DIR / 'atlas20-inventory-bridge-controls-20261007.json', report)
    text = ('Actual isolated-source synthetic experiments K05/K06/K07 only.\n'
            f'K05 stitchedNAV={stitched}; quote-fee cash-entry/switch NAV={float(cash_bridge_nav):.9f}, holding B.\n'
            f'K06 next-rowreturnNAV=110000; declared open110/close110 NAV=100000.\n'
            f'K07 L1 NAV={float(l1_expected):.9f}; self-financing quote-fee closedcash={float(wallet.cash):.9f}.\n'
            'Other prior specifications remain unexecuted by this run. Source bytes unchanged.\n'
            'Different fee/fill contracts; no measured author returns, market profit, orders, paid calls or frozen parameter changes. Independent review pending.\n')
    with (REPORT_DIR / 'atlas20-inventory-bridge-controls-20261007.txt').open('x') as f:
        f.write(text)
    print(json.dumps({'controls_executed': 3, 'ids': ['K05','K06','K07'],
                      'report_sha256': sha((DEST/'report.json').read_bytes()),
                      'market_backtest': False, 'profit_accepted': False}))


if __name__ == '__main__':
    main()
