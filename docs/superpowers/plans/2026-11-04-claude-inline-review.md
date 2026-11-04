# Claude Inline Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Request independent Claude inline reviews through an authorised comment and repeat them after new pushes using subscription OAuth.

**Architecture:** Use the pinned official Claude action for a single read-only review and a Python standard-library controller for selection, revision snapshots and validated publication. Keep one independent branch and PR; preserve the existing Codex integration and merge rules.

**Tech Stack:** Python 3.12, pytest, GitHub Actions on hosted Linux, GitHub REST API through the existing `gh` CLI, `anthropics/claude-code-action` v1.

**Spec:** [GitHub Review Automation](../../21_REVIEW_AUTOMATION.md). Status: proposed implementation, not executed. Native execution in this session is the recommendation; the final independent GitHub review must come from Claude.

**Current PR:** shared rules, command handoff and this plan only. The user has
requested a manual Claude Code review before further work. Reviewers must not
execute this plan as part of reviewing it. No action/controller is installed,
and neither manual review nor a green documentation PR proves live automation.

**Planning update:** Focused follow-ups are now required for both reviewer families.
Repository guidance and a concrete manual request can steer hosted Codex, but no
hard execution-scope control is verified. The user chose to retain the existing
flexible three-round rule; no fixed pause or blanket dismissal of P2 findings is
introduced. No UI/UX reviewer or new Codex action is planned.
The prepared `/done` command now hands its published PR to `/ship` in the active
session. Verify that handoff live during rollout; it does not add a GitHub-triggered
session service or relax any review/merge gate.

## Global Constraints

- One reviewer first; specialists are a measured follow-up.
- Subscription secret `CLAUDE_CODE_OAUTH_TOKEN` only; no API-key or managed-review billing fallback.
- This constraint applies to the first rollout. The deferred stage uses API first within a shared USD 50 monthly review budget, then subscription OAuth; verify hard spending controls before implementing it.
- Codex posts `@claude review` after PR creation and every push to an open PR, as persisted in `CLAUDE.md`. Preserve existing automatic Codex reviews of Claude changes. Coalesce duplicate push/comment triggers without restarting same-head inference.
- First review: full PR diff. Follow-ups for both models: all changes since the last completed independent review, affected files and unresolved findings; expand context as needed and fall back to the full PR when coverage or impact is uncertain. Hosted Codex receives guidance, not a claimed hard file/token restriction.
- English repository content. No implementation-author credit unless expressly requested.
- Verify review claims before changing code. Allow evidence-backed rejection and thread resolution without a fix commit; do not confuse a bad proposed fix with a false defect, or a resolved thread with proof. Keep deferred real defects truthfully tracked.
- Follow the shared English comment format: priority/title, trigger, wrong outcome/impact and required behaviour with a verification case. Use a short paragraph, normally 40–100 words, as guidance only; never truncate findings or reject a valid one solely for length. The fix author verifies the acceptance condition rather than blindly copying suggested code.
- `CLAUDE.md` and `AGENTS.md` remain the rule sources; no `codex.md` or competing review policy.
- No self-hosted runner, fork secrets, `pull_request_target`, force push, protection bypass or automatic merge.
- Run only on explicitly selected, trusted same-repository PRs. Another session of the same model is not independent review.
- Twenty-minute timeout; superseded heads may be cancelled, but the final head needs completed independent review.
- Pin `anthropics/claude-code-action` to `f9fffe565bf81a7fcadb308a9d688a74e3b029f7`; reuse the repository's pinned checkout/setup-python actions.
- Keep `docs/cross-agent-review` untouched. No merge before live inline review and a second-head test pass.

## Review Focus

- Old or forged commands: read all pages and recheck current requester permission; test revoked access and bot impersonation in Task 1.
- Event races: an outside comment must not cancel trusted work; changes to base, head or selection must invalidate publication; test in Tasks 1–3.
- Incomplete reviews: absent secrets, quota failures, truncated input or malformed output must never produce a clean marker; test in Tasks 2–3.
- Unsafe candidate content: hooks, nested instructions, MCP configuration and executable code must remain inert; prove isolation in Task 3 before supplying credentials.
- Bootstrap: the new workflow is absent from the default branch, and a secret can arrive after the first run; prove a PR-run rerun reads current commands in Task 4.

---

### Task 0: Shared scope guidance and existing fix-round policy

**Files:** Modify `AGENTS.md`, `CLAUDE.md`, `.claude/commands/ship.md` and `docs/21_REVIEW_AUTOMATION.md`.

