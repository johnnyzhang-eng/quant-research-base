# Research contract v0.1

The contract is implemented incrementally. A requirement written here is not a claim that every engine feature already exists.

| Domain | Required evidence |
|---|---|
| Dataset | Source/use permission, symbol identity/history, frozen bytes/hash, currency/unit, price and volume adjustment basis, missing/revised observations |
| Information | Event/publication/availability/receipt/revision times separately; explicit assumptions when historical observations are unavailable |
| Calendar | Market/timezone, valid sessions, special closures and intraday hours; candidate rules distinguished from independently supported dates |
| Corporate action | Signal price vs trade price, gross vs net distributions, receivable vs spendable cash, pay dates, split/share/volume units; no double counting |
| Account | Initial cash/holdings/marks, external flows, fees/taxes/interest/FX, lots, effective-dated settlement and tradability |
| Execution | Signal/order/fill times, executable price, capacity and failure behavior, no future full-day volume used at open, explicit terminal mark or liquidation |
| Experiment | Hypothesis, protocol version, development/evaluation windows, all attempts, parameter changes, source/environment version and costs/delay scenarios |
| Comparison | Same-time and same-cost baselines; risk exposure distinguished from timing; contributions from assets, fees and missed moves |
| Metrics | Periodic net-asset returns with initial value and flow policy; explicit annualization/risk-free/drawdown sampling; no undefined zero-risk ratios presented as success |
| Evidence | Orders/events, cash/receivables/holdings snapshots, independent replay, exact input/source artifacts, logs and interrupted/failed attempts |

## Acceptance of the measuring instrument

1. Fix literal known answers before execution. Compare events and states, not just final equity.
2. Use a replay that does not call the engine's equity or fee helper.
3. Preserve currency rounding and predefined numeric tolerances.
4. Feed deliberately bad input/output and mutate disposable source copies. A nonzero exit caused by missing setup is not a successful semantic mutation test.
5. Perturb future observations and compare decision prefixes. Explicitly distinguish fixed-target tests from full strategy-generation paths.
6. Report test denominators, unsupported features, data assumptions and failure classifications separately.

## Model research versus implementability

Low-frequency historical research may use frozen conservative execution, fee or delay assumptions when exact historical observations are missing. Results remain conditional. The assumption must be visible and stress-tested; unknown personal brokerage conditions are not declared verified.

Minute/tick precision is required only for mechanisms that rely on intraday information. Additional rows do not repair unit errors, lookahead or overfitting. Historical windows revisited after parameter changes are not pristine independent samples.

Reference designs: [Qlib Recorder](https://qlib.readthedocs.io/en/latest/component/recorder.html), [LEAN time modeling](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/time-modeling/timeslices). These references do not certify this repository or its data.
