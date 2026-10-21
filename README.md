<p align="center">
  <img src=".github/assets/banner.svg" width="100%" alt="MÆX, Maex Voice Agent: the neon logo and a slip with the current state, namely version, stage, number of test functions, next gate and the latest eval run on a real model.">
</p>

# MÆX (Maex Voice Agent)

<p>
  <a href="https://github.com/Maeximilion/Maex-Voice-Agent/actions/workflows/ci.yml"><img src="https://github.com/Maeximilion/Maex-Voice-Agent/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12-9D7BFF?labelColor=17102B" alt="Python 3.12"></a>
  <a href="https://www.postgresql.org/"><img src="https://img.shields.io/badge/postgresql-16-9D7BFF?labelColor=17102B" alt="PostgreSQL 16"></a>
  <a href="#license-and-contact"><img src="https://img.shields.io/badge/license-proprietary-E8FF2E?labelColor=17102B" alt="License: proprietary"></a>
</p>

<p>
  <a href="#how-a-call-flows">How a call flows</a> ·
  <a href="#status">Status</a> ·
  <a href="#measured-state">Measured state</a> ·
  <a href="#quick-start">Quick Start</a> ·
  <a href="#development">Development</a> ·
  <a href="#documentation">Docs</a>
</p>

AI-powered phone intake for hospitality. An AI agent answers calls on the restaurant's main line, handles reservations, pickup, and delivery, and hands off confirmed transactions to kitchen, register, and team. Complaints and special cases escalate to a human. The team manages operations via a browser interface on a tablet.

The voice layer (telephony, speech recognition, voice output) is our own, running on our own EU server: decided 07.10.2026, built up to the speech engines and not yet connected to a real telephone line (`docs/20_VOICE_LAYER.md`). This repository contains the domain logic, database, interface, and tests. Pilot operation, location, domain, and point-of-sale provider appear in docs as placeholders in angle brackets.

### How a call flows

<p align="center">
  <img src=".github/assets/call-flow.svg" width="100%" alt="Animated diagram of one call in two turns. The caller's words go in through the access (router or SIP trunk), Asterisk, our own exchange, and the ear of the service voice to the conversation core. The core asks the language model, runs the one tool the model asks for (get_service_status, search_menu, draft_order) against Postgres and hands the result back to the model, one tool per hop. The read-back goes out through the mouth. After the caller's yes the core runs confirm; while the confirmation goes back through the model and the mouth to the caller, the order shows on the tablet and the slip prints once the team approves it, in primary mode under the amount limit at once. When the AI is unsure, hears a complaint or the model fails, the call goes to the team phone while the restaurant is open and after hours becomes a callback on the tablet when the caller's number is known; when the service voice fails, the exchange rings the team phone. Each component is marked built, in progress or planned from the task list.">
</p>

There is no hosted voice platform: Asterisk, our own exchange, takes the call from the access and hands the audio to our service `voice`, which turns speech into text and the reply back into speech. Its speech engines are built but not wired yet (T-1.11 part 2). In each turn the language model answers with one sentence or one tool call; the conversation core checks the call against the rules, runs it against Postgres and gives the result back to the model, at most six hops. The model only understands what the caller wants. Whether a dish exists, what it costs, whether a table is free and whether the order is final is decided by code against the database, and an order is confirmed only after the read-back and the caller's yes. When the AI is unsure, hears a complaint or the model fails, the call goes to the team phone while the restaurant is open and after hours becomes a callback on the tablet when the caller's number is known; when the service `voice` fails, the exchange rings the team phone. These six rules hold everywhere in the code; the full text is in [`CLAUDE.md`](CLAUDE.md):

<p align="center">
  <img src=".github/assets/rules.svg" width="100%" alt="The six hard rules: AI understands, code decides; never guess; nothing without confirmation; errors are measured; every outage ends with the team; token-efficient by design.">
</p>

<p align="center"><img src=".github/assets/divider.svg" width="100%" alt=""></p>

## Status

<!-- readme:status -->
| | |
|---|---|
| Version | 0.0.1 |
| Stage | 0, Foundation |
| Next Gate | G0: Budget · Rights · Telephony path · Vendor chosen · Stage order |
| As of | 2026-10-20 |
<!-- /readme:status -->

<p align="center">
  <img src=".github/assets/progress.svg" width="100%" alt="Build progress: tasks done of all tasks, one bar per block of docs/07_WORKPACKAGES.md split into done, in progress, blocked and open, and the gates P and G0 to G5 on a line from plan to live operation.">
