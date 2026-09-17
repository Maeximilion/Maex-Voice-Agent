Handle this production error: $ARGUMENTS

Order is mandatory:
1. FIRST create an eval case in `evals/cases/` describing the error (format: `docs/08_EVALS.md`). Expected behavior is correct behavior.
2. Run `make eval` for this case. It MUST be red. If green, case is wrongly described — fix it before proceeding.
3. Find root cause: which module from `docs/11_MODULES.md`? Name file and function.
4. Write a unit test in the affected module that isolates the cause.
5. Fix minimally. No side changes.
6. Run `make test` and `make eval` fully. All green, accuracy not dropped.
7. Commit: `fix(<module>): <what> (#eval <case-id>)`.
No fix without a prior red test case.
