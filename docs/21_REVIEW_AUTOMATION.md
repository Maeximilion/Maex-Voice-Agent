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

Maxi prefers findings, review summaries and replies made as reviewer to appear
under that reviewer's GitHub bot (04.11.2026). PR creation, implementation
updates, replies as the implementer and review-request mentions may use Maxi's
account. Hosted Codex already publishes as
`chatgpt-codex-connector[bot]`; Claude should use `claude[bot]` when an authorised
GitHub App publishing route is available. GitHub attributes requests according
to their authentication. Subscription OAuth authenticates Claude inference.
A GitHub App installation token supplies the app's publishing identity.
The manual Claude reviews on PR #542 used Maxi's GitHub account. Until a bot
publishing route is verified, label every manual review body, inline finding
and reply made as reviewer `Claude review` or `Codex review` as appropriate. Check the
published `user.login` and `user.type` to establish the actual author.
See [GitHub App authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/about-authentication-with-a-github-app).

Independence is between the implementing and reviewing model families, not just
between sessions. Another Claude session cannot certify Claude-written changes.
The requesting person or working agent selects the other reviewer deliberately;
GitHub cannot infer hidden implementation provenance from a human commit author.
For mixed changes, both reviewers must cover the other model's contributions.
If Claude starts fixing a selected PR, stop its selection and arrange Codex review
of those fixes; Claude's existing review alone cannot clear the new head. Do not
add author labels or implementation credits to implement routing. These user
instructions replace the former self-review fallback in `CLAUDE.md`.

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

### Current rollout: manual independent review

The current PR delivers shared instructions and the plan, not the action or
controller. A user-started Claude Code session can review it now using the
existing subscription and that session's authorised GitHub account. No Actions
OAuth secret is required for this manual route. An `@claude review` comment
records the request but cannot start the not-yet-installed workflow.

The reviewer reads the current PR diff and the applicable `AGENTS.md`/`CLAUDE.md`
rules, captures both full base/head SHAs, and publishes a GitHub COMMENT review
with the head as `commit_id` and genuine findings attached to valid diff lines.
The brief review body identifies Claude as reviewer, the captured SHAs, scope,
checks actually performed and any limitations. If there are no eligible findings,
publish a completed summary without inventing an inline comment. Recheck the
refs before publication; if they changed, review the new revision first.

This is a read-only review of the branch: do not implement the plan, edit/push
files, invoke `/done` or `/ship`, or merge. GitHub publishing is authorised as
part of the requested review. A missing GitHub permission is reported; writing
findings only in chat does not count as posting them. Manual review can assess
these rules but does not demonstrate that the future action works. The current
PR remains open for the user's review; the waiting documentation branch stays
untouched. Do not add the machine marker reserved for the future action.

### Automated options

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
  without paid model calls. Prefer a verified GitHub App publishing route for
  `claude[bot]`. A workflow-token bootstrap publishes as `github-actions[bot]`
  and labels each comment `Claude review`. The dedicated Claude identity and
  its token handoff to the separate publisher remain to be validated.
- The official generic review plugin can post inline, but its documented skip
  rules include already-commented and trivial PRs. Copying it does not establish
  the required review-after-every-push behaviour.
