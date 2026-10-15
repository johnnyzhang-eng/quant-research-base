# MAX round 1: public portfolio exclusion test

This bounded round asks whether avoiding the previous calendar month's highest maximum-daily-return stocks benefits a long-only allocation. It is a public portfolio reanalysis, not a stock-level replication or a validated funded strategy.

The OpenSourceAP October 2025 MaxRet file has 1,187 monthly observations through December 2024. It lacks formation-time group capitalization weights, so exclusion from the original value-weighted universe is not identified. Before inspecting the current results, we fixed three **equal-capital group** allocations: all ten source portfolios, all except raw high-MAX, and all except raw low-MAX. Source portfolios retain their internal weights. Equal investment exposure does not imply equal risk.

| Fixed historical window | Allocation | Gross CAGR | Month-end sampled max DD | Negative months |
|---|---|---:|---:|---:|
| 2012–2024 | All ten groups | 11.43% | 26.00% | 55/156 |
| 2012–2024 | Exclude high MAX | 12.39% | 23.59% | 51/156 |
| 1962-07–2024 | All ten groups | 9.07% | 59.22% | 306/750 |
| 1962-07–2024 | Exclude high MAX | 10.29% | 55.22% | 299/750 |

The primary 2012–2024 paired mean increment is 0.06390 percentage points per month; a fixed 12-lag Bartlett-HAC descriptive 95% interval is [-0.00751, 0.13531] percentage points. It includes zero and is not corrected for publication or selection bias. Published-history extensions are not genuinely unseen validation.

A low-MAX long portfolio failing to outperform does **not** rule out benefits from excluding high-MAX stocks. We therefore calculate the direct exclusion allocation and a symmetric exclusion control. Neither equal-group basket is the original market portfolio or an individual-stock equal-weight portfolio.

Decision: the original-market exclusion question remains insufficiently identified. The predefined substitute shows limited historical gross improvement, but the fully invested path fails the research's 30% drawdown target on the full period. Do not promote it as a standalone strategy, tune windows, or add a risk overlay to rescue this round. Preserve the mechanism lead without automatically triggering more data repair. Actual costs, turnover, capacity, execution, daily risk, and account access remain untested.

Source units and direction follow the documented current-code/export path (percentage-point returns; Sign=-1; port01=raw high MAX), conditional on that mapping. The historical release's exact generating commit has not been fully certified. A separate implementation recomputed the principal metrics from the raw file; numerical agreement does not certify source lineage or profitability. A scoped official DeepSeek critique reviewed the result summary, not the raw data. No new WorkBuddy final was obtained in this execution round.

## Primary sources

- [Bali, Cakici, Whitelaw, JFE 2011](https://pages.stern.nyu.edu/~rwhitela/papers/max%20jfe11.pdf)
- [OpenSourceAP data and release documentation](https://www.openassetpricing.com/data/)
- [Pinned reconstruction code](https://github.com/OpenSourceAP/CrossSection/tree/8db892442c2c3a3779b0f1eac4370d3655be15a1)
- [Barinov competing volatility-insurance interpretation](https://faculty.ucr.edu/~abarinov/Maximum%20(December%202017).pdf)

Only this method/status summary is published here. No source return series, stock data, private prompts, credentials, account records, or trading keys are included.

## Local evidence fingerprints

- `protocol.json`: `54f592c70191e50c694f151b73567a7620f98f9e3e54cabffcd61b5dc5a3061a`
- `pre-result-addendum.json`: `a801b48172e50de33ffc5f7030e0378dd1fbaa5ebd3831428e10670a9907cb19`
- `pre-result-statistics-note.json`: `4cea063da19208c35a72b9bcf2e8e83e6265bcc22c15d49e33b975ff4e89cdad`
- `summary.json`: `10f9d9f9f1d6edd2ddf12f654e8b1ed9a595f7d03f25f23bea9f647bb523b535`
- `independent-numeric-review.json`: `2adc5f9512bb20ca59c4fdbdc1ea9491d95d88e57c59bf9582ad682b40c70f7d`
