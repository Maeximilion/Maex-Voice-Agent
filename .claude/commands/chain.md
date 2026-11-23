---
description: Work off several tasks or issues as a chain - one coordinator, one fresh session per pull request that builds and ships it, usage paced, until Maxi says stop.
argument-hint: "[plan link, issue numbers or task IDs]"
---

Run $ARGUMENTS as a chain. This session is the coordinator: it plans, starts
sessions, checks merges and reports. It builds nothing itself.

## What a chain is

A chain is Maxi saying `/chain` or "chain", or naming two or more tasks,
issues or plan pull requests to work off in order (Maxi, 2026-11-17). A
single task he takes up on its own is no chain: it follows `/task` and the
usage rules of `CLAUDE.md` alone. A running chain goes on until he says stop
or every item is merged; the coordinator then reports and stops.

## 1. Fix the frame before the first start

- **First:** the newest open "Decisions for Maxi after chain ..." issue
  (section 6), asked as popups before anything else.
- **Scope:** the list of items, each one pull request, in build order. A plan
  in an issue comment is the list; otherwise write it there first.
- **Routing:** `CLAUDE.md` "Model Routing" per item. The coordinator itself
  runs on Opus 5.5 `high` ("Threads"). Fable 5.2 builds only on Maxi's word
  for this chain, then until its week is used up, then Opus.
- **Decisions:** plain questions the sessions answer themselves and list in
  the PR under "Claude's choice, awaiting Maxi". Only money, law, an open
  security design or production data go to Maxi, as closed questions
  (`AskUserQuestion`), bundled, in a turn he began (see 4). Maxi, 2026-11-23:
  "Give me the desicions when i am at the computer in future so i can decide
  or if they are not so important just do it by yourself with the best options
  when i am sleeping or not at the pc". So: while he is away, every choice is
  made with the best option and listed in the PR; nothing waits for him except
  the four kinds above. In the first turn his own message begins, the
  coordinator gathers the listed choices of the merged and open PRs and asks
  the ones worth his time as closed popups, recommendation first (at most four
  per popup, the rest named in one line); his answer goes to the PR or a
  follow-up item.
- **Stop:** Maxi's word, the scope done, or 90 percent of the week.

## 2. One session per pull request

