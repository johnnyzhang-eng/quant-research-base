# Dated trading terms

`TradingTerms` freezes a `trading-terms/1` document containing USD fee,
execution-friction and settlement schedules. Every date must resolve to one
explicit rule. Gaps remain errors; overlapping or reversed ranges are rejected.
The original synthetic validator and formal-study gate remain unchanged.

Each rule supplies a unique identity, effective date range, knowledge timestamp
and hashed evidence reference. The document supplies `frozen_at`. Historically
effective rules must have been known before use; current-counterfactual rules
may use today's observed conditions on a past path, with their true knowledge
timestamp retained and bounded by the model freeze. An evidence hash identifies
the declared source; this module does not read that source or certify account
eligibility, applicable fees or legal rights. The integrator must verify the
referenced files before research acceptance.

Each explicit fee component has BUY/SELL applicability, broker/tax category,
notional/share/order basis, rate, minimum, optional maximum, rounding quantum
and rounding method. Zero fees require explicit zero components. Order costs
use the modeled execution notional. C1 doubles rounded complete broker charges and
execution friction, including minimum charges, while preserving tax schedules.
C2 adds 10bp to C0 friction; C3 uses C2 friction with one additional session of
delay. Both supply the 25bp entry/exit FX addition for the wallet integrator.
The module reports the delay; it does not shift prices or claim to execute.

Execution prices round away from raw opening anchors to the specified tick.
Fee minima and maxima must lie on the rounding grid; incompatible caps are
rejected rather than allowing rounding to exceed the declared maximum. Dates
must use canonical YYYY-MM-DD, preventing aliases of the same session from
counting as different settlement days.
Known raw opens are model inputs, not evidence of executable liquidity.
Zero-share orders are refused so no minimum charge is silently assessed on an
absent trade. Cost breakdowns and cash deltas remain decimal strings.

Settlement resolves the trade date's rule against an explicit supplied session
calendar, including extended sessions after the study end. BUY reports share
availability; SELL reports proceeds availability. This version requires no
reuse of unsettled proceeds and no resale of unsettled shares. It does not
certify the supplied calendar or a broker's historically applicable policy.

Controls cover literal order arithmetic, tax and broker stress distinctions,
directional tick rounding, per-share maximums, non-cent rounding, effective-date
settlement changes, unavailable rules and explicit counterfactual clocks. These
are finite model controls, not historical study results.
