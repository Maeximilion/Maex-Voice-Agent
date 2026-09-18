# Changelog

Format per Keep a Changelog. Versions follow gates, see docs/15_README_STRATEGY.md.

## [Unreleased]

### Added
- Project skeleton: FastAPI app with /health, token auth, uniform response envelope
- `api/db.py`: engine with connection ping, session per request via `get_db`, tests against real Postgres
- `api/core/`: response envelope, error classes with eight codes from docs/04, token auth, JSON logging with `request_id`/`call_id`, time helpers with business day
- Alembic in `db/` and migration 001 with ten stage-1 tables; SQLAlchemy models in `api/models/`; `make migrate` creates schema
- `scripts/seed.py`: idempotent test config (tenant, live switch, hours, capacity), `make seed`
- Tool `POST /v1/tools/get_service_status`: open/closed per service, special days override weekdays, windows crossing midnight, wait times and mode from `service_config`, read-aloud text to next opening; latency helper `p95_ms` with 300 ms budget in tests
- Tool `POST /v1/tools/check_slot`: availability from `capacity` and active reservations within `dinein` hours, up to two alternatives on grid, read-aloud text with spoken times
- Tool `POST /v1/tools/create_reservation`: creates draft (`status: draft`), checks call, phone (E.164), future, slot per same rules as `check_slot`, writes `audit_log`, delivers `readback` for read-aloud; same `idempotency_key` delivers same response without second transaction
- `api/core/ids.py`: deterministic idempotency keys for callers without own key; `api/domain/customers/phone.py`: E.164 normalization of German formats
- `create_reservation` locks check and create per tenant and day (`pg_advisory_xact_lock`) so concurrent calls can't overbooking a window; `readback` grounds "today" and "tomorrow" to creation time so replay after midnight delivers same sentence
- `sim/`: the text phone. `python -m sim.cli` runs a call in the terminal, `python -m sim.replay <case.json>` replays a transcript from `evals/cases/`, `sim/noise.py` garbles input reproducibly to exercise the understanding ladder. Both entry points use `api/agent/` directly and write to the database, so a confirmed reservation is visible without telephony (through-cut without phone)
- `sim/scripted_llm.py`: rule-based stand-in for the model until T-2.4 connects a real one; recognizes party size, date and time, name and phone number, never guesses, and reports an unrecognized turn as a failed attempt to the understanding ladder
- `api/domain/menu/numberwords.py`: spoken German numbers as integers - `parse_cardinal`, `find_numbers`, `find_item_number`, `find_quantity`. Digits and words, written together or apart, spoken digit by digit ("vierzig sieben"), umlauts in either spelling. No match gives `None`, never a guess. Pure function, no database, 375 tests
- Docker Compose for Postgres, API, n8n
- Specs docs/00 through docs/15
- Slash commands for Claude Code in .claude/commands/
- CI workflow (ruff, pytest)
- `api/gui/`: operations view for the tablet (T-3.1, T-3.3). Jinja2 templates, HTMX and `app.css` built from the approved mockup's variables and classes, served locally without any CDN. Header shows mode, delivery and wait times from `service_config`; the column "Heute" lists the confirmed reservations of the business day and updates itself over Server-Sent-Events, so a reservation talked in from the terminal appears on the tablet within seconds
- Header of the operations view is operable (T-3.2): "KI pausieren" as one-tap emergency stop, "KI einschalten" with confirmation restores the mode from before the pause, "Lieferung aus/an", "Wartezeit +15/+30" (capped at 180 min). Writes in `api/domain/status/config.py` with row lock and `audit_log`; the event stream sends `header` so every tablet shows the same state
- Callbacks column of the operations view (T-3.4): open callbacks with complaints on top, "Anrufen" as tel: link, "Erledigt" with row lock and `audit_log`; event stream signal `callbacks`, tone on new cards with a distinct tone for complaints
- Migration 002 (T-4.1): `menu_items`, `item_options`, `item_allergens`, `item_aliases`, `orders`, `order_items` with `pg_trgm` trigram indexes on dish names and aliases; the database enforces amounts in cents, matching totals, positive quantities and mandatory `menu_item_id`
- Menu import (T-4.2): `python -m scripts.import_menu <folder> [--dry-run] [--apply-price-changes]` loads the four CSVs from `docs/14_MENU_IMPORT_FORMAT.md` with all check rules, idempotent, one transaction, report with price changes and warnings
- Menu tools (T-4.3, T-4.4): `POST /v1/tools/search_menu` resolves a spoken order in the order number, exact alias, trigram over name and alias; thresholds configurable (`MENU_FUZZY_THRESHOLD_HIGH`/`_LOW`), several candidates mean a question back with at most three suggestions instead of a guess. `POST /v1/tools/get_item_details` delivers description, options and allergens; without a maintained row in `item_allergens` there is no allergen statement, only the sentence that the team confirms it
- `api/domain/reservations/today.py`: confirmed reservations of the business day plus a cheap change token for the event stream
- Desktop mockup of admin view in gui/mockups/

