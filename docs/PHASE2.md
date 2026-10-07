# Historical input, model components and offline order acceptance

The second phase adds two bounded components to the existing research base:
a historical input converter and a persistent offline paper order manager.
Neither component authorizes a historical strategy trial or connects to a broker.
The original frozen study and existing synthetic-only engine gates remain intact.

## Reproduce the finite milestone

Python 3.12+, standard library only:

```sh
python -B -m research_base phase2-controls --output /path/to/private/evidence/phase2
```

The output folder contains every attempted run, source snapshots, detailed
known-answer and rejection records, and a terminal record in `registry.jsonl`.
Its result always distinguishes invented controls from historical research and
offline venue activity from official paper orders. The aggregate checker reads
each expected/actual pair; an `accepted` label alone cannot pass acceptance.

Include the new historical-model components with:

```sh
python -B -m research_base phase2-controls --historical-model-controls \
  --output /path/to/private/evidence/phase2-model
```

This executes separate fixed-answer controls for [causal four-ETF signals](historical-signals.md),
[dated fee and settlement rules](trading-terms.md) and the [cash/FX wallet](cash-account.md).
Each preserves its evidence classification and is checked against an independent
literal inventory. This component-only command does not run a stock account.
For finite controls through an already installed Vibe environment, use:

```sh
python -B -m research_base phase2-controls --integrated-account-controls \
  --output /path/to/private/evidence/phase2-integrated
```

This includes the components plus [integrated stock/wallet controls](historical-account-controls.md),
using invented data and an independent Fraction replay. A [private conditional
account run](historical-account.md) has its own retained `account-run` entry.
These commands do not download prices, establish a formal historical strategy
return or send an official simulated order.

The [frozen private study runner](historical-study.md) retains development-only
101-candidate risk calibration followed by 32 continuous account cases,
window/year reports and paired comparisons. Model v2 includes reviewed fixed
expenses and unpaid liabilities; [constrained cash exits](historical-liquidation.md)
are separate post-study branches. Its shorter invented-control scope cannot
replace the original market study.

Verify the returned run folder with:

```sh
python -B -m research_base verify /path/to/private/evidence/phase2/runs/RUN_ID \
  --registry /path/to/private/evidence/phase2/registry.jsonl
```

A seal detects local changes; it does not certify input accuracy, source rights,
broker records, or scientific validity. Keep failed attempts and their error
classification. Source changes during acceptance invalidate that run.

## Convert an authorized local input

```sh
python -B -m research_base historical-import \
  --manifest /path/to/private/input/manifest.json \
  --private-root /path/to/private \
  --output /path/to/private/converted/NEW_BUNDLE
```

The command checks retention against the current UTC time. Use the versioned
[input schema](historical-input.md), including raw price/share units, explicit
calendar hours, observed or modelled availability, hashes, and reviewed rights
records. No data is fetched. A user-provided private root is a destination
constraint, not proof of confidentiality. Keep it outside a public checkout.

Normalization is an input artifact, not an accepted engine path. Unknown
historical revision/receipt coverage stays explicit. Modelled availability does
not turn into an observed receipt. Hashes and reviewed declarations cannot
independently establish a legal grant; recheck source conditions before reuse.

## Execution boundary and remaining acceptance

The [persistent order manager](paper-execution.md) uses a fictional local venue.
Its environment binding, recovery and reconciliation are tested on that venue.
Official adapters require separate environment/account verification and a
representation of the actual provider's callbacks, snapshots and corrections.
An installed SDK or an authenticated read-only connector does not establish
simulation order rights or a completed lifecycle.

The separate [guarded OpenD paper mapping](futu-paper-adapter.md) preserves
private account binding, intent reservations, uncertain-state recovery and
explicitly unknown execution details/fees. Its injected controls do not contact
a broker. `paper-local-readiness` is local inventory only. The finite private
run interface is documented in [paper-run](official-paper-run.md); it has no
implicit network factory or retry loop.

Before the wider second phase can be complete, it still needs authorized
historical coverage, an accepted historical engine conversion, the full frozen
strategy/benchmark/scenario matrix with continuous ledgers and paired inference,
and a bounded official simulation lifecycle with reconciliation. Offline
controls, a preflight report, or publishing code cannot substitute for them.
