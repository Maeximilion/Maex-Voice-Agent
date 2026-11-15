# Batch review brief: ledger #764 (`/code-review max` level), main @ 69ef3678

Repository: Maex Voice Agent (AI phone agent for a restaurant). Read `CLAUDE.md` section 2 (six hard rules)
and section 9 first. Hard rules that make a finding P1 when broken: rule 1 (prices, zones, hours,
availability, allergens only from the DB), rule 2 (never guess a menu item), rule 3 (nothing leaves
`draft` without the caller's explicit yes; `confirm` is the only path), rule 5 (every outage ends with the
team, no call lost).

## Your job

Read-only deep review of the merged PRs assigned to you, as they stand on `main` now (69ef3678), not only
their diffs: read the surrounding code the diff relies on. Hunt for correctness bugs, especially on the
hard-rule paths. Get each PR's diff with `git show <merge-sha> -m --first-parent` or
`git diff <merge-sha>^1 <merge-sha>` (merge SHAs are in your assignment). `gh` works only via REST
(`gh api repos/Maeximilion/Maex-Voice-Agent/pulls/<n>` for the PR body, `.../pulls/<n>/comments` for
review threads); GraphQL is blocked.

graphify-out/ does not exist in this checkout; search and read directly.

## Rules

- Your worktree is your own (path given in your assignment). Never edit tracked files outside it, never
  commit, never push, never touch `/home/user/Maex-Voice-Agent`.
- You MAY write probe tests inside your worktree (e.g. `api/tests/test_probe_<area>_*.py`) and run them:
  `cd <worktree> && . /tmp/claude-0/-home-user-Maex-Voice-Agent/57c61f4a-774e-5270-a000-171ac1882a8d/scratchpad/testenv.sh && python3 -m pytest -q -p no:cacheprovider <file>`
  (Postgres runs locally; use `python3 -m pytest`, not the bare `pytest` binary). A finding backed by a
  red probe is worth far more than a reading. Copy every probe that proves a finding into your notes
  folder so it survives.
- Write notes to your notes file AS YOU GO (append after each PR), so nothing is lost if you are cut off.
- Do not report style, formatting, naming or "consider" items. No speculative findings without a concrete
  input. Check each candidate against the code before writing it down; say "verified by probe",
  "verified by reading" or "unverified".
- Severity: P0 critical (also confidentiality leaks), P1 urgent or any hard-rule break, P2 ordinary
  concrete defect. No P3.

## Finding format (one per root cause)

```
### [P1] <specific title, max 80 chars>
PR: #<n> · File: path/to/file.py:<line> · Verified: probe | reading | unverified
Trigger: <concrete input / state>
Wrong: <actual behaviour and impact>
Required: <outcome>, verification case: <test idea>
Probe: <path of probe file in notes folder, if any>
```

End your notes file with a section `## Checked and clean` listing, per PR, the angles you checked that
held (one line each), so the skeptic and the cross-cutting wave do not repeat them.

Your final reply: at most 200 words: counts per severity and the notes file path. The notes file is the
deliverable.
