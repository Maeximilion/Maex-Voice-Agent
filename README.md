# Maex Voice-Agent

Telefonische Bestellannahme für Gastronomiebetriebe. Ein KI-Agent nimmt Anrufe auf der Festnetznummer an, erledigt Reservierung, Abholung und Lieferung und übergibt bestätigte Vorgänge an Küche, Kasse und Team. Beschwerden und Sonderfälle gehen an einen Menschen. Das Team steuert den Betrieb über eine Browser-Oberfläche auf dem Tablet.

Telefonie, Spracherkennung und Sprachausgabe laufen bei einem EU-gehosteten Anbieter. Dieses Repository enthält die Fachlogik, die Datenbank, die Oberfläche und die Tests. Pilotbetrieb, Ort, Domain und Kassenanbieter stehen in den Dokumenten als Platzhalter in spitzen Klammern.

[![CI](https://github.com/Maeximilion/Maex-Voice-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Maeximilion/Maex-Voice-Agent/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![PostgreSQL 16](https://img.shields.io/badge/postgresql-16-blue)](https://www.postgresql.org/)
[![Lizenz](https://img.shields.io/badge/lizenz-proprietär-lightgrey)](#lizenz-und-kontakt)

## Status

| | |
|---|---|
| Version | 0.0.1 |
| Stufe | 0, Fundament |
| Nächstes Gate | G0: Anbieter, Recht, Budget geklärt |
| Stand | 17.09.2026 |

Was funktioniert:

- `make up` baut das API-Image und startet Postgres, API, Dispatcher und n8n; `/health` antwortet, Token-Auth greift
- Jede Antwort der Agent-API folgt der Hülle aus `docs/04_API_TOOLS.md`; Fehler kommen als JSON mit Code und Vorlesesatz, nie als Stacktrace
- Datenbankzugang mit einer Session je Request (`api/db.py`), Logs als JSON-Zeilen mit `request_id` und `call_id`
- `make migrate` legt die zehn Tabellen der Stufe 1 an (Alembic unter `db/`, Modelle unter `api/models/`), `make seed` füllt sie idempotent mit einer Testkonfiguration
- Die Reservierung läuft durch, jedes Tool mit Latenztest gegen das 300-ms-Budget: `POST /v1/tools/get_service_status` beantwortet aus der Datenbank, ob und was gerade geht (Öffnungszeiten, Sondertage, Wartezeiten, Modus); `POST /v1/tools/check_slot` prüft einen Wunsch gegen Kapazität und Öffnungszeit und nennt bis zu zwei Alternativen; `POST /v1/tools/create_reservation` legt den Entwurf mit dem Satz zum Vorlesen an; `POST /v1/tools/confirm` macht ihn gültig, protokolliert ihn und legt das Ereignis für den kalten Pfad in die Outbox
- Der Dispatcher (`api/events/`) leert die Outbox nach n8n: eigener Prozess (`python -m api.events.dispatcher`), ein POST je Ereignis mit der Ereignis-id als Idempotenz-Schlüssel, Backoff 5 s / 30 s / 2 min / 10 min, danach `failed` mit Alarm im Log
- `make test` und `make lint` laufen im Container gegen die echte Postgres, ruff sauber
- Spezifikationen für Architektur, Datenmodell, Tools, Dialog, GUI und Evals liegen unter `docs/`

Was noch nicht funktioniert:

- Kein Telefonanschluss, kein Anbieter gewählt (Entscheidung D1)
- Keine Oberfläche, kein Gesprächs-Kern, keine Bestellungen (Stufe 2)
- In n8n liegt noch kein Workflow, der die Ereignisse des Dispatchers entgegennimmt
- Die Testkonfiguration aus `make seed` (Öffnungszeiten, Kapazität) ist ein Platzhalter, bis die Ist-Aufnahme des Betriebs vorliegt

## Voraussetzungen

- Docker und Docker Compose
- Git
- Für Entwicklung ohne Docker: Python 3.12

## Schnellstart

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

Die API läuft danach unter `http://localhost:8000`, n8n unter `http://localhost:5678`. `/health` antwortet mit `{"status": "ok", "env": "dev"}`.

Tests und Lint aus einer lokalen Python-Umgebung. Die Datenbanktests brauchen eine erreichbare Postgres, zum Beispiel die aus `make up`; `DATABASE_URL` zeigt dann auf `localhost` statt auf den Container-Namen `db`:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r api/requirements.txt
export DATABASE_URL=postgresql+psycopg://maex:maex@localhost:5432/maex_agent
pytest -q
ruff check api scripts evals && ruff format --check api scripts evals
```

## Konfiguration

Alle Einstellungen kommen aus `.env`. Vorlage und Beschreibung aller Variablen: `.env.example`. Die wichtigsten:

| Variable | Bedeutung |
|---|---|
| `DATABASE_URL` | Verbindung zur Postgres-Datenbank |
| `AGENT_API_TOKEN` | Bearer-Token, mit dem die Voice-Plattform die Tools aufruft |
| `TEAM_PHONE` | Durchwahl für Weiterleitungen an das Team |
| `MAX_CALL_SECONDS` | Obergrenze für die Anrufdauer |

## Projektstruktur

```text
api/         FastAPI-Anwendung: core, domain, agent, telephony, tools, events, jobs, gui
sim/         Text-Telefon: Gespräche mit dem Agenten ohne Telefonie
db/          Alembic-Migrationen
prompts/     System-Prompt je Version
evals/       Testfälle und Runner für die Gesprächsqualität
n8n/         Workflow-Exporte für den kalten Pfad
deploy/      Produktions-Compose und Caddyfile
docs/        Spezifikationen und Status
```

Schichten und Abhängigkeitsregeln: `docs/11_MODULE.md`.

## Entwicklung

```bash
make test        # pytest
make lint        # ruff check
make fmt         # ruff format
make eval        # Eval-Suite, optional TAGS=menu,noise
```

Das Projekt ist für die Arbeit mit Claude Code eingerichtet. `CLAUDE.md` enthält die Arbeitsanweisung, `.claude/commands/` die Befehle `/start`, `/task`, `/done`, `/bug`, `/eval`, `/gate` und `/handover`. Abläufe: `docs/12_CLAUDE_CODE_PLAYBOOKS.md`.

## Dokumentation

| Datei | Inhalt |
|---|---|
| `CLAUDE.md` | Arbeitsanweisung, Regeln, Stack, Konventionen |
| `docs/00_PCF.md` | Gesamtplan mit Stufen, Gates, Risiken |
| `docs/01_STATUS.md` | aktueller Stand, offene Entscheidungen, Blocker |
| `docs/02_ARCHITEKTUR.md` | Komponenten, Anrufablauf, Ausfallverhalten |
| `docs/03_DATENMODELL.md` | Tabellen und Felder je Stufe |
| `docs/04_API_TOOLS.md` | Tool-Verträge mit JSON-Beispielen |
| `docs/05_DIALOG_PROMPTS.md` | Gesprächsflüsse, System-Prompt, Eskalation |
| `docs/06_GUI.md` | Screens und Bedienregeln |
| `docs/07_ARBEITSPAKETE.md` | Aufgabenliste mit Abhängigkeiten |
| `docs/08_EVALS.md` | Testfälle und Metriken |
| `docs/09_BETRIEB_RECHT.md` | Runbook und Rechts-Checkliste |
| `docs/10_GLOSSAR.md` | Begriffe |
| `docs/11_MODULE.md` | Schichten, Abhängigkeitsregeln, Bauplan je Modul |
| `docs/12_CLAUDE_CODE_PLAYBOOKS.md` | Session-Abläufe und Slash-Befehle |
| `docs/13_DEPLOYMENT.md` | Betriebsorte, Tunnel, EU-Server, Backups, CI |
| `docs/14_MENU_IMPORTFORMAT.md` | CSV-Format für die Menü-Digitalisierung |
| `docs/15_README_STRATEGY.md` | Wann und wie diese README gepflegt wird |

## Mitarbeiten

Ablauf, Branch-Namen, Commit-Format und Labels stehen in [CONTRIBUTING.md](CONTRIBUTING.md). Der Fahrplan bis zum Zielzustand steht in [`docs/01_STATUS.md`](docs/01_STATUS.md), die Aufgabenliste in [`docs/07_ARBEITSPAKETE.md`](docs/07_ARBEITSPAKETE.md); jedes Arbeitspaket hat ein Issue, jeder Block ein Sammel-Issue.

Sicherheitslücken bitte vertraulich melden, nicht als Issue: [SECURITY.md](SECURITY.md).

## Bekannte Probleme

- Docker Hub begrenzt anonyme Image-Downloads. Schlägt `make up` mit einem Rate-Limit fehl: `docker login` mit einem kostenlosen Docker-Hub-Konto, danach erneut starten.
- Hinter einem TLS-terminierenden Proxy schlägt `pip install` im Image-Build mit `CERTIFICATE_VERIFY_FAILED` fehl. Abhilfe: das CA-Zertifikat des Proxys als `api/ca-bundle.crt` ablegen (ist in `.gitignore`), der Build bindet es dann automatisch ein.

## Lizenz und Kontakt

Proprietär, <Firmenname>. Alle Rechte vorbehalten, siehe [LICENSE](LICENSE). Nutzung, Vervielfältigung und Weitergabe nur mit schriftlicher Genehmigung.

Kontakt: Maximilian Dumler, maxi.dumler@gmail.com.
