Drive pull request $ARGUMENTS (without a number: the current branch's) to its merge.

1. `/done` step 7b in full: sync with `main`, `/ponytail-review`, status bump, push.
2. Wait for CI and the review on the new head, one blocking wait each, never a polling loop of short calls: `gh pr checks <n> --watch --fail-fast` in the background, and one background wait for a Codex state comment that names the head (`codex-review-state` with its `commit_sha`). A push that got no review after 15 minutes gets one comment `@codex review`.
3. Fix rounds follow `CLAUDE.md` §6 "Reviews" and "Review loop limits": check each finding against the code, fix a confirmed one with a test, answer the rest on their thread, push, then wait again (step 2). After every merge from `main`, the review is due again too.
4. Merge per `CLAUDE.md` §6 "Merging a PR into `main`". A PR with an `xhigh` trigger is reported ready and stays with Maxi. Otherwise `gh pr merge <n> --merge --match-head-commit <head sha>`; the project hook `merge_gate.sh` blocks it while a check, the sync with `main`, the bump or a clean review on that head is missing, and its message says what to do.
5. `/done` step 8: tidy up.