- The managed Claude Code Review service needs separate activation and billing.
  It is outside scope. Parallel specialists are possible but have no proven
  quota saving for this repository, so they are deferred. It is also the only
  reviewer that reads a `REVIEW.md`; the local `/code-review` command reads
  `CLAUDE.md` only, and Codex reads `AGENTS.md` (Claude Code docs "Customize
  Code Review", read 09.11.2026). Review-only rules therefore stay in
  `AGENTS.md` "Code Review Rules", and no `REVIEW.md` is kept.

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
   `AGENTS.md` and `CLAUDE.md`. The first review uses the full PR diff; follow-ups
   send the complete delta since the last completed review as described below.
   Keep the current full PR diff for inline-anchor validation in the controller.
   Give Claude read-only access to relevant code and
   a fixed review prompt. No repository hooks, code execution, fixes or pushes.
   One reviewer examines correctness and security together. Apply the existing
   model/effort routing; the initial default for workflow code is Opus at high.
5. Claude returns a structured completion flag, summary and findings with
   severity, title, path, exact diff line, side and concrete failing scenario. The
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

### Focused follow-up reviews

`@claude review` requests the appropriate scope automatically; the user does not
need to name a file. The first completed review covers all PR changes, not the
entire repository. On subsequent pushes, compare the last completed independent
review's head with the current head. Include every intervening change, even if
several pushes happened while a run was cancelled or failed. The last pushed
commit alone is not a reliable baseline.

If five files were reviewed and a fix changes only one, the next model input
focuses on that file's delta, the original finding and enough surrounding code
to check the correction. The reviewer checks both whether the defect is fixed
and whether the fix introduces another defect. Unchanged files are read only
where callers, contracts, shared configuration or other dependencies make them
relevant. The scope is based on what changed, not only on where comments existed:
an unrelated new change in a second file must also be reviewed.

Use only authentic completed review records for the same PR and reviewer family,
with a matching base and trusted policy/prompt version, and an ancestor head.
Record `mode` (`full` or `incremental`), `previous_review_id`, the delta's starting
SHA and current base/head in each result. A completed review with findings may
be the baseline, but unresolved findings stay open and block a clean verdict
where required by existing rules. Clicking Resolve on a thread is not proof
that its defect was fixed. Review coverage must trace back to a complete initial
review; an old review alone never certifies the current head.

Fall back to the full current PR diff when the baseline or coverage chain is
missing, incomplete or untrusted, the base/policy/reviewer changes, ancestry
cannot be established, or the reviewer cannot bound the impact. Broad interface,
security or configuration changes can require related files or the full PR.
A new current-head result is published after each completed follow-up; it
explicitly states its scope and inherited coverage rather than pretending every
file was read again. No scope-reduction claim is made for a failed/partial run.

This is the shared review policy for both model families. The custom Claude
action implements it in the input builder and prompt. For the existing hosted
Codex bot it is supplied through `AGENTS.md`, `CLAUDE.md` and a scoped manual
request when one is actually needed. A mention by itself cannot enforce which
files the service loads, and hosted Codex quota savings have not been verified.

### Codex support, settings and manual request

Official documentation checked on 04.11.2026 confirms `@codex review`, repository
review instructions in `AGENTS.md`, and automatic-review settings at
[Codex code review settings](https://chatgpt.com/settings/code-review).
OpenAI also documents additional guidance after the review mention. No reviewed
official source establishes a hard per-file execution limit or a guaranteed
"changes since last review" switch for the hosted GitHub bot. Do not confuse
local CLI commit review or the desktop review plugin with this integration.

Repository rules and `.claude/commands/ship.md` can be updated here. The current
account's Codex settings were not read or changed: GitHub repository access is
not access to those authenticated ChatGPT settings. To inspect them, open the
link above, select `Maeximilion/Maex-Voice-Agent` and inspect Automatic review
and Review trigger under the applicable repository/personal settings. Keep the
existing automatic behaviour; no new Codex GitHub Action is needed.

The recommended first step is to keep the native bot and make its scope request
explicit. Replacing it with a custom Codex action could control the submitted
diff more directly, but would add another execution/authentication path and
require separate billing and inline-publication validation. That larger change
is deferred; a prompt-based request must not be presented as equivalent to it.

When Claude actually needs to request a manual follow-up, start the comment with
this text, then append the concrete context listed below:

```text
@codex review
Perform a focused follow-up review using the completed review identified below.
Check every change since that reviewed head, verify the reported fixes, and look
for regressions caused by those changes. Read affected callers and contracts as
needed. Follow AGENTS.md and CLAUDE.md. Do not repeat resolved root causes or
reopen unrelated cosmetic work. Report concrete defects inline at valid lines;
Apply CLAUDE.md's performance/language signals to affected code; distinguish
measured runtime limits from model, SQL, network or audio waits. Speculative
rewrites are not findings. Use the shared English comment format: trigger, impact and required behaviour
with a verification case in one short paragraph. For a repeated finding, link
the earlier thread and explain the still-failing case. Keep any completion
summary brief. If prior coverage is invalid or the impact
cannot be bounded, explain why a broader PR review is needed.
```

The requesting agent appends the actual previous review URL and full reviewed
head SHA, current full head SHA, all changed paths since that baseline and the
relevant finding IDs. It verifies those values from GitHub/git; a failed run or
the previous push is not automatically the baseline. Without a valid completed
baseline, request a full PR review. This is a review request, not a request for
Codex to implement the fixes. Findings should be inline; a short status/coverage
summary is still necessary and is not forbidden.

Do not duplicate the automatic review on every push just to add this text. The
existing `/ship` policy permits one manual request only after a stalled review.
The automatic trigger remains unchanged when no mention is posted; repository
guidance still applies. A future switch to manual-only triggers would be a
separate decision, not an unnoticed side effect of scope instructions.

### Avoiding repeated low-value fix rounds

Existing rules already exclude style/speculation and repeated root causes,
limit eligible P2 findings to three per review, batch a round's fixes into one
push, and require a decision after three consecutive P2-only rounds. However,
the last rule currently permits continued fixes for real in-scope defects, so
it is not a fixed limit on automatic work. Retain the distinction: P2 can be
a real bug; a small patch is not evidence of low impact.

An inspection of historical bot inline comments on 04.11.2026 found:

| PR | Top-level inline findings | Revisions carrying findings | Priority labels |
|---|---:|---:|---|
| [341](https://github.com/Maeximilion/Maex-Voice-Agent/pull/341) | 9 | 4 | 9 P2 |
| [357](https://github.com/Maeximilion/Maex-Voice-Agent/pull/357) | 28 | 8 | 5 P1, 23 P2 |
| [371](https://github.com/Maeximilion/Maex-Voice-Agent/pull/371) | 3 | 2 | 3 P2 |

These counts exclude thread replies, use original reviewed commit IDs, and are
not counts of all review runs or proof that any finding was unnecessary.
The current OpenAI guide describes P0/P1-focused output, while this repository's
history includes P2. Do not assume current service defaults erase that history
or automatically implement the repository's desired loop policy.

The user chose to keep the existing flexible three-round rule on 04.11.2026,
rather than introduce a fixed decision pause. After three consecutive P2-only
rounds, the working agent makes and records the existing evidence-based decision:
continue for real in-scope defects, or use the existing triage/merge policy for
cosmetic, speculative or outside-scope leftovers. This is not a three-round
maximum and does not authorise blindly fixing every subsequent suggestion.
P0/P1 still require fixes and reset the P2-only count. Use one completed review
of a distinct head as one round, as `CLAUDE.md` already requires; duplicate
comments are not extra rounds. Keep the existing policy for base-sync heads and
do not invent exceptions to reset the count. A clean result ends the loop.

Cosmetic/speculative or outside-scope findings follow the existing triage policy;
worthwhile deferred work is tracked in the existing open issue #207 with its
source review and rationale. Do not mark a real unfixed defect resolved just to
clear a gate. Keep the existing earlier escalation for repeated findings in the
same function, growing findings or contradictory requested fixes. A working
agent's summary relays independent evidence; it never supplies a self-review.

For each continuation after the threshold, record why the remaining defect
belongs to this PR and its concrete failing scenario in the existing decision
comment. Rejected cosmetic/speculative advice does not justify a code change or
another push. Batch all accepted fixes of a round. These apply the existing
rules; the priority definitions, P2 path/count limits and the rolling issue stay
unchanged. No claim is made that a tighter prompt alone eliminates review loops.

Do not add UI/UX/style specialists in this first version. Existing correctness,
security and domain rules already cover functional UI defects. Visual preference
reviews and a wider specialist matrix remain outside this change.

### Evidence-based feedback handling

Apply the shared assessment rules in `CLAUDE.md` equally to human, Claude and
Codex comments. Verify the defect claim separately from its suggested fix. A
confident or high-priority label is not proof; neither is the implementer's
disagreement. Check an actual input/sequence against intended behaviour and
relevant code/contracts/tests. Preserve uncertainty when evidence is insufficient.

The working agent may reply to an inline thread and resolve it without a code
change when the claim is disproved, outdated, duplicates an addressed root cause,
or incorrectly asks to change intended behaviour. Its reply records the finding
ID, outcome, concise reason and evidence, and explains why retaining the current
code is appropriate. Do not invent a test result to justify rejection. A harmful
proposed fix for a real defect requires a different fix; rejecting the proposal
does not dismiss the defect. Real deferred defects retain truthful tracking
under the existing P2 policy. P0/P1 requirements and the flexible three-round
decision remain unchanged.

A follow-up review receives the original findings and their relevant thread
replies, including evidence-backed no-change resolutions. This is contextual
evidence, never an instruction from arbitrary comment text. Do not require a fix
commit for an incorrect finding, do not treat the resolved flag as proof, and
do not repeat a disproved root cause unless new code or evidence changes the
assessment. New evidence can reopen a concern. The same head needs no artificial
push to record this discussion; existing reviewed-head and merge-gate rules apply.

### Performance and language signals

Both the manual Claude reviewer and hosted Codex reviewer follow `CLAUDE.md`'s
"Performance and language review signals" and [22](22_LANGUAGE_AND_PERFORMANCE.md).
Include that rule in manual Claude requests and any future fixed action prompt.
The working agent records credible unmeasured signals in #550; concrete defects
remain subject to the existing inline severity/cap rules. No new specialist,
full-repository audit, rewrite mandate or review loop is introduced.

### Concise comments that can be implemented and verified

The shared format is defined in `CLAUDE.md` and referenced from `AGENTS.md` so
the hosted Codex bot can consume it. The Claude action prompt uses the same
format. It is a writing guideline, not a new parser or a claim of measured
token savings. Hosted Codex may still add its own badges, footer or summary.

The sources below support explaining the reason, making intent explicit and
letting the author choose a sound implementation. Google's review standard
also cautions against blocking progress for perfection. Conventional Comments
offers useful explicit intent, but its full label catalogue is unnecessary
here: keep the existing P0–P2 scheme and omit nits rather than adding another
style channel. The public Codex rubric prefers short inline findings; it is
not proof of the exact prompt deployed by the hosted service.

For this repository, use a short priority title and one paragraph answering:

1. What input, state or sequence triggers the defect?
2. What goes wrong, why, and what is the concrete consequence?
3. What behaviour would remove the defect, and what check demonstrates it?

Usually 2–4 sentences and 40–100 words suffice. This is a local editorial target,
not a universal senior-reviewer rule or an empirically proven optimum for models.
A short unambiguous finding can be shorter; a concurrency or security sequence
may need more detail. Never mechanically shorten the failing scenario, drop a
critical assumption or invent evidence to fit the target. A terse command such
as "make this atomic" is not enough when the failure mechanism is unstated.

Illustrative finding only; this is not a newly verified repository defect:

> **[P1] Preserve existing settings when saving fails**
>
> If saving fails after opening the settings file for writing, the previous
> settings are already lost. Write and validate a temporary file before replacing
> the original. Verify that a simulated write failure leaves the original file
> unchanged, and that a successful save replaces it completely.

The fix author first derives the expected behaviour from the comment, reads the
thread and relevant contract, then verifies the reported failure. Add a regression
test that exercises that failure mechanism, including meaningful nearby cases;
do not merely implement the reviewer's preferred syntax. Ask a targeted question
when the expected outcome is materially ambiguous. Do not seek confirmation of
every routine implementation detail.

Replies should normally be 1–3 sentences: disposition, reason or corrected
behaviour, and real evidence. A fixed reply names the actual commit/test result;
a no-change reply explains the contrary contract/code evidence. A bare "done"
does not tell the next reviewer whether the intended outcome was verified.
Do not fabricate those references; the examples and test suggestions here are
not executed tests.

When a concern returns, compare it with the earlier thread and fix. State the
remaining input/sequence, the relevant current line and why the prior fix did
not satisfy the agreed behaviour, or explain what new evidence changes a prior
rejection. Do not introduce new requirements under the old finding. Preserve the
existing escalation when a proposed fix contradicts an earlier one. Repeated
comments in one file alone do not establish misunderstanding: PR #357's sampled
comments included distinct backup, file-write and path-compatibility defects.

Measure the change on subsequent real reviews: repeated-root-cause findings,
clarification requests and fix rounds per PR, alongside missed defects and
invalid findings. Shorter comments alone are not the success criterion. Use the
existing review-load audit rather than building another metrics service.

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

### Automatic task handoff to shipping

Inspection of the versioned command files, project hook configuration and the
user-level hook backup found no explicit `/done` to `/ship` trigger. `/done`
previously pointed to merge rules and contained pre-merge/cleanup steps, while
`/ship` supplied the actual wait/review loop. The real personal Ubuntu settings
were not available for inspection.

The commands now define the missing handoff: once `/done` has published its PR
and finished applicable reminders/stack linking, the active Claude session
invokes `ship` through the Skill tool with the actual PR number. It starts before
checks finish because `/ship` already owns that wait. No GitHub Action, shell
command pretending to type `/ship`, new hook or background Claude process is
added. The existing Markdown command remains the single procedure.

Avoid recursion: `/ship` calls only the named `/done` helper steps, and the outer
`/done` does not repeat them after handing off. An already active operation is
resumed rather than duplicated. Failed PR creation, explicit wait/draft requests,
missing credentials/permissions, required approvals and the chosen independent
reviewer remain blockers. Codex work cannot use the native Codex review waiter
as independent evidence; until Claude review is available it remains open.

This is instruction-level automation within an active session. Closed sessions,
quota exhaustion and unavailable command registration require a later resumption;
GitHub finishing its checks alone does not wake Claude. Current checks were a
static walkthrough, not a live Claude command invocation. First live acceptance
must verify command discovery, one handoff, no duplicate push/review, CI waiting,
pause handling, and cleanup after a gated merge. No PR was merged to test this.

### Review action implementation

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
5. Verify an inline result, its exact SHA and the actual GitHub author of the
   review and each comment, then verify a second pushed head. Verify the chosen
   app's token issuance for the separate publisher while keeping write
   credentials out of the model step. Record any bootstrap fallback identity.
   Only after the installation PR is green and independently reviewed may it
   merge. Resume the waiting documentation branch afterwards, with a fresh review.

The initial PR can be created before the secret exists, but it is not an enabled
or verified review service. The live setup test is distinct from mocked tests.

## Deferred idea: draft-first reviews and staged CI

Recorded on 04.11.2026 at the owner's request; **not approved for implementation**.
Keep the current PR creation, review triggers and CI behaviour unchanged.

The idea is to open drafts, run quick lint and relevant tests alongside independent
review, then run the full required checks when marking the PR ready. Potentially
defer expensive Docker/build jobs, not all deterministic checks: discovering a
test failure only after a clean review could start another review/fix cycle.

A draft is already a pushed PR. It blocks merging but does not inherently skip CI
or prevent inline reviews. The current `ci.yml`, dependency review and title
workflows have no draft exclusion. The main CI uses default `pull_request`
activities (`opened`, `synchronize`, `reopened`); marking ready alone does not
start it. Any future staged design must explicitly handle `ready_for_review`
and ensure skipped draft jobs cannot satisfy the final merge gate without the
required checks actually running on the current head.

Manual Claude Code review can publish inline on a draft. Automatic hosted Codex
review on every draft push has not been verified for this account, and the
planned Claude action currently excludes drafts. Both trigger paths would need
verification or adjustment before adopting this idea. Measure actual CI time and
review usage before deciding whether the extra workflow complexity is worthwhile.
PR #542's temporary draft/test hold is a specific owner instruction, not adoption
of this proposal.

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
a bug fix can introduce a permission flaw. The first version's focused follow-up
review therefore still checks the new changes for both correctness and security,
and expands its scope when needed. Measure that single-reviewer approach before
adding a specialist matrix or scheduler. A specialist's old clean result is not
automatically valid for a newer head.

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
- [Codex GitHub review configuration and repository guidance](https://learn.chatgpt.com/docs/third-party/github)
- [OpenAI: additional guidance in review mentions](https://openai.com/index/introducing-upgrades-to-codex/)
- [Google: writing review comments](https://google.github.io/eng-practices/review/reviewer/comments.html)
- [Google: the standard of code review](https://google.github.io/eng-practices/review/reviewer/standard.html)
- [Google: handling reviewer comments](https://google.github.io/eng-practices/review/developer/handling-comments.html)
- [Conventional Comments](https://conventionalcomments.org/)
- [Public Codex review rubric](https://github.com/openai/codex/blob/main/codex-rs/prompts/templates/review/rubric.md)
- [Claude Code: command compatibility and model-invoked skills](https://code.claude.com/docs/en/skills)
