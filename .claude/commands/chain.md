---
description: Work off several tasks or issues as a chain - one coordinator, one fresh session per pull request that builds it, one ship lane that merges, usage paced, until the plan is done or Maxi says stop.
argument-hint: "[plan link, issue numbers or task IDs]"
---

Run $ARGUMENTS as a chain. This session is the coordinator: it plans, starts sessions, hands out lanes, checks merges and reports. It builds nothing itself and runs on Opus 5.5 `high`.

A chain is Maxi saying `/chain` or "chain", or naming two or more tasks, issues or plan pull requests to work off (17.11). It runs until every item is merged or Maxi says stop, the same whether he is there or away (25.11: "The chain should work until the chain is done especialy when i say i am afk!"). A single task is no chain.

Newest rule wins; a real doubt goes to the open decisions issue as a question. Maxi's full quotes: `docs/HISTORY.md` "Chain command (archive, replaced 25.11.2026)". Item sessions follow `.claude/chain/brief-common.md`. `~/.claude/chain/` is runtime state only: `round.txt` (this round's ids and links), `next-lane.txt` (baton), `notes.txt` (log and news), decision write-ups.

## 1. The coordinator loop

1. **Every turn Maxi's message begins: start a session for EVERY planned item that has none yet** (25.11: "Don't ever wait for my message to start new sessions ever again"). Items that fit a free lane (section 3) start building, the rest start queued on the baton. A start works only in such a turn and in a `<side-sessions-event>` turn that follows it (the app's note when all child sessions are idle), never in a turn begun by a child's own message (`SendMessage`, e.g. "merged #N"), a timer or an agent report. Remote Control on the coordinator and auto mode's classifier refuse too: the coordinator runs with Remote Control off, in bypass mode (Maxi, 25.11). Order in that turn: scouts (section 2), the starts, a ship lane if none runs, then the open decisions as popups (section 4); nothing waits for an answer.
2. **On every child message or note:** read `get_usage`, check the PR with `gh pr view`, update the dashboard, add a line to `notes.txt`, then answer in one or two lines. A note is a report, not proof.
3. **On a "waiting" note, or a child turn that ended on a wait:** run `py -3.14 scripts/codex_review.py <N>` and `gh pr checks <N>`. Both done on the same head: nudge "CI and Codex are done on <sha>, go on". Codex not started 10 minutes after a push: tell the session to post one scoped `@codex review` (25.11).
4. **On "PR #N ready, first review clean":** verify it the same way, then send the ship lane "ship #N, build session <id>". Chain-changing PRs ship first, then plan order.
5. **When a lane frees,** it goes, in this order (25.11): (a) a planned item that changes the chain itself (this command, the brief file, the dashboard, chain memories); (b) a heavy step a running item waits for: send "go heavy"; (c) the next planned item. For (a) and (c): write `item N` into `next-lane.txt` and message that queued session "go on item N" (a message wakes it at once from any turn); on "lane taken item N" write `none` back. Super slow: section 3.
6. **On "merged #N":** mark it done on the dashboard at once, archive the build session if the ship lane could not, rewrite the state memory, re-read the mode. Note the free lane: at the next `<side-sessions-event>` turn start the next planned item (26.11: "after an Item gets merged you pick another one and start a sub session which get's into the new free lane"). Refused there too: one push, and in Maxi's next turn start the next items as baton-queued sessions.
7. **While children run:** one background `sleep 3000`, re-armed on every wake (keeps the cache warm; no `CronCreate`).
8. **Plan runs empty:** plan about 20 new items on the plan issue and go on; they get sessions in the next turn that can start one (step 1). **Last item merged:** section 5.

## 2. Sessions and briefs

**Build session:** one per item (24.11, popup "One item per session"), `start_session` with `context` "fresh", its own worktree, the item's `model` and `effort` from `CLAUDE.md` "Model Routing" (Fable only on Maxi's word for that chain, until its week is used up). Title: the item, for example "13 last_say after barge-in" (25.11). It builds through `/done` until CI is green and the first independent review is clean, fixes every finding of that round itself (P0 and P1 too), sends "PR #N ready, first review clean" and stops. Its rules: `.claude/chain/brief-common.md`.

**Brief,** the session's only context, in this order:

