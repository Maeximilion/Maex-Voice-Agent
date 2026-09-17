# CLAUDE.md – Working Instructions for Claude Code

> Read this file first, then `docs/01_STATUS.md`. After that, you'll know where the project stands and what's next.
> Version 1.1 · 16.09.2026

---

## 1. What We're Building

An AI agent answers calls on the restaurant **<Pilotbetrieb>** (<Ort>)'s landline and handles **reservations, pickups, and deliveries**. Complaints and edge cases escalate to a human. The team controls everything via a browser GUI on a tablet.

**Division of Labor:** An external voice platform handles telephony, speech recognition, and voice synthesis. We build the **logic, database, and interface**. The agent calls our tools via HTTPS.

**Placeholders:** Operation, company, location, and provider names appear in code and docs as `<Pilotbetrieb>`, `<Firmenname>`, `<Ort>`, `<Kassensystem>`, `<Kassenanbieter>`, and `example.com`. Real values come from `.env` and the database, never into the repo. No emojis in code.

---

## 2. Hard Rules (Non-Negotiable)

These six rules override any convenience. If a task violates them: **stop and ask**.

1. **AI understands, code decides.** Prices, zones, hours, availability, and allergens come **only** from the database. Never from the model, never hardcoded, never estimated.
2. **Never guess.** Without a definitive `menu_item_id`, no item enters the order. On uncertainty, follow the understanding ladder (`docs/05_DIALOG_PROMPTS.md`); escalate to human at the end.
3. **Nothing without confirmation.** No transaction leaves draft status without explicit customer "yes". `confirm` is the only path from `draft` to `confirmed`.
4. **Errors are measured.** Before every merge, evals run (`docs/08_EVALS.md`). No gate without numbers.
5. **Every outage ends with the team.** If anything breaks, the phone rings. No call is lost.
6. **Token-efficient by design.** Smallest context, smallest model that passes evals. Full menu never in the prompt.

---

## 3. Stack

| Layer | Technology | Status |
|---|---|---|
| Language | Python 3.12 | Default |
| API (hot path) | FastAPI + Uvicorn, Pydantic v2 | Default |
| DB | PostgreSQL 16, SQLAlchemy 2 + Alembic | Default |
| GUI | FastAPI + Jinja2 + HTMX + SSE, Pico.css | Default, Alternative: React + Vite |
| Automation (cold path) | n8n, self-hosted via Docker | Set |
| Tests | pytest, pytest-asyncio, httpx | Default |
| Lint/Format | ruff (Format + Lint), mypy non-strict mode | Default |
| Operations | Docker Compose, Hosting in EU | Set |
| Voice Platform | Open → decided in workpackage C2 | Open |

"Default" = suggestion, can change. If Maxi disagrees, this and `docs/01_STATUS.md` get updated.

**Why HTMX over React:** one container instead of two, no Node build, no CORS, live updates via Server-Sent-Events. Sufficient for a tablet UI with lists and large buttons. Rationale in `docs/06_GUI.md`.

---

## 4. Folder Structure

The layout is modular, organized by layers with fixed dependency direction. Fully documented with rationale and blueprint: `docs/11_MODULE.md`. **Before creating any new file, check there to see where it belongs.**

```text
maex-voice-agent/
├── CLAUDE.md              ← this file
├── README.md
├── docker-compose.yml     Postgres · API · n8n (dev)
├── deploy/                Prod Compose, Caddyfile
├── .claude/commands/      /start /task /done /bug /eval /handover
├── .github/workflows/     CI: ruff + pytest
├── docs/                  Planning, specs, status  →  Section 5
├── api/
│   ├── main.py            App entry, router
│   ├── config.py          Settings from .env
│   ├── db.py              Engine, session
│   ├── core/              Response envelope, errors, logging, auth, IDs, time
│   ├── domain/            Business logic without HTTP: menu · ordering · reservations
│   │                      · delivery · customers · status · callbacks
│   ├── agent/             Conversation loop, state, ladder, escalation
│   ├── telephony/         Port + adapter per provider – ONLY place that knows it
│   ├── tools/             Thin HTTP wrapper /v1/tools/* around domain
│   ├── events/            Outbox + dispatcher → n8n
│   ├── jobs/              Cleanup, sync, daily report, holidays
│   ├── gui/               Router, SSE, templates, static
│   ├── models/  schemas/  SQLAlchemy · Pydantic
│   └── tests/
├── sim/                   Text telephone: conversations without phone
├── db/migrations/         Alembic, versioned
├── prompts/               System prompt per version
├── evals/                 cases/ · runner.py · reports/ (ignored)
├── n8n/                   Workflow exports
└── scripts/               seed · import_menu · backup · restore
```

**Dependency direction, short:** `tools`/`gui`/`sim`/`telephony` → `agent` → `domain` → `models`/`core`. Never reversed. `domain/` imports no FastAPI, no HTTP, no provider.

---

## 5. The Documents

