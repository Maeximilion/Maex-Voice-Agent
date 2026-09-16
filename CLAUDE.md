# CLAUDE.md – Arbeitsanweisung für Claude Code

> Lies diese Datei zuerst, dann `docs/01_STATUS.md`. Danach weißt du, wo das Projekt steht und was als Nächstes dran ist.
> Version 1.1 · 16.09.2026

---

## 1. Was wir bauen

Ein KI-Agent nimmt Anrufe auf der Festnetznummer des Restaurants **<Pilotbetrieb>** (<Ort>) an und erledigt **Reservierung, Abholung und Lieferung**. Beschwerden und Sonderfälle gehen an einen Menschen. Das Team steuert alles über eine Browser-GUI auf dem Tablet.

**Aufgabenteilung:** Eine externe Voice-Plattform macht Telefonie, Spracherkennung und Stimme. Wir bauen die **Logik, die Datenbank und die Oberfläche**. Der Agent ruft unsere Tools per HTTPS auf.

**Platzhalter:** Betriebs-, Firmen-, Orts- und Anbieternamen stehen in Code und Doku als `<Pilotbetrieb>`, `<Firmenname>`, `<Ort>`, `<Kassensystem>`, `<Kassenanbieter>` und `example.com`. Die echten Werte kommen aus `.env` und der Datenbank, nie ins Repo. Im Code keine Emojis.

---

## 2. Harte Regeln (nicht verhandelbar)

Diese sechs Regeln stehen über jeder Bequemlichkeit. Wenn eine Aufgabe sie verletzen würde: **stoppen und fragen**.

1. **KI versteht, Code entscheidet.** Preise, Zonen, Öffnungszeiten, Verfügbarkeit und Allergene kommen **ausschließlich** aus der Datenbank. Nie aus dem Modell, nie hartkodiert, nie geschätzt.
2. **Nie raten.** Ohne eindeutige `menu_item_id` kommt keine Position in die Bestellung. Bei Unsicherheit greift die Verständnis-Leiter (`docs/05_DIALOG_PROMPTS.md`), am Ende der Mensch.
3. **Nichts ohne Bestätigung.** Kein Vorgang verlässt den Entwurfsstatus ohne explizites „Ja" des Kunden. `confirm` ist der einzige Weg von `draft` nach `confirmed`.
4. **Fehler werden gemessen.** Vor jedem Merge laufen die Evals (`docs/08_EVALS.md`). Kein Gate ohne Zahlen.
5. **Jeder Ausfall endet beim Team.** Fällt irgendetwas aus, klingelt das Telefon beim Menschen. Kein Anruf geht verloren.
6. **Token-sparsam by design.** Kleinster Kontext, kleinstes Modell, das die Evals besteht. Das ganze Menü gehört nie in den Prompt.

---

## 3. Stack

| Ebene | Technik | Status |
|---|---|---|
| Sprache | Python 3.12 | Default |
| API (heißer Pfad) | FastAPI + Uvicorn, Pydantic v2 | Default |
| DB | PostgreSQL 16, SQLAlchemy 2 + Alembic | Default |
| GUI | FastAPI + Jinja2 + HTMX + SSE, Pico.css | Default, Alternative: React + Vite |
| Automation (kalter Pfad) | n8n, self-hosted per Docker | gesetzt |
| Tests | pytest, pytest-asyncio, httpx | Default |
| Lint/Format | ruff (Format + Lint), mypy im Nicht-Strict-Modus | Default |
| Betrieb | Docker Compose, Hosting in der EU | gesetzt |
| Voice-Plattform | offen → wird in Arbeitspaket C2 entschieden | offen |

„Default" = Vorschlag, kippbar. Wenn Maxi widerspricht, wird hier und in `docs/01_STATUS.md` nachgezogen.

**Warum HTMX statt React:** ein Container statt zwei, kein Node-Build, kein CORS, Live-Updates über Server-Sent-Events. Für eine Tablet-Oberfläche mit Listen und großen Knöpfen reicht das vollständig. Begründung in `docs/06_GUI.md`.

---

## 4. Ordnerstruktur

Der Aufbau ist modular, nach Schichten mit fester Abhängigkeitsrichtung. Vollständig mit Begründung und Bauplan: `docs/11_MODULE.md`. **Vor jeder neuen Datei dort nachsehen, wohin sie gehört.**