### Changed
- Pilot operation, company, location, and provider names replaced with placeholders (`<PilotOperation>`, `<CompanyName>`, `<Location>`, `<PointOfSale>`, `<POSProvider>`, `example.com`)
- README quick start reduced to steps that work today; `make migrate` and `make seed` follow in T-1.1 and T-1.2
- Compose additionally mounts `scripts/` and `evals/` so `make lint` runs in container
- Decision D6 settled: the GUI is server-rendered with Jinja2 and HTMX, not React (one container, no Node build, no CORS; rationale in docs/06_GUI.md §2)
- New dependency `jinja2` in `api/requirements.txt` for the GUI templates

### Fixed
- Latency tests (`p95_ms`): up to 3 measurement rounds of 20 calls, p95 always over all calls measured so far, stops as soon as it is under budget. A few outliers on a loaded shared CI runner no longer fail the run (560 ms on 18.09.2026, 10 to 21 ms locally); an overrun in more than 5 percent of calls stays red, even when it is intermittent. Budget unchanged at 300 ms locally and in CI
- `numberwords`: a number no longer grows across a punctuation mark or across the article of an amount ("Nummer 20, eine Portion" was item 21 with amount 21); an article that begins a number counts ("die ein und zwanzig" was 20); a number attached to an amount marker is not counted as a second dish number ("2 x die 23" asked back for nothing)
- `deploy/Caddyfile`: `/gui/*` now sits behind basic auth, user and password hash from the environment (`GUI_BASIC_AUTH_USER`, `GUI_BASIC_AUTH_HASH`). Before this the checked-in production stack proxied the operations view straight through, so guest names, phone numbers and notes were open on the internet
- `api/gui/router.py`: `/gui/events` resolves the tenant in its own short-lived session instead of the request-scoped one. A stream stays open for hours; the request session would have pinned a pooled connection for just as long, and a handful of tablets could have starved the hot path
- `api/gui/sse.py`: after a database outage the stream sends a `today` event on the first successful poll even when nothing changed, so the yellow connection bar disappears instead of hanging until the next booking
- Dockerfile: additional CA cert (`api/ca-bundle.crt`) is optional, not required; build works on clean checkout
- Dockerfile: source code lands under `/app/api` again so `uvicorn api.main:app` starts without bind mount
- Compose: n8n listens on `0.0.0.0` not `::`, avoids crash loop on Docker hosts without IPv6
- `check_slot`: previous day's windows crossing midnight also apply to requests after midnight
- `get_service_status`: paused delivery doesn't count as open; "Pickup available" only with open pickup window

### Open
- Writable stage-1 tools `create_reservation`, `confirm`, `create_callback`, `transfer_to_team` (T-1.5 through T-1.8)
