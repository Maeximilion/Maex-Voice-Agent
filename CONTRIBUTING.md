# Contributing

Quick guide for everyone working on this repository. Domain rules are in `CLAUDE.md`, project status in `docs/01_STATUS.md`, task list in `docs/07_WORKPACKAGES.md`.

## Development Environment

```bash
git clone https://github.com/Maeximilion/Maex-Voice-Agent.git
cd Maex-Voice-Agent
cp .env.example .env        # Enter credentials, file stays local
make up                     # Start Postgres, API, n8n
make migrate && make seed   # Create schema, load test config
make test                   # Run suite against real database
```

Requirements: Docker with Compose, Python 3.12 for runs outside containers.

## Workflow for a Change

1. Choose a task from `docs/07_WORKPACKAGES.md` whose dependencies are done. Each task has an issue (column "Issue").
2. Branch off current `main`, include issue number in name: `feature/27-confirm-tool`, `fix/31-readback-date`, `docs/44-ui-test`.
3. Build while following Definition of Done in `CLAUDE.md` §7. Errors get a red test first, then the fix.
4. Get `make lint` and `make test` green locally before pushing.
5. Open pull request, link issue (`Closes #27`), wait for CI.
6. Incorporate review, then squash-merge.

## Commits

Conventional Commits, English description, one commit per completed task:

```
feat(tools): confirm with outbox entry and audit_log
fix(reservations): overbooking on concurrent calls
docs(status): T-1.6 complete
test(evals): edge cases for zones outside service area
```

Types used: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`. Scope in brackets is the affected module (`tools`, `domain`, `gui`, `db`, `events`).

## Pull Requests

- One PR covers one topic. No catch-all PRs across multiple tasks.
- Title in Conventional Commits format, since squash-merge makes it the commit message on `main`.
- Description per template in `.github/PULL_REQUEST_TEMPLATE.md`: what changed, why, how tested, which issue closes.
- `main` always stays runnable. Never push directly to `main`.
- Before merge: `ruff check`, `ruff format --check`, and test suite run in CI. For dialog changes, also evals (`docs/08_EVALS.md`).
- Which lint rules apply is in `pyproject.toml` under `[tool.ruff.lint]`, not the default set of the ruff version. A new rule is intentionally added, an unsuitable one intentionally excluded; a version update alone doesn't change the rule set.

## Labels

| Category | Labels |
|---|---|
| Type | `feature`, `enhancement`, `bug`, `docs`, `refactor`, `chore`, `test` |
| Priority | `priority: high`, `priority: medium`, `priority: low` |
| Status | `status: blocked`, `status: in-progress`, `status: needs-review` |
| Classification | `block` (collection issue), `stage-0` through `stage-5`, `deployment` |

Each issue carries exactly one type label. Priority and status only when they actually change the state.

## Code Style

- `ruff` decides on format and linting, config in repository.
- English identifiers, German comments and error messages.
- Comments explain the why, not the what. No emojis in code, docs, commits, or UI.
- Money always as integer cents, phone numbers in E.164, times stored UTC and displayed in local time.
- Domain logic belongs in `api/domain/`, never in `api/tools/` or `api/gui/`. Dependency direction is in `docs/11_MODULES.md`.

## What Never Goes in the Repository

`.env`, credentials, real call recordings, transcripts, and customer data. Pilot operation, location, and provider names appear as placeholders in angle brackets; real values come from environment and database.

Please don't report security issues as issues; see `SECURITY.md` instead.
