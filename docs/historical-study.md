# Frozen continuous study runner

`study-run` executes local, frozen inputs through an already installed Vibe
engine and an independent Fraction account replay. It downloads no prices and
sends no orders. Keep its manifest, raw/converted inputs, account models,
research results and evidence outside the public checkout.

```sh
python -B -m research_base study-run \
  --manifest /path/to/private/study/manifest.json \
  --private-root /path/to/private \
  --output /path/to/private/evidence/study
```

## Fixed scope and attempts

Historical market inputs require the original protocol text SHA256 and the
unchanged S02 scope: full 2005 warmup, 2006–2014 development, 2015–2019
validation, and 2020–2026-09-30 confirmation for SPY/EFA/IEF/GLD. Previously
seen history is explicitly revisited; this command does not claim pristine OOS.
Month calendars must match their reviewed session inventories, and the input
must include actual following-month sessions for settlement and monthly
comparisons. Holiday split starts advance to the first included session; each
window retains its preceding actual session NAV.

Development first executes S with CNY50,000 and C0, followed by every one of
the 101 BR weights from 0.00 to 1.00. Every attempt is recorded. Calibration
selects the smallest absolute reported volatility difference; exact ties
select the smaller weight. There is no additional rounding tolerance that can
turn a near tie into an exact tie. Incomplete accounts are retained as execution
errors, never deleted as unsuccessful strategies. One weight is frozen in the
attempt journal before any full validation/confirmation account runs.

The subsequent 32 accounts are S/B0/BR/BC × CNY50,000/CNY200,000 × C0/C1/C2/C3.
Each runs continuously from development through confirmation. Windows and
years slice that one account; they do not sell assets, discard pending cash,
reset capital or charge entry expenses again at a boundary. If calibration is
unavailable, all eight BR cells remain explicitly unexecuted. A selected but
risk-unmatched BR is still run and reported without retuning.

## Manifest and reviews

The exact `historical-study/1` fields are `schema_version`, `data_kind`,
`scope`, `artifact`, `protocol`, `models`, `exit_forwards`, `reviews`, and
`revisited_history` (must be true). Each frozen file descriptor is
`{"file":"relative-private-file", "sha256":"content hash"}`. Paths are
relative to the manifest and may not traverse outside that folder.

`models` and `exit_forwards` each contain exactly `50000-C0` through `50000-C3`
and `200000-C0` through `200000-C3`. Models require
`historical-account-model/2` and a frozen [fixed-expense review and schedule](historical-expenses.md).
Models share all conditions except initial CNY capital, entry principal and
the frozen scenario. Missing fixed-cost evidence cannot become a zero expense.
An exit descriptor may be explicitly null, which leaves the branch pending.
Use [forward liquidation inputs](historical-liquidation.md) for a distinct,
constrained post-study sale/settlement/FX branch; the study's terminal mark
stays intact.

`reviews` contains exactly `source_use`, `units_calendar`, `actions_receipt`,
`funding_terms`, and `execution_friction`. Each record contains `status`
(`reviewed`/`unresolved`), a nonempty `reason`, and frozen evidence descriptors.
Market execution requires the first four reviewed gates; unresolved friction
continues to limit economic conclusions. These are externally reviewed
declarations, not automatic legal grants, receipt archives or real-fill proof.
Retention is rechecked at actual current use.

Only `invented_control` inputs may use `INVENTED_SMALL_CONTROL` with shorter
contiguous complete months and at least ten warmup months. That scope is an
instrumentation control and cannot supply a historical strategy result.

## Reports and incomplete evidence

Each retained run contains a program snapshot, frozen inputs, environment,
trial journal, calibration, individual account tapes/native fills/replays,
continuous performance ledgers, window reports, a 32-cell matrix, eight paired
comparisons, exit branches, and a readable `matrix.csv`. Both CNY and USD
reports value the entire account; the USD view divides whole CNY NAV by the
bound FX mark. The separate unconverted CNY cash background is also valued
in the same numeraire before Sharpe calculations. FX decomposition retains
the exact domestic, USD asset, FX and cross terms.

Costs already belong in NAV and are only disclosed again. Unpaid fixed costs
remain liabilities. Nonpositive whole NAV halts target trading while retaining
positions and every valuation; bankruptcy is not erased or filled with zero
returns. Reported undefined metrics and all failed/pending cells stay visible.

Paired monthly comparisons use the full ledgers and fixed 12-month circular
blocks, 2,000 repetitions, seed 20261003, 90% percentile intervals, and at
least 60 complete months in each evaluated period. Invented short windows
remain sample-insufficient. Risk equivalence and growth/drawdown conditions
are checked separately in validation and confirmation.

`MATRIX_DIAGNOSTICS_COMPUTED` means these account diagnostics were computed;
it is not historical acceptance, a completed cash exit, an official simulated
trade or goal completion. Missing exit inputs are separately counted.
The [friction probe](historical-friction.md) reruns full S and frozen-BR
accounts for each C0 capital at fixed extra per-side costs of 0/1/2/5/10/20/50/100bp.
It reports observed sign-change brackets and any later reentry, rather than
assuming a monotonic cost path or interpolating a unique exact break-even.
The separate baseline fill-notional approximation is explicitly conditional
on unchanged fills. Every rerun is retained and independently reconciled.
Hash seals detect
local alterations, not incorrect prices or terms. Read `result.json` and
retained errors before using any result.
