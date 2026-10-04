# Long-running goals and acceptance

Updated: 2026-10-04. This is a bounded research-foundation milestone, not a claim of profitable trading.

| Goal | Acceptance | Current status |
|---|---|---|
| G1 Reproducible entry | Validated spec; isolated runs; input/source snapshots; complete attempt registry; known-answer, rejection, rerun and tamper tests | Implemented for validation-only; 20 local tests recorded |
| G2 Data and instrument | Frozen provenance/units/timing; source permissions; reference controls through the actual engine path; separate discrepancies and unsupported features | Partial: reference39, shared10, aligned7 and unsupported3; local snapshot audit available; historical data/model gaps remain |
| G3 First study | Frozen four-ETF monthly ten-month-average hypothesis; next-session execution; B0/BR/BC baselines; explicit costs and robustness; independent ledgers | Pending: no formal historical-return trial; unresolved source use, price/share basis, timing, actions and account model |
| G4 Public delivery | Authorized repository; allowlisted, reviewed publication; runnable synthetic examples; documented evidence and limitations; remote validation | Initial release in progress; no vendor data or private factors included |

## Next work in dependency order

1. Resolve sustainable source permissions and select a primary price/volume definition, preserving cross-source disagreement.
2. Freeze consistent corporate-action/share units, a signal-vs-trade price mapping, information timing and calendar assumptions.
3. Extend the actual engine adapter for dividend receivable/payment, splits and delayed settlement; all extensions need known-answer and wrong-output/source-copy controls.
4. Define and independently test performance metrics and simple-holding/risk-matched/cash baselines before historical results.
5. Run one registered historical model, with frozen costs/latency assumptions and explicit limitations; preserve unsuccessful trials too.
6. Start forward records after freezing rules. A few monthly decisions are limited evidence; monitor strategy and baseline together.

Provider prices, legal use, historical raw/PIT information and personal account conditions are not inferred from file integrity. A daily model may use registered conservative assumptions; it must be labeled conditional and must not claim intraday execution or personal implementability.

## Extensions require their own evidence

- Stock factors: historical universe/delistings, announcement/revision PIT, leakage-free labels/preprocessing and experiment selection.
- Futures: historically tradable contracts, rollover, multiplier/ticks, margin/settlement and realized-profit accounting.
- Intraday ETF rules: sufficient intraday quote/trade data, price spread/capacity, sellable quantity and whole-account benchmark.
- Account execution: actual broker conditions, reconciliation, corrections, restarts, unknown state and persistent risk halts.

No live orders, automatic account login, paid purchase or capital allocation is enabled by this roadmap.
