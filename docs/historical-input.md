# Historical input contract v1

`research_base.historical_input` converts a hashed local CSV bundle into a distinct
`historical-input/1` artifact. It accepts `historical_market` or `invented_control`
provenance and preserves the value. It makes no historical-return or execution
claim. Existing synthetic-only strategy, accounting and formal gates remain in
place; conversion is not an adapter that unlocks them.

## Local API

```python
from research_base.historical_input import normalize, import_history, snapshot_as_of

artifact = normalize(manifest_path, private_root, checked_at="2026-10-05T00:00:00Z")
manifest = import_history(manifest_path, output_dir, private_root,
                          checked_at="2026-10-05T00:00:00Z")
view = snapshot_as_of(artifact, "2020-01-03T21:05:00Z")
```

Input, evidence and output paths must be under the explicitly supplied private
root. Path traversal and symlinks inside the bundle are rejected. Import creates
its output directory exclusively and writes `normalized.json` plus a checksum
manifest. Reusing an output directory fails. Files and the input manifest are
hashed before and after parsing to detect concurrent persistent changes. Hashes
provide change detection, not external notarization or proof of legal rights.
Before later use, recheck the rights record against current conditions; the saved
artifact does not refresh subscription/cancellation status or delete expired data.
The caller must archive or delete raw, normalized, backups and derived artifacts
according to the grant's conditions. This module does not fetch data or contact
any external service.

## Bundle manifest

The JSON manifest needs these fields:

- `schema_version`: `historical-input/1`.
- `data_kind`: `historical_market` or `invented_control`.
- `retrieved_at`: timezone-aware actual retrieval time.
- `files`: relative path → `{role, sha256}`. Exactly one CSV each has role
  `calendar`, `bars`, `actions`. Other files use `rights` or `evidence`.
- `sources`: source ID → hashed `rights` JSON path.
- `calendar_evidence`: an `evidence` path supporting the calendar declaration.
- `instruments`: symbol → `{source_id, currency, timezone, price_basis,
  price_unit, volume_unit}`. `timezone` must be a valid ZoneInfo key;
  `price_basis=raw_unadjusted`, `price_unit=currency_per_share`,
  `volume_unit=shares` are required.

Each row source must have an in-scope rights record. Bars bind to one explicit
source per instrument; the converter neither blends sources nor fills missing
values. Corporate-action sources may differ from the bar source when separately
bound to rights evidence. The converter does not infer that SDK/code licensing
also grants market-data rights. Both historical provenance labels and rights
source nature must agree; provenance assertions cannot detect deliberately false
attestations and must be reviewed against the actual source.

Each rights JSON has exactly the following fields: `source_id`, `source_nature`,
`terms_evidence`, `reviewer`, `authority`, `scope`, `review_note`, `purpose`,
`storage_scope`, `raw_storage_allowed`, `normalized_storage_allowed`,
`effective_at`, `reviewed_at`, `retention`, `expires_at`, `delete_by`,
`deletion_conditions`, `active_deletion_conditions`.

The source nature matches manifest provenance. A human review states authority,
scope and note and binds to hashed terms/grant evidence. Purpose must be
`local_research`, scope `private`, and both storage flags explicitly `true`.
Effective/review times must be aware and ordered before the supplied check time;
retrieval cannot predate the grant. Deletion conditions are a nonempty list of
triggers; active conditions must explicitly be an empty list. A cancellation or
other active trigger blocks conversion even before expiry. `until_expiry` requires
an expiry strictly after the check time, a deletion deadline on/after expiry and
an `expiry` trigger. A `perpetual_grant` requires explicit null expiry/deadline and
supporting evidence; the converter never infers perpetual rights. The reviewer
must refresh external conditions; checking these declarations is not legal
certification or proof that a subscription remains active.

## Exact CSV columns

Columns may appear in any order but must match the schema, including empty
inapplicable action fields. Duplicate headers, ragged rows and unknown columns
are rejected. Decimal prices remain decimal strings; volume and split components
become integers. There are no vendor downloads or vendor-specific mappings.

Calendar: `trade_date, open_at, close_at`. Dates are unique, ordered ISO dates.
Session hours must be aware, ordered and nonoverlapping. V1 supports sessions
opening and closing on the same date in every instrument's declared exchange
zone. It deliberately does not support overnight or mixed exchange calendars.

Bars: `trade_date, symbol, source_id, open, high, low, close, volume, currency,
price_basis, price_unit, volume_unit`, plus all timing columns below. Every
calendar-session/instrument pair needs exactly one bar. Prices are positive raw
OHLC in currency per contemporaneous share; volume is actual nonnegative integer
shares. Adjusted prices, lots or missing fields are rejected. OHLC must satisfy
low ≤ min(open, close) ≤ max(open, close) ≤ high. No adjustment factors, rounding,
imputation or corporate-action transformations are silently applied.

Actions: `id, symbol, source_id, type, effective_date, pay_date, currency,
numerator, denominator, gross_per_share, share_basis`, plus timing columns. IDs
are unique and effective dates are calendar sessions. Split rows need positive
integer numerator/denominator (new shares / old shares) and empty dividend
fields. Dividend rows need empty ratio fields, nonnegative gross currency per
post-split share, `share_basis=post_split`, and an ISO pay date on/after the ex
(`effective_date`) date. Payment may be outside the bar window: the entitlement
record is retained. Cash-crediting, tax, fractional-share handling and ordering
of simultaneous economic events belong to a later accepted ledger path.
Empty actions are represented by a header-only CSV, never an omitted file.

Timing columns on every bar/action: `availability_basis, available_at,
observed_received_at, timing_evidence, model_rule, revision_status`.

- `observed` requires an aware received timestamp equal to availability, a
  hashed archival evidence reference, and an empty model rule. This is an
  evidence-bound archive claim, not an independent verification of that archive.
- `modelled` requires an explicit rule, empty observed timestamp/evidence, and
  remains a model assumption. The converter never turns retrieval time or close
  time into an observed historical timestamp.
- All availability is at/before retrieval. Full OHLC bars cannot be available
  before close. Actions may be announced before their effective date and become
  available later than their effective date; conversion preserves late records.
- `revision_status` is explicitly `unknown`, `as_of_archive` or
  `unrevised_source_claim`. Unknown revision history is preserved as a limitation.

`snapshot_as_of` filters actual normalized records by availability, preserving
labels. A known future announcement remains visible; it is not applied early as
an entitlement. Calendar bars/marks do not acquire opening tradability, liquidity
or quote-availability claims through this conversion.

## Reproducible controls

`run_controls(output_dir)` creates an immutable invented fixture, normalized
artifact, report and SHA-256 inventory. The 23-ID denominator is frozen in
`EXPECTED_CONTROL_IDS`; missing/duplicate reports, mismatched actual/expected
values or forged passed flags prevent acceptance. Controls include independent
hand-calculated 2-for-1 split/per-share dividend units, raw price/volume retention,
17 rejected mutants, an observed declaration, and actual causal prefix equality
under future price/volume perturbation and future truncation. The perturbation is
also checked to change the future normalized output, preventing a no-op test.
These are converter and information-view controls, not ledger or strategy
accounting acceptance and not a historical-market-data study.

The focused unittest module also checks mid-parse file/manifest changes, expiry
at its exact boundary, cancellation, path/privacy/overwrite enforcement and
unchanged synthetic-engine rejection of a historical-labelled artifact. No
vendor data, identifiers, credentials or broker calls are used.
