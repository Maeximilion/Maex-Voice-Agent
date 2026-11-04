# 21 – GitHub Review Automation

Status: design and implementation plan, 04.11.2026. Not an installed or live-tested
review service. The first version has one Claude reviewer; specialised reviewers
are a measured follow-up, not part of the initial rollout.

## Goal and scope

Request Claude review with an authorised PR comment, then review each new pushed
head of that PR. Findings appear inline in GitHub. Use the existing Claude
subscription for the first version, with no API credential configured. Keep the
existing Codex Review Bot for work requiring its independent review. A reviewer does not certify its own
implementation. Review mentions and reviewer names are allowed; implementation
author credits are omitted unless explicitly requested.

Independence is between the implementing and reviewing model families, not just
between sessions. Another Claude session cannot certify Claude-written changes.
The requesting person or working agent selects the other reviewer deliberately;
GitHub cannot infer hidden implementation provenance from a human commit author.
For mixed changes, both reviewers must cover the other model's contributions.
If Claude starts fixing a selected PR, stop its selection and arrange Codex review
of those fixes; Claude's existing review alone cannot clear the new head. Do not
add author labels or implementation credits to implement routing. These user
instructions override the older self-review fallback still present in `CLAUDE.md`.

The usual workflow remains Claude implementation followed by the existing
automatic Codex Review Bot, including its inline comments and review after pushes.
This does not require a second Codex GitHub Actions integration. When Codex
implements, it must post a new top-level `@claude review` comment after opening
the PR and after every subsequent push to that open PR. This obligation lives
in `CLAUDE.md`, not only in chat memory. A push before a PR exists cannot have a
PR comment; opening that PR fulfils the pending request. Comment posting must
succeed, or the working agent reports the failure and retries before merge.

This is one independent PR based on `main`. The prior documentation branch
`docs/cross-agent-review` stays separate until inline Claude review works. Do not
cherry-pick it, stack this PR above it, or merge it as part of installation.

## Evidence and alternatives

The removed `ai-pr-review` workflow was inspected at commit `162d25e9`; deletion
commit `010800be` explains its removal. It used a self-hosted WSL runner and an
OpenAI-compatible Ollama/Z.ai endpoint, not Claude. Its script had already been
deleted. Re-enabling the historical Actions entry cannot restore a working bot.

Keep its useful ideas: per-PR concurrency, cancellation of older work, a timeout,
restricted GitHub permissions, and inline findings. Do not reuse its self-hosted
runner, API credentials, silent success on missing credentials, truncated-diff
approval, approximate line anchoring or fallback that loses inline findings.

The official `anthropics/claude-code-action` v1 source was inspected at
`f9fffe565bf81a7fcadb308a9d688a74e3b029f7`. It supports subscription OAuth,
explicit GitHub tokens, agent mode with a custom prompt, and schema-constrained
structured output. A provided `github_token` avoids the app-token exchange that
otherwise refuses a new workflow absent from the default branch.

Approaches considered:

- **Selected:** the official action for inference, with a small Python controller
  for selection, immutable revision checks and publishing structured findings.
  This makes malformed results, stale heads and invalid inline locations testable
  without paid model calls. Comments use `github-actions[bot]` and name Claude as
  the reviewer; no extra GitHub App or personal GitHub token is required.
- The official generic review plugin can post inline, but its documented skip
  rules include already-commented and trivial PRs. Copying it does not establish
  the required review-after-every-push behaviour.
- The managed Claude Code Review service needs separate activation and billing.
  It is outside scope. Parallel specialists are possible but have no proven
  quota saving for this repository, so they are deferred.

## First-version behaviour

1. An owner/collaborator with current write, maintain or admin permission posts
   `@claude review` as the complete first line of a new top-level PR comment.
   This project's command selects Claude now and for subsequent pushes.
   Codex additionally posts the command after PR creation and each push, as
   required above. Repeated commands and the matching push event must coalesce:
   they do not request two model runs for the same base/head pair. A same-head
   comment must not cancel and restart an already running review. Recheck for
   a completed matching review immediately before starting inference, not just
   when the event first arrives.
   `@claude review stop` removes that selection. Only these exact commands are
   interpreted; arbitrary comment text never becomes a model instruction.
2. The latest authorised command decides. Inspect all comment pages and check
   the command author's current permission. Ignore bots and outside users.
   Reject forks and outside PR authors before exposing a subscription secret.
   Closed PRs and drafts do not run; becoming ready starts the selected review.
3. Run on PR open/reopen/ready/new commits and new command comments. Use standard
   GitHub-hosted Linux runners, a 20-minute timeout and one running review per
   PR. An unauthorised comment must not cancel an authorised running review.
   Each new head requests review, but superseded heads may be cancelled: only
   the latest head must finish before merge. This avoids spending quota on a
   revision that can no longer be merged. A stop command cancels pending work;
   the publisher also checks the current selection before writing.
