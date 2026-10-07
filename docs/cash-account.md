# Decimal CNY/USD cash and FX component

`research_base.cash_account` is an independent accounting component. It does not
replace the stock engine, certify a funding path, infer an available interest
rate, contact a broker or establish a historical return result. Inputs must
supply a frozen funding declaration, explicit rates and explicit conversion
costs. Synthetic controls keep that classification throughout.

The public API is:

```python
account = CashAccount(rules)
account.exchange("CNY_TO_USD", principal_cny,
                 execution_quote=quote, as_of=at, event_id="entry",
                 extra_spread_rate="0.0025")
account.sync_usd(settled_usd=engine_cash, receivables_usd=engine_receivables,
                 as_of=at, event_id="close-sync")
result = account.accrue_day(day, as_of=account.cutoff_at(day))
result["credited"]["USD"]  # apply this credit to the external engine
account.snapshot(as_of=at, fx_mark=mark, usd_assets=security_market_value,
                 exit_quote=quote, extra_spread_rate="0.0025")
```

`exchange`, `sync_usd` and `accrue_day` return events and balances. `snapshot`
returns CNY marked equity, individual wallets and explicitly constrained exit
values. Inputs use exact decimal strings or integers; binary floating-point
amounts, nonfinite values and negative settled wallets are rejected. Events,
rules and returned values use defensive copies. Mutating a valuation neither
sells securities nor exchanges currency.

## Frozen rule contract

Root `cash-account.v1` fields are `classification`, `start_at`, `timezone`,
`accrual_cutoff`, `initial_cny`, `funding_evidence`, `money_rounding`,
`interest_schedules` and `knowledge_clock`, plus `schema_version`. The start is a
funded calendar-day midnight. All calendar dates must use canonical `YYYY-MM-DD`;
compact date aliases are rejected, and rate selection compares parsed dates.

`funding_evidence` supplies `status`, `lawful_and_usable`, `route_description`,
`effective_at`, `allowed_currencies` and `evidence`. The accepted status is
`synthetic_control` for synthetic inputs, or `accepted_for_frozen_model` for a
`conditional_historical_model`; the module does not establish the truth of that
caller-supplied declaration. Pending, absent or unaccepted declarations fail.
`allowed_currencies` must explicitly be `["CNY", "USD"]`. Initial funds are CNY;
USD starts at zero and an entry conversion must be an explicit event.

Each evidence record supplies a nonempty `reference`, frozen lowercase hex
`sha256`, original `known_at` and matching `classification`. The model checks the
declaration and its time, rather than opening or certifying the referenced
provider/account document.

`knowledge_clock` is explicit:

- `historical_point_in_time`: evidence and rates must have been known and
  effective at their historical use time, and received no later than `freeze_at`.
- `frozen_current_counterfactual`: current evidence may be applied to an older
  modeled path only against the declared `freeze_at`. Original `known_at`,
  `source_effective_at` and quote timestamps remain in the inputs/events. FX
  price timestamps must always precede use; freezing current fee/rate conditions
  never permits a future FX price to be inserted into an older path.
  Modeled `effective_date` does not become a claim of historical availability.
  Materials received after freezing are rejected.

Both clock fields (`basis`, `freeze_at`) are required. Every event and snapshot
carries the clock declaration. This distinction is independent of the external
market-data contract and does not loosen its synthetic/historical guards.

## Calendar cash interest

Every `cash-interest.v1` row requires currency, modeled `effective_date`,
`source_effective_at`, `known_at`, `balance_observation`, `net_rate_description`,
`eligibility_threshold`, `eligibility_mode`, `tier_mode`, `tiers`, `day_count`,
`rounding_quantum`, `rounding_mode`, `rounding_timing`, `crediting` and `evidence`.
Both currencies need an initial covering schedule; an unknown rate does not
become zero. A supported zero rate must be supplied explicitly with evidence.

