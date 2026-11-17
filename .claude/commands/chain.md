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

- **Scope:** the list of items, each one pull request, in build order. A plan
  in an issue comment is the list; otherwise write it there first.
- **Routing:** `CLAUDE.md` "Model Routing" per item. The coordinator itself
  runs on Opus 5.5 `high` ("Threads"). Fable 5.2 builds only on Maxi's word
  for this chain, then until its week is used up, then Opus.
- **Decisions:** plain questions the sessions answer themselves and list in
  the PR under "Claude's choice, awaiting Maxi". Only money, law, an open
  security design or production data go to Maxi, as closed questions
  (`AskUserQuestion`), bundled, in a turn he began (see 4).
- **Stop:** Maxi's word, the scope done, or 90 percent of the week.

## 2. One session per pull request

Each item gets a fresh session (`start_session`, `context` "fresh", own
worktree, the item's `model` and `effort`). The session builds and ships its
own pull request to the merge: `/done`, then `/ship` in the same session, no
separate ship session (Maxi, 2026-11-17; it worked for every pull request of
#571 from plan number 9 on). A P0 or P1 found on the way is fixed there too.

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
4. GUI work also loads the skill `ui-ux-pro-max`; `docs/06_GUI.md` and the
   GUI path rule win where they differ.
5. Base: `origin/main`, or a stack on an open item's branch when both touch
   the same files and the base is not merged yet (`/done` step 7c).
6. "Wait inside the turn for tests, CI and Codex; do not end the turn with a
   status line." Every ended turn of a child wakes the coordinator.
7. "Report once, at the end": PR number, merge commit, choices, usage.
8. The usage block of section 5 with the model's numbers.
9. Exceptions to `CLAUDE.md` quoted in Maxi's words with the date; a session
   refuses a rule change relayed without them, and it should.

## 3. Parallel or in a row

Items whose files do not overlap run at the same time; at most as many
sessions as today's row of "Agents per weekday" allows, counting their review
agents (tell each session its share). Items that touch the same files, or
where one needs the other's result, run in a row. Whoever merges second
merges `origin/main` in first; say so in both briefs. Check `gh pr view`
before naming a base.

## 4. What the coordinator does, and what it must not

- Starts sessions only in a turn Maxi's own message began. A turn begun by a
  child's note, a subagent's report, a timer or a background command cannot
  start one, and an `AskUserQuestion` answer inside such a turn does not
  change that; over Remote Control not even a proposal card shows. So: start
  every item that is ready in the turn of his message, and when the next item
  waits, say in one line that it starts with his next message.
- Never routes a new task into an existing idle session to get around a
  refused start.
- No timer, no heartbeat `sleep` in the coordinator: its wake is an automated
  turn and blocks the starts.
- On every child note: read `get_usage`, check the merge with `gh pr view`,
  answer in one or two lines. A note is the child's report, not proof.
- Archives a session only on Maxi's word.
- At 400k context of its own: write the state file (memory, `project` type:
  merged, open, next starts, waiting decisions), tell Maxi to go on in a fresh
  coordinator that reads it.

## 5. Usage pacing (in every brief)

From one `get_usage` reading: `ahead = percentUsed of the 5-hour window +
minutes to reset / 3 - 100`. The session reads it at the start, before and
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

## 6. The end of a chain

When the last item is merged: the reviews the plan asks for (security audit,
UI review) as read-only sessions that end in one issue each; a triage of the
issues the chain left open (fixable, needs Maxi, done); Maxi's decisions in
one record on the plan issue; the fix items as the next chain. The last item
of every chain that merged a migration: the workbench migration with a backup
first (the migration path rule), run by Claude. Then close the issues the
chain finished and report: merged pull requests, open decisions, usage.
