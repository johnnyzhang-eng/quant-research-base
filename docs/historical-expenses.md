# Fixed expenses in account model 2

`ExpenseWallet` is an opt-in subclass of `CashAccount`; the v1 cash rules and
account model retain their original behavior. It accepts invented controls or
a conditional historical model with matching evidence classification. A
schedule review is a frozen model declaration, not proof of a personal bill,
subscription, licensed data route or lawful funding path.

```python
wallet = ExpenseWallet(cash_rules, expense_review=review,
                       fixed_expenses=rows,
                       model_version="historical-account-model/2")
posted = wallet.book_expenses(as_of=wallet.cutoff_at(day))
```

The review has exactly these fields:

| Field | Value |
| --- | --- |
| `schema_version` | `fixed-expense-review/1` |
| `reviewer`, `scope` | Nonempty text describing the review |
| `reviewed_at` | Aware timestamp no later than the cash model freeze |
| `coverage_start_date`, `coverage_end_date` | Canonical dates covering funding and every account/continuation cutoff |
| `explicit_zero` | `true` exactly when the schedule is empty |
| `schedule_sha256` | Canonical JSON hash of the complete expense row array |
| `evidence` | Cash evidence: `reference`, `sha256`, `known_at`, `classification` |

Every expense row has exactly `schema_version` (`fixed-expense/1`), `id`,
`currency` (`CNY` or `USD`), `amount` (nonnegative decimal string on that wallet's
money grid), `book_at`, `category` (`fixed`), and `evidence`. IDs are unique; rows
ascend by booking time and ID. Booking timestamps equal the declared cash
calendar cutoff, including weekends. Receipt clocks follow the declared cash
knowledge clock: historical observation or explicit frozen current
counterfactual. Evidence must be known by the review; a later bill cannot be
silently inserted into an older review. Hashes bind declarations, not the
contents or authenticity of the referenced source document.

At each cutoff the bridge first posts effective dividends and settled cash,
then books and pays expenses, then accrues cash interest. It may retry payment
after interest credits. The first call emits `FIXED_EXPENSE_BOOKED` once per
due row. Payments emit `FIXED_EXPENSE_PAYMENT` and use settled cash, reserving
negative unpaid interest. A partial payment rounds down to the wallet quantum.
Positive unpaid interest, stock holdings, dividends and unsettled sale proceeds
cannot pay a bill. Missing a required booking cutoff is rejected; a retry never
creates a second booking. Paying a bill reduces both settled cash and its
liability, without charging equity twice.

`book_expenses` returns `events`, `usd_paid_total`, `usd_payment_events`, and
`snapshot`. For each USD payment the bridge captures the wallet journal with
the stock source cursor **before** posting exactly one native
`CASH_EXPENSE_DEBIT` with `amount`, `source_wallet_sequence`,
`source_expense_id`, and `debited_at`. CNY payments stay in the CNY wallet. An
aggregate USD debit cannot replace these individually linked payment events.

`owed` exposes liability totals by currency. `balances()` adds
`fixed_expense_owed` and subtracts liabilities from available cash.
`snapshot()` subtracts liabilities from USD/CNY equity, reports
`fixed_expense_liabilities` and `expense_schedule_sha256`, and reserves unpaid
amounts when calculating cash exit value. It cannot claim a fully discharged
exit while bills remain owed. FX also respects reserved liabilities; there is
no automatic conversion, sale of stocks, additional funding or negative cash.
A liability can leave net equity nonpositive without making settled cash
negative. At a scheduled rebalance, a nonpositive account NAV rounded to
eight decimals requires `TRADE_HALTED_NONPOSITIVE_NAV` with zero deltas and
no order attempts. Existing holdings, outstanding obligations, negative net
equity and subsequent calendar accounting remain recorded; no short target
or automatic liquidation is introduced.

Continuation restores ordinary wallet state and its complete original events,
then calls `restore_expenses_from_events()`. This reconstructs booked amounts,
payment references and outstanding balances, refusing duplicate, missing or
altered expense records. The original schedule and review must cover the exit
horizon; restarting with an empty schedule would erase liabilities.

The independent Fraction replay imports no production fee, expense, interest
or FX amount helper. It reconstructs bookings, partial payments, remaining
liabilities, allocation NAV, affordability and net snapshots. Every USD payment
needs exactly one stock debit. It checks due bookings and payments before
interest. Isolated unconverted-CNY opportunity-cost wallets retain their own
cash-only state and receive no portfolio expenses.

Focused controls include a CNY bill exceeding cash, unpaid USD bills with
receivables, negative-interest reserves, explicit zero schedules, input clocks
and money grids, checkpoint continuation, real installed-Vibe partial payment
before/after month-end credit, and altered amount/duplicate-native-debit
attacks. They are finite instrument controls, not certification of a complete
historical experiment or actual provider costs.
