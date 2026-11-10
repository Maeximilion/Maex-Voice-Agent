<p align="center">
  <img src=".github/assets/banner.svg" width="100%" alt="MÆX, Maex Voice Agent, an AI phone agent for restaurants. An example call from left to right: the caller orders dish 25 twice for pickup, the neon sign MÆX stands in the middle, and on the right lies the order slip, ready in about 30 minutes, read back and confirmed by the caller's yes.">
</p>

# MÆX (Maex Voice Agent)

<p>
  <a href="https://github.com/Maeximilion/Maex-Voice-Agent/actions/workflows/ci.yml"><img src="https://github.com/Maeximilion/Maex-Voice-Agent/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src=".github/assets/badge-python.svg" height="20" alt="Python 3.12">
  <img src=".github/assets/badge-postgres.svg" height="20" alt="PostgreSQL 16">
  <a href="#license-and-contact"><img src=".github/assets/badge-license.svg" height="20" alt="License: proprietary"></a>
  <a href="#status"><img src=".github/assets/badge-stage.svg" height="20" alt="Current stage of the roadmap"></a>
  <a href="#status"><img src=".github/assets/badge-evals.svg" height="20" alt="Latest eval run on a real model: passed cases of all cases"></a>
</p>

<p>
  <a href="#demo">Demo</a> ·
  <a href="#how-a-call-flows">How a call flows</a> ·
  <a href="#what-works">What works</a> ·
  <a href="#status">Status</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#documentation">Docs</a>
</p>

An AI agent answers the restaurant's main line, takes reservations, pickup and delivery orders, and hands every confirmed order to the kitchen and the team. Complaints and anything it is unsure about go to a human. The team runs the evening from a browser view on a tablet.