Each item gets a fresh session (`start_session`, `context` "fresh", own
worktree, the item's `model` and `effort`). The session builds its pull
request through `/done` until CI is green and the first independent review is
clean, fixes every finding of that first round itself (a P0 or P1 too), and
ends with "PR #N ready, first review clean". Then it is done and archived.

**The ship lane** (Maxi, 2026-11-24, "One Sonnet ship lane"): one fresh Sonnet
`medium` session, started in Maxi's turn together with the first build
sessions of the chain, ships the queue: it waits until the coordinator sends
"ship #N", merges `origin/main` in, gets CI, Codex and the evals on the new
head, fixes what that round finds, merges, messages the coordinator "merged
#N", and waits for the next "ship #N". One pull request merges at a time
anyway, so one lane is enough. Why: a long build session ships in a big Opus
context, and every CI or Codex wake rereads all of it; the ship lane rereads a
small Sonnet context. A P1 in an `xhigh` path of the ship round is fixed on
Opus: the ship lane asks the coordinator, which switches its model and effort
(`set_session_model`, `set_session_effort`) and back afterwards. When its
context passes 300k, the coordinator starts a fresh ship lane in Maxi's next
turn, and the old one finishes the pull request it holds. The ship lane does
not count as a build lane (section 3): it mostly waits, about 1 point an hour
(#965). Its brief: `Route: claude-sonnet-5-5 medium`, the chain and plan
link, the order rule ("merge only on ship #N, one at a time"), the merge
gate of `CLAUDE.md` ("Merging a PR into `main`"), the milestone line of item
12, the usage block of section 5 (Sonnet row), and "Do not hand over at 300k
or 450k".

The brief is the session's only context. It has, in this order:

1. `Route: <model-id> <effort>` as the first line (hook `.claude/hooks/route_advice.sh`).
2. Background of at least two sentences: the chain, the plan link, what is
   merged, which sessions run in parallel and in which folders.
3. The task: items with issue and finding ids; "a finding is a claim, check it
   against the code"; the `xhigh` triggers it touches, and then: red test
   first, deep review in `/done`, evals where the call path reads the change.
   A write path also gets this line: look for a write that passes validation
   but puts a reader into a wrong state, and for a write that leaves a stale
   sibling row (the two P1 shapes Codex found on #820).
4. Where the code lives: the files, functions and tests the item touches,
   found by the coordinator with the Graphify connector (`graphify_find`,
   `graphify_impact`, `graphify_tests_for`; `CLAUDE.md` "graphify"), with the
   approach, the edge cases to test and what not to touch. Pointers, not file
   contents. The coordinator does not search for all of it itself: in Maxi's
   turn, before it starts the sessions, it runs one scout agent per item in the
   foreground, all in parallel (Explore type, `model` haiku; Maxi's idea,
   2026-11-23, so the sessions start faster; about 30 to 40 seconds each). Each
   scout is told to use the Graphify connector first and to return a map of at
   most 300 words: the file and line of each function the item names, its direct
   callers, the linked tests with their names, and the `commitSha` of the graph
   with whether it equals `HEAD`, plus its caveats (a name not in the graph, a
   file that does not exist, a setting not found). The coordinator puts each
   map, caveats kept, straight into that item's brief under "Where the code
   lives". Only a map that was not ready in time follows by `SendMessage`, and
   the brief then says so and what to read meanwhile; a message to a session
   that is mid-turn is queued until its turn ends, so it may come too late to
   help. The session uses the map instead of searching again and re-reads only
   the files changed since that commit.
5. GUI work also loads the skill `ui-ux-pro-max`; `docs/06_GUI.md` and the
   GUI path rule win where they differ.
6. Base: `origin/main`, or a stack on an open item's branch when both touch
   the same files and the base is not merged yet (`/done` step 7c).
7. "Wait inside the turn for tests, CI and Codex; do not end the turn with a
   status line." A child's ended turn does not always wake the coordinator
   (item 12).
8. "Report once, at the end": PR number, merge commit, choices, usage.
9. The usage block of section 5 with the model's numbers.
10. Exceptions to `CLAUDE.md` quoted in Maxi's words with the date; a session
   refuses a rule change relayed without them, and it should.
11. The next reserve item (section 4) and this line: "If your item stops
   without a PR (blocked by a Maxi decision, nothing to do), post why on its
   issue and take the reserve item yourself in this same session; if its route
   differs from yours, ask the coordinator to switch model and effort."
12. The coordinator's session id and this line: "Send the coordinator
   (`SendMessage` to `<its local_ id>`) one short message at each milestone:
   PR ready, merged, stopped or blocked, reserve taken, a choice that needs
   Maxi. Nothing in between." (Maxi, 2026-11-23, asked for this fix "with
   the milestones".) Why: the app wakes the coordinator after a child's
   ended turn only when no other side session it started still works, so with
   one long build running every other note waits for Maxi's next message (#955
   merged 21:37 UTC, its note arrived about 14 minutes later). A child's own
   message arrives at once, also while others work (tested 23.11.2026 with
   #959). Every step would be too many: each message is one request on the
   coordinator's whole context and makes every later one larger, and its
   turns cannot start a session anyway.
13. The heavy-step line (section 3): "A heavy step (a review from several
   angles with many agents, `/code-review max`, a workflow, a wave of three or
   more agents) holds 2 lanes: message the coordinator 'need 2 lanes for
   <step>' and wait for 'go heavy' before you start it."

## 3. Parallel or in a row

Items whose files do not overlap run at the same time, counted in lanes; the
ship lane (section 2) comes on top of them. A build side session holds one
lane, a pull request the coordinator opens holds one, and a **heavy step**
holds two (Maxi, 2026-11-24: reviews "aus verschiedenen Angles ... die viele
Agents oder Subagents benötigen, müssen definitiv mehrere Lanes blockieren
... am besten 2 Lanes"; the same for `/code-review max` and workflows). A heavy
step is a review from several angles with finder or verifier agents (the
deep review of `/done`, `/code-review max`, a ledger batch review), a workflow,
or any wave of three or more agents at once. Before it starts one, a session
messages the coordinator "need 2 lanes for <step>" and waits (one background
sleep under 60 minutes, renewed) for "go heavy"; the coordinator answers when
a second lane is free under the mode's count, otherwise the session waits until
one frees. Normal mode has 3 lanes (Maxi: "Vielleicht könnten 3 Lanes im Normal
mode passend sein wenn ... teure Items ... zum Blockieren von 2 lanes führen"),
the other modes as in section 5, never above today's row of "Agents per
weekday". The measurement of #965 likely included such steps counted as one
lane, which made three lanes look more expensive than they are; check the next
full window with `scripts/window_burn.py`. The limit counts their review agents
too (tell each session its share). Items that touch the same files, or
where one needs the other's result, run in a row. Whoever merges second
merges `origin/main` in first; say so in both briefs. Check `gh pr view`
before naming a base.

## 4. What the coordinator does, and what it must not

- Starts sessions only in a turn Maxi's own message began. A turn begun by a
  child's note, a subagent's report, a timer or a background command cannot
  start one, and an `AskUserQuestion` answer inside such a turn does not
  change that; over Remote Control not even a proposal card shows. So: start
  every item that is ready in the turn of his message, and when the next item
  waits, say in one line that it starts with his next message. A scout
  agent's report turn is such a turn too, so sessions start in Maxi's turn,
  after the foreground scouts returned; only a late map follows by message
  (section 2, item 4).
- Never asks Maxi whether to start the next item (Maxi, 2026-11-23: "Don't ask
  me ever again starting the Items / Side Sessions just do it because you are
  using /chain"). When a lane frees, the coordinator starts the next item in
  that same turn; only when the app refuses the start does it wait for his next
  message, and it says so in one line, never as a question.
- Never routes a new task into an existing idle session to get around a
  refused start.
- Reserve items (Maxi, 2026-11-23: "if something stops, work on the next one so
  the chain doesn't break"): the plan lists one or two after the chain items,
  each with route and a short brief, and every brief names the next one (item
  11 of section 2). A note turn cannot start a session, so the stopped session
  takes the reserve item itself. The coordinator crosses it off and, in Maxi's
  next turn, names a new reserve.
- Cache: a coordinator woken after more than an hour of silence rereads its
  whole context at the cache write price, about 40 times a warm read. While
  a child runs and no start waits for a note turn, keep it warm with one
  background `sleep` of 50 minutes, re-armed on each wake; a timer turn
  cannot start a session, so drop it when the next start should come from a
  child's note. Over Remote Control every start waits for Maxi's message
  anyway, so there the heartbeat costs nothing. No `CronCreate`.
- On every child note or milestone message (section 2, item 12): read
  `get_usage`, check the merge with `gh pr view`, update the dashboard in that
  same turn, answer in one or two lines. A note is the child's report, not
  proof. The dashboard stays the coordinator's: its state is per session and
  each update replaces the whole list, so children cannot write to it.
- Archives a session only on Maxi's word.
- At 400k context of its own: write the state file (memory, `project` type:
  merged, open, next starts, waiting decisions), tell Maxi to go on in a fresh
  coordinator that reads it. Not under the option of section 5a.

## 5. Usage pacing (in every brief)

From one `get_usage` reading: `ahead = percentUsed of the 5-hour window +
minutes to reset / 3 - 97` (Maxi, 2026-11-24: "Keep / 3, aim at 97"; until
then `- 100`). An even pace of 20 points an hour from the window's start lands
at 97 percent at the reset, the 3-point buffer of the next paragraph. The session reads it at the start, before and
after every agent, before every large step and at the end.

| Model | Pause at a task boundary | Hard brake at any reading |
|---|---|---|
| Fable | `ahead` over 15: one pause of exactly 50 minutes | over 30: finish the step, commit, push, pause 50 |
| Opus | `ahead` over 25: one pause of exactly 30 minutes | over 40: the same, pause 30 |
| Sonnet | none; no new fix round at 85 percent of the window | at 90: commit, push, sleep to the reset |

A pause is one background `sleep`, never 60 minutes or more (the cache holds
an hour), repeated while still over. Waiting for tests, CI or Codex counts as
pause. From 45 minutes before the reset no pause, but nothing token-heavy that
would not finish. At 3 points left: commit, push, one background `sleep` to
two minutes after the reset, then back to work. At 90 percent of the week:
finish the step, commit, push, report. Cheap work first (read, test, build),
agent waves and deep reviews when they fit into what is left of the window.

The goal is the whole window used by the reset, so the chain takes the reset
and keeps going (Maxi, 2026-11-23: "the target is to use all Usage limit until
the reset so we can take the reset and just keep going. Thats why we can go
until 3% is left for the Reset. Important is that the 3% are reached in a zone
where the Cache don't run cold, so like 30 minutes before the reset."). Inside
a chain the 70 percent start limit of `CLAUDE.md` "Usage check" does not apply:
the pacing above decides, and the last 3 points are reached about 30 minutes
before the reset, so the sleep to the reset stays short and the cache warm. A
brief never sets a lower stop.

Chain modes, read by the coordinator at every reading of `ahead`; Auto on the
chain dashboard sets them by the same rules. Four modes with hysteresis (Maxi,
2026-11-24, on the numbers of #965: three lanes burned 25 to 27 points an hour
while they worked, slow mode at +10 never came on, three Opus lanes for five
hours would reach about 125 percent). "First hour" is over 240 minutes to the
reset, "last 2 hours" under 120 (Maxi, 2026-11-24: slow "at the beginning of
the first hour ... at ahead +5 until it reaches -20"). Every mode keeps the
ship lane on top of its lanes; lanes are counted as section 3 says (a heavy
step holds two).

| Mode | On at | Back at | Lanes | Heavy steps |
|---|---|---|---|---|
| Super slow | over +15 | +5 or below, to slow | 1; every other build session pauses with background sleeps of 50 minutes, renewed on each wake | wait for slow or normal |
| Slow | over +5 in the first hour, +10 in the middle, +5 in the last 2 hours | to normal at -20 or below in the first hour, -15 in the middle, -5 in the last 2 hours | 2, a 30-minute sleep between parts | one at a time, it holds both lanes |
| Normal | | | 3 | as section 3 |
| Turbo | under -25 | -10 or above, to normal | up to 5, never above today's row of "Agents per weekday" | as section 3 |

From normal, over +15 goes straight to super slow. When turbo ends, running
sessions finish their job; only no new start beyond the new count (Maxi: "let
the lanes get the Job done (no force break)"). In super slow (Maxi,
2026-11-24) all build sessions but one pause with 50-minute sleeps, renewed on
each wake, so none falls into a cold cache; when the running one is done, a
paused item takes its lane over ("go on #N"). The pauses of the first table
still hold in every mode.

A side quest Maxi starts during a chain (a measurement, a question for a
session) counts as a lane. When it takes the chain above the lanes of its mode,
the coordinator tells the lowest item in the order to sleep (one background
sleep under 60 minutes, renewed) until a lane is free again (Maxi,
2026-11-23).

In slow and super slow mode (Maxi, 2026-11-23: "in the Slow mode i would
prefer to let Items work which needs smaller models or less effort or less
thinking"):

- The next start is the first open item routed to Sonnet or Haiku, or to a
  lower effort; Opus items wait for normal mode unless none of the cheap kind
  is left.
- A session takes one background `sleep 1800` (30 minutes; Maxi, 2026-11-24,
  was 15) after each finished part of its work, before it starts the next
  part; a paused session in super slow sleeps 50 minutes. Measured on 23.11.2026
  (6,000 requests of three days): a wake after any wait under 60 minutes costs
  the same as one working request, because the cache is kept for an hour; the
  wait itself costs nothing. So one long wait is cheap, many short polls are
  not, and a wait of 60 minutes or more pays the cache write again (about
  twenty times the read).

A mode Maxi pins on the chain dashboard stays until he sets it back to auto.
When a mode is pinned and `ahead` reaches +30 or -30, the coordinator reminds
him once, as a heading in large text at the top of its reply and as a push
notification, to set the mode back to auto (Maxi, 2026-11-23: "give me once a
reminder if we got to an extreme ahead value of 50", lowered to 30 on his
question for a better value). It reminds him again only after `ahead` has been
back inside -30 to +30. Why 30: three Opus lanes used about 26 points an hour
on 23.11, an even pace is 20; a forgotten turbo pin climbs about 20 points an
hour, a forgotten slow pin falls about 10, and turbo closes about 20 an hour.
At +30 more than an hour stays before the window runs out; at -30 about an
hour and a half of turbo still catches up. At 50 neither is true late in the
window.

## 5a. Option: no handover limit (overnight)

Maxi names it when he starts a chain ("no handover limit", "overnight"); without
it the normal limits hold (Maxi, 2026-11-18 on #562: 300k and 450k stay for
normal sessions, a chain may run the whole night). Under it the coordinator and
every child skip the handover at 300k and 450k (`CLAUDE.md` "Session End",
`/handover` "When a block is due") and the coordinator skips its 400k state file
(section 4). Auto compaction at about 550k tokens takes over, set by
`autoCompactWindow` 550000 in the user settings (`docs/19` "Auto compaction for
an overnight chain"). It cannot be scoped to chain sessions, so Maxi switches
the key on before the chain and off after it; before the first start the
coordinator reads `get_usage` for itself and checks `autoCompactsAtPercent` is
about 55 (97 means it is not on: say so, and keep the normal limits). Pacing
(section 5) and the weekly stop still hold.

A child brief then says: "No handover limit this chain (Maxi, 2026-11-18): do
not hand over at 300k or 450k and do not stop for context size. Auto compaction
at about 550k tokens may happen: commit and push at every step and keep what you
need in the PR and in files, so a compaction loses nothing."

A child never hands its work to a new session because of context size (Maxi,
2026-11-23: "start ship sessions in chain yourself in future when this happens
so it doesn't break when i am not at the computer so in future it doesn't
matter if i am here or not"). Only a turn Maxi's message began can start a
session, so a hand-over at 450k stalls the chain until he is back. Every child
brief therefore says, with that quote: "Do not hand over at 300k or 450k; build
and `/done` in this session until the PR is ready with a clean first review.
Commit and push at every step and keep what you need in the PR and in files,
so an auto compaction loses nothing." Shipping goes to the ship lane (section
2), which already runs. The 1M window compacts at about 97 percent, so a child
reaches it rarely; a child whose turn fails on context anyway asks the
coordinator by message.

## 6. The end of a chain

When the last item is merged: the reviews the plan asks for (security audit,
UI review) as read-only sessions that end in one issue each; a triage of the
issues the chain left open (fixable, needs Maxi, done); Maxi's decisions in
one record on the plan issue; the fix items as the next chain, run on at once
(Maxi, 2026-11-18): the questions that need him go out as one bundle, and the
fix items that need no answer start without waiting for it. The last item
of every chain that merged a migration: the workbench migration with a backup
first (the migration path rule), run by Claude. Then close the issues the
chain finished and report: merged pull requests, open decisions, usage.

When the chain ends while Maxi is away (asleep, not at the computer), the
coordinator files the open decisions as one issue ("Decisions for Maxi after
chain ...": each choice with its PR, the option taken, the alternative, the
recommendation) and links it on the plan issue (Maxi, 2026-11-24: "note it down
in an Issue and the first thing to do when we start the chain or a new
session, to decide what i have to like you just did with pop ups"). The first
step of the next chain (section 1) and of `/start` is to read the newest open
such issue and ask its decisions as closed popups (`AskUserQuestion`,
recommendation first, at most four per popup); his answers go into the issue
and to the items they change, then the issue is closed.
