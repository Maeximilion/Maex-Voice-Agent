Run the eval suite and assess the result.

1. `make eval` (optionally filtered: $ARGUMENTS).
2. Compare with the last run in `evals/reports/`: accuracy, the three hard metrics (guessed line items, unconfirmed transactions, missed escalations), tokens and cost per case.
3. List every newly failed case with expected/got.
4. Judgment in one sentence: mergeable or not, and why.
5. A full run on a real model recorded itself in `docs/metrics/` and redrew the README images (`docs/08_EVALS.md` §4): commit `docs/metrics/` and the changed README images on the branch. On `main` nothing was written; the run waits for `/done` (`python3 scripts/readme_assets.py pending`).
A violation of a hard metric means: not mergeable, regardless of accuracy.
