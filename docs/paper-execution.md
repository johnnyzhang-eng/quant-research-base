# Persistent offline order acceptance

The `research_base.execution` default runner is a finite **offline, synthetic**
OMS acceptance milestone. It contains no network sender or broker credentials.
`OFFLINE_SIMULATION` and `FICTIONAL-CNY-ONLY` are the only accepted environment and
account. Constructing a venue and manager requires an explicit
`SimulationBinding` with that account in its allowlist; the identity is persisted
in each SQLite database and checked on reopen. A real or unknown environment is
rejected before a send.

The reused core is the project's first-phase
`offline-simulator/order_simulator.py`: separate durable SQLite OMS and fictional
venue databases, integer-cent money, risk limits, reservation arithmetic,
audited transitions, and the original frozen 19 scenarios. The original source
and evidence were retained unchanged in the first-phase archive. Copied project
source follows this repository's MIT license. The synthetic fixtures retain the
original byte hash; no vendor data or actual account identifiers are present.

Run from the installed package or repository:

```sh
python -m research_base.execution --output-dir /path/to/private/evidence
```

Or import `run_offline_controls(output_dir)` from `research_base.execution`.
Each run creates a fresh directory containing input copies, two SQLite databases
per scenario, every observed step, event journals, final states, source hashes,
and a summary. Do not commit these generated evidence directories.

Acceptance retains **19 frozen scenarios, 119 steps and 290 scalar
expectations**, plus three instrument checks and **44 new known-answer
controls**, for **337 expected result rows**. The independent `_expected.py`
manifest fixes IDs, answers, fixture hash and denominators. A missing, duplicate,
unknown or relabelled result cannot become a smaller successful suite.
`validate_control_report` rejects these faults; tests also attack the validator
with dropped rows/cases, forged answers, a changed fixture hash and boolean versus
integer confusion. The comparator is recursive and type-sensitive.

The offline scenarios support these bounded claims:

- Intent and reservation are committed before the adapter call. A timeout means
  `UNKNOWN`, including when the fictional venue may already have accepted it;
  duplicate submission never resends that intent. A real subprocess exit after
  venue acceptance exercises restart recovery through durable databases.
- Recovery queries the bound venue before considering a resend. Only an explicit
  retry with final authoritative absence can resend. The retry requires a current
  timestamp and rechecks quote age, price bounds, lot/tick, session, reserved
  cash/inventory, and independent pause reasons. `MANUAL` or risk pauses block
  retry. Recovery does not implicitly roll an intent into another trading day.
- Unique executions book partial fills exactly once. Cumulative quantities are
  comparison-only reports; a quantity ahead of known executions pauses for query.
  The manager does not fabricate execution IDs from cumulative-only reports.
- Cancel request keeps reservations until confirmation. Pending duplicates do
  not resend cancel; terminal states survive stale rejections. State guard and
  transition are one SQLite transaction. Repeated attempts have persisted
  correlation IDs; a rejection after the first attempt needs the current ID.
- An authoritative execution delivered after cancellation is still booked.
  Cash/inventory or aggregate-reservation breaches pause **after** booking;
  reconciliation cannot automatically clear these risk causes.
- Late fees and quantity/price revisions have unique adjustment IDs and sequential
  revisions. They replace execution economics, adjust cash, inventory, notional,
  fees and reservations, preserve the old payload in the audit history, and
  reconcile against independent fictional-venue accounting. Duplicate revisions
  and stale original execution replay do not debit twice. Missing revision
  delivery is recovered on restart. Fee-cap, negative-balance, price-limit and
  reservation breaches are booked and remain paused. Invalid binding, overfill,
  revision gaps and conflicting adjustment IDs cannot mutate the ledger.

The fixture runner blocks socket construction and restores the original socket
functions after each library call, including exceptional exit. The fictional
venue is entirely local. Parallel controls exercise shared reservation limits;
focused tests also exercise concurrent unknown recovery and cancellation callback
serialization.

This milestone does not establish official paper-trading access, actual simulated
broker orders, cumulative-only broker execution reconstruction, realistic queues,
market liquidity, settlement eligibility, cash-flow/fee API coverage or live
trading readiness. Quotes, calendar, same-day sellability and fees are fictional.
The adapter protocol is a local contract only. An official adapter requires a
separate environment-bound acceptance, including provider-specific reporting,
revisions and reconciliation. The separately injected [OpenD paper adapter](futu-paper-adapter.md)
is not that official acceptance: provider-shaped controls and SDK call mapping
cannot establish live paper access, actual execution details or actual fees.
