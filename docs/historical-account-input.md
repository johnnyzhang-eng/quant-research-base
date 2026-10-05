# Conditional historical stock-account input

`research_base.historical_contract.prepare(artifact, features, model)` creates
an explicit account layout from a normalized input artifact, the fixed causal
signal report and a frozen account model. `validate_account_input(data)` rebuilds
that layout and compares the complete canonical content. Updating a checksum
while changing prepared cash, capacity, timestamps or execution dates cannot
bypass that comparison.

The output retains `data_kind=historical_market` or `invented_control`, declares
`account_model_version=historical-stock/1` and
`classification=conditional_account_model`, and keeps
`historical_engine_ready=false`. The old synthetic validator rejects this
layout. The adapter does not change it or `vibe_accounting` and does not send an
order. The explicit historical bridge must select this validator and mandatory
dated fee/settlement and cash hooks. There are no fabricated static `fees` or
`settlement` placeholders.

## Exact model fields

The model contains exactly `schema_version`, `scenario`, `start`, `end`,
`initial`, `seed_volume_history`, `instruments`, `terms`, `cash_rules`, `fx`,
`execution`, `withholding`, `performance_boundary`, `dividend_posting`.
Version `historical-account-model/2` contains those fields plus exactly
`expense_review` and `fixed_expenses`, as defined in the
[fixed-expense contract](historical-expenses.md). The full study runner requires
v2 so that missing subscription, data and server costs cannot become zero.
Existing v1 invented/account controls remain a separately declared layout.

- Version is explicitly `historical-account-model/1` or `/2`; scenario is C0–C3. The
  feature report's start/end must equal the account window and its delay must be
  one session for C3 or zero for C0–C2. Changing window or delay does not silently
  change the original S02 protocol. The fixed feature report is independently
  regenerated and compared before any account layout is accepted.
- `initial` has exactly `holdings` and `marks`, with all four ETF symbols.
  Holdings are explicit integer zero. This funded account does not support
  incoming stock transfers or free opening positions. Marks match the immediately
  preceding actual session's raw closes and must be available before the first
  modeled opening.
- `seed_volume_history` maps every symbol to twenty explicit exact prior volumes.
  Values are checked against the actual twenty immediately preceding artifact
  sessions; their data must be available strictly before the first opening.
  Source dates, volumes, clocks and row hashes survive in the prepared output.
  Unknown data do not become a twenty-element default list.
- `instruments` maps each symbol to exactly `lot_size` and `price_tick`. This
  bridge accepts explicit one-share lots only. Positive ticks must agree with
  every applicable dated execution rule.
- `terms` is validated with `TradingTerms`, including fee components, rules,
  original knowledge clocks and settlement. `cash_rules` is validated with
  `CashAccount`, including funding, both currencies' interest and rounding. No
  omitted fee or rate is interpreted as zero. Market inputs require reviewed
  terms and a conditional historical cash model; invented inputs require the
  separate invented/synthetic-control terms and cash classifications. Source
  rights nature must match the normalized artifact's provenance.

Exact FX fields are `entry`, `close_marks`, `exit_quotes`:

- `entry` has `at`, `principal_cny`, `execution_quote`. It occurs at/after the
  performance boundary close and at/before the first modeled open. Cash funding
  starts at midnight on the boundary day. Before conversion, every calendar
  cutoff strictly before entry is accrued in sequence, including CNY negative
  interest. Principal plus fixed fee must be affordable from that actual wallet.
  C2/C3 explicitly add 25bp FX stress; C0/C1 add zero. Entry is computed once for
  the prepared USD capital; the execution bridge must reconstruct and match it,
  then use that wallet without applying the exchange a second time.
- `close_marks` has exactly one explicit `fx-mark.v1` per running session.
  Valuation use time is that date's declared cash cutoff, which cannot precede
  the regular stock close. The stock mark remains the regular close, separate
  from the cash posting time.
- `exit_quotes` has exactly the same session keys, each containing an explicit
  `fx-execution.v1` or null. Null preserves an unavailable FX-exit quote; it does
  not grant a costless executable exit. Quote/evidence, spreads, fees and
  knowledge-clock validation reuse CashAccount.

FX `quoted_at` must not exceed its actual modeled valuation/exchange use time,
even when the knowledge declaration is a frozen current counterfactual. Current
retrieval/knowledge may remain later under that explicit model clock, but a
future FX price cannot value an earlier account. The adapter never rewrites the
quote's original time. `performance_boundary` contains exactly `date` and
`fx_mark`; the date is the immediately preceding session. Its CNY snapshot is
before entry conversion, at that session's close, so entry spread and fixed fees
remain inside the subsequent performance window.

