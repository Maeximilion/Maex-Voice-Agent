Hand unfinished work to a fresh session, only when that is necessary or cheaper than going on here (Maxi, 2026-11-12, replaces "after every merged PR" of 2026-11-10). This command is the single source for handover rules; memories hold none of them.

Measure first: `python3 scripts/session_usage.py context` prints the current context in tokens. The session measures before each large step, at `/done` and after a merge (`CLAUDE.md` "Hand over before compression").

## When a block is due

Only when unfinished work continues in a new session:

- **Model switch:** the `/ship` hand-off to Sonnet, a P1 that goes back to Opus, a Fable session whose PR opened (`CLAUDE.md` "Cache-friendly sessions" 2, "Fable 5.2").
- **Context:** at 300k tokens or more before the next large step (a new build step, a new task; not a push, a short fix or a wait), and at 450k or more at the next clean point whatever comes next. A session never runs into compression.
- **Break with work left:** "PC shuts down at <time>" (park everything 15 minutes before), a pause over 45 minutes (ask how long), the idle check-in firing, 90 percent of the 5-hour window (name the reset time).
- **A task cut in parts** on purpose.

Not due: after a merged PR with nothing of the session left open (write the next start only, below); mid-task under 300k; a break with nothing left open (push, say so in one line).

Why these numbers (Maxi, 2026-11-12, from the references of #562): every call rereads the whole context at 1/40 of the price of writing it (Opus 5.5 cache read 0.20, 1-hour write 8 USD/M; the ratio is the same on Sonnet). A fresh session stands at 49k to 115k at its first reply (median about 70k) and at 100k to 160k once `/task` has read the spec and code; a task ended at 180k to 445k after 200 to 360 calls. After a merge, everything above the fresh start (about 70k) is dead weight on every call of the next task: at 150k that is about 3.2 USD over 200 calls on Opus against about 0.6 USD for writing a fresh start, below 120k the saving is under 1.5 USD and not worth a restart. Mid-task the fresh session must read the task again (about 130k, about 1 USD): at 300k with 100 more calls the switch saves about 2.4 USD, at 250k only about 1.4 USD, and every handover risks losing what lived only in the chat. 450k is the hard stop whatever comes next. `/done` records each task's context in `docs/metrics/context_history.csv` (`scripts/session_usage.py record`) and posts it as a context reference to #562; after the series of 25 Maxi decides whether these numbers change.

## Before the block

1. Commit and push all work on its own branch. Where a commit parks unfinished work, its body carries the block's `Next` and `Know` lines, so `git log -1 <branch>` recovers them if the chat is lost. No line in `docs/01_STATUS.md`, no status bump and no `docs/handover-*` branch for a handover: the branch, its PR and `/done` hold the state.
2. `git log -1 --format=%B`: no attribution line (`CLAUDE.md` §8).

## The block

Two code blocks in the chat, nothing the repository already says (diff, PR text, Done rows, specs, gate):

1. **The start**, its console named right above the block, runnable as written, with no placeholder left in it:
   - Ubuntu terminal (WSL), a new worktree: `cd ~/Maex-Voice-Agent && claude -w <name> --model <model-id> --effort <level>`, one line, never without `--effort`; the worktree name says what the session does. To continue a branch that a worktree has checked out already, `cd` into that worktree and start `claude --model <model-id> --effort <level>` there.
   - Windows: the Claude Code Desktop App, no `cd` and no `claude` line: "new session in the Windows app, repository folder, worktree on", with the model and effort to pick in the app menu.
2. **The first message**, to paste as it is:

```text
## Handover <date> - <task or PR> - <model switch | context 270k | break | part done>
Route: <model-id> <effort>
Where: branch <branch>, PR #<n> or none, head <sha7>, all pushed
Next: 1. <first concrete step> 2. ...
Know: <only what lives in this chat: Maxi's word not yet in the repo, an approach that failed, a wait in progress (CI run, review, GPU window)>
Start with /task T-x.y
```

The last line names the command that resumes it: `/task <id>` for a task (it continues the named branch), `/ship <PR>` for a PR in review; other work leaves it out. The block opens with `## Handover`, never with the command, so the command gets only its own argument and the route hook (`.claude/hooks/route_advice.sh`) reads the block as a handover. Leave out a line that would be empty.

## Next start after a merge

No block. Two lines: what comes next and how it starts. The next task is the one "What's next" in `docs/01_STATUS.md` names after `/done` step 4, with its route from the Model Routing table; read nothing extra to find it, and where none is clear the next start is `/start`. With a context of 150k or more, or a next task on another model, it is the start form above with the first message `/task <id>` or `/start`; otherwise "next message here: `/task <id>`".