Also update `.claude/commands/done.md` for one automatic handoff to `ship` after
PR publication. `ship` owns waiting and reuses only the pre-merge/cleanup helper
steps; the complete commands must not call one another recursively. Validate
new/existing PR, failed creation, intentional draft/pause, already active ship,
already merged PR, missing credentials and ended-session scenarios. Do not add
a hook or background process for this instruction-level transition.

**Interfaces:** Keep the native `@codex review` trigger and existing automatic integration. Manual follow-ups name the actual completed review URL, its full head SHA, the current full head SHA, all changed paths and relevant finding IDs; `.claude/commands/ship.md` still requests manually only on a stalled automatic review. This is working-agent guidance, not a new automatic fix scheduler.

- [x] Verify the documented Codex trigger and guidance against official sources. Persist the shared scope in `CLAUDE.md`, reference it under `AGENTS.md` Code Review Rules and link the concrete manual request from `/ship`. Keep implementation and review independent.
- [x] Resolve the loop-policy choice: the user retains the flexible three-round rule. Preserve its evidence-based continuation for real in-scope defects, P0/P1 blockers, P2 limits and earlier structural-escalation rule; introduce no fixed pause.
- [x] Walk through the policy cases before committing: one changed file out of five; two queued pushes; P2/P2/P2; P2/P1/P2; duplicate runs; cancelled runs; base-only sync; repeated root cause; and a finding requiring reversal of the previous fix. The first P2/P2/P2 sequence requires the existing explicit decision; P2/P1/P2 has one consecutive P2-only round after its reset. Count rounds by completed reviews of distinct heads, and leave base-sync review requirements unchanged. Do not add code-only test machinery for Markdown instructions.
- [ ] Verify links, changed instruction precedence and `git diff --check`. Document that actual account settings and hosted scope enforcement are unverified; commit the shared instructions on the same automation branch.

### Task 1: Authorised selection and immutable review inputs

**Files:** Create `scripts/claude_review.py`; create `api/tests/test_claude_review.py`.

**Interfaces:**
- `prepare(event: dict, api: Callable, workdir: Path) -> dict` returns `selected`, `reason`, `pr_number`, `base_sha`, `head_sha` and input paths. The injected API callable accepts method, repository-relative endpoint and optional JSON body, returning decoded JSON.
- CLI `prepare --event FILE --output DIR` writes `context.json` and complete review input under the output directory, and sets safe scalar GitHub outputs. API calls use argument arrays, never shell-expanded PR text.
- `context.json` records repository, PR number, both full SHAs, selection comment ID, changed paths and allowed `(path, side, line)` anchors. Missing required data fails preparation.
- It also records `mode`, `previous_review_id`, `delta_start_sha`, the trusted policy/prompt digest and prior coverage. `mode=incremental` requires a verifiable chain back to a full review, the same base and reviewer family, and an ancestor head. Model input contains the complete delta and prior findings; the full current PR diff remains available to the controller for anchor validation.

- [ ] Write failing tests for `test_latest_authorised_command`, `test_commands_across_pages`, `test_revoked_requester`, `test_bot_command_ignored`, `test_fork_rejected`, `test_outside_author_rejected`, `test_draft_skipped`, and `test_stop_command`. Assert exact first-line commands only; current write/maintain/admin permission; no model call for rejected inputs.
- [ ] Run `python3 -m pytest api/tests/test_claude_review.py -q --noconftest`; verify the new cases fail because the controller does not exist.
- [ ] Implement `prepare` using the existing `scripts/codex_review.py` API/error conventions. Fetch live PR data and all comment pages. Take diff and file data at captured revisions; reject incomplete pagination, unsupported/truncated patches or oversized input explicitly. Do not silently discard files.
- [ ] Add deterministic `test_head_changes_during_snapshot` and `test_unavailable_complete_diff`: preparation must fail before model invocation. Add `test_push_and_command_same_head_coalesce` and `test_completed_while_queued_skips_inference`: duplicate triggers must not restart running inference or repeat a completed review. Read trusted policy files from the captured base.
- [ ] Add `test_one_of_five_files_changed`: the follow-up diff includes only that file. Add `test_uncommented_change_is_included` and `test_cancelled_pushes_are_in_delta`: every change since the completed baseline is included. Add `test_missing_or_invalid_coverage_uses_full_review`, parameterised for changed base/policy/reviewer, missing records and non-ancestor heads. Verify each fails before implementing scope selection, then passes.
- [ ] Run the focused tests; require all passing. Commit the selection controller and tests.