`execution` has exactly `rows`. Every running date/symbol pair needs exactly one
row with `date`, `symbol`, `open_quote_known_at`, `open_status`,
`open_capacity_shares`, `evidence`. Status is explicitly tradable/halted/unknown;
capacity is nonnegative integer shares or explicit null. No missing row or
capacity is filled. A tradable quote cannot be declared known after the modeled
opening. An unknown state may retain a late clock and null capacity; the account
engine must reject execution from that state. Evidence has the TradingTerms
reference/hash/basis shape, retaining historical/model/control distinctions.
Daily-bar opening prices are only modeled anchors. These declarations do not
prove an actual opening quote, capacity or fill was observed.

`withholding` maps exactly the within-account-window dividend IDs to explicit
rates in [0,1]. Signal dividends remain gross and separate. Account actions
preserve `known_at=available_at`, currency, post-split share basis, ex/payment
semantics and withholding. An action arriving after its effective opening is
rejected: this bridge has no late correction path and will not backfill it.
Payment dates may be nontrading bank days. `dividend_posting` contains exactly
`local_time`, `timezone`, `basis`, `evidence`: a naive local time, a ZoneInfo key,
`basis=modelled_at_declared_cutoff`, and the evidence reference/hash/model basis.
For each dividend the adapter produces aware `payment_at` from its actual
`pay_date` and that model declaration, with `payment_basis` and
`payment_evidence`. Posting cannot precede the declared information availability.
The account hook must process this clock even when no stock session exists;
regular-open hooks must not credit it early. This is a modeled payment posting,
not certification of actual intraday receipt. A future observed-receipt contract
needs separate per-action evidence; it is not inferred here. Payments beyond the
run end remain receivables rather than disappearing.

## Output and replay boundary

Common account fields are `start/end`, `symbols`, `instruments`,
`calendar{sessions,month_end_sessions=[],source}`, `initial{holdings,marks,
settled_cash}`, `seed_volume_history`, `bars`, `actions`, `control_intents`.
Prepared bars cover the running window; the full source calendar is retained for
historical decisions, next sessions and settlement. Every possible running day
is checked against both dated BUY/share and SELL/cash settlement lags; a missing
tail session rejects preparation even if no fill has yet occurred. `start/end`
limit execution, not the retained calendar. Intents retain their actual
`execution_date`, including C3, original availability and decision hash.

The output defensively embeds `historical_artifact`, `historical_features`,
`historical_model`, their full lineage hashes, seed and dated-rule provenance,
`cash_entry_journal`, `cash_entry_balances`, and the opening CNY snapshot. Original
OHLC close availability and revision labels remain separate from modeled opening
execution declarations. The later bridge owns live stock state, daily cash
credits, dated fee components, settlement liabilities and independent replay.
`valuation_policy=carry_last_known_close_at_cash_cutoff` fixes the integration
boundary: a close received after the cash valuation cutoff must leave the last
known stock mark in place, with a stale valuation diagnostic. The engine must
not use a future received close as that day's known mark. Opening liquidity
likewise needs the exact twenty prior volumes known before the opening; a late
volume cannot silently become an available capacity sample. The adapter retains
these late source clocks for the bridge's causal hooks rather than relabelling
or rejecting all potentially delayed closes. This adapter produces no fills,
strategy-return report or broker evidence.

Invented integration tests exercise literal C0 entry of 1000 USD, pre-entry CNY
interest of 0.80 and remaining CNY 923.80 from an 8000 CNY wallet; C3 entry is
997.50 USD with 25bp stress and explicit later execution dates. They also check
actual seed values, zero opening holdings, missing capacity/fees/interest,
settlement tails, negative interest blocking entry, source/feature/layout
mutations, FX clocks, withholding and late actions. Invented smaller windows and
amounts are controls, not substituted S02 experiments. Full S02 historical scope,
two original principals, scenarios and inference still need their own accepted
research and execution evidence.

`control_model(artifact, scenario="C0", *, start="2020-10-01", end="2020-11-03")`
returns a complete invented `(model, features)` for the public signal control
fixture. It is reusable by an integration runner without importing tests and
refuses `historical_market` inputs. Its defaults are only invented-control
parameters; `prepare` has no fee, capacity, funding or rate defaults.