```text
maex-voice-agent/
├── CLAUDE.md              ← diese Datei
├── README.md
├── docker-compose.yml     Postgres · API · n8n (dev)
├── deploy/                Prod-Compose, Caddyfile
├── .claude/commands/      /start /task /done /bug /eval /handover
├── .github/workflows/     CI: ruff + pytest
├── docs/                  Planung, Specs, Status  →  Abschnitt 5
├── api/
│   ├── main.py            App-Einstieg, Router
│   ├── config.py          Settings aus .env
│   ├── db.py              Engine, Session
│   ├── core/              Antwort-Hülle, Fehler, Logging, Auth, IDs, Zeit
│   ├── domain/            Fachlogik ohne HTTP: menu · ordering · reservations
│   │                      · delivery · customers · status · callbacks
│   ├── agent/             eigener Gesprächs-Loop, Zustand, Leiter, Eskalation
│   ├── telephony/         Port + Adapter je Anbieter – der EINZIGE Ort, der ihn kennt
│   ├── tools/             dünne HTTP-Hülle /v1/tools/* um domain
│   ├── events/            Outbox + Dispatcher → n8n
│   ├── jobs/              Löschjob, Abgleich, Tagesbericht, Feiertage
│   ├── gui/               Router, SSE, Templates, Statisches
│   ├── models/  schemas/  SQLAlchemy · Pydantic
│   └── tests/
├── sim/                   Text-Telefon: Gespräche ohne Telefon
├── db/migrations/         Alembic, versioniert
├── prompts/               System-Prompt je Version
├── evals/                 cases/ · runner.py · reports/ (ignoriert)
├── n8n/                   Workflow-Exporte
└── scripts/               seed · import_menu · backup · restore
```

**Abhängigkeitsrichtung, kurz:** `tools`/`gui`/`sim`/`telephony` → `agent` → `domain` → `models`/`core`. Nie umgekehrt. `domain/` importiert kein FastAPI, kein HTTP, keinen Anbieter.

---

## 5. Die Dokumente

| Datei | Inhalt | Wann lesen |
|---|---|---|
| `docs/00_PCF.md` | Projekt-Gesamtplan, Stufen, Gates, Risiken | einmal zum Einstieg |
| `docs/01_STATUS.md` | **Wo stehen wir, was ist als Nächstes dran** | zu Beginn jeder Session |
| `docs/02_ARCHITEKTUR.md` | Komponenten, heißer/kalter Pfad, Anrufablauf, Ausfallverhalten | vor Arbeit an API oder n8n |
| `docs/03_DATENMODELL.md` | Tabellen, Felder, DDL je Stufe | vor jeder Migration |
| `docs/04_API_TOOLS.md` | Vertrag je Tool: Request, Response, Fehler, Latenzbudget | vor Arbeit an `api/tools/` |
| `docs/05_DIALOG_PROMPTS.md` | Gesprächsflüsse, System-Prompt, Verständnis-Leiter, Eskalation | vor Arbeit an `prompts/` |
| `docs/06_GUI.md` | Screens, Komponenten, Zustände, Bedienregeln | vor Arbeit an `gui/` |
| `docs/07_ARBEITSPAKETE.md` | **Aufgabenliste T-x.y mit Abhängigkeiten und Definition of Done** | zur Aufgabenwahl |
| `docs/08_EVALS.md` | Testfall-Format, Metriken, Regressionslauf | vor jedem Merge |
| `docs/09_BETRIEB_RECHT.md` | Runbook, Notfälle, Rechts-Checkliste | vor jedem Go-live-Schritt |
| `docs/11_MODULE.md` | **Schichten, Abhängigkeitsregeln, Bauplan je Modul, Tests je Modul** | vor jeder neuen Datei |
| `docs/12_CLAUDE_CODE_PLAYBOOKS.md` | neun Session-Abläufe (Feature, Bug, Migration, Prompt, Import, Adapter, Deploy …) | zu Session-Beginn, je nach Situation |
| `docs/13_DEPLOYMENT.md` | Tunnel für Testanrufe, EU-Server, Caddy, Backups, CI | vor dem ersten Testanruf |
| `docs/14_MENU_IMPORTFORMAT.md` | CSV-Vertrag zwischen Chat (Digitalisierung) und Import | vor T-4.2 |
| `docs/15_README_STRATEGY.md` | Wann und wie README und CHANGELOG gepflegt werden, Versionierung je Gate | bei jedem Gate, bei neuen Abhängigkeiten |

---

## 6. So arbeitest du

### Slash-Befehle (`.claude/commands/`)
`/start` Session beginnen · `/task T-x.y` Aufgabe bauen · `/done` abschließen · `/bug "…"` Fehler mit rotem Eval-Fall zuerst · `/eval` Suite laufen und bewerten · `/gate Gx` Gate abschließen, README und Version nachziehen · `/handover` Übergabeblock. Abläufe im Detail: `docs/12_CLAUDE_CODE_PLAYBOOKS.md`.

