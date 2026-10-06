Work on task $ARGUMENTS from `docs/07_WORKPACKAGES.md`.

0. Model check first, before reading any doc other than the task's row: grep the ID in `docs/07_WORKPACKAGES.md`, take the area from the row's task text and Spec column, and look it up in the "Model Routing" table in `CLAUDE.md`. A row that names no area gets the default (Opus 5.5). If the running model is not the routed one, stop with one line naming the model and effort to switch to; if it matches, compare the routed effort with `printenv CLAUDE_EFFORT` and stop the same way on a mismatch. Only where that variable is empty, ask one closed popup question whether the routed effort is set; continue on yes, stop the same way on no. Do not load specs, status or modules on the wrong model or before the effort is confirmed.
1. Check whether all dependencies are complete. If not: name the missing one and stop.
2. Read the spec from the "Spec" column and check in `docs/11_MODULE.md` which modules the files belong to.
3. Create branch `task/<id-with-hyphens>-<shortname>`. Then rename the session to `<next gate> - <task id> - <branch>`, for example `G0 - T-1.12 - task/1-12-shortname`; the gate is "Next gate" in the header of `docs/01_STATUS.md`. Use the session-title tool (`set_session_title`, session `self`) where the app offers it, and skip this where it does not.
4. Show a plan with max 5 lines: files, tests, migration needs. Build directly; wait only if plan touches money, law, external impact, production data, or irreversibility.
5. Write tests first (normal case + at least two edge cases), then code until green. Actually run them. If the same failure survives two fix attempts, stop and report it with the escalation row from the "Model Routing" table instead of a third try on the same setting.
6. For hot-path tools: measure response time and report value.
7. Set status in `docs/07_WORKPACKAGES.md` to in-progress at start, done at end. If `/done` follows directly, it handles the status bump — otherwise run `python scripts/status_bump.py patch "<Task-ID> done"` at the end.
8. Use up to three follow-up steps for obvious gaps, then report: what's running, what was measured, what's open.
Follow the hard rules from `CLAUDE.md` §2. Ask closed questions with marked recommendation, one question per interruption.
