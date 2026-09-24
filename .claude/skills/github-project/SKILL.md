---
name: github-project
description: Rules for the GitHub Project board of this repo. Load before any gh project call, before opening an issue or pull request, and whenever board state, roadmap, backlog, iterations or item status come up. Enforces hands-off during work, one single project, and the daily maintenance job.
---

# GitHub-Project

The board is a derivation, not handiwork. Full convention: `docs/16_GITHUB_PROJECT.md`.

## Hands-off during work

**No `gh project` call inside the coding loop** - not reading, not writing. Not in `/task`, not in `/done`, not in `/bug`. Every such call pulls item lists and node IDs into context and moves no code forward.

The board follows from events. Work links to it through three things only:

- closing keyword in the pull request description: `Closes #12`
- branch name carrying the issue number: `task/T-4.3-search-menu`, `fix/12-kurzbeschreibung`
- labels set once, when the issue is created

## One project

There is exactly one project. A missing perspective is a **view**, never a second project. Never create a project. Never add the same issue twice.

## Status

Status lives in the project's Status field and nowhere else - never additionally as a label. It moves only through GitHub's built-in workflows:

| Event | Status |
|---|---|
| Issue or pull request opened | Todo |
| Pull request merged | Done |
| Issue closed | Done |
| Item reopened | In Progress |

`Done` comes from the merge. Never set it by hand, never before the merge.

Exception in the label set: `status: blocked` mirrors the "blockiert" state that `docs/07_WORKPACKAGES.md` tracks, because that file is the source of truth. `status: in-progress` and `status: needs-review` are abolished.

## Labels at issue creation

Exactly one type label (`feature`, `chore`, `docs`, `test`, `refactor`, `bug`), one `stufe-N`, and `priority: *` when it is not medium. Set them once, at creation. Relabeling mid-task is board churn by another name.

## Maintenance runs daily, never per commit

`.github/workflows/project-maintenance.yml` runs `scripts/project_report.py --fix` once a day. It sets items that are finished (issue `CLOSED`, PR `MERGED` - a PR closed without merge is not finished) but not on `Done` to `Done` - the gap the built-in workflows leave, since they never act retroactively - and reports everything else in the job summary. Archiving never runs automatically; finished work stays visible in the roadmap. What it cannot decide lands in one issue labelled `projektpflege` (`scripts/decision_issue.py`, written with the built-in `GITHUB_TOKEN` so the mention actually notifies). Mention only on new points; the issue closes itself when nothing is left. Read that issue at the start of `/project` - it is the to-do list.

Needs `PROJECT_TOKEN` as a secret and `PROJECT_NUMBER` (= 2) as a repo variable, both set up 2026-09-24 - the `GITHUB_TOKEN` cannot read account-owned projects.

## When writing is allowed

Only in `/project`, or inside `/gate` when the board has to reflect a passed gate. Maxi gave standing authorization (2026-09-24) for Claude to clean up and update the board itself there: mechanical fixes are applied directly, judgment calls (iterations, archiving, views) are proposed. Deleting a project, an item or a field is never covered. Everywhere else: read `docs/07_WORKPACKAGES.md` and `docs/01_STATUS.md`, which are the real source anyway.
