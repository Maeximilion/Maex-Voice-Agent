# 16 – GitHub Project

> The board on GitHub is a **derivation**, not manual work. It shows the state that issues and pull requests already have.
> Version 1.2 · 21.10.2026

---

## 1. The rule in one sentence

**During work, nobody touches the board.** The status moves through events that GitHub itself triggers; everything else shows up once a day in a report.

Why: a board that is written to by hand or by the agent in the middle of a task collects duplicates and noise. Every change costs context and moves no code forward. And an entry that stands on `Done` before anything was merged simply tells an untruth.

---

## 2. One project, no second one

**There is exactly one project.** Several boards for one repo and one person only create duplicates: the same item stands in two places and the two drift apart.

What looks like a second project is always a **view**. Views cost nothing, share the same data and cannot contradict each other:

| View | Type | Shows |
|---|---|---|
| Board | Board by status | The work flow: Todo, In progress, Done |
| Current iteration | Table | What is on right now |
| Backlog | Table, by priority | Open, not yet assigned to an iteration |
| Roadmap | Roadmap | The stages over time. Month or quarter is a setting of this view, not a separate project |
| In Review | Table | Pull requests waiting for review |

If a view is missing: create a view, never a project.

**The project is number 2, `Maex Voice-Agent`** (`https://github.com/users/Maeximilion/projects/2`). Merged on 24.09.2026: number 3 was a complete subset of 2 (77 issues, not a single entry of its own), numbers 1 and 4 were empty. All three are closed, not deleted - they can be reopened if anything is ever missing.

---

## 3. Fields

| Field | Type | Where the value comes from |
|---|---|---|
| Status | Single select | Built-in workflows (section 4). Never by hand |
| Stufe (stage) | Single select `stufe-0` to `stufe-5` | Label of the issue, set on creation |
| Art (kind) | Single select `feature`, `chore`, `docs`, `test`, `refactor` | Label of the issue, exactly one |
| Prioritaet (priority) | Single select `hoch` (high), `mittel` (medium), `niedrig` (low) | Label `priority: *` |
| Iteration | Iteration | Set during planning, not during work |

**Status lives only in the field, never additionally as a label.** Two places for the same truth always drift apart. The labels `status: in-progress` and `status: needs-review` are therefore abolished.

The one exception: `status: blocked`. `docs/07_WORKPACKAGES.md` lists "blocked" as a task status, and that file is the source of truth. The label mirrors it so that a blocked work package is visible even without the board.

Dropped and paused work (06.10.2026): a pull request cannot be deleted, only closed, so the list grows. Work that is dropped on purpose (a Python or Postgres major bump that stays out for compatibility, a feature that was decided against) gets the existing label `wontfix` on its closed pull request or issue. Work that is only paused is not a label: it stays an open issue on the board in `Todo`, because a second "parked" place would drift from the Status field. The pull request list is read with `is:pr is:open`.

---

## 4. How the status moves

Switched on once in the project settings under "Workflows", after that without further action:

| Event | Result |
|---|---|
| Issue or pull request opened | Entry is added, status `Todo` |
| Pull request merged | Status `Done` |
| Issue closed | Status `Done` |
| Entry reopened | Status `In Progress` |

So that the merge takes the linked issue along, **every** pull request description carries a keyword: `Closes #12`. Without it the merge closes only the pull request and the issue stays open.

`Done` arises exclusively here. Not earlier, not by hand, not through Claude Code.

---

## 5. The daily maintenance

`.github/workflows/project-maintenance.yml` runs once a day at 03:00 UTC and calls `scripts/project_report.py --fix`. The run corrects exactly one kind of error itself and reports everything else in the job summary.

**Corrected:** what is done (issue `CLOSED`, pull request `MERGED`) but the board does not show as `Done`. A pull request that was closed without a merge also has the state `CLOSED` but is not done - it is never set to `Done` automatically. This is the gap the built-in workflows leave, because they act only on new events and never retroactively. The correction is idempotent and only pulls the board toward the truth, never away from it.

**Filled in**, for pull requests and only into empty fields:

| Field | Value |
|---|---|
| Start date | Day the pull request was opened |
| Target date | Day of the merge; for one closed without a merge, the day it was closed |
| Iteration, Quarter | The period in which the pull request was opened - none if the day falls before or between the periods |

Days are counted in Berlin time. What was set by hand stays. Issues get none of this: for them the iteration is planning, not fact.

In addition, `scripts/pr_metadata.py` sets the repo owner as assignee on every pull request without one, and on every pull request without a label the type label from the title (`feat` -> `feature`, `fix` -> `bug`, `docs`, `chore`, `refactor`, `test`). Without a recognizable type the label stays empty - better none than a guessed one. Every open issue without an assignee gets the owner as well; its labels are set at creation, not derived. It runs when a pull request or an issue opens or reopens (`.github/workflows/pr-metadata.yml`), so a pull request opened without the flags of `/done` step 7 is complete within a minute, and again in the daily run for whatever slipped past.

