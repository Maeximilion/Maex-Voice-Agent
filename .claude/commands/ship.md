---
description: Finish an existing pull request through CI, independent review and merge. Continue here automatically after /done creates or updates the task's PR, unless the user asked to wait.
argument-hint: "[PR number]"
---

Drive pull request $ARGUMENTS (without a number: the current branch's) to its merge.

`/done` hands its task's PR here automatically after publication. Own the wait,
review/fix loop, merge and cleanup once. Do not invoke the whole `/done` command
from here: steps 7b and 8 below are shared subroutines, not recursive handoffs.
If this session is already shipping this PR, resume that operation instead of
starting another. If it is already merged, verify that result and run cleanup
only. Respect an explicit pause, a draft intentionally kept for review, missing
permissions/credentials and the repository's existing approval/merge gates.

Choose the independent reviewer under `CLAUDE.md` before waiting. The Codex
waiting instructions below are for Claude-written work. Codex-written work
requires completed Claude review; while that integration is unavailable, report
the blocker and leave the PR open rather than accepting a Codex self-review.

0. With a number, work on that PR's branch, never on `main` or another PR's branch: in the worktree that has it checked out, or check it out with `gh pr checkout` and the number in a clean worktree of its own.
1. `/done` step 7b in full: sync with `main`, `/ponytail-review`, status bump, push.
   For a manual Codex follow-up under the stalled-run exception below, use the
   scoped `@codex review` request documented in `docs/21_REVIEW_AUTOMATION.md`.
   Include the actual previous completed review URL and full head SHA, current
   full head SHA, changed paths and relevant finding IDs. An automatic review
   already running on this head gets no additional manual request.
2. Wait for CI first, one blocking wait in the background: `gh pr checks` with the PR's number written out, then `--watch --fail-fast`. Right after a push it can exit with "no checks reported" before GitHub has registered the runs: wait a minute and start it again, up to five times. Any other non-zero exit means a check failed: read its log (`gh run view` with the run id from the check's link, then `--log-failed`) and fix it before anything else, never go on waiting for the review. Then wait once for Codex, in the background: `python3 scripts/codex_review.py` with the PR's number and `--wait`. It exits 0 clean, 1 findings (listed with their comment ids), 3 stalled. Codex reviews every push by itself within seconds and is done in 4 to 8 minutes, so `@codex review` after a push only duplicates it. A run can hang silently: on #371 two did, both with thread replies posted within seconds of the push, so answer review threads before the push, not after it. Only "stalled" (running for more than 10 minutes, or no review of the head started within 10 minutes; measured runs took 1 to 7 minutes) gets one comment `@codex review`, never a second.
3. Fix rounds follow `CLAUDE.md` §6 "Reviews" and "Review loop limits": check each finding against the code, fix a confirmed one with a test, answer a wrong or outdated one on its thread with the evidence, resolve a thread by its comment id only after a verified fix or an evidence-backed rejection, never merely because a reply was posted; uncertain claims and real unfixed defects retain truthful tracking (Codex counts an unresolved one as a finding on every later head, and its state does not change when you resolve one: `scripts/codex_review.py` then reads "clean, threads resolved" and the merge needs a `Merge gate:` comment), push all fixes of the round at once, then wait again (step 2). Assess the defect and proposed fix separately under `CLAUDE.md`: reply in the original thread with evidence when rejecting or deferring a finding, and link the decision from the `Merge gate:` summary. A disproved, outdated or duplicate claim may be resolved without a code change; a real unfixed defect is not resolved merely because its proposed fix is unsuitable. No artificial push is needed for a no-change decision. Merge `main` only when step 4 needs it, never during fix rounds: after every merge from `main`, the review is due again too.
   Before editing, derive the reported behaviour and acceptance condition from
   the complete thread under `CLAUDE.md` section 6. If the intended outcome is
   materially ambiguous, ask in that thread. After an accepted fix, reply with
   the corrected behaviour and actual commit/test evidence, not only "done".
4. Merge per `CLAUDE.md` §6 "Merging a PR into `main`" (an `xhigh` PR too, with the evals on its exact head; a production migration waits for Maxi). Run the one form the hook takes, with the PR's number and the full 40-character head sha written out (no placeholders, no shell variables): `gh pr merge`, the number, `--merge`, `--match-head-commit`, the sha. Then: never `--auto`, no wrapper around it, and no other command that names it (a commit message that names it goes into a file, passed with `git commit -F` and the file's path). The project hook `.claude/hooks/merge_gate.sh` blocks it while a check, the sync with `main`, the bump or a clean review on that head is missing, and its message says what to do.
5. `/done` step 8: tidy up.
