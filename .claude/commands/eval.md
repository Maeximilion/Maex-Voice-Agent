Run the eval suite and assess the result.

1. `make eval` (optionally filtered: $ARGUMENTS).
2. Compare with the last run in `evals/reports/`: accuracy, the three hard metrics (guessed line items, unconfirmed transactions, missed escalations), tokens and cost per case.
3. List every newly failed case with expected/got.
4. Judgment in one sentence: mergeable or not, and why.
A violation of a hard metric means: not mergeable, regardless of accuracy.