The balance observation is `settled_at_calendar_cutoff`. The same API can run a
separate CNY opportunity-cost account without entering USD or sharing a strategy
wallet. Net APR is supplied explicitly, including its stated tax basis; the
module does not invent taxes or provider rates.

Threshold mode is either `gate_all` (all settled cash qualifies once the
threshold is met) or `above_threshold` (only the excess qualifies). Tier mode is
`marginal` or `whole_balance`. Each contiguous tier supplies lower/upper bounds,
`net_annual_rate` and original `known_at`; only the last upper bound is unbounded.
Rates known too late for the selected clock, tier gaps, duplicate currency/date
rows and unknown semantics fail.

One full calendar day uses ACT365 or ACT360. Rounding is explicit: power-of-ten
quantum, DOWN/HALF_UP/HALF_EVEN, and either rounding each daily accrual or rounding
only on payment. Posting is `daily` or `calendar_month_end`, at the stated local
cutoff, including weekends and holidays. The caller must process every calendar
day in order; missing days, repeated days or reverse timestamps fail. Unpaid
positive interest increases equity but is unavailable to spend. Accrued negative
interest is a liability and reduces available cash; posting cannot create a
negative settled wallet. The component uses Decimal precision 38 for accrual and
conversion calculations, rather than claiming unlimited precision.

## FX marking, execution and exit

`fx-mark.v1` supplies `quoted_at`, `known_at`, `cny_per_usd` and `evidence`.
`fx-execution.v1` additionally requires `buy_usd_spread_bps`,
`sell_usd_spread_bps`, `buy_fixed_fee_cny` and `sell_fixed_fee_usd`. Mark and
execution are separate inputs. Buy USD uses ask = mark × (1 + buy bps/10000);
sell USD uses bid = mark × (1 − sell bps/10000). Fixed fees are additional debits
in the source currency. Received currency uses its configured quantum/mode.
Source principal is debited in full; rounding of received currency is part of
the modeled conversion cost.

An explicit `extra_spread_rate` stress multiplies ask by `(1 + extra)` and bid by
`(1 − extra)`. Base quote and extra rate are retained separately. The component
does not infer a stress case or automatically double FX costs for another fee
scenario. Provider spread, fixed fees and stresses are supplied parameters.

CNY marked equity is CNY cash/receivables/unpaid interest plus USD settled cash,
USD external receivables, USD unpaid interest and externally supplied USD
security value, multiplied by the independently supplied FX mark. Receivables
never become cash available for conversion or interest eligibility.

`settled_cash_exit_cny` applies the supplied exit bid/fixed fee only to available
settled cash, without executing. `exit_equity_cny` is populated only when all
equity is settled cash; it remains `None` when assets or unpaid amounts require
liquidation/settlement. The snapshot records exit-quote costs and this limitation.
No zero-USD conversion fee is charged merely to value a CNY cash account.

## Vibe integration boundary and controls

The caller uses existing Vibe capital/receivables/positions as authoritative stock
state. Sync settled capital and external receivables at a hook, accrue every
calendar day, apply returned USD interest credits to Vibe capital, and then pass
security values to valuation. Apply credits before the next sync to avoid
intentionally overwriting that cash. This module provides these hooks; this
milestone does not itself certify the combined Vibe pipeline or the full S02
matrix.

`run_controls(output_dir)` saves a fresh report and all journals.
`EXPECTED_CONTROL_IDS` fixes **40 synthetic controls**; the independent
`validate_control_report` / `control_report_accepted` reject missing or duplicate
rows, altered expected answers, type confusion and classification forgery.
Controls include entry/exit spreads and fixed fees, explicit 25bp stress,
weekend/month-end posting, ACT365/360, tiers/thresholds, rounding, effective-rate
changes, receivable exclusion, negative/replayed/reverse cash, future evidence,
canonical-date rejection and the distinction between historical and frozen
current counterfactual knowledge.