</p>

### Measured state

The images below are drawn from the measurement files in [`docs/metrics/`](docs/metrics/), one dated row per run, so every number on this page has a date and a source. Real-model runs happen on the workbench against a local model with invented test cases only; CI runs the rule-based stand-in, which passes every regular case by design and therefore says nothing about a model.

<p align="center">
  <img src=".github/assets/eval-latest.svg" width="100%" alt="The latest eval run on a real model: passed and not passed cases for the regular and the target suite, with hard violations, missed escalations, false escalation rate and tokens per case. Numbers in docs/metrics/eval_history.csv.">
</p>

<p align="center">
  <img src=".github/assets/eval-trend.svg" width="100%" alt="Pass rate of the real model over time for the regular and the target cases, against the target of 99 percent from docs/08_EVALS.md. Numbers in docs/metrics/eval_history.csv.">
</p>

<p align="center">
  <img src=".github/assets/voice-bench.svg" width="100%" alt="Measurements of the own voice layer with date, value, limit and verdict go, at limit or no-go: the telephony spike, silent wrong numbers of the speech recognition, dishes heard right in spoken orders and the outage drills. Numbers in docs/metrics/voice_history.csv.">
</p>

How the images stay current: a full eval run on a real model records itself (`make eval` and `make eval-targets` on a branch), the voice kits record their own measurements, and every Sunday night a routine redraws the images and the status from the files and merges them in one PR (`docs/15_README_STRATEGY.md`, section "Assets and design").

<!-- readme:capabilities -->
**What works:**

- `make up` builds the API image and starts Postgres, API, dispatcher, n8n and the push server for team notifications; `/health` responds, token auth works
- Every agent API response follows the envelope from `docs/04_API_TOOLS.md`; errors return JSON with code and read-aloud text, never stacktrace
- Database access with one session per request (`api/db.py`), logs as JSON lines with `request_id` and `call_id`
- `make migrate` creates the stage-1 tables and the stage-2 tables for menu and orders (Alembic in `db/`, models in `api/models/`), `make seed` fills stage 1 idempotently with test config
- Call log without recording per `docs/17_ANRUFPROTOKOLL.md`: `python -m scripts.call_log imports/anrufprotokoll.csv` prints the C1 baseline (volume, intent mix, outcomes, peak hours), `--cases <dir>` writes eval case skeletons without real customer sentences, `--frist-tage N --loeschen` enforces the deletion period; rejects files with phone numbers, e-mail, street addresses or a person's allergy
- Menu import from CSV per `docs/14_MENU_IMPORT_FORMAT.md`: `python -m scripts.import_menu imports/ --dry-run` checks and reports, without `--dry-run` it loads; idempotent, price changes only with `--apply-price-changes`, dishes missing from the file inactive only with `--deactivate-missing`. The CSVs live in `imports/`, which is git-ignored. From the register: `python -m scripts.kasse_to_csv imports/kasse --out imports` turns copies of the register `.dbf` files into these CSVs (docs/14 §Quelle Kasse)
- Reservation flows end-to-end, every tool latency-tested against 300 ms budget: `POST /v1/tools/get_service_status` answers from DB whether and what's available (hours, special days, wait times, mode); `POST /v1/tools/check_slot` checks a request against capacity and hours, offers up to two alternatives; `POST /v1/tools/create_reservation` creates draft with read-aloud text; `POST /v1/tools/confirm` makes it final, logs it, puts event for cold path in outbox
- Kitchen ticket (T-4.6): the print bridge (`printbridge/`, stdlib only) runs on a machine in the restaurant, fetches confirmed orders over HTTPS from `POST /v1/kitchen/claim` and prints them as ESC/POS on the receipt printer, over the network (TCP 9100) or the Windows print queue (USB). Nothing needs to be opened in the restaurant's router. The tablet card turns red at once on a print error or when no ticket was fetched for 60 s, and back to normal once it prints; a lost acknowledgement never prints a second slip, an outdated revision never prints after a newer one. Setup: `printbridge/README.md`, server side `KITCHEN_BRIDGE_TOKEN` and `KITCHEN_BRIDGE_TENANT_ID` (one bridge per restaurant)
- Dispatcher (`api/events/`) drains outbox to n8n (all events except the kitchen ticket): separate process (`python -m api.jobs.cold_path`, also runs the kitchen-ticket watchdog), one POST per event with event-id as idempotency key, backoff 5 s / 30 s / 2 min / 10 min, then `failed` with alarm in log
- `POST /v1/tools/create_callback` creates a callback task for the team when agent is stuck: task in DB, log entry, event for cold path; at most one open callback per call
- Conversation core (`api/agent/`) with understanding ladder and escalation, driven from the text phone (`sim/`): `python -m sim.cli` runs a call in the terminal, `python -m sim.replay <case>` replays a transcript; a confirmed reservation lands in the database without any telephony
- Operations view for the tablet at `/gui/` (`api/gui/`): header with mode, delivery and wait times and the buttons to pause the AI, switch delivery and raise the wait time; the column "Heute" with the confirmed reservations of the business day and the column "Rückrufe" with open callbacks, a tone for new ones and a done button. It updates itself over Server-Sent-Events, so a call held in `sim/` shows up on every tablet a moment later without reloading
- `make test` and `make lint` run in container against real Postgres, ruff clean
- CI additionally builds API image without local CA cert and checks startup, `/health`, and token auth (missing, wrong, valid) without bind mount
- Specs for architecture, data model, tools, dialog, GUI, and evals are in `docs/`

