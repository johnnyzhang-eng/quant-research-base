# Offline durable BTC signal bridge

The [bridge v1 source](../tools/binance_signal_order_bridge_20261008_v1.py) retains SHA256 `07c7d706fd15ef937691e368e3074a953f6f890ee6f29ec8ee30c468a3ff2c20`. The [producer test source](../tools/test_binance_signal_order_bridge_20261008_v1.py) retains SHA256 `c65b282644d0dd639572d5bd47370dead7c5593f7d30c091f59658954052cce4`; the [v4 adapter copy](../tools/binance_spot_testnet_adapter_20261008_v4.py) retains SHA256 `64189cdeaaaccf52ac5e8446e2233c1c63a5d8a5d6da025577df923d3d734891`, identical to the already published adapter. Original long module names and imports are preserved.

[The producer report](evidence/bridge-2026-10-08/binance-signal-order-bridge-20261008-v1.json), SHA256 `cd8e422a98cc394ace4a9909b839edfbd81c72b743e37fa12212618681243593`, records 18 semantic methods and 115 executed assertions. [The independent audit](evidence/bridge-2026-10-08/binance-signal-order-bridge-independent-audit-20261008-v1.json), SHA256 `d4b514f7682054715614bcf0a1d842a503f7a70fe9b4d47db8b467af4d4ac07b`, records 15 independently constructed control groups and 307 assertions, with no producer tests imported. The frozen report still preserves its original relative provenance paths. This public batch does not claim that the independent audit's private prerequisite layout is distributed as a self-contained runner.

The [minimal discovery shim](../tests/test_binance_signal_bridge_portable.py) verifies the public bridge, adapter and producer byte hashes and actual import origins, then discovers and executes the original 18 methods. It does not execute the producer's `__main__` report writer. Run the ordinary repository CI command:

```text
python -B -m unittest discover -s tests -v
```

Do not run the producer report writer as a portable entry: that historical writer expects its original local prerequisite layout. The three files above remain exact byte copies; no compatibility alias or weakened source pin is introduced. The adapter's own CLI is an offline manifest preview, with no live-execution flag. The existing public operator's `FINITE_PROBE_ADMITTED=False` remains unchanged.

The bridge only compiles a BTC BUY projection using artificial capital, prices and clocks. One retained fake-only registry binds the portfolio/cutoff identity and immutable plan, rejects same-cutoff intent renaming and consumes a dispatch stamp before a separate fake order journal. Reopen and unknown-order controls cannot manufacture another POST. The cutoff-to-eligibility span is capped at 60,000ms as a declared fixture assumption; deadline equality is allowed, and the last local dispatch-clock sample does not bound exchange arrival or fill.

Registry and order journal commits are separate. A crash between them can conservatively lose eligibility. Deleting, copying or resetting either file does not establish account-wide deduplication. A retained plan does not reevaluate fresh risk or observations at fake dispatch. The closed FakeScenario's fills and wallet arithmetic are synthetic and demonstrate no actual new fill. ETH, SELL, rotations, live observations, prospective point-in-time evidence, continuous scheduling, deposit integration and economic returns remain outside the implemented scope. No real key loader, forward feed or network dispatcher is added by this bridge.
