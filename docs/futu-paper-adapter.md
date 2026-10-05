# Guarded OpenD paper adapter

`research_base.execution.futu_paper` is a separate USD paper transport guard. It
never relabels the fictional CNY simulator or inherits its balances, simulated
fills, fees, or same-day sellability. Importing it does not import the official
SDK, create an OpenD context, unlock trading, or open a socket. The caller injects
an already constructed context and SDK enums. No actual broker request is part
of these controls.

## Binding and preflight

`FutuPaperBinding` requires a concrete positive account ID, explicit account and
US-code whitelists, fixed peer host/port, US market, and `SIMULATE`. Every live
account refresh must find exactly that account, with `STOCK_AND_OPTION` paper
subtype and `ACTIVE` status. `REAL`, unknown environments, account ID zero,
automatic account selection, duplicate records, and changed peers are refused. Raw account IDs retain their
provider types: floats, booleans, and numeric strings cannot coerce into a
whitelisted integer account ID.
Peer identity is a caller-supplied attestation of the actual context connection;
a callback merely echoing configuration is insufficient evidence. The guard
checks the peer before and after account queries. It does not authenticate OpenD
or make a remotely mutable context immutable.

`PaperRiskEnvelope` requires maximum whole-share quantity, limit price, aggregate
pending notional, pending-order count, a freshness bound no greater than 300
seconds, and explicit opening/closing timestamps for the finite send window.
Orders have exactly `code`, `side` (`BUY` or `SELL`), integer `quantity`, and
positive cent `limit_price`. The adapter permits no margin, shorts, market
orders, automatic cross-period submitting, or implicit follow-up strategy.

Before sending, fresh account, positions, and order requests must establish
settled available USD cash, settled sellable quantity when selling, and a
complete open-order list. Buying power and unsettled sale proceeds cannot supply
cash. Unknown facts block sending. An explicit positive fee bound with evidence,
known-at time, and expiry must reserve cash for the order. This is a disclosed
assumption of an upper bound, not an official fee observation; actual fees remain
unknown. A cash resolver receives raw account/position/order records and must
supply reviewed evidence. Without one, the SDK mapping leaves cash unknown and
supports read-only preflight only.

Request start and completion timestamps come from a real clock around the SDK
requests (an injected deterministic clock in controls). Freshness bounds the
whole request interval and age at checking. `observed_at` means request completion,
with `snapshot_clock_basis=non_atomic_refreshed_request`; `provider_as_of` stays
unknown. These sequential queries are not an atomic provider account snapshot.
The send window, fee expiry, and request age are checked again just before send.

## Durability and uncertainty

SQLite `BEGIN IMMEDIATE` serializes local reservations across connections;
`SUBMITTING`, remark, payload, and cash/share reservations commit before the SDK
call. Same-intent retries return the stored result and never send again. A payload
or fee-bound change under the same intent is refused. Restart changes durable
`SUBMITTING` to `UNKNOWN`. Unknown sends retain reservations and block new sends.
Bound provider open orders are not counted twice, while unbound orders require
explicit reservation/exposure evidence. All pending local quantities/notionals
also consume the envelope limits. This protects this database and adapter;
separate writers or external account trading require fresh authoritative evidence
and coordinated exclusive account operation.

The stable `qr-…` remark is a discovery correlation tag, **not a broker idempotency
key**. `recover` (also `reconcile`) queries current and historical order lists,
retains every raw result and SHA256, checks bound order identity and payload, and
persists a local query watermark. Empty eventual or non-atomic queries never
establish authoritative absence, release reservations, or enable resubmission.
Multiple matching order IDs, conflicting duplicate snapshots, changed payloads,
unknown statuses, overfills, or cumulative reductions require manual review. No
automatic clear/resume operation is supplied for durable blockers.

Cancellation commits a request correlation token before calling OpenD. API
success means the request was accepted, not that cancellation completed. Only
subsequent order snapshots establish terminal status. Stale rejection responses
and stale active snapshots cannot resurrect terminal orders. A valid fill while
cancelling retains its evidence and blocks claims of economic reconciliation.
Provider `FILLED_ALL`, `CANCELLED_ALL`, and partial statuses must agree with their
cumulative quantities. No cumulative delta is assigned an invented execution ID.

## Official mapping and limits

The injected mapping sends `trd_env=SIMULATE` and the concrete `acc_id` on every
account-specific call. Placement uses `OrderType.NORMAL`, `TimeInForce.DAY`,
`Session.RTH`, `adjust_limit=0`, and a remark within 64 UTF-8 bytes. Current order,
account, and position queries request `refresh_cache=True`; history queries span
the intent creation day through the recovery day. Cancellation calls
`modify_order(CANCEL, order_id, 0, 0, …)`. These are the documented OpenD interfaces:
[placement](https://openapi.futunn.com/futu-api-doc/en/trade/place-order.html),
[order query](https://openapi.futunn.com/futu-api-doc/en/trade/get-order-list.html),
[trade FAQ](https://openapi.futunn.com/futu-api-doc/en/qa/trade.html).

Official OpenD paper does not support deal/history-deal queries or order-fee and
cash-flow queries. `query_deals` returns an explicit unsupported result.
`execution_records=[]` and `actual_fees=None` express the gap; they are not proof
of zero fills or fees. Cumulative fills and average prices remain provider
observations, with economic reconciliation explicitly unverified. A positive
fill adds a durable blocker before another send. Official FAQ Q16 warns that the
older US paper service will gradually be withdrawn; it gives no retirement date
that would establish current unavailability. REST `/sim-trade` is a different
protocol and is not mapped here.

The caller owns secure private database placement (including sidecars), actual
context/socket attestation, authorized SDK/context construction, and retained
raw records. Records can contain account/order identity and must not be published.
No official paper connectivity, actual order acceptance, legal funding route,
fee accuracy, or fully reconciled paper trading is certified by this module.

## Reproducible controls

`python -m unittest tests.test_futu_paper -v` executes 22 invented provider-shaped
controls. Their known answers include cash reservation USD 11 for a one-share
USD 10 order plus USD 1 assumed fee, one provider send after repeated intent
calls, zero recovery sends after a separate-process crash, retained USD 11 on
empty recovery, and terminal cancellation never resurrecting an order. Controls
also test actual concurrent connections, stale/future request clocks, explicit
SIMULATE SDK arguments, insufficient cash/sellability, ambiguous remarks,
conflicting snapshots, cumulative revisions, and unknown fee/cash rejection.
The provider-shaped SDK table fixtures are invented. They do not report market
results or official provider acceptance.
