Generate the handover block for this session per `docs/00_PCF.md` section 12:

## Handover <today's date> – <task IDs>
Status: what's running, what's not
Artifacts: changed files, branch, commits
Decisions: assumptions made with rationale
Gate: next gate status
Open: next concrete step first
Lessons: what the next session must know

Also add the block at the top of the "Done" section in `docs/01_STATUS.md` (short form, one line) and check that "What's next" is correct. Then run `python scripts/status_bump.py patch "Handover: ..."` if no bump has happened in this session since the last one. Replace the `...` with the real one-line summary of this session, written out before the command runs; the text goes into the changelog verbatim, so never run it with a generic sentence.

This command is the single source for handover rules. Memories hold none of them (Maxi, 2026-11-10).

## When

- After every merged PR, write the handover block in the chat and push everything, naming the next point (below).
- Before a break: "PC shuts down at <time>" means finish or park everything 15 minutes before, write the handover, push all work. "Pause" means ask how long; over 45 minutes, write the handover.
- When the context fills up, finish the current step at a clean point, write the handover, push, and continue in a new session (`CLAUDE.md` §6 "Hand over before compression").

## What the next point is

- Name the next point first: the next open `priority: high` issue, the `Route: <model-id> <effort>` line from `CLAUDE.md` Model Routing, and the first message. Goal of 09.11.2026 (Maxi): close the open `priority: high` issues created that day under epic #567 (#573 provisioning, #572 menu intake, which needs Maxi's vision model; #570 and #588 are done, #590 n8n restart loop first if still open), then the others.
- Check whether open issues with the same label or area fit together into one coherent change (Maxi, 2026-11-09). If so, propose one PR that closes all of them (`Closes #A`, `Closes #B`), with description and checklist kept current for each; unrelated issues stay separate (`CLAUDE.md` §8). Name the bundle in the block.
- Ultrareview ledger #546 (Maxi, 2026-11-09): the 40 open candidates run as 8 area batches, one `/code-review max` session each, plan in the comment https://github.com/Maeximilion/Maex-Voice-Agent/issues/546#issuecomment-6745257236. Order: 0 (#586 #587 #589, reviews the PR diffs), 1 conversation core, 2 allergens and consent, 3 telephony outage, 4 speech to dish number, 5 reservations, 6 modes and approvals, 7 deploy and tooling; batches 1 to 7 review the current files of their area on `main`. After each batch's merge, the handover names the next batch. A batch session: at most 5 agents at once, fork included; every reviewer saves findings at once to a file per PR and angle in the `.review-notes` folder of the worktree and the fork keeps a `STATE` file there, so a hit usage limit loses nothing; put both into the batch's first message. Verify each finding, one fix PR per area (Opus `xhigh`), unfixed findings as one checklist issue per future fix PR, tick each PR with `scripts/ultrareview_ledger.py done <PR> --level max --findings ...`. When all 8 batches are ticked, remove this paragraph.

## Hand-over to a fresh session

When the handover goes to a fresh session (Maxi, 2026-11-09), give the exact start form in the chat block:
- Mark the block with a line `==================== HANDOVER START ====================` above and `==================== HANDOVER END ======================` below.
- Inside it, name the console and the `Route: <model-id> <effort>` line. Every command Maxi runs by hand carries its console label right above the code block ("Ubuntu terminal (WSL)", "Git Bash (Windows)", "PowerShell") and the folder where it matters.
- Ubuntu: the start command is always one line with every flag, in its own code block: `cd ~/Maex-Voice-Agent && claude -w <name> --model <model-id> --effort <level>`. The worktree name says what the session does; never leave out `--effort`.
- Windows work runs in the Claude Code Desktop App, not a terminal: give no `cd` and no `claude ...` line. Say "new session in the Windows app, repository folder, worktree on", the model and effort to pick in the app menu, and the first message.
- The first message to type (for example `/task #562`) in its own code block.

## Session title

Keep the title current without being asked (Maxi, 2026-10-06; format 2026-11-10): call `mcp__ccd_session_mgmt__set_session_title` with `session_id: "self"` (load it with ToolSearch first) once the topic is clear and again on a real scope change. Format: `<next gate> · <task or topic> · <branch>` with middle dots, for example `G0 · T-1.12 · task/1-12-shortname`; the gate is "Next gate" in the header of `docs/01_STATUS.md`. A session without a task puts two to four topic words in the middle. Titles the app generated are replaced without a prompt; a title Maxi typed is left alone unless clearly stale. `/task` step 3 and the user-level `session-title` hook (docs/19) enforce it.

## Standing measurements and routing notes in the block

- Context references for #562 (Maxi, 2026-11-07, series extended 2026-11-09): task sessions post one comment per session to #562 after their PR, titled `## Context reference N/10` (references 1 to 4 exist), with session title, model and effort, how it started (fresh, handover, `/start`, `/task`), context at start and after the PR, the PR number and what took most of it if known; Maxi's `/context all` numbers win if pasted. Two tool calls, never a reason to delay the task. After 10, a second series of 25 measures more often (first reply, after `/task` reading, after the first build, after the PR, every 50th reply if cheap, what was read) to learn whether context grows at the start or towards the end. The handover threshold is no longer a context size: handover follows every merged PR. The block states which reference number comes next.
- Split routing (`CLAUDE.md` "Split routing", trial since #561, Maxi decides after about five recorded tasks in `docs/metrics/routing_history.csv`): on an Opus session a part Sonnet or Haiku can do goes to a subagent on that model by default; where it would not lower the usage limit (small piece, tied to the main reasoning, or only a wait on a background Bash call), say so in one line instead of delegating or skipping silently. A subagent rereads its files, so only big self-contained chunks qualify.
- GPU runs (Maxi, 2026-10-21): llama-server eval runs block Maxi's GPU. Before the first GPU start of a session, name the duration (plan 15 minutes for a full pair on IQ3_XXS with load and stop; check `nvidia-smi`, it needs 13.5 GB free) in the same reply as the suggestion and let Maxi name the start time. Collect every GPU run into one block. Every status message about a running GPU job names the clock time it ends. Stop the server as soon as the block ends. Include any pending GPU block and its window question in the handover.