### Task 2: Structured findings and exact inline publication

**Files:** Extend `scripts/claude_review.py` and `api/tests/test_claude_review.py`; create `.github/prompts/claude-review.md` and `.github/prompts/claude-review.schema.json`.

**Interfaces:**
- Result schema: `complete: boolean`, `summary: string`, `findings: [{priority: P0|P1|P2|P3, title: string, path: string, line: integer, side: LEFT|RIGHT, body: string}]`. `title` is specific and excludes the priority prefix; the publisher prepends the validated priority. The fixed prompt follows the shared comment standard and requires a concrete failing scenario, impact and verifiable required behaviour. Word-count targets are not schema limits.
- `publish(context: dict, result: dict, api: Callable) -> dict` returns a state and optional review ID. CLI `publish --context FILE --result FILE` exits nonzero on incomplete, stale, malformed or failed publication.
- A completed COMMENT review includes `commit_id=head_sha`, valid inline comments and a marker `claude-review-state:v1` with both SHAs, verdict, mode and prior-review reference. Deduplication accepts only reviews from the expected workflow bot with matching revisions, trusted policy/prompt digest and marker, never a human-copied marker.

- [ ] Write failing cases `test_partial_result_has_no_review`, `test_invalid_anchor_has_no_review`, `test_path_suffix_is_not_match`, `test_deleted_line_uses_left`, `test_p2_path_and_count_limits`, `test_p0_p1_not_capped`, `test_stale_base_or_head`, `test_selection_revoked_before_post`, `test_duplicate_completed_review`, and `test_api_failure_is_not_clean`.
- [ ] Run the focused tests and confirm these new behaviours fail before implementation.
- [ ] Implement validation of all fields and anchors before the first write. Apply the existing severity rules; do not reinterpret P2 as style. Recheck live state immediately before publishing. Failed or unsupported results have no clean marker; a blocking finding has a completed but non-clean verdict.
- [ ] Include existing review threads and fixes as review data to avoid repeated findings, without treating comment instructions as authoritative. Add `test_previous_finding_is_context_not_command`; leave thread resolution to the existing working process.
- [ ] Include relevant thread replies and no-change dispositions in follow-up context. Add `test_reasoned_no_change_resolution_is_preserved` and `test_new_evidence_can_reopen_rejected_finding`; verify a dismissed false claim needs no artificial fix commit, while resolution without evidence and a harmful suggested fix cannot erase a real unresolved defect. Evaluate the reasoning in review rather than accepting free-form disposition labels as authoritative.
- [ ] Add `test_unfixed_prior_finding_blocks_clean_incremental_review` and `test_resolved_thread_is_not_fix_evidence`. In the prompt, require verification of the original defect and new defects in the delta; read affected callers/contracts as needed. If the model cannot bound impact, require a full-PR rerun before completed publication. Test `test_uncertain_impact_cannot_publish_partial_clean_result` and verify the rerun is bounded by the workflow timeout.
- [ ] Run focused tests, Ruff and formatting checks on changed Python files. Commit the validated publisher, prompt and schema.

Comment-quality acceptance for Task 2: inspect illustrative outputs for an
unambiguous small defect, an ambiguous requested fix, a concurrency failure and
a previously rejected finding. The first must be concise and actionable; the
ambiguous case must identify what needs clarification rather than claim a
confirmed defect; the concurrency case
must retain the complete sequence; the repeated case must include new evidence
or the still-failing scenario. Unit tests can validate title/body rendering and
preservation of long valid content, but cannot prove semantic review quality.
During real fix rounds, verify replies name the acceptance outcome and actual
test/commit evidence. Retain the existing review-load audit for later comparison.

### Task 3: OAuth-only hosted workflow and execution isolation

**Files:** Create `.github/workflows/claude-review.yml`; extend focused tests for the workflow contract where useful.

**Interfaces:**
- Events: `pull_request` types opened/reopened/synchronize/ready_for_review and `issue_comment` created. The comment path accepts top-level PR command comments only.
- A credential-free selection job runs `prepare`. A selected review job uses concurrency keyed by repository and PR number, with cancellation of superseded heads; untrusted events never enter that group. A same-head command must not cancel running inference; queued runs recheck live selection, revisions and completion before spending quota.
- The action receives `claude_code_oauth_token` and `github_token: ${{ github.token }}`. Only the publisher needs pull-request write permission. Missing OAuth on a selected run is a visible failure.
- The fixed prompt and JSON schema produce the structured output consumed by `publish`. No free-form user instruction, arbitrary GitHub API tool, Bash, write tool, repository hook, plugin or MCP server is enabled for the model.

