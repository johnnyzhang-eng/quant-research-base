# Conditional stock/wallet execution

The separate `historical-account/1` path connects a bound normalized artifact,
fixed four-ETF signal output, dated trading terms and CNY/USD cash rules to an
already installed Vibe stock engine. It neither downloads data nor connects to
a broker. Existing synthetic-only entry points keep their original guards.

Input preparation is defined by [the account contract](historical-account-input.md).
`execute` rebuilds that contract, preserves `historical_market` or
`invented_control`, and rechecks declared source use/storage expiry at the actual
time of execution. This recheck cannot discover a later revocation or independently
establish a legal grant. Source review remains required.

Allocation uses the whole CNY account converted at a nonfuture FX price: CNY
cash/receivables/accrued interest, USD settled/pending cash, net dividend rights,
USD accrued interest and held shares. CNY money is not automatically converted
to fund a stock purchase. Fixed sequential integer buys remain constrained by
settled USD cash, dated fees and a causal prior-volume screen. Missing or late
opening quotes use the last known mark when forming targets and cancel fills.
Future raw opening prices cannot change earlier planned order quantities.

Stock decisions use explicit scheduled execution sessions. C3 executes one
additional stock session later, and applies that actual day's fee and settlement
rules. A daily capacity model remains an assumption, not evidence of opening
liquidity or a guaranteed fill.

Cash accrues across calendar days, including weekends. Net dividend rights are
unspendable until the separately declared payment cutoff; payment transfers
receivables to cash once. Currency interest is posted at its declared daily or
calendar-month-end cutoff. Negative accrued interest reserves cash before
posting. Daily USD stock and CNY wallet snapshots are both refreshed after the
cash cutoff. Delayed source closes retain their last known valuation mark.

## Retained private account run

Using the Python interpreter of an existing Vibe environment:

```sh
python -B -m research_base account-run \
  --input /path/to/private/prepared.json \
  --private-root /path/to/private \
  --output /path/to/private/evidence/account
```

The private root must be outside the distributable checkout. Each attempt saves
the exact input, program and native-engine fingerprints, linked stock/wallet
journals, and [independent Fraction replay](historical-replay.md). Reconciled
attempts also save the complete CNY performance input and diagnostics. Failed
attempts and replay discrepancies remain sealed in the registry. No dependencies
are installed by this command.

Opening performance NAV precedes entry conversion; entry spread/fees stay in
net returns. Sharpe uses the same frozen CNY cash schedule in a separate
unconverted wallet. A USD wallet with FX exposure is not the CNY risk-free rate.
Trade cost disclosure uses valuation FX and does not deduct already booked
costs a second time.

`MODEL_DIAGNOSTICS_RECONCILED` is bounded account-model evidence. It does not
certify source rights, point-in-time archives, funding eligibility, calibrated
execution costs, the full S02 scenario/capital/time matrix or profitability.
End positions remain marked, with no forced last-day sale. A complete future
liquidation path and realizable terminal proceeds still require separate work.
