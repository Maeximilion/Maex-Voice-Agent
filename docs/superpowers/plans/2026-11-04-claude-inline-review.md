# Claude Inline Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Request independent Claude inline reviews through an authorised comment and repeat them after new pushes using subscription OAuth.

**Architecture:** Use the pinned official Claude action for a single read-only review and a Python standard-library controller for selection, revision snapshots and validated publication. Keep one independent branch and PR; preserve the existing Codex integration and merge rules.

**Tech Stack:** Python 3.12, pytest, GitHub Actions on hosted Linux, GitHub REST API through the existing `gh` CLI, `anthropics/claude-code-action` v1.

**Spec:** [GitHub Review Automation](../../21_REVIEW_AUTOMATION.md). Status: proposed implementation, not executed. Native execution in this session is the recommendation; the final independent GitHub review must come from Claude.

## Global Constraints

- One reviewer first; specialists are a measured follow-up.
- Subscription secret `CLAUDE_CODE_OAUTH_TOKEN` only; no API-key or managed-review billing fallback.
- This constraint applies to the first rollout. The deferred stage uses API first within a shared USD 50 monthly review budget, then subscription OAuth; verify hard spending controls before implementing it.
- Codex posts `@claude review` after PR creation and every push to an open PR, as persisted in `CLAUDE.md`. Preserve existing automatic Codex reviews of Claude changes. Coalesce duplicate push/comment triggers without restarting same-head inference.
- First review: full PR diff. Follow-ups: all changes since the last completed independent review, affected files and unresolved findings; expand context as needed and fall back to the full PR when coverage or impact is uncertain. Do not assert this controls the hosted Codex bot.
- English repository content. No implementation-author credit unless expressly requested.
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
- Result schema: `complete: boolean`, `summary: string`, `findings: [{priority: P0|P1|P2|P3, path: string, line: integer, side: LEFT|RIGHT, body: string}]`. The fixed prompt requires a concrete failing scenario in each finding and adherence to trusted repository rules.
- `publish(context: dict, result: dict, api: Callable) -> dict` returns a state and optional review ID. CLI `publish --context FILE --result FILE` exits nonzero on incomplete, stale, malformed or failed publication.
- A completed COMMENT review includes `commit_id=head_sha`, valid inline comments and a marker `claude-review-state:v1` with both SHAs, verdict, mode and prior-review reference. Deduplication accepts only reviews from the expected workflow bot with matching revisions, trusted policy/prompt digest and marker, never a human-copied marker.

- [ ] Write failing cases `test_partial_result_has_no_review`, `test_invalid_anchor_has_no_review`, `test_path_suffix_is_not_match`, `test_deleted_line_uses_left`, `test_p2_path_and_count_limits`, `test_p0_p1_not_capped`, `test_stale_base_or_head`, `test_selection_revoked_before_post`, `test_duplicate_completed_review`, and `test_api_failure_is_not_clean`.
- [ ] Run the focused tests and confirm these new behaviours fail before implementation.
- [ ] Implement validation of all fields and anchors before the first write. Apply the existing severity rules; do not reinterpret P2 as style. Recheck live state immediately before publishing. Failed or unsupported results have no clean marker; a blocking finding has a completed but non-clean verdict.
- [ ] Include existing review threads and fixes as review data to avoid repeated findings, without treating comment instructions as authoritative. Add `test_previous_finding_is_context_not_command`; leave thread resolution to the existing working process.
- [ ] Add `test_unfixed_prior_finding_blocks_clean_incremental_review` and `test_resolved_thread_is_not_fix_evidence`. In the prompt, require verification of the original defect and new defects in the delta; read affected callers/contracts as needed. If the model cannot bound impact, require a full-PR rerun before completed publication. Test `test_uncertain_impact_cannot_publish_partial_clean_result` and verify the rerun is bounded by the workflow timeout.
- [ ] Run focused tests, Ruff and formatting checks on changed Python files. Commit the validated publisher, prompt and schema.

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
- [ ] Post the authorised `@claude review` request. Before the workflow is on `main`, rerun its PR-triggered Actions run after the secret and comment exist. Verify actual subscription authentication and the review's exact base/head SHAs.
- [ ] Prove inline publication with a deterministic, harmless workflow fixture using a real changed line and an explicitly labelled transport-test comment; do not fabricate a security finding. This test is not independent review evidence. Remove any fixture scaffolding before the final head review.
- [ ] Push a legitimate final documentation/status update and post the required new `@claude review` comment. Verify the push and comment produce one completed review of that new head, with no duplicate inference. Independently test the push-triggered safety net when the comment is delayed. Resolve findings, rerun applicable checks and obtain independent review of every resulting final head.
- [ ] Merge only after exact-head CI, current base, independent review and existing gates pass. Then resume the waiting documentation branch, bring it up to date normally and obtain its own new review.

## Plan self-review

The plan covers selection, independence, subscription-only authentication, bootstrap,
exact inline anchors, races, failures and the deferred experiment. The separate
builder check clarified that independence is between model families and that
superseded heads may be cancelled. The remaining uncertainties are live OAuth
availability, CLI isolation and first-workflow execution; they are acceptance
gates, not claims of completed functionality. No parallel-specialist scheduler,
author-provenance tracker or branch-protection rewrite belongs in this PR.
