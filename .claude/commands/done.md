Close out the current task.

1. If the diff against `main` contains anything other than `.md` files, run `/ponytail-review` on it and apply the cuts that hold. It only hunts complexity and is not a merge gate (`CLAUDE.md` §6); it runs here, before the PR opens, so a deletion does not move the head commit after its review.
2. Run `make lint` and `make test`. Show results. If red: fix first, then continue.
3. Check the Definition of Done from `CLAUDE.md` §7 point by point and show the list as complete/open.
4. Update `docs/07_WORKPACKAGES.md` (status done) and `docs/01_STATUS.md` (completion table, next steps, new assumptions, new blockers). Then `python scripts/status_bump.py patch "<one line>"` (for new decision or changed "what's next": `minor`; for passed gate/stage change: `major`).
5. Check per `docs/15_README_STRATEGY.md` whether README is affected (quick start, requirements, config, known issues, new doc file). If yes: update it, current state, no emojis.
6. Sync affected spec docs in `docs/` if implementation differs from spec. Name the deviation explicitly.
7. Suggest a commit message per Conventional Commits and commit after confirmation.
8. After Maxi has merged the PR (`CLAUDE.md` §6), tidy up that task only: `git switch main`, `git pull --ff-only origin main`, `git worktree remove <path>` (never `--force`), `git branch -d <branch>`. A squash merge leaves the branch looking unmerged and `-d` refuses: confirm the merged PR has the same head SHA (`gh pr list --state merged --head <branch> --json number,headRefOid`) and only then use `git branch -D <branch>`. GitHub deletes the remote branch on merge (repo setting "Automatically delete head branches"); do not delete remote branches by hand. Leave all other branches and worktrees alone.
