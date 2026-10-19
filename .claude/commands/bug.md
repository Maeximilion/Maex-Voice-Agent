Handle this production error: $ARGUMENTS

Run `git fetch --prune origin` and branch from `origin/main`. Order is mandatory, after loading the skill `code-autopilot` (Skill tool; where it is not installed, say so in one line and go on):
1. FIRST write a test that reproduces the error, with correct behavior as the expectation: an eval case in `evals/cases/` (format: `docs/08_EVALS.md`) when the error is in the conversation path, otherwise a unit or integration test in `api/tests/` (GUI, jobs, scripts, infrastructure).
2. Run that test (`make eval` for an eval case, `pytest` for a test). It MUST be red. If green, the test is wrongly described — fix it before proceeding.
3. Find root cause: which module from `docs/11_MODULE.md`? Name file and function.
4. Write a unit test in the affected module that isolates the cause.
5. Fix minimally. No side changes.
6. Run `make test` and `make eval` fully. All green, accuracy not dropped.
7. Commit: `fix(<module>): <what> (#eval <case-id>)`, or the test name instead of the eval case.
No fix without a prior red test case.
