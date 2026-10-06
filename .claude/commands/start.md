Session start for the Maex Voice Agent.

1. Sync: the project's SessionStart hook already fetched and fast-forwarded a clean `main`. Confirm with `git status -sb`. If a hook warning appeared (lock file, divergence, uncommitted changes on `main`), stop and report; never stash, reset or force.
2. Tidy up, only what is verifiably finished: a local branch whose remote is gone, or whose PR is merged with the same head SHA (check as in `/done` step 8), is deleted together with its worktree. Branches with unpushed commits, a PR closed without merge, or no PR stay; list each in one line. Touch nothing else.
3. Read `CLAUDE.md` in full.
4. Read `docs/01_STATUS.md` and `docs/07_WORKPACKAGES.md`.
5. Summarize in max 6 lines: current stage, next gate, open blockers, tasks with in-progress status.
5a. Security alerts: run `gh api 'repos/{owner}/{repo}/code-scanning/alerts?state=open&per_page=100' --jq 'length'` and the same for `dependabot/alerts`. If either is above zero, add one line with the counts, the highest severity and the affected files, and list fixing them among the suggestions in step 6. With zero open alerts say nothing. An API error (for example a feature that is switched off) is reported in one line, not treated as zero.
6. Suggest 1–3 ready tasks (all dependencies done), mark one as recommendation and justify in one sentence. Name the model and effort from the "Model Routing" table in `CLAUDE.md` for each task; if the running model differs, say so now so the switch happens before `/task` loads the specs.
7. Wait for your choice. Don't build yet.
