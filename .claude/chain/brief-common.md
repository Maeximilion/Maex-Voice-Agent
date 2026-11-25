# Chain: rules for every item session

Every build or queued session of a chain reads this file and `~/.claude/chain/round.txt` first. `round.txt` is written by the coordinator for the running round: its session id, the ship lane's id, the plan link, the decisions issue, the reserve item (missing: your brief names them). The coordinator's side: `.claude/commands/chain.md`. Maxi's full quotes: `docs/HISTORY.md` "Chain command (archive, replaced 25.11.2026)". You build one item, the ship lane merges it; you never merge.

## Queued (only if your brief says so)
- Do no work yet. Loop: one background `sleep 3000` (50 minutes, never 60 or more), then read `~/.claude/chain/next-lane.txt` and `~/.claude/chain/notes.txt`. Never end a turn without that sleep running.
- The baton names your item (`item 14`) or the coordinator messages "go on item N" (a message wakes you at once): message the coordinator "lane taken item N" and start. Otherwise renew the sleep.
- An item that runs after another one starts only once that one is merged (`gh pr list --state merged --search <its PR>`).

## While you build
- Maxi's exceptions to `CLAUDE.md` (quote them where a rule seems to say otherwise): "Full wave don't stop at 90 go on until 100%" (24.11.2026: weekly stop at 100); "Don't stop the chain any more" (24.11.2026: no handover at 300k or 450k, auto compaction carries you, so commit and push at every step and keep your state in the PR and in files); "Don't ever wait for my message to start new sessions ever again" (25.11.2026).
- Title: your item, for example "13 last_say after barge-in" (25.11.2026). Ignore the title hook. Never call `set_monitor`.
- Maxi away: take every choice with the best option and list it in the PR under "Claude's choice, awaiting Maxi". Money, law, security design, production data or a product behaviour he has not decided: also add it to the round's decisions issue.
- `xhigh` triggers (hard rules 1, 2, 3, 5): red test first, deep review in `/done`, evals where the call path reads the change. A write path: look for a write that passes validation but puts a reader into a wrong state, and for a write that leaves a stale sibling row.
- Heavy step (`/code-review max`, a review from several angles, a workflow, three or more agents at once): message "need 2 lanes for <step>" and wait (background sleep under 60 minutes, renewed) for "go heavy". Not in turbo.
- Wait inside the turn for tests, CI and Codex (`gh pr checks <N> --watch`, `py -3.14 scripts/codex_review.py <N> --wait`). Exit 0 clean; 1 findings: fix them all yourself, P0 and P1 too; 2 not done: wait again in the same turn; 3 stalled: post one scoped `@codex review`, then wait again. No Codex start 10 minutes after a push: one scoped `@codex review`. "CI green, Codex still running" is a stall to check, never a state to sleep on: nothing wakes you for a clean result. Never end a turn while tests, CI, Codex or evals on your head are pending (25.11.2026, #1042).
- Before "PR ready": no review agent or background shell of yours still runs (25.11.2026: a leftover finder agent blocked an archive).
- After "PR ready" you push to your PR no more: a conflict or an Auto-fix event goes to the coordinator; the ship lane owns the branch (25.11.2026).

## Usage
Read `get_usage` at the start, before and after every agent, before every large step and at the end. `ahead = 5-hour percentUsed + minutes to reset / 3 - 97`.
- Opus: over +25 at a task boundary: one background sleep of exactly 30 minutes, repeated while over. Over +40 at any reading: finish the step, commit, push, sleep 30.
- Fable: the same with +15 and +30, sleeps of 50 minutes.
- Sonnet: no new fix round at 85 percent of the 5-hour window; at 90: commit, push, sleep to the reset.
- 3 points left in the window: commit, push, background sleep to two minutes after the reset, go on.
- Slow mode: a 45-minute sleep after each finished part. Super slow or Hold: the coordinator tells you; sleep 50 minutes, renewed, and read the baton on every wake.

## Messages to the coordinator (SendMessage to its id), nothing in between
"PR #N ready, first review clean" (then stop: no push after it), "stopped/blocked: <why>", "reserve taken", "need 2 lanes for <step>", "lane taken item N", a choice that needs Maxi. "Waiting for CI" is no milestone.
If your item stops without a PR: post why on its issue and take the reserve item your brief names in this same session; if its route differs, ask the coordinator to switch your model and effort.
