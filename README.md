# Maex Voice-Agent

Telefonische Bestellannahme für Gastronomiebetriebe. Ein KI-Agent nimmt Anrufe auf der Festnetznummer an, erledigt Reservierung, Abholung und Lieferung und übergibt bestätigte Vorgänge an Küche, Kasse und Team. Beschwerden und Sonderfälle gehen an einen Menschen. Das Team steuert den Betrieb über eine Browser-Oberfläche auf dem Tablet.

Telefonie, Spracherkennung und Sprachausgabe laufen bei einem EU-gehosteten Anbieter. Dieses Repository enthält die Fachlogik, die Datenbank, die Oberfläche und die Tests. Pilotbetrieb ist Yoki Yoki in Sinzheim.

## Status

| | |
|---|---|
| Version | 0.0.1 |
| Stufe | 0, Fundament |
| Nächstes Gate | G0: Anbieter, Recht, Budget geklärt |
| Stand | 16.09.2026 |

Was funktioniert:

- API startet, `/health` antwortet, Token-Auth greift, drei Tests laufen grün
- Spezifikationen für Architektur, Datenmodell, Tools, Dialog, GUI und Evals liegen unter `docs/`

Was noch nicht funktioniert:

- Kein Telefonanschluss, kein Anbieter gewählt (Entscheidung D1)
- Keine Datenbanktabellen, keine Tools, keine Oberfläche
- `docker compose up` wurde noch nicht gegen echtes Docker ausgeführt

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
make test
```

Die API läuft danach unter `http://localhost:8000`, n8n unter `http://localhost:5678`.

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

## Bekannte Probleme

- Docker Hub begrenzt anonyme Image-Downloads. Schlägt `make up` mit einem Rate-Limit fehl: `docker login` mit einem kostenlosen Docker-Hub-Konto, danach erneut starten.

## Lizenz und Kontakt

Proprietär, Yoki Yoki GmbH. Kontakt: Maximilian Dumler, maxi.dumler@gmail.com.
