# 16 – GitHub Project

> The board on GitHub is a **derivation**, not manual work. It shows the state that issues and pull requests already have.
> Version 1.3 · 02.11.2026

---

## 1. The rule in one sentence

**During work, nobody touches the board.** The status moves through events that GitHub itself triggers; everything else shows up once a day in a report.

Why: a board that is written to by hand or by the agent in the middle of a task collects duplicates and noise. Every change costs context and moves no code forward. And an entry that stands on `Done` before anything was merged simply tells an untruth.

---

## 2. One project, no second one

**There is exactly one project.** Several boards for one repo and one person only create duplicates: the same item stands in two places and the two drift apart.

What looks like a second project is always a **view**. Views cost nothing, share the same data and cannot contradict each other:

| View | Type | Filter | Shows |
|---|---|---|---|
| Monthly roadmap | Roadmap, grouped by Milestone | `is:issue` | The issues over time, one collapsible block per gate |
| Quarterly roadmap | Roadmap, grouped by Milestone | `is:issue` | The same over a longer range |
| Backlog | Table, grouped by Milestone | `is:issue is:open` | The open issues per gate; a block folds out into its sub-issues |
| Board | Board by Status | `-closed:<@today-3d` | Everything open plus what closed in the last three days: Todo, Blocked, In progress, In review, Done |

The REST API can create a view but not change one, and the group of a view is set only in the web interface. A view takes one level of grouping, so a roadmap groups by gate or by parent issue, not by both.

If a view is missing: create a view, never a project.

**The project is number 2, `Maex Voice-Agent`** (`https://github.com/users/Maeximilion/projects/2`). Merged on 24.09.2026: number 3 was a complete subset of 2 (77 issues, not a single entry of its own), numbers 1 and 4 were empty. All three are closed, not deleted - they can be reopened if anything is ever missing.

---

## 3. Fields

| Field | Type | Where the value comes from |
|---|---|---|
| Status | Single select `Todo`, `Blocked`, `In progress`, `In review`, `Done` | Section 4. Never by hand |
| Milestone | The gates G0 to G5, and `Features`, `Bugs`, `Etcetera` for pull requests that belong to no gate | `scripts/pr_metadata.py`, section 5. Set by hand it stays: the label is only the default, a task a gate waits for gets that gate by hand (T-10.1 and T-10.2 carry `stufe-1` and belong to G0) |
| Iteration, Quarter | Iteration | The daily run (section 5) |
| Start date, Target date | Date | The daily run (section 5) |
| Team | Single select, GitHub's default values | Empty on every entry, not used |

Stage, kind and priority are labels only (`stufe-N`, the type label, `priority: *`), set on creation. They are no fields of the project, and a label cannot be grouped by.

Add a Status column in the web interface, never with `updateProjectV2Field`: that call replaces the whole option list, and a wrong id would take the status of every entry with it.

**Status lives only in the field, never additionally as a label.** Two places for the same truth always drift apart. The labels `status: in-progress` and `status: needs-review` are therefore abolished.

The one exception: `status: blocked`. `docs/07_WORKPACKAGES.md` lists "blocked" as a task status, and that file is the source of truth. The label mirrors it so that a blocked work package is visible even without the board, and the column `Blocked` follows the label.

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

On top of them, `.github/workflows/project-sync.yml` runs `scripts/project_report.py --fix` on every pull request event (opened, reopened, closed, edited, ready for review) and every issue opened, reopened, closed, labeled or unlabeled (Maxi, 2026-10-28, 2026-11-02):

| Event | Result |
|---|---|
| Pull request opened | Status `In review`, start date and iteration set |
| Pull request with `Closes #12` opened | Issue #12 `In progress`, its start date and iteration from that pull request |
| Label `status: blocked` set, on an open issue or pull request | Status `Blocked`, whatever else is true of it |
| Label removed | Back to what the facts say: `In review`, `In progress`, or `Todo` for an issue without an open pull request |
| Pull request merged, issue closed | Status `Done`, target date set |

