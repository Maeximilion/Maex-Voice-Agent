# Changelog

Format nach Keep a Changelog. Versionen folgen den Gates, siehe docs/15_README_STRATEGY.md.

## [Unreleased]

### Hinzugefügt
- Projektgerüst: FastAPI-App mit /health, Token-Auth, einheitlicher Antwort-Hülle
- `api/db.py`: Engine mit Verbindungs-Ping, Session je Request über `get_db`, Tests gegen echte Postgres
- `api/core/`: Antwort-Hülle, Fehlerklassen mit den acht Codes aus docs/04, Token-Auth, JSON-Logging mit `request_id`/`call_id`, Zeit-Helfer mit Betriebstag
- Docker Compose für Postgres, API und n8n
- Spezifikationen docs/00 bis docs/15
- Slash-Befehle für Claude Code unter .claude/commands/
- CI-Workflow (ruff, pytest)
- Desktop-Mockup der Adminansicht unter gui/mockups/

### Geändert
- Betriebs-, Firmen-, Orts- und Anbieternamen durch Platzhalter ersetzt (`<Pilotbetrieb>`, `<Firmenname>`, `<Ort>`, `<Kassensystem>`, `<Kassenanbieter>`, `example.com`)
- README-Schnellstart auf die heute lauffähigen Schritte reduziert; `make migrate` und `make seed` folgen mit T-1.1 und T-1.2
- Compose mountet zusätzlich `scripts/` und `evals/`, damit `make lint` im Container läuft

### Behoben
- Dockerfile: Zusatz-CA-Zertifikat (`api/ca-bundle.crt`) ist optional statt Pflicht, Build läuft auf sauberem Checkout
- Dockerfile: Quellcode landet wieder unter `/app/api`, damit `uvicorn api.main:app` auch ohne Bind-Mount startet
- Compose: n8n lauscht auf `0.0.0.0` statt `::`, sonst Crash-Schleife auf Docker-Hosts ohne IPv6

### Offen
- Alembic-Migration 001 und Seed-Skript (T-1.1, T-1.2)