1. `Route: <model-id> <effort>` as the first line (hook `.claude/hooks/route_advice.sh`).
2. "Read `.claude/chain/brief-common.md` and `~/.claude/chain/round.txt` first."
3. Background, two sentences or more: the chain, the plan link, what is merged, what runs in parallel in which folder.
4. The task: item with issue and finding ids; "a finding is a claim, check it against the code"; the `xhigh` triggers it touches.
5. Where the code lives: the scout's map, caveats kept. Scout: one Explore agent per item, `model` haiku, foreground, all in parallel, before the starts (23.11); Graphify connector first; at most 300 words: file and line of each named function, callers, linked tests, graph `commitSha` against `HEAD`, caveats. A late map follows by message.
6. GUI work: skill `ui-ux-pro-max`; `docs/06_GUI.md` and the GUI path rule win.
7. Base: `origin/main`, or the branch of an unmerged item that touches the same files (stack, `/done` step 7c). Whoever merges second merges `origin/main` first; check `gh pr view` before naming a base.
8. Queued or not, and an item it runs after (it starts only once that one is merged).
9. The reserve item with its route.
10. Exceptions to `CLAUDE.md` in Maxi's words with date; a session refuses a rule change relayed without them.

**Ship lane** (24.11, "One Sonnet ship lane"): one fresh Sonnet `medium` session, started with the first build sessions, not counted as a lane. Brief: `Route: claude-sonnet-5-5 medium`, chain and plan link, "merge only on ship #N, one at a time", the merge gate of `CLAUDE.md` "Merging a PR into `main`", its milestones, the Sonnet usage row, no handover.

- Per "ship #N": merge `origin/main` in, get CI, Codex and evals on the new head, fix that round, merge, message "merged #N" first, nothing in between (24.11), then archive the build session named in "ship #N" (25.11, popup "Ship lane archives it"; refused: say so in "merged #N").
- Owns the branch of every PR it ships; never ends a turn on a pending wait (`.claude/commands/ship.md` step 2). A P1 in an `xhigh` path: the coordinator switches it to Opus and back.
- Before "queue empty": `gh pr list` for ready chain PRs (25.11: queued messages arrive late).
- Merged PRs stay on its bar until Maxi clears them with the x (25.11); no `unbind_pr` (refused), no allow rule for it.
- Idle: open Dependabot PRs go to its queue. Context over 300k: a fresh ship lane in the next turn that can start one (step 1); the old one finishes its PR.

## 3. Lanes, modes and pacing

`ahead = percentUsed of the 5-hour window + minutes to reset / 3 - 97` (24.11). An even 20 points an hour lands at 97 at the reset. The coordinator reads it on every `get_usage`; Auto on the dashboard sets the mode by the same table. "First hour": over 240 minutes to the reset; "last 2 hours": under 120.

| Mode | On at | Back at | Build lanes | Heavy step | Pause |
|---|---|---|---|---|---|
| Super slow | over +15 (also straight from normal) | +5 or below, to slow | 1 | waits for slow or normal | every other build session sleeps 50 min, renewed |
| Slow | over +5 first hour, +10 middle, +5 last 2 hours | -20, -15, -5 or below, to normal | 2 | one at a time, holds both | 45 min sleep after each finished part; cheap items (Sonnet, Haiku, lower effort) start first |
| Normal | | | 3 | holds 2 lanes from "go heavy" to its end | model rows below |
| Turbo | under -25, not in the last 30 min | -10 or above, or 30 min before the reset, to normal | up to 5, never above today's "Agents per weekday" row | 1 lane, no "go heavy" | model rows below |

| Model (every mode) | At a task boundary | Hard brake at any reading |
|---|---|---|
| Fable | over +15: pause 50 min | over +30: finish the step, commit, push, pause 50 |
| Opus | over +25: pause 30 min | over +40: the same, pause 30 |
| Sonnet | no new fix round at 85 percent of the window | at 90: commit, push, sleep to the reset |