4. Snapshot the base and head SHAs and the full PR diff, plus current trusted
   `AGENTS.md` and `CLAUDE.md`. Give Claude read-only access to relevant code and
   a fixed review prompt. No repository hooks, code execution, fixes or pushes.
   One reviewer examines correctness and security together. Apply the existing
   model/effort routing; the initial default for workflow code is Opus at high.
5. Claude returns a structured completion flag, summary and findings with
   severity, path, exact diff line, side and concrete failing scenario. The
   controller validates the entire result before publishing a GitHub COMMENT
   review anchored to the reviewed head, with inline findings. Do not silently
   truncate an oversized diff, invent anchors, or turn partial output into clean.
6. Apply `AGENTS.md`: P0/P1 findings are never capped; at most three P2 findings
   are posted, only in `api/`, `printbridge/` and `deploy/`. P3 and style findings
   are not posted. A blocking result or an incomplete/failed run is not clean.
7. Recheck both base and head, PR state and selection immediately before posting.
   Record the reviewed revisions and a machine-readable completion marker.
   Re-running an already published review for that revision is idempotent.
   A push during publication leaves at most a review of an older explicitly
   recorded SHA; it must never satisfy the latest-head gate.
8. Missing/expired OAuth, exhausted quota, invalid output, unresolved blocking
   findings or publication failure leave the PR unmergeable under the working
   rules. An unselected PR is explicitly skipped, never called reviewed.
   Existing CI, evals, sync and status-bump requirements remain in force.

The first version does not change the repository ruleset or enable automatic
merging. The existing merge hook does not understand Claude bot reviews. A
working agent verifies the completed independent result and existing checks,
then may record its evidence using the existing `Merge gate:` convention. This
is a relay of a real external review, never a substitute self-review.
These are working rules, not new server-enforced branch protections. A successful
skipped job is not review evidence. The existing automatic Codex integration may
still run; its output cannot replace Claude's independent gate on Codex changes.

The controller and fixed instructions come from the trusted base revision after
installation. The installation PR is the explicit bootstrap exception: its new
controller must be inspected and unit-tested before a secret-bearing run. PR code
is review data, never executable setup. Nested instruction files, MCP servers,
plugins and repository hooks must not activate from the candidate checkout.
The exact isolation settings are an implementation verification item; inability
to prove isolation blocks the live credential test.

An unchanged head with a changed base invalidates previous review evidence.
The first version does not schedule every open PR whenever `main` moves: the
existing requirement to merge current `main` before merging produces a new head
and triggers review. The final gate checks both captured revisions.

## Implementation sequence

The [implementation plan](superpowers/plans/2026-11-04-claude-inline-review.md)
keeps the work in four testable stages in the same branch and eventual PR:

1. Implement and test command selection, permissions and revision snapshots.
2. Implement and test complete structured results and exact inline publication.
3. Wire the pinned action, OAuth-only authentication and isolated read-only input.
4. Validate the workflow, document setup, then prove inline publication and
   automatic re-review on a second head using the real subscription.

The design has had a separate builder check and a second consistency pass.
No workflow or controller is implemented yet. Design review does not count as
the independent Claude review required for the eventual implementation PR.

## Severity is not review specialisation

| Priority | Meaning | Repository treatment |
|---|---|---|
| P0 | Critical/release-blocking defect | Always fix; confidentiality leaks are P0 |
| P1 | Urgent/high-impact defect | Always fix; critical domain rules are P1 |
| P2 | Ordinary concrete defect | Existing path limits, cap and fix-round rules |
| P3 | Low-impact improvement/defect | Not posted by this repository's reviewers |

P2 does not mean cosmetics. Security versus correctness describes the kind of
review; P0–P3 describes the priority of a finding. Follow the concrete definitions
in `AGENTS.md`, including escalation of critical domain defects. Ruff and CI
already cover formatting and style; do not add a cosmetics model reviewer.

## Authentication and first-run bootstrap

The subscription token is a real prerequisite. The documented command is
`claude setup-token`: it opens browser authorisation and prints a long-lived
token to that terminal. No model review is needed to create it. A trusted terminal
can be on another machine; being away from the home PC does not change the
credential requirement. With only a phone browser and no existing token or
terminal access, a browser-only token-creation route has not been verified.
Never put the token in chat, a public terminal recording, code or an issue.

Once implementation is ready, the setup guide must give these exact locations:

1. Claude account: Settings > Usage. Keep paid extra usage/credit reload disabled
   if no spending beyond the subscription is allowed. OAuth uses subscription
   limits but is not itself an account spending cap.
2. Generate the token with `claude setup-token` in a trusted terminal and approve
   the browser login using the Claude account with the subscription.
