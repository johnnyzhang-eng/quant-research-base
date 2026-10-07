# Pinned source review and its evidence

[The source review](recent-crypto-source-review.md) is copied byte-identically from the frozen review, SHA256 `0f1441a2cf036d1cb8f8adf2b54c82bc775ff14df0e4bd4db840bfa28b24912d`. This separate index adds repository-relative links without rewriting that review.

- [Root static-disclosure audit](evidence/source-review-2026-10-08/recent-crypto-source-audit-20261008-v0.json): SHA256 `3290e0f9008e1d8d691065dccdc29fd7cd4abebae01538f395aedaec2bb8da5c`.
- [Independent audit](evidence/source-review-2026-10-08/recent-crypto-source-independent-audit-20261008-v0.json): SHA256 `acaacf559319b854df1cfe7f974cdf877f92b07aa99a1c48b1e9fc5db0270ef6`; all 16 captures have uniquely matched repository/revision/path identities. The root's two `README.md` entries must not be indexed by basename alone.
- [Cost-aware paper source review](evidence/source-review-2026-10-08/cost-aware-paper-source-review-20261008-v0.json): SHA256 `9dd19d9e1847469be40fad5170d942b0e613ca15f9cbf227ba18222ddc89551e`. Its 18 arithmetic assertions examine formulas and definitions; they are not author backtests, market observations or bootstrap replication.

The [offline audit script](../tools/recent_crypto_source_audit_20261008_v0.py) retains original SHA256 `998d48055dafc8fee7b86bf59ee6f217d176e91bb36e4683cfdb22054e0a13ed`. It does not download inputs or execute the reviewed strategy files. The captured inputs were retained privately and are not distributed here. A reader can obtain the same publicly pinned tree metadata and files from the original repositories, respecting their licenses, then provide their own two capture directories:

```text
python -B tools/recent_crypto_source_audit_20261008_v0.py --capture-v0 CAPTURE_V0 --capture-v1 CAPTURE_V1 --output NEW_AUDIT.json
```

`CAPTURE_V0` must contain `index.json`, the ten `cards/*.json` corresponding to the `passed/` keys, `passed/asia_drift_btc_v2.py`, `passed/crash_recovery_eth_v3.py`, and `walk-forward-redaction-report.md`. `CAPTURE_V1` must contain `crypto-strategies-tree.json`, `crypto-strategies-README.md`, `walk-forward-crypto-tree.json`, and `walk-forward-crypto-README.md`. Tree metadata comes from the original GitHub Git Trees endpoint with `recursive=1`; file bytes must match the pinned blobs. The script exclusive-creates its output.

The fixed sources are [Crypto-Strategies revision f8e3259](https://github.com/CacheCarti/Crypto-Strategies/tree/f8e32599c192b823dbe7aea77a2b9341c6114527) and [walk-forward-crypto revision 8ec32ea](https://github.com/AKzar1el/walk-forward-crypto/tree/8ec32ea1ad454edd88abc5118a808c6fa764063b). The captured mirror's redaction document supplies `walk-forward-redaction-report.md`. Reading those inputs reproduces a disclosure audit, not their economic returns. An unestablished exact reproduction does not prove that approximate reimplementation is impossible or that either author's private strategy failed.

No historical raw market data, author implementation, account information, keys or private capture paths are published. Neither this batch nor its CI grants trading permission, resets an existing order budget or completes a continuously executed strategy.