**Not yet working:**

- No phone line yet. The own voice layer (`docs/20_VOICE_LAYER.md`) is built up to the speech engines: the call session (T-10.4), the AI telephone as Compose services `asterisk` and `voice` behind the profile `voice` (T-1.11), and outage drills on the workbench, `python3 -m scripts.voice_drill`, in which every way the AI telephone can fail ends at the team telephone. The speech engines run inside `voice` since T-1.11 part 2 (Whisper and Piper, the model files of the speech kit). Missing: the test at the pilot's router (T-10.1 step 2, T-10.2) and real voices (T-10.7)
- The admin view (`docs/06_GUI.md` §4) is still only a mockup
- Orders only for pickup: the menu can be imported, searched (`POST /v1/tools/search_menu`), asked about (`POST /v1/tools/get_item_details`) and ordered from (`POST /v1/tools/draft_order`); `confirm` gives a pickup code; in mode `primary` the ticket goes to the kitchen at once, in every other mode after "Passt" on the tablet (T-4.7). Delivery follows in T-6.5
- No real menu data yet: the CSVs come from the chat digitization (C1)
- The conversation core runs against a rule-based stand-in for the model (`sim/scripted_llm.py`) by default and in CI. A real model can be switched in for the text phone and the evals with `--model <name>` (T-2.4). The first full runs on a local model (`qwen3.6-35b-a3b`, 13./14.10.2026) pass 26 of 41 regular cases and 2 of 27 target cases, far from the target of 99 % ("Measured state" above)
- Team notifications (`n8n/team_events.json` to the push server in the stack, D13) are built but reach nobody until the workflow is imported, the push users and token are set in `.env` and the team's devices subscribe (`n8n/README.md`); iPhones and iPads get a message at once only through an outside relay, which is not configured (D14)
- Test config from `make seed` (hours, capacity) is placeholder until actual ops capture arrives
<!-- /readme:capabilities -->

<p align="center"><img src=".github/assets/divider.svg" width="100%" alt=""></p>

## Requirements

For development and for the server. A restaurant that uses the product needs a browser, not this repository (`docs/13_DEPLOYMENT.md` §0a).

- Docker and Docker Compose 2.24.4 or later
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

API then runs at `http://localhost:8000`, the operations view at `http://localhost:8000/gui/`, n8n at `http://localhost:5678`, the push server at `http://localhost:8090` (closed until users are set in `.env`). `/health` responds with `{"status": "ok", "env": "dev"}`. These ports are bound to `127.0.0.1` (`docker-compose.override.yml`), so they are reachable from this machine only; the production stack publishes none of them (`docs/13_DEPLOYMENT.md` §3).