3. GitHub repository > Settings > Secrets and variables > Actions > New repository
   secret. Name: `CLAUDE_CODE_OAUTH_TOKEN`. Paste only the token as its value.
4. On the installation PR, post `@claude review`. Before the new workflow is on
   `main`, `issue_comment` cannot start it. Re-run the PR-triggered review workflow
   from its Actions run after adding the comment and secret; it must read live
   comments. Use the provided workflow token so no Claude-app validation requires
   an early merge. A permission or first-workflow limitation keeps the PR open;
   do not bypass protection or merge unreviewed to solve bootstrap.
5. Verify an inline result and its exact SHA, then verify a second pushed head.
   Only after the installation PR is green and independently reviewed may it
   merge. Resume the waiting documentation branch afterwards, with a fresh review.

The initial PR can be created before the secret exists, but it is not an enabled
or verified review service. The live setup test is distinct from mocked tests.

## Deferred experiment: specialised reviewers

### Future authentication order and budget

The later API stage is explicitly wanted, but no API connection or spending is
authorised for the first rollout. The user reports a monthly USD 100 allowance;
its eligibility, expiry, covered API products and billing treatment still need
verification when that account is connected. Do not assume it is cash credit
or silently fall through to paid usage after it expires.

The target is **API first, with at most USD 50 per month for the review workflow
across all its reviewers, then subscription OAuth**. This replaces an earlier
proposal of subscription first with an API fallback. Plan the API connection
together with the specialist experiment in a later independent PR. Use the
same review rules and independent model family through either authentication
path. Reaching the API budget changes authentication, not reviewer independence.

A hard cap requires verified provider spending controls and accounting for
in-flight work, retries, parallel specialists, input/output and cache charges.
Do not start a call merely because the reported spend is USD 49.99. Reserve a
conservative maximum request cost before dispatch, reconcile actual usage and
route to OAuth before a request could exceed the remaining budget. Prefer a
dedicated API workspace/key and provider-enforced cap where available; billing
data can lag. Specify the monthly reset timezone and credit treatment during
that implementation. Unknown or unavailable accounting must choose OAuth,
not permit unbounded API use. If an exact ceiling cannot be enforced, disclose
that and keep API execution disabled until an acceptable limit is agreed.

An interrupted review is incomplete. A fallback retry must be bounded and
publish at most one completed result per revision. Exhaustion of both allowed
routes leaves review pending; it must not approve the PR or exceed the budget.

### Specialist experiment

Preserve this idea here, linked from README, for a later independent task.
Claude supports custom subagents and parallel sessions. Under subscription OAuth,
each consumes the same account's usage allowance; API reviewers share the monthly
review budget above. Extra contexts can increase tokens even when wall
time falls. Job names such as `Claude Security` and `Claude Correctness` do not
create different account identities. Separate bot identities are unnecessary.

Compare one combined reviewer with two specialised reviewers on the same fixed
set of representative PR revisions and seeded defects. Measure total input,
cached-input and output tokens where available, elapsed time, quota observations,
missed known defects, false positives and duplicate findings. Model dollar
estimates are not proof of subscription-quota savings. Accept a specialised
design only after the comparison shows a meaningful benefit without lost coverage.

A previously clean security review does not justify skipping security forever:
a bug fix can introduce a permission flaw. A future incremental design needs a
small whole-change risk pass after every push, invalidation on affected files and
dependencies, rechecks of fixes, and a whole-PR fallback when impact is uncertain.
Never treat a result from an old SHA as review of the current one. Start by
measuring a focused single-reviewer recheck before adding a matrix or scheduler.

The existing Codex GitHub review integration remains unchanged. Its service and
`openai/codex-action` have different authentication and billing arrangements;
do not assume the action automatically uses the ChatGPT subscription. Verify
that separately before a later Codex-specialist experiment.

## Validation and sources

Before calling this working, test selection/stop/permissions/pagination, forks,
missing OAuth, quota failure, malformed or partial model output, changed base or
head, invalid inline anchors, posting failure and duplicate runs. Then perform
real subscription and GitHub inline-comment tests. No live review or token-saving
measurement has been made during planning.

- [Claude GitHub Actions](https://code.claude.com/docs/en/github-actions)
- [Pinned action source](https://github.com/anthropics/claude-code-action/tree/f9fffe565bf81a7fcadb308a9d688a74e3b029f7)
- [Subscription token creation](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)
- [Claude subagents](https://code.claude.com/docs/en/sub-agents)
- [Claude usage and team costs](https://code.claude.com/docs/en/costs)
- [GitHub workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [Codex review rubric](https://github.com/openai/codex/blob/main/codex-rs/prompts/templates/review/rubric.md)
- [Codex Action](https://github.com/openai/codex-action)
