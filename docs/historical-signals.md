# Historical signal features v1

`research_base.historical_signals.generate(artifact, variant, *, trade_start,
end_date, k=None, delay_sessions=0)` consumes `historical_input.normalize` output
plus a reviewed complete-calendar inventory. It returns causal features and
monthly targets, never a prepared synthetic engine input. Provenance remains
`historical_market` or `invented_control`; no historical rows are relabelled as
`synthetic_fixture` and existing strategy/accounting guards are unchanged.

The fixed universe is SPY/EFA/IEF/GLD in USD. `trade_start` and `end_date` must be
explicit resolved calendar sessions. `delay_sessions=0` schedules the next
session; `1` schedules the preregistered C3 extra session. The API rejects other
values. It does not silently move a late signal to a later opening.

## Full-month calendar binding

The normalized artifact's `calendar` is a list of `{trade_date, open_at,
close_at}` rows, not a list of dates. The extension `signal_calendar` needs:

```json
{
  "schema_version": "signal-calendar/1",
  "inventory": {
    "2020-01": {
      "expected_sessions": ["2020-01-02", "...", "2020-01-31"],
      "month_end_session": "2020-01-31"
    }
  },
  "inventory_file": "complete-months.json",
  "inventory_sha256": "sha256 of canonical inventory JSON bytes",
  "review": {
    "reviewer": "calendar reviewer",
    "reviewed_at": "2021-02-01T00:00:00Z",
    "scope": "full session inventory and all month boundaries checked",
    "source_nature": "historical_market"
  }
}
```

The ellipsis above is explanatory and cannot appear in actual input. The
inventory file is an independently obtained complete-session inventory, included
as an `evidence` file in the original hashed input bundle. Its bytes are exactly
canonical JSON for the `inventory` value: sorted keys, UTF-8, no spaces/newline,
finite values. Therefore `canonical_hash(inventory)`, `inventory_sha256` and
`artifact.input_files[inventory_file].sha256` must agree. Review provenance must
match the artifact and review time cannot exceed the converter's check time.
This is an evidence-bound completeness declaration, not independent exchange
certification. The preparer must obtain/review the calendar rather than derive a
supposed independent inventory from whichever sparse price rows happen to exist.

A missing declaration rejects generation. A month whose actual sessions differ
from the declared inventory is skipped and breaks the trusted total-return
chain. Whole missing months are detected from consecutive month keys. Missing
sessions cannot be replaced with ten sparse month-end rows. A broken chain is
not automatically reseeded or repaired; later S decisions remain skipped.
A trailing partial month cannot invalidate decisions already produced for prior
complete months. The complete input should begin with a fully reviewed warmup
month; partial initial months are not silently treated as complete.

## Gross signal calculation and timing

Each ETF begins with an arbitrary index of 100 at its first raw close. No return
before this anchor is invented. First-session actions have no prior
close return to apply; the index is anchored after that session. This arbitrary
constant scale does not change later index-versus-SMA comparisons. For subsequent
sessions:

`X_today = X_previous * split_ratio * (raw_close + gross_post_split_dividend) /
raw_previous_close`.

All splits on a date multiply their new/old share ratios. All already-effective
gross dividends add in currency per post-split share. Payment date does not delay
this ex-date gross signal; account withholding, entitlement/payment and net cash
belong to a separate ledger. An announced future action does not affect the
index before its effective session.

S uses ten consecutive complete month-end indices, including the current month.
Each asset receives target 25% only when its index is strictly above its ten-month
average; equality produces zero. Fractions retain exact arithmetic in
`index_fraction` and `sma_fraction`. Less than ten full months emits a warmup
skip. B0 has monthly targets of 25% per ETF, BR has `k/4` per ETF and BC has zero
risky holdings. BR requires an externally fixed k on 0, 0.01, ..., 1; this module
does not calibrate k or claim risk matching. Static comparators do not consume
price/action features to choose their fixed targets.

For S every bar/action from the anchor through the decision contributes its
`available_at`, timing basis, revision status and record hash. The maximum
availability must be strictly before the originally planned execution opening.
Close data arriving after close but before that opening are allowed. A late old
bar, revised bar or action causes an explicit skip at the original due date;
future known values cannot backfill that old signal. A later decision may use
records that have since become available. A single input revision per record is
supported; this module does not fabricate an as-of archive from revised final
snapshots. Modelled times and unknown revisions retain their original limits.

The output contains `decisions`, executable-within-window `intents`, `skipped`,
`unexecuted_intents` and a shared `provenance_records` table. Each decision binds
its causal component count/hash, availability modes, revision statuses, calendar
inventory hash, scheduled execution date and weights. Intents also contain the
actual decision-availability clock and decision hash. These are proposed targets;
no quote retrieval, order or fill occurs. `NO_NEXT_SESSION` preserves a terminal
unexecuted intent without blocking earlier decisions. A due date after the
requested end is marked `OUTSIDE_EXECUTION_WINDOW`. Before-start decisions remain
auditable but do not become executable intents.

## Acceptance boundary

Known-answer tests use only an invented full weekday schedule and normalize the
fixture through the real importer. They cover equality/cash, a 2-for-1 split plus
post-split dividend preserving index 100, and a positive control changing SPY to
index 200/SMA 110/weight 25%. Actual earlier decisions stay unchanged under future
price perturbation or truncation. Other checks cover old-bar/action lateness,
next-open timing, explicit C3 delay, future-effective actions, sparse/missing
sessions, whole missing months, terminal intent handling, static benchmarks,
cent-grid k and calendar/provenance mutation. The invented weekday calendar is
not presented as a real exchange calendar.

This layer computes features and targets only. It does not produce historical
strategy returns, implement the full S02 RMB/FX/interest/cost matrix, validate
source rights/PIT completeness independently, calibrate BR, or authorize broker
orders. `historical_engine_ready` remains false.

`run_controls(output_dir)` writes an immutable invented fixture, normalized input,
baseline/split-dividend/positive/late feature traces, the corresponding altered
invented inputs, report and hash manifest. Every generated report binds the whole
normalized artifact by its canonical hash; mutated control artifacts retain the
original fixture manifest reference as lineage, not as proof that changed rows
were in that original snapshot.
The 19 literal IDs in `EXPECTED_CONTROL_IDS` and independent literal answers in
`EXPECTED_CONTROLS` fix the acceptance denominator and answers. A missing,
duplicated, mismatched or forged row (even with both reported expected and actual
changed to the same wrong value) is rejected by `control_report_accepted`.
A type-preservation control temporarily supplies the historical label on invented
in-memory bytes solely to exercise the API's type handling; it is not saved or
reported as actual market-data evidence. Every evidence bundle remains classified
`INVENTED_CAUSAL_SIGNAL_CONTROLS_ONLY`.
