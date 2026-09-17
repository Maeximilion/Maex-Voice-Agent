Generate the handover block for this session per `docs/00_PCF.md` section 12:

## Handover <today's date> – <task IDs>
Status: what's running, what's not
Artifacts: changed files, branch, commits
Decisions: assumptions made with rationale
Gate: next gate status
Open: next concrete step first
Pitfalls: what the next session must know

Also add the block at the top of the "Completed" section in `docs/01_STATUS.md` (short form, one line) and check that "What's next" is correct. Then run `python scripts/status_bump.py patch "Handover: <summary>"` if no bump has happened in this session since the last one.
