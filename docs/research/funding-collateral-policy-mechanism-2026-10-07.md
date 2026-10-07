# Collateral-policy translation checkpoint

Source: [fixed arXiv v1](https://arxiv.org/html/2605.05089v1), sections III–IV and VIII, read 2026-10-07.

The model links collateral to hedged inventory: initial spot quantity is `Q=(1-alpha)*D/p0`, hedge quantity is `H=-Q/zeta`, and current collateral share is `alpha=V_F/(Q*p+V_F)`.

Its lower trigger uses a liquidation-probability budget over an execution-latency horizon. Its upper trigger requires both a band breach and expected recovered funding covering intervention costs. Those costs include fees, impact, gas and execution friction, adjusted for basis gains. Forecast funding is an estimate, not realized income.

A faithful translation still needs venue-specific margin/settlement rules, causal funding estimates, executable two-leg quotes, transfer latency and whole-account cash flows. Table VI labels lower/upper breaches buy/sell; mapping those labels to the basis directions defined in Section III still needs transaction-level reconciliation.

Our finite planner is a separately versioned synthetic next-intent and accounting control. It uses explicit toy targets, an IM-buffer rule and unused cash; it does not implement the paper's calibrated collateral bands, executable-size optimization or claimed historical performance. Cash transfers alone do not change funding-bearing position size.