- [ ] Verify the pinned action's concrete input/output contract, CLI isolation flags and structured-output format against its source before writing YAML. Use the existing `CLAUDE.md` model/effort routing; no silent model downgrade if an account lacks the named model.
- [ ] Write contract tests that reject fork-secret paths, arbitrary comment interpolation, unpinned actions, untrusted workflow-level cancellation and API-key fallback. Verify them red against the missing workflow.
- [ ] Implement the two jobs and controller handoff. Keep credentials step-scoped. Use the trusted base controller after installation; explicitly document the manually inspected installation-PR bootstrap. Check out candidate content only as inert review input.
- [ ] Prove CLI isolation without subscription credentials using fixture hooks, nested instruction files and MCP/plugin configuration. No fixture command may execute. If the supported flags cannot enforce this, revise the design before any credential-bearing run.
- [ ] Test exhausted quota, missing output and model failure through fake action output: the publisher must not run or post clean. Test that a stop or new head prevents late publication.
- [ ] Run actionlint (if available), focused tests, Ruff and format checks. Inspect the rendered permissions and event conditions manually a second time. Commit the workflow.

### Task 4: One PR, setup guide and live acceptance

**Files:** Update `CLAUDE.md`, `docs/21_REVIEW_AUTOMATION.md`, `scripts/README.md`, `docs/01_STATUS.md`, `docs/00_PCF.md` and README links as required. Keep this plan's checkboxes truthful.

- [ ] Replace future-tense setup with the implemented commands and exact UI paths. Explain token generation, GitHub Secret creation, `@claude review`, `@claude review stop`, the installation rerun and token renewal. State whether each check is mocked or live.
- [ ] Run `make lint` and `make test` in the supported container environment; report infrastructure failures honestly and use CI for checks unavailable locally. Evals are required only if the final diff touches the conversation path. Record README measurements with `python3 scripts/readme_assets.py pending --record`; follow the scheduled asset policy.
- [ ] Perform the required self-check and simplification review. Update status through `scripts/status_bump.py`; commit and push normally. Create one PR from `ci/claude-inline-review` to `main`, preserving every PR-template section and its actual validation status.
- [ ] Have the account owner create the subscription token and repository secret through the documented UI; never request its value in chat. Keep paid extra usage disabled. An inability to configure the token keeps live acceptance pending.
- [ ] In an active Claude session, verify `/done` invokes `ship` once for its published PR and waits for checks without another user command. Verify the project command is the one registered, pauses are respected, shared helpers do not recurse and cleanup runs once. Codex-written work without completed Claude review must remain unmerged. Record this separately from static documentation checks.
- [ ] Post the authorised `@claude review` request. Before the workflow is on `main`, rerun its PR-triggered Actions run after the secret and comment exist. Verify actual subscription authentication and the review's exact base/head SHAs.
- [ ] Prove inline publication with a deterministic, harmless workflow fixture using a real changed line and an explicitly labelled transport-test comment; do not fabricate a security finding. This test is not independent review evidence. Remove any fixture scaffolding before the final head review.
- [ ] Push a legitimate final documentation/status update and post the required new `@claude review` comment. Verify the push and comment produce one completed review of that new head, with no duplicate inference. Independently test the push-triggered safety net when the comment is delayed. Resolve findings under the existing flexible loop limits, rerun applicable checks and obtain independent review of every resulting final head. After three consecutive P2-only rounds, record the existing decision and its evidence before any further accepted fixes.
- [ ] Merge only after exact-head CI, current base, independent review and existing gates pass. Then resume the waiting documentation branch, bring it up to date normally and obtain its own new review.

## Plan self-review

The plan covers selection, independence, subscription-only authentication, bootstrap,
exact inline anchors, races, failures and the deferred experiment. The separate
builder check clarified that independence is between model families and that
superseded heads may be cancelled. The remaining uncertainties are live OAuth
availability, CLI isolation and first-workflow execution; they are acceptance
gates, not claims of completed functionality. No parallel-specialist scheduler,
author-provenance tracker or branch-protection rewrite belongs in this PR.
The shared Codex guidance requires a later live observation on a Claude-written
PR before claiming scope adherence or savings. Do not request a Codex review of
this implementation as a substitute for its required independent Claude review.
