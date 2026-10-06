# AGENTS.md

Instructions for automated agents that review or change this repository. The working rules for
Claude Code are in `CLAUDE.md`; this file is what a reviewer needs. Where the two differ,
`CLAUDE.md` wins.

## Code Review Rules

How to review:

- Report a finding only when you can name a concrete failing input or sequence. No scenario, no finding.
- Do not report style, naming, formatting or personal preference: `ruff` and the CI cover those.
- A finding that was answered with a commit is not reported again in other words. If the same spot
  produces a new finding after a fix, check whether the root cause was fixed before reporting.
- Severity: P0 and P1 are security, secrets, data loss, a broken hard rule, or a broken `main`.
  P2 is a concrete defect with a scenario. Anything below that is not posted.

### Security and secrets (this repository is public)

- The real name of the pilot restaurant, of its company or location, or of its telephony and
  cash-register providers, a real phone number or address, a credential, token, call recording,
  transcript or customer datum anywhere (file, test, fixture, doc, commit message, PR text) is P0.
  These names are placeholders: `<Pilotbetrieb>`, `<Firmenname>`, `<Ort>`, `<Kassensystem>`,
  `<Kassenanbieter>`, `example.com`.
- Names of software, tools and vendors the project uses or documents (GitHub, OpenAI, Anthropic,
  n8n, PostgreSQL, Ollama) are not restricted and are not findings.
- Secrets come from the environment only; `.env` is never committed.
- A route under `/v1/tools` or `/v1/kitchen` that answers without its token check (`require_token` with `AGENT_API_TOKEN`, `require_kitchen_token`) is P1. `/health` is open by design.

### Hard rules and dialog (`CLAUDE.md` section 2, `docs/08_EVALS.md`)

- Prices, delivery zones, opening hours, availability and allergens come from the database only. A
  hard-coded value, or one placed in a prompt, is P1.
- No order item without a definitive `menu_item_id`; code that picks a "best guess" item is P1.
- A transaction leaves `draft` only through `confirm` after an explicit customer "yes".
- A new failure branch in the call path must end with a transfer to the team or a callback, never
  with a dropped call.
- The full menu is never written into a prompt or a model context.
- A change to `prompts/`, `api/agent/` or a tool's dialogue behaviour needs the eval numbers
  (`make eval`) in "How Tested"; without them it is P2.

### Data and migrations

- Money is integer cents; a float anywhere in a money path is P1.
- Phone numbers are normalised to E.164 at entry; times are stored in UTC and shown in `Europe/Berlin`.
- Every write carries a `call_id`; write tools are idempotent (same `idempotency_key`, same result,
  no second transaction).
- A schema change comes with an Alembic migration that has a working downgrade and a test for up and
  down. A model change without a migration is P1.
- A migration that touches production data names the backup (`scripts/backup.sh`) it relies on.

### Architecture (`docs/11_MODULE.md`)

- Dependency direction: `tools`, `gui`, `sim`, `telephony` -> `agent` -> `domain` -> `models`, `core`.
  `domain/` imports no FastAPI, no HTTP client, no provider.
- Business logic in `tools/` or `gui/` instead of `domain/` is P2.
- A provider name outside `api/telephony/` (planned with the telephony adapter, not built yet) is P2.
- n8n and external APIs are reached through the outbox, never directly from `domain/`.
- Errors reach the agent as structured JSON, never as a stack trace.

### Shell scripts and operations (`scripts/*.sh`, `deploy/`, `Makefile`)

- Check every pipeline under `set -euo pipefail`: a reader that stops early can kill the writer with
  SIGPIPE (status 141) and fail a good run. An `exit` inside a brace group that is the last element
  of a pipeline ends the whole script when `lastpipe` is on; a subshell `( ... )` does not.
- A backup counts only after it was read back to the end; it never overwrites an existing file and is
  encrypted unless the caller says otherwise.
- Commands in docs and handovers must run as written: no `<placeholder>` inside a command, line
  continuations where needed, example dates on an open day (Monday is closed in the seed data).

### Containers and deployment (`docker-compose*.yml`, `deploy/`)

- Image tags are pinned, never `latest`. A pin below the version that fixes a published advisory for
  that image is a finding (check the advisory, do not guess).
- Dev host ports bind to loopback only; production publishes only the reverse proxy.
- PostgreSQL stays on major version 16; a major bump needs a dump and restore plan.

### CI and workflows (`.github/workflows/`)

- `permissions:` is the least that works; no secrets in a workflow that runs for fork pull requests;
  no `pull_request_target` that checks out pull request code.
- A change that adds a self-hosted runner, or lets code from a fork pull request run on one, is P0
  (the repository is public).
- A test is never skipped, disabled or quarantined to get green; a skip needs a platform reason
  written in the test.

### Tests

- A bug fix comes with a test that failed before it. A test that claims to prove a race or a timing
  case must be deterministic or say why it cannot be.
- A hot-path tool states its measured response time (budget: under 300 ms with a local database).

### Docs and pull requests

- Everything written for the repository is English. German only in what the agent says to callers
  (`say` texts, prompts), in caller sentences in `evals/`, and in caller sentences used as test input
  in `api/tests/` for code that parses German speech; test names, docstrings and comments around that
  input are English. A German caller sentence in a test is not a finding. Report a language slip at
  most once per pull request and only as P2.
- Changing `docs/07_WORKPACKAGES.md` requires `docs/01_STATUS.md` to change too (the CI checks it);
  the status version moves through `scripts/status_bump.py`.
- Pull request titles follow Conventional Commits (the title becomes the commit message on `main`).
  A "Generated with" footer in a pull request text is not accepted.
