# Persistent prospective offline research entry

The private research implementation now persists two independent hypothetical
accounts instead of restarting a historical replay each day. A bounded archive
collector saves every request attempt, original bytes and checksum receipt.
Source admission checks the raw archive, UTC boundaries and transformed prices.
The fixed candidate and calendar benchmark were frozen before the new evaluation
day, and an offline journal was actually initialized and reopened without a state
change. This checkpoint relates to #1; implementation hashes are in the adjacent
JSON. Private data, parameters, targets and account state are retained locally.

An intent must have its complete target and unique future opening durably saved
before that opening. A later completed-day archive can measure this previously
fixed hypothetical path; its acquisition and measurement times remain explicit.
It does not demonstrate that the price was received at the opening, that the
instrument was then accessible, or that an actual order filled. Missing pairs
wait without advancing accounts. Missing exact opening prices remain unresolved;
a later opening cannot silently replace the scheduled price.

Validation covers 35 producer synthetic controls and 33 independent controls,
including 25 daily account checkpoints recomputed using Fraction arithmetic,
complete signal recurrence, buy/sell/reentry, independent calendar scheduling,
source tampering, late receipts, interrupted commits and restart recovery. The
actual initialized context matches the independently checked full historical
context. Earlier failed attempts are preserved. Synthetic checks are evidence
about the tested mechanisms, not independent profitable market trials.

The first full evaluation day is 2026-10-08 UTC. No new hypothetical target,
measured future fill or forward-return sample exists at this checkpoint. Actual
source acquisition added two historical warmup observations using five recorded
GET attempts, all returning HTTP 200; this is not future strategy performance.

The [reviewed Dataset Terms](https://github.com/binance/binance-public-data/blob/f446ce3812bd4e5521f21faecd4ae3c6460e49fc/TERMS_AND_CONDITIONS.md)
support the limited personal noncommercial nonproduction archive research scope.
The [README](https://github.com/binance/binance-public-data/blob/f446ce3812bd4e5521f21faecd4ae3c6460e49fc/README.md)
notes next-day availability and possible archive revisions. Data and derived
models remain subject to their data license; this repository's software license
does not grant production use. Live trading, brokerage/jurisdiction eligibility,
actual CNY account controls, historical publication/PIT and stable economic edge
remain unverified. The original research goal remains unfinished.
