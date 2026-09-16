# Changelog

Format nach Keep a Changelog. Versionen folgen den Gates, siehe docs/15_README_STRATEGY.md.

## [Unreleased]

### Hinzugefügt
- Projektgerüst: FastAPI-App mit /health, Token-Auth, einheitlicher Antwort-Hülle
- `api/db.py`: Engine mit Verbindungs-Ping, Session je Request über `get_db`, Tests gegen echte Postgres
- `api/core/`: Antwort-Hülle, Fehlerklassen mit den acht Codes aus docs/04, Token-Auth, JSON-Logging mit `request_id`/`call_id`, Zeit-Helfer mit Betriebstag
- Alembic unter `db/` und Migration 001 mit den zehn Stufe-1-Tabellen; SQLAlchemy-Modelle unter `api/models/`; `make migrate` legt das Schema an
- `scripts/seed.py`: idempotente Testkonfiguration (Mandant, Live-Schalter, Öffnungszeiten, Kapazität), `make seed`
- Tool `POST /v1/tools/get_service_status`: offen/geschlossen je Service, Sondertage schlagen Wochentage, Fenster über Mitternacht, Wartezeiten und Modus aus `service_config`, Vorlesesatz zur nächsten Öffnung; Latenz-Helfer `p95_ms` mit 300-ms-Budget in den Tests
- Tool `POST /v1/tools/check_slot`: Verfügbarkeit aus `capacity` und aktiven Reservierungen innerhalb der `dinein`-Öffnungszeit, bis zu zwei Alternativen im Raster, Vorlesesatz mit gesprochenen Uhrzeiten
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
- `check_slot`: Fenster des Vortags, die über Mitternacht reichen, gelten auch für Wünsche nach Mitternacht
- `get_service_status`: pausierte Lieferung zählt nicht als offen; „Abholung ist möglich" nur bei offenem Abholfenster

### Offen
- Schreibende Stufe-1-Tools `create_reservation`, `confirm`, `create_callback`, `transfer_to_team` (T-1.5 bis T-1.8)
