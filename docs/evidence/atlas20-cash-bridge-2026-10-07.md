# Atlas20 cash/inventory bridge controls — 2026-10-07

Three previously specified controls, K05/K06/K07, were actually executed with isolated functions from fixed source commit `46c1af3934a459252c8cea18c1c149fc75705505` and independently reviewed. They are invented fixtures, not market backtests or author-return reproductions. The other ten specifications were not executed by this run.

The fixture begins with a test numeraire of 100,000. It is unrelated to any personal account or capital. K05 and K07 assume 20 basis points per synthetic fill, paid in quote currency; K06 deliberately uses zero fees to isolate the timing difference. All fixtures use fractional spot quantities, zero slippage and no borrowing. These assumptions are not venue quotes or changes to the programme's frozen cost assumptions.

| Control | Actual isolated-source result | Independently reconciled cash/fill result |
| --- | --- | --- |
| K05: first selected candidate A, then switch to B | Chained returns 0 and −0.004; terminal NAV 99,600 | Enter A from cash, then sell A and buy B: marked NAV 99,401.994414; B remains held |
| K06: signal close100, eligible next open110, next close110 | Supplied next-row close-to-close return gives NAV110,000 | Buying at110 and marking at110 gives NAV100,000 |
| K07: cash → A → B → cash at constant100 | L1 turnover 0,1,2,1; terminal NAV99,201.9984 | Actual quote fees and financed quantities give closed cash99,203.190426 |

The main source engine does charge initial entry. The rolling selector instead stitches prebuilt candidate returns: its first segment has no independent entry step, and a later candidate-name change incurs a fixed switch charge. These mechanisms must not be confused.

The return-cost and cash-fee contracts differ; their numerical difference alone does not establish a bug, misconduct or a direction of bias in the author's real returns. Actual inventory, cost currency and decision/fill clocks remain prerequisites for economic reproduction.

An independent reviewer compared selected ASTs, recalculated every saved synthetic fill with exact fractions, and exercised nonempty zero-fee/cash and rejected-order controls. The original private helper is unsuitable for simultaneous assets at different prices. The public version rejects a second nonzero holding before any state change, so it is explicitly a single-position rotation helper rather than a general portfolio engine. Root also ran its public CLI in a fresh output directory; the reviewer independently checked the guard and source-function behavior, not that CLI/main invocation.

## Reproduce the finite offline experiment

Use Python with pandas2.2.3 and numpy2.3.5; the observed run used Python3.12.14. The experiment performs no downloads or orders and executes no upstream module imports, loaders or main. Download these fixed public code snapshots separately and save each under the indicated filename:

| Fixed source | Local snapshot filename |
| --- | --- |
| [engine.py](https://raw.githubusercontent.com/WW-shan/Atlas20/46c1af3934a459252c8cea18c1c149fc75705505/src/atlas20/backtest/engine.py) | `src__atlas20__backtest__engine.py` |
| [rolling selector](https://raw.githubusercontent.com/WW-shan/Atlas20/46c1af3934a459252c8cea18c1c149fc75705505/scripts/run_vol_target_walk_forward.py) | `scripts__run_vol_target_walk_forward.py` |
| [metrics dependency](https://raw.githubusercontent.com/WW-shan/Atlas20/46c1af3934a459252c8cea18c1c149fc75705505/scripts/run_strategy_evidence_audit.py) | `scripts__run_strategy_evidence_audit.py` |

The script validates all source hashes before selecting a fixed AST whitelist. Source hashes, exact rational expectations and result fingerprints are in the companion JSON. Provide a fresh run directory and an existing fresh report directory:

```sh
mkdir synthetic-reports
python experiments/atlas20_inventory_bridge_controls.py \
  --source-dir ./fixed-source-snapshots \
  --output-dir ./synthetic-private-run \
  --report-dir ./synthetic-reports
```

Re-running in the same directories deliberately refuses to overwrite an earlier run. The output contains invented fill ledgers and extracted source functions; keep it private unless separately reviewed for publication.

No market-return advantage, funded-account access, production execution or overall Goal completion is accepted. The original four-ETF and cross-market research requirements remain open. Raw chats, screenshots, private paths, credentials and market data are excluded from this checkpoint.