It is built for one pilot restaurant first and is not live yet: the conversation, the database, the tablet view and our own voice layer run on the workbench with invented test data, and no real telephone line is connected ([Status](#status)). Pilot operation, location, domain and point-of-sale provider appear in this repository as placeholders in angle brackets.

## Demo

<p align="center">
  <img src=".github/assets/demo.gif" width="100%" alt="Screen recording in two panes. Left, the text phone replays one pickup call: the caller orders, the agent reads the order back, the caller says yes. Right, the operations view on the tablet: the board stays empty during the read-back, and the order card is there after the yes.">
</p>

One replayed pickup call in the text phone on the left, the tablet's operations view on the right: nothing is booked during the read-back, and the order is on the tablet after the caller's yes. The recording comes from a real run of the code with the test menu, made by `python -m scripts.readme_demo` (Python 3.12 or newer, Docker, a Chromium-family browser and ffmpeg); the tablet is shot after each turn, so the live update of the real view is not what it shows; the caller's number on the card is from the range the Bundesnetzagentur reserves for media productions and belongs to nobody.

## How a call flows

<p align="center">
  <img src=".github/assets/call-flow.svg" width="100%" alt="One pickup call as a transcript in three columns. Left, what is said on the phone; right, what the code does with it; between them, the stations each sentence passes: access, Asterisk, ear and conversation core on the way in, core, mouth, Asterisk and access on the way out. The language model hangs off the core and touches nothing else. The caller orders dish 25 twice, and the core runs get_service_status and search_menu against Postgres, one tool per hop, at most six. The caller gives a name, and the core runs draft_order with every price from the database. The model reads the order back word for word, and the core checks that the caller heard it. The caller says yes, and only now the core runs confirm. The core says the booking itself from the result, without the model; the order shows on the tablet and the slip prints after the team's approval, in primary mode up to the amount limit at once. Set apart below, the ways to the team: a complaint is caught before the model, and with it, when the AI is unsure or the model fails, the team phone rings while the restaurant is open; after hours it becomes a callback on the tablet when the caller's number is known. When the service voice fails, the exchange rings the team phone. Each station is marked built, in progress or planned from the task list.">
</p>

There is no hosted voice platform. Asterisk, our own exchange, takes the call and hands the audio to our service `voice`, which turns speech into text and the reply back into speech. In each turn the language model answers with one sentence or asks for one tool; the conversation core checks that request against the rules, runs it against Postgres and gives the result back, at most six hops. The sentence that an order is booked is the core's own, built from what `confirm` returned, never the model's. The model only understands what the caller wants. Whether a dish exists, what it costs, whether a table is free and whether an order is final is decided by code against the database, and nothing is confirmed before the read-back and the caller's yes.

Six rules hold everywhere in the code; the full text is in [`CLAUDE.md`](CLAUDE.md):

<p align="center">
  <img src=".github/assets/rules.svg" width="100%" alt="The six hard rules: AI understands, code decides; never guess; nothing without confirmation; errors are measured; every outage ends with the team; token-efficient by design.">
</p>

## What works

<!-- readme:capabilities -->
Built means: it runs on the workbench and in CI with invented test data. Nothing here has taken a real call yet.

| Capability | State | What it is |
|---|---|---|
| Reservations | built | Check a slot against hours and capacity, offer alternatives, book after the caller's yes; the team can enter one by hand on a table of the floor plan. |
| Pickup and delivery orders | built | Search the menu, take the order, check zone, fee and minimum order for a delivery, read back, confirm. |
| Conversation core | built | Understanding ladder, guards and escalation; a whole call runs in the text phone and in the evals without any telephony. |
| Tablet view for the team | partly | Mode, wait times and switches in the header, today's reservations, orders and callbacks, live without reloading; pages for the owner behind an admin PIN. |
| Kitchen slip | built | A print bridge in the restaurant fetches confirmed orders and prints them; a lost acknowledgement never prints a second slip. |
| Team notifications | partly | Events leave through an outbox with retries, for push messages to the team's devices. |
| Phone line and voice | partly | Our own voice layer: Asterisk, the service voice with its speech engines, and outage drills in which every failure ends at the team phone. |
| Language model | partly | The core runs on a scripted stand-in by default and on any chat-completions server by a switch; the eval suites measure a real model against the targets. |
| Privacy by design | built | A call log without recording, deletion periods enforced every hour, a caller's allergy passed on only after explicit consent. |
| Data of the pilot restaurant | open | The pilot's real menu, hours and capacity in the database. |
| Foundation | built | One command starts the stack; migrations, seed, tests against a real Postgres, lint, CI with an image smoke test, the specs in docs. |

<details>
<summary>Every capability in full</summary>

**What works:**

- `make up` builds the API image and starts Postgres, API, dispatcher, n8n and the push server for team notifications; `/health` responds, token auth works
- Every agent API response follows the envelope from `docs/04_API_TOOLS.md`; errors return JSON with code and read-aloud text, never stacktrace
- Database access with one session per request (`api/db.py`), logs as JSON lines with `request_id` and `call_id`
- `make migrate` creates the stage-1 tables and the stage-2 tables for menu and orders (Alembic in `db/`, models in `api/models/`), `make seed` fills stage 1 idempotently with test config
- Call log without recording per `docs/17_ANRUFPROTOKOLL.md`: `python -m scripts.call_log imports/anrufprotokoll.csv` prints the C1 baseline (volume, intent mix, outcomes, peak hours), `--cases <dir>` writes eval case skeletons without real customer sentences, `--frist-tage N --loeschen` enforces the deletion period; keeps only the hour of a call and cuts a minute in older rows; rejects files with phone numbers, e-mail, street addresses or a person's allergy
- The deletion job of `docs/03_DATA_MODEL.md` "Löschkonzept" runs in the dispatcher once an hour: caller numbers after 90 days, guest details on orders, reservations and callbacks after 90 days, a caller's allergy note the night after the order, outbox payloads after 7 days (unsent 30), the audit log after 12 months, a consent answer at the end of the third calendar year after its year; every period a setting, one log line per pass
- Menu import from CSV per `docs/14_MENU_IMPORT_FORMAT.md`: `python -m scripts.import_menu imports/ --dry-run` checks and reports, without `--dry-run` it loads; idempotent, price changes only with `--apply-price-changes`, dishes missing from the file inactive only with `--deactivate-missing`. The CSVs live in `imports/`, which is git-ignored. From the register: `python -m scripts.kasse_to_csv imports/kasse --out imports` turns copies of the register `.dbf` files into these CSVs (docs/14 §Quelle Kasse)
- Reservation flows end-to-end, every tool latency-tested against 300 ms budget: `POST /v1/tools/get_service_status` answers from DB whether and what's available (hours, special days, wait times, mode); `POST /v1/tools/check_slot` checks a request against capacity and hours, offers up to two alternatives; `POST /v1/tools/create_reservation` creates draft with read-aloud text; `POST /v1/tools/confirm` makes it final, logs it, puts event for cold path in outbox
- Kitchen ticket (T-4.6): the print bridge (`printbridge/`, stdlib only) runs on a machine in the restaurant, fetches confirmed orders over HTTPS from `POST /v1/kitchen/claim` and prints them as ESC/POS on the receipt printer, over the network (TCP 9100) or the Windows print queue (USB). Nothing needs to be opened in the restaurant's router. The tablet card turns red at once on a print error or when no ticket was fetched for 60 s, and back to normal once it prints; a lost acknowledgement never prints a second slip, an outdated revision never prints after a newer one. Setup: `printbridge/README.md`, server side `KITCHEN_BRIDGE_TOKEN` and `KITCHEN_BRIDGE_TENANT_ID` (one bridge per restaurant)
- Dispatcher (`api/events/`) drains outbox to n8n (all events except the kitchen ticket): separate process (`python -m api.jobs.cold_path`, also runs the kitchen-ticket watchdog), one POST per event with event-id as idempotency key, backoff 5 s / 30 s / 2 min / 10 min, then `failed` with alarm in log
- `POST /v1/tools/create_callback` creates a callback task for the team when agent is stuck: task in DB, log entry, event for cold path; at most one open callback per call
- Conversation core (`api/agent/`) with understanding ladder and escalation, driven from the text phone (`sim/`): `python -m sim.cli` runs a call in the terminal, `python -m sim.replay <case>` replays a transcript; a confirmed reservation lands in the database without any telephony
- Operations view for the tablet at `/gui/` (`api/gui/`): header with mode, delivery and wait times and the buttons to pause the AI, switch delivery and raise the wait time; the column "Heute" with the confirmed reservations of the business day and the column "Rückrufe" with open callbacks, a tone for new ones and a done button. It updates itself over Server-Sent-Events, so a call held in `sim/` shows up on every tablet a moment later without reloading
- Reservations the team takes by hand, for a guest who asked an employee directly: the sheet "Reservierung eintragen" in the column "Heute" writes a draft without a call, shows the load of the capacity window and books it at the team's tap, on one table or a combined pair of the floor plan (`/gui/raumplan`). It counts against the same seats as the agent; a full window needs "Trotzdem eintragen", a table taken that evening is refused
- `make test` and `make lint` run in container against real Postgres, ruff clean
- CI additionally builds API image without local CA cert and checks startup, `/health`, and token auth (missing, wrong, valid) without bind mount
- Specs for architecture, data model, tools, dialog, GUI, and evals are in `docs/`
- Orders for pickup and delivery: the menu can be imported, searched, asked about and ordered from, and a delivery order checks its zone, fee and minimum order
- A caller's own allergy reaches the kitchen with its reason only after their explicit consent, logged per call, and a question about allergens beyond the maintained values goes to the team before `confirm` (legal check L13, L17)

**Not yet working:**

- No phone line yet. The own voice layer (`docs/20_VOICE_LAYER.md`) is built up to the speech engines: the call session (T-10.4), the AI telephone as Compose services `asterisk` and `voice` behind the profile `voice` (T-1.11), and outage drills on the workbench, `python3 -m scripts.voice_drill`, in which every way the AI telephone can fail ends at the team telephone. The speech engines run inside `voice` since T-1.11 part 2 (Whisper and Piper, the model files of the speech kit). Missing: the test at the pilot's router (T-10.1 step 2, T-10.2) and real voices (T-10.7)
- The admin view (`docs/06_GUI.md` §4) is built in parts: the key figures, the device list and the owner's settings behind the admin PIN run in the browser; the tabs for menu, delivery and calls are still only a mockup
- No real menu data yet: the CSVs come from the chat digitization (C1)
- The conversation core runs against a rule-based stand-in for the model (`sim/scripted_llm.py`) by default and in CI. A real model can be switched in for the text phone and the evals with `--model <name>` (T-2.4). No real model passes the suites on target yet; the latest run with its date is the eval image under Status
- Team notifications (`n8n/team_events.json` to the push server in the stack, D13) are built but reach nobody until the workflow is imported, the push users and token are set in `.env` and the team's devices subscribe (`n8n/README.md`); iPhones and iPads get a message at once only through an outside relay, which is not configured and not planned: the team's devices are Android devices or the web page on the tablet (D14)
- Test config from `make seed` (hours, capacity) is placeholder until actual ops capture arrives

</details>
<!-- /readme:capabilities -->

<p align="center"><img src=".github/assets/divider.svg" width="100%" alt=""></p>

## Status

<!-- readme:status -->
| | |
|---|---|
| Version | 0.0.1 |
| Stage | 0, Foundation |
| Next Gate | G0: Budget · Rights · Telephony path · Vendor chosen · Stage order |
| As of | 2026-11-06 |
<!-- /readme:status -->

<p align="center">
  <img src=".github/assets/roadmap.svg" width="100%" alt="The roadmap as one line with a station per stage, 0 Foundation to 6 Primary operation, and a gate between two stages. The running stage is lit; under each station stands how many of its tasks are built.">
</p>

The stages follow the gates, not the code: much of the later stages is built ahead, but a stage counts only when its gate is passed with evidence. The roadmap with every gate's condition is in [`docs/01_STATUS.md`](docs/01_STATUS.md), the task list in [`docs/07_WORKPACKAGES.md`](docs/07_WORKPACKAGES.md).

<p align="center">
  <img src=".github/assets/eval-latest.svg" width="100%" alt="The latest eval run on a real model: passed and not passed cases for the regular and the target suite, with hard violations, missed escalations, false escalation rate and tokens per case. Numbers in docs/metrics/eval_history.csv.">
</p>

Every number on this page has a date and a source: the images are drawn from the measurement files in [`docs/metrics/`](docs/metrics/), one dated row per run. Real-model runs happen on the workbench against a local model with invented test cases only. CI runs a rule-based stand-in for the model, which passes every regular case by design and therefore says nothing about a model.

<details>
<summary>Measured state: eval trend, voice layer, progress per block</summary>

<p align="center">
  <img src=".github/assets/eval-trend.svg" width="100%" alt="Pass rate of the real model over time for the regular and the target cases, against the target of 99 percent from docs/08_EVALS.md. Numbers in docs/metrics/eval_history.csv.">
</p>

<p align="center">
  <img src=".github/assets/voice-bench.svg" width="100%" alt="Measurements of the own voice layer with date, value, limit and verdict go, at limit or no-go: the telephony spike, silent wrong numbers of the speech recognition, dishes heard right in spoken orders and the outage drills. Numbers in docs/metrics/voice_history.csv.">
</p>

<p align="center">
  <img src=".github/assets/progress.svg" width="100%" alt="Build progress: tasks done of all tasks, one bar per block of docs/07_WORKPACKAGES.md split into done, in progress, blocked and open, and the gates P and G0 to G5 on a line from plan to live operation.">
</p>

How the images stay current: a full eval run on a real model records itself (`make eval` and `make eval-targets` on a branch), the voice kits record their own measurements, and every Sunday night a routine redraws the images and the status from the files and merges them in one PR ([`docs/15_README_STRATEGY.md`](docs/15_README_STRATEGY.md)).

</details>

<p align="center"><img src=".github/assets/divider.svg" width="100%" alt=""></p>

## Quick start

For development and for the server. A restaurant that uses the product needs a browser, not this repository (`docs/13_DEPLOYMENT.md` §0a). You need Docker with Compose 2.24.4 or later and Git; Python 3.12 only for work outside the containers.

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

The API then runs at `http://localhost:8000`, the operations view at `http://localhost:8000/gui/`, n8n at `http://localhost:5678`, the push server at `http://localhost:8090` (closed until users are set in `.env`). `/health` responds with `{"status": "ok", "env": "dev"}`. These ports are bound to `127.0.0.1` (`docker-compose.override.yml`), so they are reachable from this machine only; the production stack publishes none of them (`docs/13_DEPLOYMENT.md` §3).

The operations view asks for the restaurant's password, in development too (#570). Make the one-time setup link once per database, open it and set a password, the team PIN and the owner's admin PIN: `docker compose exec api python -m api.jobs.gui_access link --base-url http://localhost:8000`.

A call without a telephone, in the running container:

```bash
docker compose exec api python -m sim.cli                              # a conversation in the terminal
docker compose exec api python -m sim.replay evals/cases/<case>.json   # replay a transcript
```

<details>
<summary>Tests and lint from a local Python environment</summary>

Database tests need a reachable Postgres, for example the one from `make up`; `DATABASE_URL` then points to `localhost` instead of the container name `db`:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r api/requirements.txt
export DATABASE_URL=postgresql+psycopg://maex:maex@localhost:5432/maex_agent
pytest -q
ruff check api scripts evals printbridge sim db && ruff format --check api scripts evals printbridge sim db && mypy
```

</details>

<details>
<summary>Dependencies and the lock file</summary>

`api/requirements.in` lists what the project needs, as version ranges, and is the file edited by hand. `api/requirements.txt` is compiled from it (`make lock`, pip-tools in the `python:3.12-slim` image) and pins every package, the indirect ones included, to one version with hashes; the image build and CI install it with `--require-hashes`, so both run the same versions. To add or bump a dependency, edit `requirements.in`, run `make lock`, commit both files; `make lock ARGS="--upgrade-package <name>"` moves one pin inside its range, `ARGS=--upgrade` all of them. The compiled file is for Linux and Python 3.12 (containers, CI, server). A venv on a Windows host cannot install it (checked 12.10.2026: `uvloop` is pinned and has no Windows build, and `tzdata` and `colorama`, which Windows needs, are missing, which hash mode refuses); use `pip install -r api/requirements.in` there, or the container.

</details>

## Architecture

<p align="center">
  <img src=".github/assets/architecture.svg" width="100%" alt="The architecture as two paths. The hot path runs while the caller is on the line: caller, access, Asterisk, ear and mouth of the service voice, the conversation core with the language model beside it, the tools and Postgres. The cold path runs after the yes: confirm writes the order to Postgres, from where the tablet, the order slip fetched by the print bridge and the push notifications reach the team. The team phone stands with the team but is rung during the call: the exchange rings it on a transfer or when the service voice fails. Each component is marked built, in progress or planned from the task list.">
</p>

Two paths with different rules. On the hot path the caller is waiting: the conversation core and the tools run in one process without HTTP between them, and each tool has a budget of 300 ms (p95 with a local database, `docs/04_API_TOOLS.md`). Everything else, the kitchen slip, the tablet and the notifications, runs on the cold path from what `confirm` wrote to the database: the dispatcher retries a failed delivery with backoff and then raises an alarm, and the print bridge fetches the slip itself. Inside the code the layers depend in one direction only, entry points to agent to domain to models, and the domain knows no HTTP and no provider ([`docs/11_MODULE.md`](docs/11_MODULE.md), [`docs/02_ARCHITECTURE.md`](docs/02_ARCHITECTURE.md)).

<details>
<summary>Project structure</summary>

```text
api/              FastAPI application: core, domain, agent, telephony, tools, events, jobs, gui
sim/              Text phone: conversations with the agent without telephony
db/               Alembic migrations
prompts/          System prompt per version
evals/            Test cases and runner for conversation quality
n8n/              Workflow exports for the cold path
deploy/           Production compose, Caddyfile, the AI telephone's Asterisk config (asterisk/conf/) and its drills
scripts/          Seed, import, backup, drills, and the generators of the README images and the demo
docs/             Specs and status; docs/metrics/ holds the dated measurements
.github/assets/   README images, generated by scripts/readme_assets.py and scripts/readme_demo.py
```

</details>

## Configuration

All settings come from `.env`. Template and description of all variables: `.env.example`. A container gets only the variables `docker-compose.yml` names for it, not the whole file: a new setting of `api/config.py` goes into the list `x-settings` there, a new secret into the service that reads it (`docs/13_DEPLOYMENT.md` §3).

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
| `ACCESS_ALSO` | More names or IPv4 addresses Asterisk takes SIP from, comma-separated, looked up once at the start: the pilot's router calls from its public address, also through its VPN. Read by Asterisk only |
| `ACCESS_WIREGUARD_CONF` | Test phase only: path of the router's WireGuard file for the AI telephone's own tunnel (`deploy/asterisk/docker-compose.line.yml` with the probe, `deploy/asterisk/docker-compose.tunnel.yml` on top of the product's stack) |
| `TRANSFER_TARGETS` | Optional, comma-separated: the numbers besides `TEAM_PHONE` a transfer may go to. The dialplan dials no other number; one the AI names that is not listed rings `TEAM_PHONE`, and a listed one nobody takes is followed by `TEAM_PHONE`; a mailbox that answers takes the call (`docs/20_VOICE_LAYER.md` §5). Asterisk dials only these; the api offers them, in E.164, as today's team number on the tablet |
| `MAX_CALL_SECONDS` | Maximum call duration a new tenant is seeded with. A call is ended by the tenant's own value (`service_config.max_call_seconds`) |
| `PRIVACY_NOTICE_ADDRESS` | Where the privacy notice is, as a caller should hear it; a new tenant is seeded with it, and the phone greeting names the tenant's own value (`service_config.privacy_notice_address`). Empty: the greeting leaves the sentence out. Set before the first real call |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | Language model of the conversation core, any server with the chat completions API. Empty by default: the text phone and the evals then run on the scripted stand-in. A local model is for the workbench with invented test data only (`docs/13_DEPLOYMENT.md` §0) |
| `BACKUP_PASSPHRASE_FILE` | File with the passphrase for database dumps, outside the repo. Without it `make backup` writes nothing (`docs/13_DEPLOYMENT.md` §4) |

</details>

## Development

The project is set up for Claude Code. `CLAUDE.md` contains the work instructions, `.claude/commands/` holds the commands `/start`, `/task`, `/done`, `/ship`, `/bug`, `/eval`, `/gate` and `/handover`. Workflows: `docs/12_CLAUDE_CODE_PLAYBOOKS.md`.

<details>
<summary>Make targets and the text phone</summary>

```bash
make test        # pytest
make lint        # ruff check, mypy
make fmt         # ruff format
make backup      # encrypted dump of the database of DATABASE_URL into backups/, needs BACKUP_PASSPHRASE_FILE (docs/13 §4)
make eval        # the regular cases (level 1-2) on a throwaway DB, optional TAGS=menu,noise; exit 1 on a hard metric, a crash or a case that was green in the last passing run
make eval-targets  # the target cases (level 3-5, known gaps), not in CI, red allowed until the real model passes them (T-2.4, T-6.8)
make eval-record  # record the newest real-model run in docs/metrics/; after make eval-targets: make eval-record SUITE=targets
make eval-voice   # the cases spoken and heard by the service's ear on the graphics card (docs/08 §8), optional MODEL=, LIMIT=, VOICE=; needs make up and the speech kit's model files
make readme-assets  # redraw the README images from docs/metrics/, the task list and the status (Python 3.12 or newer)
make readme-check   # README links, anchors, image safety and blocked terms; then images up to date with their data

# in the api container (docker compose exec api ...) or a local environment with DATABASE_URL on localhost:
python -m sim.cli                                   # conversation in the terminal
python -m sim.cli --model qwen3:14b                 # the same on a local model (LLM_* in .env, docs/18 §5)
python -m sim.replay evals/cases/<case>.json        # replay a transcript
# with the stack up (make up): http://localhost:8000/gui/dev/console is the same text phone in the browser, ENV=dev only
```

</details>

## Documentation

Start with [`docs/01_STATUS.md`](docs/01_STATUS.md) for where the project stands and [`docs/02_ARCHITECTURE.md`](docs/02_ARCHITECTURE.md) for how it is built.

<details>
<summary>All documents</summary>

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
| `docs/15_README_STRATEGY.md` | When and how to maintain this README, its images, the demo and their design |
| `docs/16_GITHUB_PROJECT.md` | GitHub Project board: one project, derived from issues and PRs |
| `docs/17_ANRUFPROTOKOLL.md` | Call log without recording: CSV format, paper sheet, baseline and eval drafts |
| `docs/18_MODEL_SELECTION.md` | Operation model, local test model, setup script, parallel calls |
| `docs/19_CLAUDE_CODE_SETUP.md` | Personal Claude Code setup: user-level hooks, settings block, restore steps |
| `docs/20_VOICE_LAYER.md` | Own voice layer: components, access, call session rules, outage map, cost model |
| [docs/21_REVIEW_AUTOMATION.md](docs/21_REVIEW_AUTOMATION.md) | Planned subscription-based inline reviews: setup, independent reviewers, validation and deferred specialist experiment |
| `docs/metrics/` | Dated measurements behind the README images: eval runs on real models and voice layer measurements |
| [docs/22_LANGUAGE_AND_PERFORMANCE.md](docs/22_LANGUAGE_AND_PERFORMANCE.md) | Python/TypeScript assessment, measured latency gate and UI/type-checking decisions |
| [docs/23_ACCESS_SECURITY.md](docs/23_ACCESS_SECURITY.md) | Threat model and access design for several customers: operator dashboard, customer logins, tokens, uploads |

</details>

## Contributing

Workflow, branch names, commit format, and labels are in [CONTRIBUTING.md](CONTRIBUTING.md). Each work package has an issue, each block a collection issue. Please report security issues confidentially, not as an issue: [SECURITY.md](SECURITY.md).

## Known issues

- Docker Hub rate-limits anonymous image downloads. If `make up` fails with rate limit: `docker login` with a free Docker Hub account, then restart.
- Kitchen ticket printing: the test slip was checked on paper on the restaurant's Epson TM-T20II (register PC, Windows queue, 06.10.2026). A slip of a real order through the server has not been printed yet, and the network path is tested against a simulated printer only.
- `make eval` measures the rule-based stand-in model (`sim/scripted_llm.py`); tokens and cost per case are empty there. `make eval MODEL=<name>` runs the same cases on a model of the server in `LLM_BASE_URL` and fills them. The cost per case stays empty when `MODEL` names another model than `LLM_MODEL`: the prices in `.env` are not its own. No model has passed the full suite yet; the latest run is the eval image under [Status](#status), and the prompt work is open (issue #37).
- Behind a TLS-terminating proxy, `pip install` in image build fails with `CERTIFICATE_VERIFY_FAILED`. Fix: place the proxy's CA cert as `api/ca-bundle.crt` (in `.gitignore`), the build auto-includes it.
- The README images are SVG files with their own midnight background, so they look the same in GitHub's light and dark theme. The call flow reveals its rows one after another in browsers that run CSS animation; where motion is reduced, the whole picture shows at once. The social preview (`.github/assets/social-preview.png`) has to be uploaded by hand in the repository settings, the API cannot set it.

## License and contact

Proprietary, <Firmenname>. All rights reserved, see [LICENSE](LICENSE). Use, reproduction, and distribution only with written permission.

Contact: Maximilian Dumler, maxi.dumler@gmail.com.
