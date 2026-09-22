# Maex Voice Agent

AI-powered phone intake for hospitality. An AI agent answers calls on the restaurant's main line, handles reservations, pickup, and delivery, and hands off confirmed transactions to kitchen, register, and team. Complaints and special cases escalate to a human. The team manages operations via a browser interface on a tablet.

Telephony, speech recognition, and voice output run on an EU-hosted provider. This repository contains the domain logic, database, interface, and tests. Pilot operation, location, domain, and point-of-sale provider appear in docs as placeholders in angle brackets.

[![CI](https://github.com/Maeximilion/Maex-Voice-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Maeximilion/Maex-Voice-Agent/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![PostgreSQL 16](https://img.shields.io/badge/postgresql-16-blue)](https://www.postgresql.org/)
[![License](https://img.shields.io/badge/license-proprietary-lightgrey)](#license-and-contact)

## Status

| | |
|---|---|
| Version | 0.0.1 |
| Stage | 0, Foundation |
| Next Gate | G0: Provider, legal, budget clarified |
| As of | 2026-09-17 |

**What works:**

- `make up` builds the API image and starts Postgres, API, dispatcher, and n8n; `/health` responds, token auth works
- Every agent API response follows the envelope from `docs/04_API_TOOLS.md`; errors return JSON with code and read-aloud text, never stacktrace
- Database access with one session per request (`api/db.py`), logs as JSON lines with `request_id` and `call_id`
- `make migrate` creates the stage-1 tables and the stage-2 tables for menu and orders (Alembic in `db/`, models in `api/models/`), `make seed` fills stage 1 idempotently with test config
- Menu import from CSV per `docs/14_MENU_IMPORT_FORMAT.md`: `python -m scripts.import_menu imports/ --dry-run` checks and reports, without `--dry-run` it loads; idempotent, price changes only with `--apply-price-changes`. The CSVs live in `imports/`, which is git-ignored
- Reservation flows end-to-end, every tool latency-tested against 300 ms budget: `POST /v1/tools/get_service_status` answers from DB whether and what's available (hours, special days, wait times, mode); `POST /v1/tools/check_slot` checks a request against capacity and hours, offers up to two alternatives; `POST /v1/tools/create_reservation` creates draft with read-aloud text; `POST /v1/tools/confirm` makes it final, logs it, puts event for cold path in outbox
- Dispatcher (`api/events/`) drains outbox to n8n: separate process (`python -m api.events.dispatcher`), one POST per event with event-id as idempotency key, backoff 5 s / 30 s / 2 min / 10 min, then `failed` with alarm in log
- `POST /v1/tools/create_callback` creates a callback task for the team when agent is stuck: task in DB, log entry, event for cold path; at most one open callback per call
- Conversation core (`api/agent/`) with understanding ladder and escalation, driven from the text phone (`sim/`): `python -m sim.cli` runs a call in the terminal, `python -m sim.replay <case>` replays a transcript; a confirmed reservation lands in the database without any telephony
- Operations view for the tablet at `/gui/` (`api/gui/`): header with mode, delivery and wait times and the buttons to pause the AI, switch delivery and raise the wait time; the column "Heute" with the confirmed reservations of the business day and the column "Rückrufe" with open callbacks, a tone for new ones and a done button. It updates itself over Server-Sent-Events, so a call held in `sim/` shows up on every tablet a moment later without reloading
- `make test` and `make lint` run in container against real Postgres, ruff clean
- CI additionally builds API image without local CA cert and checks startup, `/health`, and token auth (missing, wrong, valid) without bind mount
- Specs for architecture, data model, tools, dialog, GUI, and evals are in `docs/`

**Not yet working:**

- No phone line, no provider chosen (decision D1)
- The column "Neue Bestellungen" stays empty until T-4.7, and the admin view (`docs/06_GUI.md` §4) is still only a mockup
- No orders yet: the schema is in place (migration 002), the menu can be imported, searched (`POST /v1/tools/search_menu`) and asked about (`POST /v1/tools/get_item_details`), but `draft_order` and the order confirmation follow in T-4.5
- No real menu data yet: the CSVs come from the chat digitization (C1)
- The conversation core runs against a rule-based stand-in for the model (`sim/scripted_llm.py`); a real model with token counting follows in T-2.4
- No n8n workflow yet to receive dispatcher events
- Test config from `make seed` (hours, capacity) is placeholder until actual ops capture arrives

## Requirements

- Docker and Docker Compose
- Git
- For development without Docker: Python 3.12

## Quick Start

```bash
git clone <repo-url> maex-voice-agent
cd maex-voice-agent
cp .env.example .env
make up
make migrate
make seed
curl http://localhost:8000/health
make test
```

API then runs at `http://localhost:8000`, the operations view at `http://localhost:8000/gui/`, n8n at `http://localhost:5678`. `/health` responds with `{"status": "ok", "env": "dev"}`.

Tests and lint from a local Python environment. Database tests need reachable Postgres, e.g. from `make up`; `DATABASE_URL` then points to `localhost` instead of container name `db`:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r api/requirements.txt
export DATABASE_URL=postgresql+psycopg://maex:maex@localhost:5432/maex_agent
pytest -q
ruff check api scripts evals && ruff format --check api scripts evals
```

## Configuration

All settings come from `.env`. Template and description of all variables: `.env.example`. Most important:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres database connection |
| `AGENT_API_TOKEN` | Bearer token for voice platform to call tools |
| `TEAM_PHONE` | Extension for forwarding to team |
| `MAX_CALL_SECONDS` | Maximum call duration |
| `GUI_BASIC_AUTH_USER`, `GUI_BASIC_AUTH_HASH` | Access to the operations view. Only in production: Caddy guards `/gui/*` with it (`deploy/Caddyfile`), the application checks no browser login itself. Hash with `docker run --rm caddy:2-alpine caddy hash-password --plaintext '<password>'` |

## Project Structure

```text
api/         FastAPI application: core, domain, agent, telephony, tools, events, jobs, gui
sim/         Text phone: conversations with agent without telephony
db/          Alembic migrations
prompts/     System prompt per version
evals/       Test cases and runner for conversation quality
n8n/         Workflow exports for cold path
deploy/      Production compose and Caddyfile
docs/        Specs and status
```

Layers and dependency rules: `docs/11_MODULE.md`.

## Development

```bash
make test        # pytest
make lint        # ruff check
make fmt         # ruff format
make eval        # eval suite, optional TAGS=menu,noise (runner arrives with T-5.1)

python -m sim.cli                                   # conversation in the terminal
python -m sim.replay evals/cases/<case>.json        # replay a transcript
```

The project is set up for Claude Code. `CLAUDE.md` contains work instructions, `.claude/commands/` holds `/start`, `/task`, `/done`, `/bug`, `/eval`, `/gate`, and `/handover` commands. Workflows: `docs/12_CLAUDE_CODE_PLAYBOOKS.md`.

## Documentation

| File | Content |
|---|---|
| `CLAUDE.md` | Work instructions, rules, stack, conventions |
| `docs/00_PCF.md` | Master plan with stages, gates, risks |
| `docs/01_STATUS.md` | Current state, open decisions, blockers |
| `docs/02_ARCHITECTURE.md` | Components, call flow, failure behavior |
| `docs/03_DATA_MODEL.md` | Tables and fields per stage |
| `docs/04_API_TOOLS.md` | Tool contracts with JSON examples |
| `docs/05_DIALOG_PROMPTS.md` | Conversation flows, system prompt, escalation |
| `docs/06_GUI.md` | Screens and interaction rules |
| `docs/07_WORKPACKAGES.md` | Task list with dependencies |
| `docs/08_EVALS.md` | Test cases and metrics |
| `docs/09_OPERATIONS_LEGAL.md` | Runbook and legal checklist |
| `docs/10_GLOSSARY.md` | Terms |
| `docs/11_MODULE.md` | Layers, dependency rules, build plan per module |
| `docs/12_CLAUDE_CODE_PLAYBOOKS.md` | Session workflows and slash commands |
| `docs/13_DEPLOYMENT.md` | Operating locations, tunnel, EU server, backups, CI |
| `docs/14_MENU_IMPORT_FORMAT.md` | CSV format for menu digitization |
| `docs/15_README_STRATEGY.md` | When and how to maintain this README |

## Contributing

Workflow, branch names, commit format, and labels are in [CONTRIBUTING.md](CONTRIBUTING.md). The roadmap to the target state is in [`docs/01_STATUS.md`](docs/01_STATUS.md), the task list in [`docs/07_WORKPACKAGES.md`](docs/07_WORKPACKAGES.md); each work package has an issue, each blocker a collection issue.

Please report security issues confidentially, not as an issue: [SECURITY.md](SECURITY.md).

## Known Issues

- Docker Hub rate-limits anonymous image downloads. If `make up` fails with rate limit: `docker login` with a free Docker Hub account, then restart.
- `make eval` fails until the eval runner exists (T-5.1). `evals/` currently holds cases only, no `runner.py`; `/eval` and step 2 of `/bug` are blocked until then.
- Behind a TLS-terminating proxy, `pip install` in image build fails with `CERTIFICATE_VERIFY_FAILED`. Fix: place the proxy's CA cert as `api/ca-bundle.crt` (in `.gitignore`), the build auto-includes it.

## License and Contact

Proprietary, <Firmenname>. All rights reserved, see [LICENSE](LICENSE). Use, reproduction, and distribution only with written permission.

Contact: Maximilian Dumler, maxi.dumler@gmail.com.
