Generate the handover block for this session per `docs/00_PCF.md` section 12:

## Handover <today's date> – <task IDs>
Status: what's running, what's not
Artifacts: changed files, branch, commits
Decisions: assumptions made with rationale
Gate: next gate status
Open: next concrete step first
Lessons: what the next session must know

Also add the block at the top of the "Done" section in `docs/01_STATUS.md` (short form, one line) and check that "What's next" is correct. Then run `python scripts/status_bump.py patch "Handover: ..."` if no bump has happened in this session since the last one. Replace the `...` with the real one-line summary of this session, written out before the command runs; the text goes into the changelog verbatim, so never run it with a generic sentence.

When the handover goes to a fresh session (Maxi, 2026-11-09), give the exact start form in the chat block:
- Mark the block with a line `==================== HANDOVER START ====================` above and `==================== HANDOVER END ======================` below.
- Inside it, name the console ("Ubuntu terminal (WSL)") and the `Route: <model-id> <effort>` line.
- Start command, always one line with every flag, in its own code block: `cd ~/Maex-Voice-Agent && claude -w <name> --model <model-id> --effort <level>`. The worktree name says what the session does; never leave out `--effort`.
- The first message to type (for example `/task #562`) in its own code block.
