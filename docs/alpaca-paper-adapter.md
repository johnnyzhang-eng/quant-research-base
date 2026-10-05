# Alpaca paper alternative

The Alpaca adapter reuses the persistent paper intent core without importing
Futu's SDK or connecting to OpenD. Its standard-library HTTP client has one
origin: `https://paper-api.alpaca.markets`. It verifies TLS, does not follow
redirects, ignores proxy environment variables, and does not retry requests.
Transport errors retain only status/type, never request headers or error bodies.
Imports and constructors perform no network requests.

The `official-paper-run/2` config adds `provider: "alpaca"` to the original
runner fields. Binding requires a canonical paper account UUID, account and
`US.SYMBOL` whitelists, `market: "US"`, `environment: "PAPER"`, and the fixed
paper host/port. The previous OpenD `official-paper-run/1` schema is preserved.
The account API has no authoritative paper/live boolean; the fixed verified
paper origin and matching fresh account UUID are both required. An ACTIVE USD
account must explicitly report all three blocking flags as false.

The first probe permits one whole-share BUY limit DAY order, no extended hours,
one pending intent, and a maximum one-hour submission window. Regular market
clock, tradable US equity identity, current time, cents and finite monetary
limits are checked before the HTTP send. The runner requires a positive
documented assumed fee reserve; it is not a provider fee observation.

Default available-cash evidence is only supplied for a flat paper account with
no open orders, no positions, no long/short market value, and cash equal to
equity. It is **virtual paper cash**, not actual settled securities cash or
margin buying power. Other account states fail send preflight. This adapter is
a bounded lifecycle probe, not an ongoing multi-order strategy executor.

`client_order_id` is the durable hashed intent token. Unknown sends retain
reservation across restart; 404, timeout or empty lookup never authorize a
resend. Cancellation acknowledgment is not terminal cancellation. Partial fills
remain cumulative provider evidence until independently compared with activity
IDs, quantities, prices, virtual account cash and position observations.

For recover/cancel runs, the runner can archive a finite complete account
activity interval, maximum ten pages of 100 rows. Truncation, repeated activity
IDs, wrong order identity, inconsistent fills, position/cash differences or
unfinished orders prevent a matching observation. The probe audit never clears
OMS blockers. Matching observations are non-atomic and require human review;
all official acceptance and goal completion flags stay false. Paper regulatory
fees, dividends, queue position, market impact and latency slippage are not
verified by this simulator.

Credentials live in a separate private JSON file, mode 0600, with exactly
`key_id` and `secret_key`. They are not placed in config, result, source or
stdout. Use paper keys created in the paper dashboard. An explicit run is:

```sh
python -m research_base alpaca-paper-run \
  --config PRIVATE_CONFIG --credentials PRIVATE_CREDENTIALS \
  --private-root PRIVATE_ROOT --output PRIVATE_OUTPUT
```

Config, SQLite state and evidence must remain outside the public checkout,
under the private root; existing private path/symlink checks apply. The command
performs exactly the requested preflight/recover/submit/cancel action and finite
recovery observations. It never creates accounts, keys, timers or a live
fallback. The local readiness command supports both schemas without contact.

Controls use explicitly invented protocol-shaped records. They verify the
mapping and safeguards, not an authenticated official account or trade.

Official references reviewed for this implementation:

- [Paper rules and simulator limits](https://docs.alpaca.markets/us/docs/paper-trading)
- [Account](https://docs.alpaca.markets/us/reference/getaccount-1)
- [Place order](https://docs.alpaca.markets/us/reference/postorder)
- [Client order lookup](https://docs.alpaca.markets/us/reference/getorderbyclientorderid)
- [Open order coverage](https://docs.alpaca.markets/us/reference/getallorders-1)
- [Activity pagination](https://docs.alpaca.markets/us/reference/getaccountactivities-2)
- [Activity field semantics](https://docs.alpaca.markets/us/docs/account-activities)