Tests and lint from a local Python environment. Database tests need reachable Postgres, e.g. from `make up`; `DATABASE_URL` then points to `localhost` instead of container name `db`:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r api/requirements.txt
export DATABASE_URL=postgresql+psycopg://maex:maex@localhost:5432/maex_agent
pytest -q
ruff check api scripts evals printbridge sim db && ruff format --check api scripts evals printbridge sim db && mypy
```

<details>
<summary>Dependencies and the lock file</summary>

`api/requirements.in` lists what the project needs, as version ranges, and is the file edited by hand. `api/requirements.txt` is compiled from it (`make lock`, pip-tools in the `python:3.12-slim` image) and pins every package, the indirect ones included, to one version with hashes; the image build and CI install it with `--require-hashes`, so both run the same versions. To add or bump a dependency, edit `requirements.in`, run `make lock`, commit both files; `make lock ARGS="--upgrade-package <name>"` moves one pin inside its range, `ARGS=--upgrade` all of them. The compiled file is for Linux and Python 3.12 (containers, CI, server). A venv on a Windows host cannot install it (checked 12.10.2026: `uvloop` is pinned and has no Windows build, and `tzdata` and `colorama`, which Windows needs, are missing, which hash mode refuses); use `pip install -r api/requirements.in` there, or the container.

</details>

## Configuration

All settings come from `.env`. Template and description of all variables: `.env.example`.

<details>
<summary>The most important variables</summary>

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres database connection of the application |
| `OWNER_DATABASE_URL` | Only on the server: the owner of the schema, for migrations, backup and restore. `DATABASE_URL` then names a role without superuser that `bash scripts/db_role.sh` sets up; in `ENV=prod` the API does not start as a superuser (`docs/13_DEPLOYMENT.md` §3). Empty on the workbench |
| `AGENT_API_TOKEN` | Bearer token for the tool endpoints (`/v1/tools`). Outside `ENV=dev` and `ENV=test`, the API does not start on the placeholder, fewer than 32 characters, or any whitespace (`openssl rand -hex 32`) |
| `TEAM_PHONE` | Extension for forwarding to team; the AI telephone's dialplan rings it whenever the service `voice` cannot say where a call goes |
| `VOICE_MEDIA_TOKEN`, `VOICE_NUMBER`, `VOICE_EAR`, `VOICE_MOUTH` | The service `voice` of the own voice layer (Compose profile `voice`): the token Asterisk presents, the number the AI telephone answers, and the speech engines (`whisper`, `piper`; `VOICE_EAR_DEVICE`, `VOICE_MOUTH_VOICE`, model files in `deploy/speech/run/models`). It does not start with one missing, with an engine it cannot load, or with the team on its own number (`docs/20_VOICE_LAYER.md` §7) |
| `ACCESS_HOST`, `ACCESS_USER`, `ACCESS_PASSWORD` | Where the AI telephone registers: the pilot's router in the test phase, a SIP trunk in operation. Asterisk takes SIP only from the IPv4 addresses of the servers its `_sip._udp` SRV records name, or of `ACCESS_HOST` itself without such records, looked up when it starts; a lookup that fails stops the start until DNS answers. Read by Asterisk only |
| `TRANSFER_TARGETS` | Optional, comma-separated: the numbers besides `TEAM_PHONE` a transfer may go to. The dialplan dials no other number; one the AI names that is not listed rings `TEAM_PHONE`, and a listed one nobody takes is followed by `TEAM_PHONE`; a mailbox that answers takes the call (`docs/20_VOICE_LAYER.md` §5). Read by Asterisk only |
| `MAX_CALL_SECONDS` | Maximum call duration a new tenant is seeded with. A call is ended by the tenant's own value (`service_config.max_call_seconds`) |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | Language model of the conversation core, any server with the chat completions API. Empty by default: the text phone and the evals then run on the scripted stand-in. A local model is for the workbench with invented test data only (`docs/13_DEPLOYMENT.md` §0) |
| `BACKUP_PASSPHRASE_FILE` | File with the passphrase for database dumps, outside the repo. Without it `make backup` writes nothing (`docs/13_DEPLOYMENT.md` §4) |
| `GUI_BASIC_AUTH_USER`, `GUI_BASIC_AUTH_HASH` | Access to the operations view. Only in production: Caddy guards `/gui/*` with it (`deploy/Caddyfile`), the application checks no browser login itself. Hash with `docker run --rm caddy:2-alpine caddy hash-password --plaintext '<password>'` |

</details>

## Project Structure

```text
api/              FastAPI application: core, domain, agent, telephony, tools, events, jobs, gui
sim/              Text phone: conversations with agent without telephony
db/               Alembic migrations
prompts/          System prompt per version
evals/            Test cases and runner for conversation quality
n8n/              Workflow exports for cold path
deploy/           Production compose, Caddyfile, the AI telephone's Asterisk config (asterisk/conf/) and its drills
scripts/          Seed, import, backup, drills, and the generator of the README images
docs/             Specs and status; docs/metrics/ holds the dated measurements
.github/assets/   README images, generated by scripts/readme_assets.py
```

Layers and dependency rules: `docs/11_MODULE.md`.

## Development

```bash
make test        # pytest
make lint        # ruff check, mypy
make fmt         # ruff format
make backup      # encrypted dump of the database of DATABASE_URL into backups/, needs BACKUP_PASSPHRASE_FILE (docs/13 §4)
make eval        # 41 regular cases (level 1-2) on a throwaway DB, optional TAGS=menu,noise; exit 1 on a hard metric, a crash or a case that was green in the last passing run
make eval-targets  # 27 target cases (level 3-5, delivery, known gaps), not in CI, red allowed until T-2.4 and T-6.5
make eval-record  # record the newest real-model run in docs/metrics/; after make eval-targets: make eval-record SUITE=targets
make readme-assets  # redraw the README images from docs/metrics/, the task list and the status

python -m sim.cli                                   # conversation in the terminal
python -m sim.cli --model qwen3:14b                 # the same on a local model (LLM_* in .env, docs/18 §5)
python -m sim.replay evals/cases/<case>.json        # replay a transcript
# with the stack up (make up): http://localhost:8000/gui/dev/console is the same text phone in the browser, ENV=dev only
```

The project is set up for Claude Code. `CLAUDE.md` contains work instructions, `.claude/commands/` holds `/start`, `/task`, `/done`, `/ship`, `/bug`, `/eval`, `/gate`, and `/handover` commands. Workflows: `docs/12_CLAUDE_CODE_PLAYBOOKS.md`.

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
| `docs/13_DEPLOYMENT.md` | Operating locations, EU server, backups, CI |
| `docs/14_MENU_IMPORT_FORMAT.md` | CSV format for menu digitization |
| `docs/15_README_STRATEGY.md` | When and how to maintain this README, its images and their design |
| `docs/16_GITHUB_PROJECT.md` | GitHub Project board: one project, derived from issues and PRs |
| `docs/17_ANRUFPROTOKOLL.md` | Call log without recording: CSV format, paper sheet, baseline and eval drafts |
| `docs/18_MODEL_SELECTION.md` | Operation model, local test model, setup script, parallel calls |
| `docs/19_CLAUDE_CODE_SETUP.md` | Personal Claude Code setup: user-level hooks, settings block, restore steps |
| `docs/20_VOICE_LAYER.md` | Own voice layer: components, access, call session rules, outage map, cost model (decided 07.10.2026, not built) |
| `docs/metrics/` | Dated measurements behind the README images: eval runs on real models and voice layer measurements |

## Contributing

Workflow, branch names, commit format, and labels are in [CONTRIBUTING.md](CONTRIBUTING.md). The roadmap to the target state is in [`docs/01_STATUS.md`](docs/01_STATUS.md), the task list in [`docs/07_WORKPACKAGES.md`](docs/07_WORKPACKAGES.md); each work package has an issue, each blocker a collection issue.

Please report security issues confidentially, not as an issue: [SECURITY.md](SECURITY.md).

## Known Issues

- Docker Hub rate-limits anonymous image downloads. If `make up` fails with rate limit: `docker login` with a free Docker Hub account, then restart.
- Kitchen ticket printing: the test slip was checked on paper on the restaurant's Epson TM-T20II (register PC, Windows queue, 06.10.2026). A slip of a real order through the server has not been printed yet, and the network path is tested against a simulated printer only
- `make eval` measures the rule-based stand-in model (`sim/scripted_llm.py`); tokens and cost per case are empty there. `make eval MODEL=<name>` runs the same cases on a model of the server in `LLM_BASE_URL` and fills them. The cost per case stays empty when `MODEL` names another model than `LLM_MODEL`: the prices in `.env` are not its own. No model has passed the full suite yet: the first full runs on a local model reach 26 of 41 regular cases (14.10.2026); the prompt work is open (issue #37).
- Behind a TLS-terminating proxy, `pip install` in image build fails with `CERTIFICATE_VERIFY_FAILED`. Fix: place the proxy's CA cert as `api/ca-bundle.crt` (in `.gitignore`), the build auto-includes it.
- The README images are SVG files with their own midnight background, so they look the same in GitHub's light and dark theme. The animated call flow moves in browsers that run SVG animation; where motion is reduced, the static picture shows. The social preview (`.github/assets/social-preview.png`) has to be uploaded by hand in the repository settings, the API cannot set it.

## License and Contact

Proprietary, <Firmenname>. All rights reserved, see [LICENSE](LICENSE). Use, reproduction, and distribution only with written permission.

Contact: Maximilian Dumler, maxi.dumler@gmail.com.