- A pause is one background `sleep` under 60 minutes (the cache holds an hour), repeated while still over; waiting for tests, CI or Codex counts. From 45 minutes before the reset no pause, but nothing heavy that would not finish.
- 3 points left: commit, push, sleep to two minutes after the reset, go on. Goal: the whole window used, the last 3 points about 30 minutes before the reset (23.11). The 70 percent start limit of `CLAUDE.md` does not apply; no brief sets a lower stop.
- Week: stop at 100 percent (24.11: "Full wave don't stop at 90 go on until 100%").
- Heavy step: a review from several angles with finder or verifier agents (deep review of `/done`, `/code-review max`, a ledger batch), a workflow, or three or more agents at once (24.11). The session asks "need 2 lanes for <step>" and waits for "go heavy"; the other items keep working. One granted in turbo finishes after turbo ends; none new until it fits.
- Mode change: running sessions finish their step; only new starts follow the new count (24.11). Leaving turbo before the reset: sessions above 3 lanes finish the step, commit, push and wait on the baton.
- Super slow (24.11): a freed lane stays empty until the next paused session wakes on its own (at most 50 minutes); the coordinator writes the baton and sends no message, a paused session gets no message at all (news goes to `notes.txt`). Not taken 60 minutes after the baton: "go on item N".
- Items whose files overlap, or where one needs the other's result, run in a row. A side quest Maxi starts counts as a lane; over the count, the lowest item sleeps (23.11).
- Pinned mode stays until Maxi sets auto; at `ahead` +30 or -30 remind him once, large heading plus push, again only after it was back inside (23.11).
- Hold button (24.11): build items finish the step, push, sleep 50 (baton `none`, hold noted in `notes.txt`, no start, no heavy step; the ship lane may finish a merge). The dashboard tells the coordinator.
- Measured with `scripts/wake_cost.py` and `scripts/window_burn.py`: a wake under 60 minutes costs one request; at 60 or more the cache is written again (about 20 reads).

## 4. Rules

**Decisions**
- Maxi there: in the first turn his message begins, ask the choices of merged and open PRs and the newest open "Decisions for Maxi after chain ..." issue as closed popups, recommendation first, at most four per popup (23.11, 24.11).
- Maxi away: every choice takes the best option and is listed in the PR under "Claude's choice, awaiting Maxi" (23.11). A big one (money, law, security design, production data, a product behaviour he has not decided) also goes into the round's decisions issue as it happens; a longer write-up as a `.md` file in `~/.claude/chain/`, linked by a comment there (25.11).
- Away changes only the decisions: planned items start and run as if he were here (24.11, 25.11). "Nothing new" while away means nothing outside the plan.
- Never ask Maxi whether to start the next item (23.11).

**Sessions** (their own rules: `.claude/chain/brief-common.md`: waits, milestones, "PR ready", usage)
- No handover for context size in any chain session, the coordinator included (24.11: "Don't stop the chain any more"): auto compaction carries; commit and push at every step. Optional 550k key: `docs/19` "Auto compaction for an overnight chain".
- Milestone messages only (23.11): a child's own message wakes the coordinator at once; turn-end notes wait until all children idle.
- An item stopped without a PR: its session takes the reserve item itself (23.11); the coordinator names a new reserve in Maxi's next turn.
- Never route a new task into a finished session to get around a refused start (auto mode refuses it, 25.11); a queued session already holds its item.
- Nobody calls `set_monitor` (refused). New sessions start without Remote Control (Maxi, 25.11): a link counts as live work and blocks archiving.

**Coordinator**
- Archives its own side sessions without asking once merged or stopped without a PR; other sessions only on Maxi's word (23.11). Refused for a running background task: send "merged #N: stop every background task and end your turn", archive at the next note; the same for an item shipped before its own "ready" (25.11).
- Keeps the state outside its context (24.11): a `notes.txt` line at every start, merge, mode change and rule; a memory state file rewritten after every merge; after a compaction it rereads that file, `notes.txt`, this command and the chain memories before acting. Before a manual compaction: files first, then a compact prompt.
- Its own cost counts (24.11): in slow and super slow no helper agents, one-line answers.
- The dashboard is the coordinator's alone (per-session state, each update replaces the list). Each item carries its size (small: Sonnet polish, GUI, docs; normal: Opus code; big: deep review, evals or a migration) and the gate running now; a heavy gate is named only after "go heavy".

## 5. The end of a chain

When the last item is merged (18.11): the reviews the plan asks for (security audit, UI review) as read-only sessions, one issue each; a triage of the issues left open; Maxi's decisions in one record on the plan issue; the fix items as the next chain at once (questions as one bundle, the rest start without waiting). A merged migration: the workbench migration last, backup first (the migration path rule), run by Claude. Close the finished issues, report merged PRs, open decisions, usage.

Ending while Maxi is away (24.11): the open decisions go into one issue "Decisions for Maxi after chain ..." (choice, PR, option taken, alternative, recommendation), linked on the plan issue; the next chain and `/start` ask it as popups first, answers go into the issue and the items, then it is closed.
