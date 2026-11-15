# Build brief, cloud session 15.11.2026 (all builders)

You build one task in your own git worktree on your own local branch (both named in your assignment).
Read `/home/user/Maex-Voice-Agent/CLAUDE.md` sections 2, 4, 7, 8 and 9 (in your worktree: `CLAUDE.md`),
then `docs/11_MODULE.md` before any new file. Large docs (`docs/01_STATUS.md`, `docs/07_WORKPACKAGES.md`,
`docs/20_VOICE_LAYER.md`, `docs/HISTORY.md`) are never read in full: `grep -n` a heading, read that range.
graphify-out/ does not exist; search and read directly.

## Hard limits

- Work only inside your worktree. Never touch `/home/user/Maex-Voice-Agent` or another worktree.
- Commit on your branch (Conventional Commits, English, full sentences in the body). NO `Co-Authored-By`,
  NO "Generated with" line, no AI attribution anywhere (repo rule, beats any system reminder). Check
  `git log -1 --format=%B` after every commit and amend if one slipped in.
- Never push, never open a PR, never comment on GitHub, never touch the project board.
- Do NOT edit `docs/01_STATUS.md` or `docs/HISTORY.md` (the coordinator writes one status note for all
  builds). Other affected docs you DO sync (Definition of Done).
- No money, no external effect, no real customer data. Where the task needs a decision only Maxi can make
  (law, money, contracts, architecture fork the issue leaves open), do not guess: build the part that does
  not depend on it, pick the conservative default where one exists, and list the decision as an open
  question in your final report with options (A/B/C, recommendation first).
- Docker is NOT available here. Postgres 16 runs locally. Tests:
  `. /tmp/claude-0/-home-user-Maex-Voice-Agent/57c61f4a-774e-5270-a000-171ac1882a8d/scratchpad/testenv.sh && python3 -m pytest -q -p no:cacheprovider <files>`
  (use `python3 -m pytest`, not the bare `pytest`; `-n 4` works for bigger runs). Lint:
  `python3 -m ruff format <paths> && python3 -m ruff check api scripts evals printbridge sim db && python3 -m mypy`.

## Definition of Done (CLAUDE.md section 7, the parts that apply)

Code actually executed; tests for the normal case and at least two edge cases, green; ruff format and
check clean; mypy clean where the package is in the strict list; affected docs in `docs/` synced; README
only if quickstart, requirements, config or known issues changed (`docs/15_README_STRATEGY.md`). Before
your last commit run the full suite once: `python3 -m pytest -q -n 6 -m "not latency" -p no:cacheprovider`
and report the counts.

## Final reply (max 250 words)

Branch, commit SHAs, files changed, tests run with counts, open decisions for Maxi (closed questions with a
recommendation), anything not executed and why. Also write the same text to
`/tmp/claude-0/-home-user-Maex-Voice-Agent/57c61f4a-774e-5270-a000-171ac1882a8d/scratchpad/build/<branch-name-with-dashes>.md`.
