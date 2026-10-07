# BTCUSDT: continuous synthetic ledger acceptance and historical gaps

A monthly study cannot use the old 128-event account by restarting capital or event identities at each boundary. A separate `funding-account/2` candidate now retains one initial capital allocation, a fixed run-wide event budget, both wallets, FIFO lots, pending funding, transfers and global identities across months. The reviewed v1 source snapshot remains unchanged.

Independent synthetic accounting acceptance covers exact duplicate retries without reposting, conflicting-ID rejection, staged failures without state changes, complete journal/snapshot replay, and cross-month continuation. Pending funding remains a memo until cash posting and is excluded from NAV and spendable margin. The new sticky-v2 policy preserves a prior margin breach and permanently rejects further increases in the perpetual short, including after collateral recovery; it is an explicit new policy, not the original v1 rule.

The producer generated 44,830 synthetic committed events: 44,640 minute marks, 93 funding captures, 93 cash posts and four fills. The independent auditor built a separate 178-event cross-month trajectory and completed 594 assertions, including repeated state comparisons and adversarial recovery controls. It checked the producer journal chain and selected records, but did not independently replay the economics of all 44,830 events. These counts are synthetic accounting evidence, not independent market or strategy experiments.

The separate numeric audit completed 165 controls using 85 independently evaluated arithmetic expressions. Its mathematical contract is conditional on the stated operations and enforced input domain: fixed `1 <= N <= 1,000,000`, external magnitude `B = 1E60`, at most 60 significant/fractional digits, and no derived-balance feedback that bypasses admission. Aggregate inventory can reach `N*B`, but an external funding-eligible quantity must still be at most `B` and equal the held short; short inventory above `B` therefore cannot pass that funding-capture input. A single external liquidation quantity has the same cap. The bound `22*N*B^2 + (6*N+4)*B < 1E128`, monetary scale at most 240, and trapped 512-digit Decimal context apply to this specified model. No million-event execution, memory or runtime acceptance was performed.

The model uses fixed synthetic IM/MM rates of 20%/10%, USDT fees, unit contract multiplier and exact FIFO money calculations; these are assumptions, not verified venue rules. Journal and snapshot hashes support integrity, not authentication against replacement of an entire run contract. External anchoring is needed for that stronger claim. The acceptance does not cover durable disk transactions, concurrent writers or live execution.

The retained January 2020 BTCUSDT inputs still contain 93 funding observations and four minute-price series. The shared mark/index gap has 29 minutes, from January 19 13:09 through 13:37 UTC, affecting 30 adjacent valuation increments. No funding observation falls inside that gap, but this does not establish margin survival. All 93 containing-minute OHLC bars finish after the funding clock; 92 strict preceding-minute references satisfy only a necessary clock condition, with historical publication/reception times unproved. At 15 observations, mark and perpetual-trade minute ranges are disjoint; these are retrospective interval comparisons, not simultaneous executable spreads. Conditional cash envelopes were not summed into income or NAV.

The [current official funding-history schema](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data) documents a funding valuation mark, but historical endpoint availability, exact settlement marks, eligible quantities and actual payments remain unverified. The [January 16 retrospective](https://www.binance.com/en/blog/all/421499824684900356) describes specifications as of January 1: linear BTC/USDT, one BTC per contract unit, USDT quote/settlement and base IM/MM of 0.8%/0.4%. The [January 3 announcement](https://www.binance.com/en/blog/all/419799810281013248) dates the isolated-margin rollout. These dated descriptions do not establish point-in-time availability, whole-month margin tiers/deductions, account fee schedules or liquidation survival; an isolated model starting January 1 needs an explicit counterfactual label.

Historical funding eligibility and posting clocks, period-valid maker/taker fees and fee assets, complete margin rules, missing prices, ordered executable fills, whole-month fee-inclusive NAV and comparison with simple holding remain open. No historical-return or profitability claim follows from the synthetic acceptance. A bounded official daily-archive cross-check has now completed: four GET requests returned HTTP 200 and both ZIPs matched the official CHECKSUM. Mark and index daily archives each contain 1,411 of 1,440 minutes, and each agrees with the monthly archive on all 12 fields of every common row. Both retain the same 29-minute gap. This is coverage evidence from one provider, not an independent valuation source or proof of margin survival.

The exact continuous ledger source is published as `research_base/funding_continuous_account.py`. The portable synthetic suite is `tests/test_funding_continuous_account.py`; only import and local recording scaffolding were adapted, so its source hash differs from the private producer suite. Run `python -m unittest discover -s tests -p test_funding_continuous_account.py -v` from the repository root. This new module is an explicit isolated candidate and is not wired into the old funding planner or a broker.

Reviewed evidence fingerprints:

| Artifact | SHA256 |
| --- | --- |
| Daily archive cross-check result | `b2d3042587ab0fd97bc22c43dfa47ce618aa378ed7b830ed99e67e77a2712d67` |
| Daily archive cross-check source | `3e7d19b98538ad6bfa116ccc6c7e70b3fde3614383425a78a89f236f79d87873` |
| Temporal producer source | `c710b358a339dc3d7ab5e524f947c69b8676359d222fc873e60c86bdf2f2891e` |
| Temporal result | `a9363ecc87f89736a93e78f0c5da4fbe6801c164c2933bb4c9bc16696bb9630e` |
| Temporal independent audit | `c82afe6e926af80fe70d69b829b088e0cf89b5a6cff0fd4cecdbf3429c3f4c68` |
| Dated-rule review | `ded1abb236a22bf409488bda331121c10028bcf2f9670a1be0f7094f4b424e18` |
| Continuous ledger source | `dbea70295ef81f79e4df9a113997b30445add6bac2c9a219182f4f79d4024680` |
| Continuous producer report | `be6f3e1e5d7449659ca9bcf90863da197e30d46d18b3218d4c48c76e5d2b9bd6` |
| Continuous independent auditor source | `ee89f28a72eb29eee06c236211e0df77149bfe3cc9c4d540ba84e267e452e159` |
| Continuous independent audit | `41da1f3f0db250ceb0d7d14636726026da23d10b8adaf0baf0c60327e3551cc2` |
| Numeric producer source | `9a9b26f75817311b68e9116cfc39d43871a375ed502a0a4942b7f118508de2bb` |
| Numeric contract report | `db2956c58bb3bc2317aa3dcae837b829135b3027d5b6c7159ec332d487604ed2` |
| Numeric independent auditor source | `7b839686408f5e8deb0c854f9108e7e8b06ffaa74ccb4f6befa90ce4d947342b` |
| Numeric independent audit | `54ca4ca3cfcdb79a46edadeaadfeddeb32773f970a8d5413cf26373a9b48e688` |
| Retained v1 account source snapshot | `65c216bf02845d8686ebce0492b0f1329809e8f3e62169faf3edbaf3d563a9aa` |
| Retained code MIT license | `fbd4175fb69811ed478d8c320f65a2f7c76ec443607da078c8f43fee3f2c33af` |

The fingerprints identify reviewed bytes; this note alone does not attest to remote source publication or remote CI. No raw market rows, archives, private account information or credentials are included. No new market request, order or paid call was made to prepare this note.

Binance Vision is the source of the archive-derived diagnostics. The previously bound [Binance Vision Dataset Terms](https://raw.githubusercontent.com/binance/binance-public-data/f446ce3812bd4e5521f21faecd4ae3c6460e49fc/TERMS_AND_CONDITIONS.md) govern that research scope; those derivative diagnostics in this note are provided under [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/). Code and synthetic accounting verification retain their MIT scope; this note does not relicense the repository code. Separate REST retention, production-use and execution rights remain unverified.