So that the merge takes the linked issue along, **every** pull request description carries a keyword: `Closes #12`. Without it the merge closes only the pull request and the issue stays open.

`Done` arises exclusively here. Not earlier, not by hand, not through Claude Code.

---

## 5. The daily maintenance

`.github/workflows/project-maintenance.yml` runs once a day at 03:00 UTC and calls `scripts/project_report.py --fix`. The run corrects exactly one kind of error itself and reports everything else in the job summary.

**Corrected:** what is done (issue `CLOSED`, pull request `MERGED`) but the board does not show as `Done`. A pull request that was closed without a merge also has the state `CLOSED` but is not done - it is never set to `Done` automatically. This is the gap the built-in workflows leave, because they act only on new events and never retroactively. The correction is idempotent and only pulls the board toward the truth, never away from it.

**Set from the facts** (`wanted_status`): open and labeled `status: blocked` to `Blocked`, an open pull request to `In review`, an open issue that an open pull request closes (`Closes #N`) to `In progress`. An issue someone put on `In progress` by hand stays. An open item on `Done` is reported, not moved.

**Filled in**, only into empty fields, for pull requests and for issues someone works on:

| Field | Value |
|---|---|
| Start date | Day the pull request was opened; for an issue, the day the first pull request that closes it was opened |
| Target date | Day of the merge (a pull request closed without a merge: the day it was closed); for an issue, the day it was closed |
| Iteration, Quarter | The period that contains the start date - none if the day falls before or between the periods |

Days are counted in Berlin time. What was set by hand stays. An issue without a pull request that closes it gets none of this: its dates would be a guess. That is why every pull request for a task issue says `Closes #N` (`/done` step 7).

**Iterations roll on:** GitHub does not add iterations by itself (Iteration 3 ended on 2026-10-27 with none after it, 2026-10-28). The run appends iterations of the same length until one covers the day 28 days ahead; existing ones keep their ids, so the values on items stay.

In addition, `scripts/pr_metadata.py` sets the repo owner as assignee on every pull request without one, and on every pull request without a label the type label from the title (`feat` -> `feature`, `fix` -> `bug`, `docs`, `chore`, `refactor`, `test`). Without a recognizable type the label stays empty - better none than a guessed one. Every open issue without an assignee gets the owner as well; its labels are set at creation, not derived. Every issue without a milestone, open or closed, gets the gate of its `stufe-N` label (`stufe-1` to `G1 ...`); one without such a label stays in the group "No Milestone" of the roadmap. Every pull request without a milestone takes the gate of the issue it closes, else of the task in its title or branch name (`docs/07_WORKPACKAGES.md`, a task without an issue counts through the group issue of its block), else `Features` for `feat`, `Bugs` for `fix` and `Etcetera` for the rest. A pull request in `Features`, `Bugs` or `Etcetera` is never part of a gate, and the roadmaps show issues only. It runs when a pull request or an issue opens or reopens (`.github/workflows/pr-metadata.yml`), so a pull request opened without the flags of `/done` step 7 is complete within a minute, and again in the daily run for whatever slipped past.

**Only reported** is everything that needs a decision:

- open issues and pull requests of the repo that are missing on the board - in case the automatic intake missed something. The URL is compared, not the number
- open, but on `Done` - a reopened entry that the workflow did not reset
- pull request closed without a merge and not on `Done` - whether to archive or leave it is decided by `/project`
- archived, but open again - it is brought back, not created anew. Archived duplicates are excepted: if an active entry with the same URL is on the board, the copy stays in the archive. Archived entries count as present when matching against the repo, but not in any other check
- the same item twice on the board
- entries without a status
- entries in progress or in review without an iteration (deliberately not the whole backlog, otherwise the report would list everything daily)
- `In progress` or `In review` for seven days without movement — reported, not moved

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
