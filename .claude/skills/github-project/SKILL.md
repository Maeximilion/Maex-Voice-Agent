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
- branch name carrying the issue number: `task/T-4.3-search-menu`, `fix/12-short-description`
- labels set once, when the issue is created

## One project

There is exactly one project. A missing perspective is a **view**, never a second project. Never create a project. Never add the same issue twice.

## Status

Status lives in the project's Status field and nowhere else - never additionally as a label. It moves through GitHub's built-in workflows and the `project-sync` workflow on every pull request and issue event (`docs/16_GITHUB_PROJECT.md` section 4):

| Event | Status |
|---|---|
| Issue opened | Todo |
| Pull request opened | In progress |
| Pull request with `Closes #N` opened | Issue #N In progress |
| Pull request merged | Done |
| Issue closed | Done |
| Item reopened | In Progress |

`Done` comes from the merge. Never set it by hand, never before the merge.

Exception in the label set: `status: blocked` mirrors the blocked state that `docs/07_WORKPACKAGES.md` tracks, because that file is the source of truth. `status: in-progress` and `status: needs-review` are abolished.

## Labels and assignee at creation

Issues: assignee `@me`, exactly one type label (`feature`, `chore`, `docs`, `test`, `refactor`, `bug`), one `stufe-N`, and `priority: *` when it is not medium. Set them once, at creation. Relabeling mid-task is board churn by another name.

Pull requests: always `gh pr create --assignee @me --label feature`, where `feature` is replaced by the type label matching the Conventional Commit prefix of the title (`feat` -> `feature`, `fix` -> `bug`, `docs`, `chore`, `refactor`, `test`). The workflow `pr-metadata` fills in what is still missing when the pull request opens, and the daily run catches the rest, but a pull request should be complete from the start.

## Maintenance runs on events and daily, never from a session

`.github/workflows/project-sync.yml` runs `scripts/project_report.py --fix` on every pull request and issue event, `.github/workflows/project-maintenance.yml` once a day. Besides status it fills start date, target date, iteration and quarter from facts and appends iterations before the last one ends. The daily run It sets items that are finished (issue `CLOSED`, PR `MERGED` - a PR closed without merge is not finished) but not on `Done` to `Done` - the gap the built-in workflows leave, since they never act retroactively - and reports everything else in the job summary. Archiving never runs automatically; finished work stays visible in the roadmap. What it cannot decide lands in one issue labelled `projektpflege` (`scripts/decision_issue.py`, written with the built-in `GITHUB_TOKEN` so the mention actually notifies). Mention only on new points; the issue closes itself when nothing is left. Read that issue at the start of `/project` - it is the to-do list.

Needs `PROJECT_TOKEN` as a secret and `PROJECT_NUMBER` (= 2) as a repo variable, both set up 2026-09-24 - the `GITHUB_TOKEN` cannot read account-owned projects.

## When writing is allowed

Only in `/project`, or inside `/gate` when the board has to reflect a passed gate. Maxi gave standing authorization (2026-09-24) for Claude to clean up and update the board itself there: mechanical fixes are applied directly, judgment calls (iterations, archiving, views) are proposed. Deleting a project, an item or a field is never covered. Everywhere else: read `docs/07_WORKPACKAGES.md` and `docs/01_STATUS.md`, which are the real source anyway.