**Only reported** is everything that needs a decision:

- open issues and pull requests of the repo that are missing on the board - in case the automatic intake missed something. The URL is compared, not the number
- open, but on `Done` - a reopened entry that the workflow did not reset
- pull request closed without a merge and not on `Done` - whether to archive or leave it is decided by `/project`
- archived, but open again - it is brought back, not created anew. Archived duplicates are excepted: if an active entry with the same URL is on the board, the copy stays in the archive. Archived entries count as present when matching against the repo, but not in any other check
- the same item twice on the board
- entries without a status
- entries in progress without an iteration (deliberately not the whole backlog, otherwise the report would list everything daily)
- `In Progress` for seven days without movement — reported, not moved

**Nothing is ever archived automatically.** For a portfolio, visible finished work in the roadmap is an asset and not ballast. Whoever wants to tidy up starts the run by hand with the `archive` switch; then `Done` entries that have not moved for fourteen days disappear - but only finished ones. An open entry on `Done` is never archived, and what the same run has just set to `Done` counts as freshly moved.

### The decision issue

Nobody reads the job summary on their own. So the run collects everything it cannot decide itself in exactly one issue with the label `projektpflege` (project maintenance, `scripts/decision_issue.py`):

- **Create** as soon as something is open - with a mention, so the notification arrives.
- **Update** every day, without a new mention. A mention happens only when a new item is added, and then only for that one.
- **Close** as soon as nothing is open any more - and **reopen** it at the next open item, with a mention. A second issue is never created.

Since T-0.11 (15.10.2026) the same issue also carries the section "README images": the run checks the images on `main` and the uploaded social preview first, without `PROJECT_TOKEN`, and hands its findings to `decision_issue.py --also`. When a findings file is missing because its step failed, the run is partial: it rewrites only the sections of the files it has and never closes the issue (`docs/15_README_STRATEGY.md`, "Keeping them current").

The issue is written by the built-in `GITHUB_TOKEN`, not the `PROJECT_TOKEN`. The reason is a quirk of GitHub: a mention that comes from your own account triggers no notification. If it comes from `github-actions`, it does.

### Setup

The `GITHUB_TOKEN` of an action may not read account-owned projects. One-time setup:

1. Create a token, either
   - classic at `https://github.com/settings/tokens/new`, scopes **`project`** and **`repo`** (simplest way), or
   - fine-grained, resource owner = own account, repository access = this repo, then under **Account permissions** **Projects: Read and write** and under **Repository permissions** **Issues** and **Pull requests**, each read-only

   The repo permission is not an extra: without it GitHub returns every board entry of a private repo with empty content, so without number and state. The script then aborts instead of silently doing nothing (first run 24.09.2026).
2. Store it in the repo as the secret `PROJECT_TOKEN`
3. Store the number from the project URL in the repo as the variable `PROJECT_NUMBER`

Both have been set up since 24.09.2026 (`PROJECT_NUMBER` = 2). The repo for the comparison of open entries is read by the run in the action from `GITHUB_REPOSITORY`; locally it comes from `PROJECT_REPO`. If both are missing, the report says explicitly that it did not look for missing entries. If the token expires, the daily run turns red - this is intended, a silent pause would be worse.

The same run locally:

```bash
PROJECT_TOKEN=$(gh auth token) PROJECT_OWNER=Maeximilion PROJECT_NUMBER=2 PROJECT_REPO=Maeximilion/Maex-Voice-Agent python scripts/project_report.py
```

---

## 6. What Claude Code may do

| | Allowed |
|---|---|
| During `/task`, `/done`, `/bug` | Nothing. No `gh project`, neither reading nor writing |
| When creating an issue | Set labels: kind, stage, priority |
| When opening a pull request | `Closes #N` in the description, branch name with the issue number |
| On `/project` or in `/gate` | Read and maintain the board. Mechanical corrections (status after merge, missing entries, duplicates - the surplus one is archived, never deleted) directly, matters of judgment (iteration, archive, new views) as a proposal |

The basis is Maxi's standing approval of 24.09.2026: Claude tidies the board itself and keeps it current - but only outside work on a task. Deleting projects, entries or fields stays excluded.

The connection between work and board arises through keywords and branch names, not through API calls. The branch name carries the number: `task/T-4.3-search-menu` or `fix/12-short-description`.

---

## 7. Related files

| File | Relation |
|---|---|
| `docs/07_WORKPACKAGES.md` | Source of truth for tasks and dependencies. The board mirrors it |
| `docs/01_STATUS.md` | Where the project stands. Not the board |
| `scripts/project_report.py` | The daily check |
| `.github/workflows/project-maintenance.yml` | The daily run |
| `.claude/skills/github-project/SKILL.md` | The same rule for Claude Code |
