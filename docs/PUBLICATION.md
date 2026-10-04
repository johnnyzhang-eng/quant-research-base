# Public/private boundary

Public by review: foundational code, ordinary public-method explanations, synthetic fixtures, reproducible acceptance procedures, approved summaries and conclusions.

Private by default: credentials and account/session files; account screenshots/positions/orders; vendor data without redistribution permission; potential valuable factors and their feature definitions, parameters, universe filters, signals, trading rules and results that reveal them. Private material belongs outside this Git working directory and must not be automatically exported.

High apparent alpha is not proof of value. Nevertheless, until disclosure is reviewed, a candidate with potential economic value stays private. The tooling must not decide to publish it solely from an in-sample metric. Research details can be separated from a public methodological summary; an ambiguous export remains local.

## Before publishing

1. Verify actual GitHub identity and target owner.
2. Stage only the curated public roots. Inspect the staged diff and filenames.
3. Run `python -B tools/check_publication.py` and separately review personal identity/employer/quotation content and data licenses.
4. Confirm that no factor definition/parameter/signal or sensitive result has crossed from private research.
5. Publish and verify the remote outcome. A locally prepared commit is not delivery.

The guard covers tracked files, known credential patterns, personal filesystem paths, prohibited directories and selected file types. It is not a complete detector of confidential ideas or every possible secret. `.gitignore` is a convenience and can be bypassed; it is not the sole boundary.

Already published information cannot be made reliably private by deleting a later commit. Therefore review happens before first publication.
