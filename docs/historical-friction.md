# Full-account friction sensitivity

`scan_friction(run_trial, *, context, windows, grid_bp=DEFAULT_GRID_BP)` is a composable offline probe. It calls the supplied runner once at each predeclared additional per-side friction candidate:0,1,2,5,10,20,50 and100bp. Each call must execute complete, continuous S and BR accounts over the same original window, with the same frozen k, opening capital, funding/interest/FX/fixed-cost model, market data, execution constraints and comparison identities. Both variants receive the same additional friction. This C0 probe is separate from the original capital×C0–C3×variant matrix; it does not replace or select that matrix.

The caller owns actual native execution, independent account replay, and retention of every attempted model, feature/intent set, complete account, net ledger, replay and failure. The probe validates reconciliation references and their hash format; references alone cannot authenticate that a native execution occurred. Known callback fixtures test the scanner as a measurement instrument and establish no market result.

The context requires exactly:

- `schema_version: historical-friction/1`, `data_kind: historical_market` or `invented_control`, `scenario: C0`.
- `capital_cny`, `frozen_k`: exact decimal strings.
- `calendar`: the whole continuous session-close calendar, including its opening performance boundary.
- `comparison_context`: the five frozen SHA256 values `data`, `account`, `costs`, `execution`, `fx` used by the CNY performance ledger.
- `terms`: the complete original dated trading-terms document.
- `fixed_expenses` and `expense_review`: the explicit original model2 schedule/review, including a reviewed zero declaration where applicable. The review must bind the schedule and cover the full trial calendar.

`windows` supplies original `validation` and `confirmation`, each with exact `{boundary_date, end}` session dates. The two periods share their original intervening boundary. `full` is derived from the continuous ledger; no new account is opened at either segment boundary.

The runner receives an exact additional-bp string and returns:

```
{
  extra_per_side_bp, frozen_k, terms,
  accounts: {
    S:  {ledger, reconciliation},
    BR: {ledger, reconciliation}
  }
}
```

Each reconciliation contains exactly `passed: true`, `errors: []`, `source_trial_ref`, `account_sha256`, and `replay_sha256`. Each `ledger` is the complete CNY `performance_input` ledger, including opening equity, every daily valuation, actual fills, fixed/FX costs and the shared cash benchmark. The production performance validator checks its fields, fee components, calendar and return/insolvency conventions. The probe rejects missing dates, changed capital, added contributions or boundary withdrawals, changed cash benchmark returns, an altered frozen k, and context differences beyond the permitted execution-friction term.

Only `terms.rules[*].execution.friction_rate` may change: each must equal its original value plus `extra_per_side_bp / 10000`. All other fields, including fees, taxes, settlement, dates, evidence, freeze clock and fixed-cost review, remain frozen. Numeric formatting of that one rate is permitted; the actual trial cost hash is recomputed from `{terms, scenario, fixed_expenses, expense_review}`. Other comparison identities remain unchanged. C1 is excluded here to avoid doubling the additional probe rate through that scenario's existing stress rule.

For each full or original validation/confirmation period, the scanner computes the geometric relative gain from exact endpoint ratios:

```
relative_growth = (S_end / S_boundary) / (BR_end / BR_boundary)
log_gain = log(relative_growth)
```

The economic sign uses exact Fraction comparison with1. There is no economic zero band. Decimal logs have50 digits of precision; an extremely small nonzero gain can display0, but its exact ratio, positive/negative sign and `log_gain_rounded_to_zero` flag remain present. Every prefix before a period's end is inspected for nonpositive equity. An insolvent account cannot revive when a later window starts or an endpoint becomes positive again; its geometric log gain remains undefined.

The report retains the entire declared grid, attempted/validated counts and failed rows. It reports observed positive-to-nonpositive intervals, exact zero candidates, later re-entry into positive gain and whether observed gains increase. It never performs a binary search, interpolates a precise root, assumes monotonicity, or claims a unique global break-even cost. Integer shares, fees, cash shortages and missed fills can change the trading path; quantity-path hashes expose such changes relative to baseline. A trial failure is an instrument/account error, not proof of an economic loss. A missing or insolvent candidate before a later crossing prevents calling that later crossing the first interval. A zero/negative baseline yields `BASELINE_NOT_POSITIVE`; if a positive baseline never crosses on the supplied grid, the result is `NOT_FOUND_ON_SCANNED_GRID`, with no out-of-grid tolerance claim.

Baseline turnover arithmetic is a separate `DISCLOSURE_ONLY` approximation. It reports actual one-side fill counts/notional, one-bp cash cost and average cost per fill. Its linear estimate is baseline log gain divided by `(S notional / S terminal NAV - BR notional / BR terminal NAV)`, multiplied by10000. This freezes the baseline path and ignores integer-share changes, timing/compounding and canceled orders. It is explicitly `formal_threshold: false`; it is not substituted for the full rerun interval.

Run `python -m unittest tests.test_historical_friction`. Twelve known-answer instrument tests cover a first observed bracket1–2bp with re-entry2–5bp, a quantity jump at2bp, exact zero at20bp, a disclosed linear estimate outside the scanned range despite a rerun loss at2bp, tiny nonzero signs, nonpositive equity, callback errors, missing dates, wrong capital, changed model/k/cash background, and zero or unreached baselines. Actual historical sensitivity requires the owning study's retained native reruns and replay evidence.
