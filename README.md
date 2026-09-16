# Yoki Voice-Agent

KI-Telefonannahme für das Restaurant Yoki Yoki: Reservierung, Abholung, Lieferung.

## Schnellstart

```bash
cp .env.example .env        # Werte eintragen
make up                     # Postgres + API starten
make migrate                # Schema anlegen
make seed                   # Testdaten für Yoki Yoki
make test                   # Tests
open http://localhost:8000/gui
```

## Für Claude Code

**Tippe `/start`.** Der Befehl liest `CLAUDE.md` und `docs/01_STATUS.md` und schlägt die nächste Aufgabe vor.
Die Aufgabenliste ist `docs/07_ARBEITSPAKETE.md`.

## Dokumente

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
| `docs/13_DEPLOYMENT.md` | Tunnel, EU-Server, Caddy, Backups, CI |
| `docs/14_MENU_IMPORTFORMAT.md` | CSV-Format für die Menü-Digitalisierung |

## Status

Stufe 0 (Fundament) · Stand 16.09.2026 · Nächstes Gate: **G0 Go/No-Go** (🔴 offen)

Lauffähiges API-Minimalgerüst (FastAPI `/health`, Token-Auth, einheitliche Antwort-Hülle, 3 Tests grün). Betriebs- und Adminansicht als GUI-Mockup abgenommen. Datenbank, Tools und produktive GUI fehlen noch. Details und nächste Schritte: `docs/01_STATUS.md`.
