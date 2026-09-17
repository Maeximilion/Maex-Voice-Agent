Work on task $ARGUMENTS from `docs/07_WORKPACKAGES.md`.

1. Check whether all dependencies are complete. If not: name the missing one and stop.
2. Read the spec from the "Spec" column and check in `docs/11_MODULES.md` which modules the files belong to.
3. Create branch `task/<id-with-hyphens>-<shortname>`.
4. Show a plan with max 5 lines: files, tests, migration needs. Build directly; wait only if plan touches money, law, external impact, production data, or irreversibility.
5. Write tests first (normal case + at least two edge cases), then code until green. Actually run them.
6. For hot-path tools: measure response time and report value.
7. Set status in `docs/07_WORKPACKAGES.md` to in-progress at start, done at end. If `/done` follows directly, it handles the status bump — otherwise run `python scripts/status_bump.py patch "<Task-ID> done"` at the end.
8. Use up to three follow-up steps for obvious gaps, then report: what's running, what was measured, what's open.
Follow the hard rules from `CLAUDE.md` §2. Ask closed questions with marked recommendation, one question per interruption.