| File | Content | When to Read |
|---|---|---|
| `docs/00_PCF.md` | Project master plan, stages, gates, risks | once at startup |
| `docs/01_STATUS.md` | **Where we stand, what's next** | at start of every session |
| `docs/02_ARCHITECTURE.md` | Components, hot/cold path, call flow, failure behavior | before work on API or n8n |
| `docs/03_DATA_MODEL.md` | Tables, fields, DDL per stage | before any migration |
| `docs/04_API_TOOLS.md` | Contract per tool: request, response, errors, latency budget | before work on `api/tools/` |
| `docs/05_DIALOG_PROMPTS.md` | Conversation flows, system prompt, understanding ladder, escalation | before work on `prompts/` |
| `docs/06_GUI.md` | Screens, components, states, interaction rules | before work on `gui/` |
| `docs/07_WORKPACKAGES.md` | **Task list T-x.y with dependencies and definition of done** | for task selection |
| `docs/08_EVALS.md` | Test case format, metrics, regression run | before every merge |
| `docs/09_OPERATIONS_LEGAL.md` | Runbook, emergencies, legal checklist | before every go-live step |
| `docs/11_MODULE.md` | **Layers, dependency rules, build plan per module, tests per module** | before any new file |
| `docs/12_CLAUDE_CODE_PLAYBOOKS.md` | Nine session workflows (feature, bug, migration, prompt, import, adapter, deploy …) | at session start, per situation |
| `docs/13_DEPLOYMENT.md` | Tunnel for test calls, EU server, Caddy, backups, CI | before first test call |
| `docs/14_MENU_IMPORT_FORMAT.md` | CSV contract between chat (digitization) and import | before T-4.2 |
| `docs/15_README_STRATEGY.md` | When and how to maintain README and CHANGELOG, versioning per gate | at every gate, with new dependencies |

---

## 6. How You Work

### Slash Commands (`.claude/commands/`)
`/start` begin session · `/task T-x.y` build task · `/done` close out · `/bug "…"` error with red eval case first · `/eval` run and assess suite · `/gate Gx` close gate, sync README and version · `/handover` handover block. Detailed workflows: `docs/12_CLAUDE_CODE_PLAYBOOKS.md`.

### Session Start
1. Read `docs/01_STATUS.md` → current stage and open tasks
2. `docs/07_WORKPACKAGES.md` → choose next task with satisfied dependencies
3. Read the spec for the task (column "Spec" in task list)
4. Show brief plan (max 5 lines), then build

### The Loop
**Plan → Build → Execute → Assess → Iterate.** After each task, independently take up to **3 follow-up steps** toward the goal (add tests, close obvious gaps, sync docs), then report results. Larger scope expansions only as a proposal.

### Questions
Ask closed questions (yes/no or A/B/C with marked recommendation), **one per interruption**, and only when the answer is needed. Research answerable questions yourself. Mark assumptions and write them to `docs/01_STATUS.md`.

**Make decisions with confidence** (Maxi, 2026-09-16): show plan, state recommendation, build. Wait only for matters of money, law, external impact, production data, or irreversibility (§10).

### Session End
Update `docs/01_STATUS.md`: completed tasks, new insights, next step. Add a handover block per `docs/00_PCF.md` section 12.

---

## 7. Definition of Done

A task is complete when **all** of these are true:

- [ ] Code runs, was **actually executed**, not just written
- [ ] Tests for normal case **and** at least two edge cases, green
- [ ] `ruff format` and `ruff check` clean
- [ ] For tools in hot path: response time measured, < 300 ms with local DB
- [ ] Schema change as Alembic migration, up **and** down tested
- [ ] Affected docs in `docs/` synced
- [ ] README updated if quickstart, requirements, config, or known issues changed (`docs/15_README_STRATEGY.md`)
- [ ] `docs/01_STATUS.md` updated
- [ ] Commit follows Conventional Commits, `main` stays runnable

---

## 8. Conventions

**Code**
- Prices **always** as integer cents. No float for money, anywhere.
- Phone numbers in E.164 format (`+4972215551234`), normalized at entry.
- Store times in UTC, display in `Europe/Berlin`.
- Every transaction carries a `call_id`. No write without `call_id`.
- Write tools are idempotent: same `idempotency_key` → same result, no duplicate transaction.
- Errors return structured JSON, never stack trace to the agent.
- German comments and error messages, English identifiers in code.
- No emojis in code, docs, commits, or UI. Status expressed in words.

**Git**
- Conventional Commits: `feat(tools): check_delivery with polygon validation`
- One commit = one completed task
- `main` always stays runnable
- No "🤖 Generated with Claude Code" badge/footer in PR descriptions or elsewhere in repo (README, docs, files)

**Security**
- `.env`, real recordings, transcripts, and customer data **never** go in repo
- API keys only via environment variables
- Agent API accessible only with token (`AGENT_API_TOKEN`)
- Before any production migration: backup (`scripts/backup.sh`)

---

## 9. Never Do This

- Hard-code prices, hours, delivery zones, or allergens in code or prompt
- Book a transaction final without explicit customer confirmation
- Give allergen information not maintained as a DB value
- Process real call recordings before legal check in `docs/09_OPERATIONS_LEGAL.md` is ticked
- Migrate production data without backup
- Skip tests as "already works" or claim green without running them
- Write the entire menu into the system prompt
- Put domain logic in `tools/` or `gui/` instead of `domain/`
- Use a provider name outside `telephony/`
- Call n8n or external API directly from `domain/` (always via outbox)
- Fix a bug without first having a red eval or unit test for it

---

## 10. What Runs in Claude Code and What Stays in Chat

| In Claude Code | In Claude Chats (see `docs/00_PCF.md` section 10) |
|---|---|
| API, tools, data model, migrations | C1 current state capture, baseline, legal check |
| GUI | C2 provider research and selection |
| Eval runner and test cases | Negotiations, contracts, budget decisions |
| n8n workflows as code export | Gate decisions with Maxi |
| Import and ops scripts | Decisions involving money or law |

Anything that costs money, triggers a contract, or binds us legally → Maxi decides in chat. You build.
