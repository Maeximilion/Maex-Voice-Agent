Close out the current task.

1. Run `make lint` and `make test`. Show results. If red: fix first, then continue.
2. Check the Definition of Done from `CLAUDE.md` §7 point by point and show the list as complete/open.
3. Update `docs/07_WORKPACKAGES.md` (status done) and `docs/01_STATUS.md` (completion table, next steps, new assumptions, new blockers). Then `python scripts/status_bump.py patch "<one line>"` (for new decision or changed "what's next": `minor`; for passed gate/stage change: `major`).
4. Check per `docs/15_README_STRATEGY.md` whether README is affected (quick start, requirements, config, known issues, new doc file). If yes: update it, current state, no emojis.
5. Sync affected spec docs in `docs/` if implementation differs from spec. Name the deviation explicitly.
6. Suggest a commit message per Conventional Commits and commit after confirmation.
