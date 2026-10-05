# Finite private paper run

`research_base.official_paper_run.run_paper(config_path, private_root, output,
transport=reviewed_transport)` performs one requested action on the separate
OpenD adapter. It does not supply an SDK import/connection factory, account
creation, cash resolver or credentials. The transport must attest its actual
context peer and freshly query the explicit US SIMULATE account.

The strict private `official-paper-run/1` config has exactly `schema_version`,
`binding`, `envelope`, `state_file`, `action`, `intent`, `payload`, `fee_bound`.
Binding and envelope follow [adapter](futu-paper-adapter.md). For the first
probe, peer host must be loopback, maximum quantity and pending intents must
both be one, and submission window cannot exceed one hour. `state_file` is a
relative private SQLite path. `action` is `preflight`, `recover`, `submit` or
`cancel`; only `submit` may contain a new payload and fee bound. A fee bound is
an explicit assumed upper limit, never an observed actual broker fee.

Each run first queries existing unresolved intents, then performs that one
requested action. Empty or eventually inconsistent queries cannot authorize
resubmission. Restart does not extend the persisted submission window. Cancel
must query and bind the original provider order first; request acceptance is
not terminal cancellation. The runner has no automatic polling, waiting,
resubmitting, cancellation, next-day scheduling or real account fallback.

Input and output must stay under the supplied private root and outside the
source checkout. Child symlinks, including an existing registry link, are
rejected. A private directory is a placement constraint, not encryption or an
independent confidentiality audit. The run preserves its config, restart
queries, final adapter journal, source hashes, errors and sealed registry entry.
`durable_submission_intents_this_run` counts saved before-place intents; it
cannot assert a request crossed the network or reached the provider. An action
return, zero errors or a seal does not verify the official lifecycle. Official
chain/economic verification and goal completion remain explicitly false.

A command is available for **local inventory only**:

```sh
python -m research_base paper-local-readiness --config PRIVATE_CONFIG --private-root PRIVATE_ROOT
```

It reports whether this Python environment can discover `futu`; it neither
imports/installs that dependency nor opens a context/socket. It never verifies
login, rights, peer identity, settled cash or a simulated order. Account IDs
remain in private files and are not printed by this command.
