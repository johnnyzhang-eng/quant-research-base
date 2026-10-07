# Conditional spot-grid economic diagnostic

The offline diagnostic compares a fixed arithmetic spot grid with holding the same paid initial inventory and with cash. It accounts for the complete cash and coin wallet, initial purchase costs, passive fill costs, reservations, and a separately identified hypothetical exit valuation. This public package contains original model code and synthetic known-answer controls; market archives, derived account traces, plots, private protocols, and account information are excluded.

## Reproduce the public controls

Run from the repository root with Python 3.12 or newer:

```sh
python -B tools/test_spot_grid_economic_diagnostic_20261008_v0.py
python -B tools/binance_spot_grid_economic_independent_audit_20261008_v0.py --self-controls
python -B -m unittest discover -s tests -v
```

The controls use invented price and volume paths to test paid initialization, whole-book funding, shared directional volume budgets, partial quantities, next-minute replacements, marketable-order deferral, self-crossing, reservations, cancellation, both fee sides, and exact whole-account valuation. Passing them establishes the stated model behavior; it does not establish profitable execution.

The independent matcher has its own nine known-answer groups and five instrument groups, including deliberately wrong fee, clock, quantity, and phase cases. It does not import or call producer helpers. Its public source is copied byte for byte from the final actual private audit source, which independently checked every retained minute, modeled fill, lifecycle row, and artifact hash.

## Source and private-replay boundary

The private historical replay used a frozen local producer. The public producer is a separately fingerprinted portable variant: its original personal filesystem root is replaced with an unconfigured relative placeholder, and its historical CLI requires an explicit `--local-root`. All non-`main` function and class ASTs are unchanged. The original controls are copied byte for byte and rerun against the variant. The public variant has not been used to rerun the private month and must not be presented as its original frozen source.

The fixed private protocol, authorized owner-held archive, retained rights evidence, and exact input digests are still required for historical replay. They are not supplied by this repository. Public CI exercises synthetic controls and must not fetch private data, read credentials, or submit orders.

The provenance record separates the original replay, public variant, control run, and independent audit digests. A completed private replay remains conditional model evidence. It does not certify a native Binance bot, actual fills, account eligibility, clean out-of-sample performance, or an investment advantage.

The actual audit covers one previously seen market month and correlated model paths, with zero clean out-of-sample market trials. The full historical audit still binds the original frozen private producer digest. It will reject substituting the public plumbing variant for that source; running public known-answer controls is a separate acceptance scope.

## Fee fields and account valuation

In the frozen v0 schema, `ordinary_fees_USDT` includes the paid initialization fee in both the grid and matched-holding accounts. Likewise, minute `fees_USDT` is cumulative execution fees including initialization; it excludes the separately reported hypothetical exit fee. Do not add `initialization_fee_USDT` again when aggregating execution fees. Grid-only post-initialization fees can be derived by subtracting initialization from the cumulative field; matched holding has no post-initialization modeled trade.

Reservations are subsets of the cash or coin wallet, not additional assets. Marked NAV includes the retained coin inventory. Canceling outstanding quotes releases reservations but does not sell the inventory or prove an actual flat account. The terminal fee-inclusive figure is a hypothetical conversion at the last observed close; neither that conversion nor the initialization is certified by depth or executable volume.

## Limits of the fill model

The model consumes one shared directional taker-volume participation budget per minute and uses exact Fraction arithmetic for cash, coin quantities, fees, reservations, and drawdown. A minute's total volume is not contra volume at a quoted level, a queue position, or a causally available intraminute fill signal. Touches and open-gap executions are declared conditional modeling choices, and outside-OHLC fills are flagged.

The OHLC and OLHC traversal cases are sensitivity cases, not bounds on actual tick matching. Separate directional budgets, once-per-minute fills, fixed same-side priority, and next-minute replacement can make their account results coincide by construction. Such equality is not two independent return samples or proof of real-world fill robustness.

Historical filters and fee currencies have not been established by the simplified quote-fee model. Native base-asset buy fees, actual quantity rules, spread, queueing, latency, slippage, and strategy access must be evaluated separately. Coupons and platform rebates are excluded from the economic model.

## Licenses

Original code follows this repository's MIT license. Binance's market dataset has separate CC BY-NC-SA terms and personal historical-research limits; the code license does not relicense that dataset or its derived outputs. This candidate does not distribute either. The retained terms review is fingerprinted as private evidence, and any future redistribution or live use requires a separate rights assessment. See the [official dataset terms](https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md).

Data schema and publication timing: [official Binance public-data README](https://github.com/binance/binance-public-data/blob/master/README.md).

Tracked research scope: [Issue 1](https://github.com/johnnyzhang-eng/quant-research-base/issues/1).
