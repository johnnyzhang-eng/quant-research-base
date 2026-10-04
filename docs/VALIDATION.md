# Validation scope, 2026-10-04

- **20 local unit/workflow tests**: strict contracts, nonfinite/duplicate fields, traversal/symlinks, read-only evidence, data sanity, tamper/reseal detection, attempt records, two reproducible reference runs and a disposable wrong-source copy. Publication guard tests include synthetic secret detection without retaining a secret.
- **39 reference controls** through the copied synthetic accounting reference. Only four cases exercise the full ten-month-average signal path; the other cases test accounting or rejection semantics. This is not 39 independent strategies.
- **10 shared hand-answer cases**: no trade, cash weight/terminal mark, next open, integer fees, minimum fee, zero opening capacity, dividend payment, delayed settlement, split units and partial capacity.
- Reference plus independent replay matches all 10. The aligned Vibe model matches 7 supported cases; corporate actions, delayed settlement and initial-holdings transfer are not implemented and remain unsupported. Unsupported cases are not counted as passes.
- Native Vibe under the same declared fixed-target/decision calendar matches1, differs6, unsupported3 in this contract. Differences include model choices for fees, fractional shares, terminal liquidation and opening capacity. These are bounded contract differences, not a verdict that the framework is generally unusable.

## Instrument corrections before acceptance

Initial adapter runs exposed mistakes in our integration: the size-method signature and the one-shot decision calendar were mapped incorrectly; final-state reconstruction was initially called after the engine had cleared temporary arrays. These were instrument faults and were corrected before accepting the final shared-case evidence. Early attempts are retained locally and are not used as evidence against the engine.

The aligned model explicitly adds integer sizing, registered fees/ticks, a known opening-capacity bound and terminal valuation. It still uses private Vibe engine hooks; installed source hashes and dependencies are captured. It is a synthetic fixed-target experiment, not the Vibe full strategy signal path or a broker test.

A wrong fee in a copied event stream is rejected by independent replay; a wrong cash-update sign in a disposable reference source copy causes semantic comparison failures. Original sources remain unchanged. No loaders or brokerage methods are invoked in these tests.

No formal historical strategy return, forward strategy decision or broker order is claimed by these counts. Local vendor snapshots, source permissions and unresolved data assumptions are separate from this public synthetic evidence.