### Session-Start
1. `docs/01_STATUS.md` lesen → aktuelle Stufe und offene Aufgaben
2. `docs/07_ARBEITSPAKETE.md` → nächste Aufgabe mit erfüllten Abhängigkeiten wählen
3. Die Spec zur Aufgabe lesen (Spalte „Spec" in der Aufgabenliste)
4. Kurzen Plan zeigen (max. 5 Zeilen), dann bauen

### Der Loop
**Plan → Bauen → Ausführen → Bewerten → Weiterdenken.** Nach jeder Aufgabe selbstständig bis zu **3 Folgeschritte** in Richtung Ziel machen (Tests ergänzen, offensichtliche Lücke schließen, Doku nachziehen), dann Ergebnis melden. Größere Scope-Erweiterungen nur als Vorschlag.

### Rückfragen
Geschlossen stellen (Ja/Nein oder A/B/C mit markierter Empfehlung), **eine pro Unterbrechung**, und genau dann, wenn die Antwort gebraucht wird. Recherchierbares selbst recherchieren. Was du annimmst, markierst du als Annahme und schreibst es in `docs/01_STATUS.md`.

**Entscheidungen mit Empfehlung nimmst du selbst ab** (Maxi, 16.09.2026): Plan zeigen, Empfehlung nennen, weiterbauen. Warten nur, wenn es um Geld, Recht, Außenwirkung, Produktivdaten oder Irreversibles geht (§10).

### Session-Ende
`docs/01_STATUS.md` aktualisieren: erledigte Aufgaben, neue Erkenntnisse, nächster Schritt. Dazu einen Übergabeblock nach `docs/00_PCF.md` Abschnitt 12 ausgeben.

---

## 7. Definition of Done

Eine Aufgabe ist fertig, wenn **alle** Punkte stimmen:

- [ ] Code läuft, wurde **wirklich ausgeführt**, nicht nur geschrieben
- [ ] Tests für Normalfall **und** mindestens zwei Grenzfälle, grün
- [ ] `ruff format` und `ruff check` sauber
- [ ] Für Tools im heißen Pfad: Antwortzeit gemessen, < 300 ms bei lokaler DB
- [ ] Schema-Änderung als Alembic-Migration, up **und** down getestet
- [ ] Betroffene Doku in `docs/` nachgezogen
- [ ] README aktualisiert, falls sich Schnellstart, Voraussetzungen, Konfiguration oder bekannte Probleme geändert haben (`docs/15_README_STRATEGY.md`)
- [ ] `docs/01_STATUS.md` aktualisiert
- [ ] Commit nach Conventional Commits, `main` bleibt lauffähig

---

## 8. Konventionen

**Code**
- Preise **immer** als Integer in Cent. Kein Float für Geld, nirgends.
- Telefonnummern im Format E.164 (`+4972215551234`), normalisiert beim Eingang.
- Zeiten in UTC speichern, in `Europe/Berlin` anzeigen.
- Jeder Vorgang trägt eine `call_id`. Ohne `call_id` kein Schreibvorgang.
- Schreibende Tools sind idempotent: gleicher `idempotency_key` → gleiches Ergebnis, kein zweiter Vorgang.
- Fehler geben strukturiertes JSON zurück, nie einen Stacktrace an den Agenten.
- Deutsche Kommentare und Fehlermeldungen, englische Bezeichner im Code.
- Keine Emojis in Code, Doku, Commits oder Oberfläche. Status wird mit Wörtern ausgedrückt.

**Git**
- Conventional Commits: `feat(tools): check_delivery mit Polygon-Prüfung`
- Ein Commit = eine abgeschlossene Aufgabe
- `main` bleibt immer lauffähig

**Sicherheit**
- `.env`, echte Aufnahmen, Transkripte und Kundendaten kommen **nie** ins Repo
- API-Schlüssel nur über Umgebungsvariablen
- Die Agent-API ist nur über Token erreichbar (`AGENT_API_TOKEN`)
- Vor jeder Migration auf Echtdaten: Backup (`scripts/backup.sh`)

---

## 9. Was du nie tust

- Preise, Öffnungszeiten, Lieferzonen oder Allergene im Code oder Prompt hartkodieren
- Einen Vorgang ohne Kundenbestätigung final buchen
- Eine Allergen-Auskunft geben, die nicht als gepflegter DB-Wert vorliegt
- Echte Anrufaufnahmen verarbeiten, bevor der Rechts-Check in `docs/09_BETRIEB_RECHT.md` abgehakt ist
- Auf Produktivdaten ohne Backup migrieren
- Tests als „geht schon" überspringen oder grün behaupten, ohne sie laufen zu lassen
- Das komplette Menü in den System-Prompt schreiben
- Fachlogik in `tools/` oder `gui/` ablegen statt in `domain/`
- Einen Anbieternamen außerhalb von `telephony/` verwenden
- n8n oder eine externe API direkt aus `domain/` aufrufen (immer über die Outbox)
- Einen Fehler beheben, ohne vorher einen roten Eval- oder Unit-Test dafür zu haben

---

## 10. Was in Claude Code läuft und was im Chat bleibt

| Hier in Claude Code | In den Claude-Chats (siehe `docs/00_PCF.md` Abschnitt 10) |
|---|---|
| API, Tools, Datenmodell, Migrationen | C1 Ist-Aufnahme, Baseline, Rechts-Check |
| GUI | C2 Anbieter-Recherche und Auswahl |
| Eval-Runner und Testfälle | Verhandlungen, Verträge, Budgetentscheidungen |
| n8n-Workflows als Code-Export | Gate-Entscheidungen mit Maxi |
| Import- und Betriebsskripte | Entscheidungen, die Geld oder Recht berühren |

Alles, was Geld kostet, einen Vertrag auslöst oder rechtlich bindet, entscheidet Maxi im Chat. Du baust.
