# Changelog

Format per Keep a Changelog. Versions follow gates, see docs/15_README_STRATEGY.md.

## [Unreleased]

### Added
- Project skeleton: FastAPI app with /health, token auth, uniform response envelope
- `api/db.py`: engine with connection ping, session per request via `get_db`, tests against real Postgres
- `api/core/`: response envelope, error classes with eight codes from docs/04, token auth, JSON logging with `request_id`/`call_id`, time helpers with business day
- Alembic in `db/` and migration 001 with ten stage-0 tables; SQLAlchemy models in `api/models/`; `make migrate` creates schema
- `scripts/seed.py`: idempotent test config (tenant, live switch, hours, capacity), `make seed`
- Tool `POST /v1/tools/get_service_status`: open/closed per service, special days override weekdays, windows crossing midnight, wait times and mode from `service_config`, read-aloud text to next opening; latency helper `p95_ms` with 300 ms budget in tests
- Tool `POST /v1/tools/check_slot`: availability from `capacity` and active reservations within `dinein` hours, up to two alternatives on grid, read-aloud text with spoken times
- Tool `POST /v1/tools/create_reservation`: creates draft (`status: draft`), checks call, phone (E.164), future, slot per same rules as `check_slot`, writes `audit_log`, delivers `readback` for read-aloud; same `idempotency_key` delivers same response without second transaction
- `api/core/ids.py`: deterministic idempotency keys for callers without own key; `api/domain/customers/phone.py`: E.164 normalization of German formats
- `create_reservation` locks check and create per tenant and day (`pg_advisory_xact_lock`) so concurrent calls can't overbooking a window; `readback` grounds "today" and "tomorrow" to creation time so replay after midnight delivers same sentence
- Docker Compose for Postgres, API, n8n
- Specs docs/00 through docs/15
- Slash commands for Claude Code in .claude/commands/
- CI workflow (ruff, pytest)
- Desktop mockup of admin view in gui/mockups/

### Changed
- Pilot operation, company, location, and provider names replaced with placeholders (`<PilotOperation>`, `<CompanyName>`, `<Location>`, `<PointOfSale>`, `<POSProvider>`, `example.com`)
- README quick start reduced to steps that work today; `make migrate` and `make seed` follow in T-1.1 and T-1.2
- Compose additionally mounts `scripts/` and `evals/` so `make lint` runs in container

### Fixed
- Dockerfile: additional CA cert (`api/ca-bundle.crt`) is optional, not required; build works on clean checkout
- Dockerfile: source code lands under `/app/api` again so `uvicorn api.main:app` starts without bind mount
- Compose: n8n listens on `0.0.0.0` not `::`, avoids crash loop on Docker hosts without IPv6
- `check_slot`: previous day's windows crossing midnight also apply to requests after midnight
- `get_service_status`: paused delivery doesn't count as open; "Pickup available" only with open pickup window

### Open
- Writable stage-0 tools `create_reservation`, `confirm`, `create_callback`, `transfer_to_team` (T-1.5 through T-1.8)
