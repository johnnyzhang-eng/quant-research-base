# Offline funding accounting

The package now contains an exact, bounded two-wallet ledger and a canonical
funding-observation compiler. These are executable research components. They
do not certify historical strategy returns, broker access or real cash credits.

## Reproduce a closed ledger

Run from the repository with Python 3.12+; standard library only:

```sh
python -B -m unittest discover -s tests -p 'test_funding*.py' -v
python -B -m research_base.funding_demo --output runs-local/funding-demo-01
```

The second command creates a new directory and refuses to overwrite an existing
run. Its JSON report records invented inputs, cash, positions, fees, the pending
funding memo, a synthetic settlement contract, restart recovery and exact final
NAV. Initial quote cash is 2,000 across two wallets. Two spot units are bought at
100, two linear perpetual units sold at 101, then closed at 110 and 108. Every
fill costs 1%; synthetic funding adds 0.204 after capture and closure. Final NAV
is exactly 1,997.824. No market data, credentials, network or orders are used.

## Public interfaces

- `research_base.funding_account.CarryAccount`: cash-basis accounting, exact FIFO
  fill costs, separate wallets, in-flight internal transfers, signed derivative
  debit balances, explicit costs and marks, transaction rejection, idempotency
  and complete-event snapshot replay.
- `research_base.funding_events.compile_observations`: caller-supplied market
  and symbol namespace, exact millisecond identities, duplicate instances,
  quarantined conflicting observations and optional containing-minute reference
  envelopes. Its output never qualifies a cash payment.
- `research_base.funding_events.SyntheticFundingBridge`: caller-bound namespace,
  pre-action eligibility capture and a later explicitly synthetic cash posting.
  An unrelated market or symbol cannot qualify the same held position.

Snapshots use package schema versions; private experiment snapshots keep their
original schemas and remain separate. The public module is a reviewed extraction,
not the byte-identical private audit target.

## Numeric and economic limits

Money calculations use a 512-digit isolated Decimal context with rounding and
inexact results rejected. Inputs have at most 60 significant digits, normalized
fractional scale 60 and absolute magnitude 1E60. Each account and synthetic
bridge is limited to 128 committed events or operations. A full historical
runner must satisfy this bound or introduce and verify a separately versioned
numeric contract; chaining reset accounts is not a continuous historical replay.

The ledger supports spot long and linear perpetual short positions. Funding is
recognized only on cash posting; positive pending funding is neither NAV nor
available margin. Default margin rates and liquidation events are explicit
synthetic model inputs, not a dated venue fee or liquidation implementation.

A minute OHLC range is not an exact settlement mark. Archive observation time is
not proof of historical knowledge time, eligibility, cash receipt or venue event
ordering. To produce a qualified historical carry account, provide the applicable
settlement rule, holdings eligibility, valuation, cash timing, fee and margin
schedule, complete data coverage and execution constraints. Independent synthetic
checks establish bounded accounting behavior, not profitable strategy evidence.

The original historical-study and forward-validation acceptance conditions remain
open in Issue #1 and the draft implementation PR.
