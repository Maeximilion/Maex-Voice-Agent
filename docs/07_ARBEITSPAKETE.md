# 07 – Arbeitspakete

> Die Aufgabenliste für Claude Code. Eine Aufgabe ist startklar, wenn alle Abhängigkeiten erledigt sind.
> Status je Aufgabe: offen · in Arbeit · fertig · blockiert (Abhängigkeit oder Entscheidung fehlt)
>
> **Diese Datei ist die Quelle der Wahrheit.** Auf GitHub existiert ein Spiegel: je Block ein Sammel-Issue, je Arbeitspaket ein Sub-Issue darunter (Spalte „Issue"). Der Spiegel dient der Sichtbarkeit und der Verknüpfung mit Pull Requests; Abhängigkeiten, Specs und der Status werden hier gepflegt und nicht dort.

---

## Aufteilung: Chat gegen Claude Code

| Ebene | Wo | Was |
|---|---|---|
| **Entscheidung** | Claude-Chat (C0–C2) | Anbieter, Budget, Recht, Gates, Ist-Aufnahme |
| **Bau** | Claude Code | alles unter `api/`, `db/`, `gui/`, `evals/`, `n8n/`, `scripts/` |
| **Messung** | Claude Code baut, Maxi bewertet | Eval-Läufe, Latenzmessung, Rollenspiel-Protokolle |

Die Chats C1 bis C8 aus `docs/00_PCF.md` Abschnitt 10 bleiben bestehen. Alles, was dort Code ist, ist hier eine Aufgabe.

---

## Block 0 – Gerüst (startklar, kein Anbieter nötig)  ·  Sammel-Issue #5

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-0.1 | #14 | `docker-compose.yml`: Postgres 16 + API. `docker compose up` läuft, `/health` antwortet `{"status":"ok"}` | CLAUDE.md §3 | – | fertig 16.09.2026: Postgres + API healthy, `/health` geprüft, Codex-Findings am Dockerfile behoben |
| T-0.2 | #15 | FastAPI-Grundgerüst: `main.py`, `config.py` (Settings aus `.env`), `db.py`, Fehler-Handler mit der Antwort-Hülle aus 04 §1 | 04 §1 | T-0.1 | fertig 16.09.2026: `db.py` mit Engine, `SessionLocal`, `get_db`; 4 Tests gegen echte Postgres |
| T-0.3 | #16 | Token-Auth als Dependency, greift für alle `/v1/tools/*` | 02 §7 | T-0.2 | fertig, mit Test |
| T-0.4 | #17 | pytest, ruff, Makefile mit `make test`, `make lint`, `make up`, `make migrate` | CLAUDE.md §7 | T-0.2 | fertig 16.09.2026: `make test`, `make lint`, `make fmt`, `make up`, `make migrate` laufen im Container, ruff format + check sauber |
| T-0.5 | #18 | Latenz-Testhelfer: misst p95 je Tool-Endpunkt, schlägt über 300 ms fehl | 04 §1 | T-0.4 | fertig 16.09.2026: `p95_ms` in `api/tests/conftest.py`, jeder Tool-Test hat einen Latenztest |
| T-0.6 | #19 | `core/`: Antwort-Hülle, Fehlerklassen → Hülle, JSON-Logging mit `call_id`/`request_id`, Zeit-Helfer | 11 §core | T-0.2 | fertig 16.09.2026: `envelope`, `errors`, `auth`, `logging`, `time`; `ids.py` seit T-1.5 |
| T-0.7 | #20 | Slash-Befehle in `.claude/commands/` einmal durchspielen, CI-Workflow grün bekommen | 12, 13 §6 | T-0.4 | offen |
| T-0.8 | #21 | `domain/menu/numberwords.py`: deutsche Zahlwörter und Mengen, ≥ 100 Unit-Tests, ohne DB | 11 §menu | T-0.6 | offen |

---

## Block 1 – Stufe 1: Reservierung  ·  Sammel-Issue #6

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-1.1 | #22 | Alembic einrichten, Migration 001 (Stufe-1-Tabellen), `up`/`down` getestet | 03 | T-0.2 | fertig 16.09.2026: `db/alembic.ini`, `env.py`, `versions/001_stufe1.py`, Modelle `api/models/`; up/down/up gegen Wegwerf-DB getestet, Modelle und Schema ohne Diff |
| T-1.2 | #23 | Seed-Skript `scripts/seed.py`: Mandant <Pilotbetrieb>, Öffnungszeiten, Kapazität, Testkonfiguration | 03 | T-1.1 | fertig 16.09.2026: `scripts/seed.py`, idempotent, Live-Schalter bleibt unangetastet, Werte sind Platzhalter bis C1 |
| T-1.3 | #24 | Tool `get_service_status` inkl. Sondertage und Wartezeiten | 04 | T-1.2 | fertig 16.09.2026: `domain/status/` + `tools/service_status.py`, 16 Tests, p95 weit unter 300 ms |
| T-1.4 | #25 | Tool `check_slot`: Verfügbarkeit plus bis zu 2 Alternativen | 04 | T-1.2 | fertig 16.09.2026: `domain/reservations/{capacity,slots,spoken}.py` + `tools/check_slot.py`, 23 Tests, p95 rund 13 ms |
| T-1.5 | #26 | Tool `create_reservation` als `draft`, mit `readback` und Idempotenz | 04 | T-1.4 | fertig 16.09.2026: `domain/reservations/create.py` + `tools/create_reservation.py`, `core/ids.py`, `domain/customers/phone.py` (E.164), 40 Tests, p95 rund 15 ms |
| T-1.6 | #27 | Tool `confirm` generisch (Reservierung und Bestellung), `audit_log`, Ereignis-Warteschlange | 04 | T-1.5 | fertig 17.09.2026: `domain/confirm.py` + `tools/confirm.py`, Zeilensperre gegen doppelte Ereignisse, Outbox-Eintrag `reservation.confirmed`, 17 Tests, p95 rund 10 ms. Bestellungen sind im Vertrag vorgesehen, aber bis Stufe 2 `not_found` |
| T-1.7 | #28 | Tool `create_callback` | 04 | T-1.1 | offen |
| T-1.8 | #29 | Tool `transfer_to_team` inkl. Schleifenschutz und Erreichbarkeitsprüfung | 04 | T-1.1 | offen |
| T-1.9 | #30 | Anruf-Log: `POST /v1/calls/start` und `/end`, Tool-Aufrufe mit Dauer | 03 | T-1.1 | offen |
| T-1.10 | #31 | System-Prompt `prompts/system_v1.md` plus Tool-Beschreibungen für die Plattform | 05 | T-1.3…T-1.8 | offen |
| T-1.11 | #38 | **Anbieter-Adapter** `telephony/adapters/<anbieter>.py` gegen aufgezeichnete Webhooks | 11 §telephony, 12 S7 | T-1.13, D1 entschieden | blockiert |
| T-1.12 | #32 | `events/`: Outbox schreiben in `confirm`, Dispatcher mit Backoff, Fake-n8n im Test, Alarm bei `failed` | 11 §events, 03 | T-1.6 | offen |
| T-1.13 | #33 | `telephony/port.py` Interface + `adapters/fake.py`, der Anrufe aus Dateien abspielt | 11 §telephony | T-1.10 | offen |

---

## Block 1b – Stufe 1: Gesprächs-Kern und Simulator  ·  Sammel-Issue #7

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-2.1 | #34 | `agent/`: Prompt-Aufbau mit Menü-Index, Zustand, Loop, Dispatch auf `domain`, Fake-LLM für Tests | 11 §agent, 05 | T-1.10, T-0.6 | offen |
| T-2.2 | #35 | `agent/ladder.py` + `escalation.py`: Verständnis-Leiter als Zustandsmaschine, Eskalations-Auslöser vor dem Modell | 05 §2, §4 | T-2.1 | offen |
| T-2.3 | #36 | `sim/cli.py` + `sim/replay.py`: erstes Gespräch im Terminal, Reservierung landet in der DB | 11 §sim | T-2.1, T-1.5 | offen |
| T-2.4 | #37 | `agent/llm.py` gegen ein echtes Modell, Token-Zählung, Kosten je Gespräch im Log | 05 §5 | T-2.3 | offen |
| T-2.5 | #39 | Sim-Konsole in der GUI (`gui/dev/console.html`), nur `ENV=dev` | 06, 11 §gui | T-2.3, T-3.1 | offen |

**→ Meilenstein „Durchstich ohne Telefon"** nach T-2.3 und T-3.3: Terminal → Agent → DB → Tablet.

## Block 2 – Stufe 1: GUI-Grundlage  ·  Sammel-Issue #8

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-3.1 | #40 | GUI-Gerüst: Jinja2, HTMX, `app.css` aus den Variablen und Klassen von `gui/mockups/admin-desktop.html`, Layout mit Kopfzeile | 06, gui/mockups | T-0.2, D6 | offen |
| T-3.2 | #41 | Kopfzeile live: KI-Modus, Lieferung an/aus, Wartezeit, Not-Aus-Knopf | 06 §3 | T-3.1, T-1.3 | offen |
| T-3.3 | #42 | Spalte „Heute": Reservierungen mit Live-Aktualisierung über SSE | 06 §3 | T-3.1, T-1.5 | offen |
| T-3.4 | #43 | Spalte „Rückrufe" mit Ton und Erledigt-Knopf | 06 §3 | T-3.1, T-1.7 | offen |
| T-3.5 | #44 | Bedientest: ein Teammitglied bedient 5 Minuten ohne Erklärung, Protokoll | 06 §1 | T-3.2…T-3.4 | offen |

**→ Gate G1** nach T-1.11 und T-3.5: 20 Rollenspiel-Anrufe, Latenz, Kosten, Ausfalltest.

---

## Block 3 – Stufe 2: Abholung  ·  Sammel-Issue #9

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-4.1 | #45 | Migration 002 (Menü, Bestellungen) | 03 | T-1.1 | offen |
| T-4.2 | #46 | Menü-Import `scripts/import_menu.py` nach Format 14, `--dry-run`, Prüfregeln, idempotent | 14, 11 §menu | T-4.1, CSV aus dem Chat | offen |
| T-4.3 | #47 | Tool `search_menu` mit der Auflösungsreihenfolge aus 04, Trigram-Index, Schwellen konfigurierbar | 04 | T-4.2 | offen |
| T-4.4 | #48 | Tool `get_item_details` inkl. Allergen-Regel „unbekannt ≠ keine" | 04 | T-4.2 | offen |
| T-4.5 | #49 | Tool `draft_order` mit allen Prüfungen und `readback` | 04 | T-4.3 | offen |
| T-4.6 | #50 | Übergabe über n8n mit `handover_state`: **B** Netzwerk-Bondrucker (ESC/POS) zuerst, **C** <Kassensystem>-Bestell-Eingang als zweiter Adapter nach Antwort von <Kassenanbieter>; Idempotenz, Wiederholung | 02 §2, 01 D2 | T-1.6 | offen |
| T-4.7 | #51 | GUI Spalte „Neue Bestellungen" mit Passt/Korrigieren und Korrekturgründen | 06 §3 | T-3.1, T-4.5 | offen |
| T-4.8 | #52 | GUI „Gericht aus" | 06 §3 | T-4.1 | offen |
| T-4.9 | #53 | Preis-Abgleich Kasse gegen Agent-DB als Skript; Abweichung als rotes Badge am Gericht in der Admin-Liste, übernehmen oder verwerfen | 02 §6, 06 §4 | T-4.2 | offen |
| T-5.1 | #54 | Eval-Runner `evals/runner.py`, Fall-Format, Report | 08 | T-4.5 | offen |
| T-5.2 | #55 | Eval-Suite v1: mindestens 100 Fälle (Menü, Mengen, Optionen, Störgeräusche, Eskalation) | 08 | T-5.1 | offen |
| T-5.3 | #56 | Modellvergleich über die Eval-Suite, Kosten je Anruf, Empfehlung | 05 §5 | T-5.2 | offen |

**→ Gate G2**

---

## Block 4 – Stufe 3: Lieferung  ·  Sammel-Issue #10

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-6.1 | #57 | Migration 003 (Kunden, Adressen, Zonen) | 03 | T-4.1 | offen |
| T-6.2 | #58 | Tool `find_customer` mit Normalisierung der Rufnummer | 04 | T-6.1 | offen |
| T-6.3 | #59 | Tool `check_delivery`, PLZ-Variante | 04 | T-6.1, D5 | offen |
| T-6.9 | #60 | `scripts/seed_zones.py`: Lieferzonen aus der bestehenden Liefergebietsliste des Pilotbetriebs | 03, 04 | T-6.1, D5 | offen |
| T-6.4 | #61 | Polygon-Variante mit `shapely`, GeoJSON-Import | 04 | T-6.3 | offen |
| T-6.5 | #62 | `draft_order` um Lieferung erweitern: Pauschale, Mindestbestellwert, Lieferzeit | 04 | T-6.3, T-4.5 | offen |
| T-6.6 | #63 | GUI Lieferaufträge plus Schalter „Lieferung pausieren" | 06 | T-4.7 | offen |
| T-6.7 | #64 | Löschjob für personenbezogene Daten, täglich, mit Protokoll | 03 | T-6.1 | offen |
| T-6.8 | #65 | Eval-Suite v2 mit Adressfällen (buchstabieren, Tastatur, außerhalb der Zone) | 08 | T-5.2, T-6.5 | offen |

**→ Gate G3**

---

## Block 5 – Stufe 4: Einlernen  ·  Sammel-Issue #11

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-7.1 | #66 | Transkriptions-Pipeline auf dem EU-Server: Whisper-Container (CPU, nachts) oder EU-Transkriptionsdienst mit AVV, Ergebnis als JSON | 00 §Stufe 4, 13 §0 | Rechts-Check bestanden, T-9.2 | blockiert |
| T-7.2 | #67 | Extraktion: Transkript → Vorgangs-JSON mit demselben Schema wie die Evals | 08 | T-7.1 | blockiert |
| T-7.3 | #68 | Abgleich mit Kasse oder Bon, Fehler-Taxonomie, Report je Woche | 00 §Stufe 4 | T-7.2 | blockiert |
| T-7.4 | #69 | Alias-Vorschläge aus echten Anrufen in die GUI, mit Annehmen-Knopf | 06 §4 | T-7.3 | blockiert |
| T-7.5 | #70 | Fehlerfälle automatisch als neue Eval-Fälle anlegen | 08 | T-7.3 | blockiert |

**→ Gate G4**

---

## Block 5b – Deployment und Umgebung (ab Stufe 1 nötig)  ·  Sammel-Issue #12

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-9.1 | #71 | Tunnel für Testanrufe einrichten, dokumentieren, einmal durchgeprobt | 13 §2 | T-0.1 | offen |
| T-9.2 | #72 | `deploy/docker-compose.prod.yml` + Caddy auf einem EU-Server, `/health` von außen erreichbar | 13 §3 | T-0.1, D3 | offen |
| T-9.3 | #73 | `scripts/backup.sh` + `restore.sh`, Cron, Wiederherstellung einmal wirklich geprobt | 13 §4 | T-1.1 | offen |
| T-9.4 | #74 | Uptime-Check auf `/health` mit Benachrichtigung | 13 §5 | T-9.2 | offen |
| T-9.5 | #75 | `jobs/holidays.py`: Feiertage des Bundeslandes (konfigurierbar) als Vorschlag in `special_days` | 11 §jobs | T-1.2 | offen |

## Block 6 – Stufe 5/6: Betrieb  ·  Sammel-Issue #13

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-8.1 | #76 | Modus-Umschaltung `shadow`/`overflow`/`primary`/`paused` inkl. Zeitfenster | 02 §4 | T-3.2 | offen |
| T-8.2 | #77 | Freigabe-Fluss im Überlauf: Status `approved`, Freigabeknopf, abschaltbar | 03, 06 | T-4.7, T-8.1 | offen |
| T-8.3 | #78 | Monitoring: Heartbeat, Kosten-Alarm, Fehler-Alarm, Tagesbericht | 02 §5 | T-1.9 | offen |
| T-8.4 | #79 | Kennzahlen: Leiste mit 4 Werten in jedem Admin-Bereich, Verlauf im Bereich „Kennzahlen" | 06 §4 | T-8.3 | offen |
| T-8.5 | #80 | Runbook und Team-Schulung, 1 Seite | 09 | T-8.1 | offen |
| T-8.6 | #81 | Backup und Wiederherstellung, Wiederherstellung einmal wirklich geprobt | CLAUDE.md §8 | T-1.1 | offen |

**→ Gate G5, dann Monats-Review**

---

## Empfohlene Reihenfolge für die ersten Sessions

1. **Session 1:** T-0.1 → T-0.2 Rest → T-0.4 Rest → T-0.7 (Gerüst läuft in Docker, Slash-Befehle und CI funktionieren)
2. **Session 2:** T-0.6 → T-1.1 → T-1.2 (Querschnitt, Schema, Testdaten)
3. **Session 3:** T-1.3 → T-1.4 → T-1.5 (erste echte Tools mit Tests und Latenzmessung)
4. **Session 4:** T-1.6 → T-1.12 → T-1.7 → T-1.8 → T-1.9 (Vorgänge vollständig, Outbox steht)
5. **Session 5:** T-1.10 → T-2.1 → T-2.2 → T-2.3 (**erstes Gespräch im Terminal**)
6. **Session 6:** T-3.1 → T-3.2 → T-3.3 (die Reservierung erscheint live auf dem Tablet)
7. **Session 7:** T-1.13 → T-9.1 → T-9.3 (Telefon-Interface, Tunnel, Backup) — danach wartet nur noch D1

Nach Session 6 existiert der **Durchstich ohne Telefon**. Sobald der Anbieter feststeht, kommt mit T-1.11 nur noch das Telefon davor. Parallel dazu kann T-0.8 (Zahlwörter) jederzeit laufen — reine Funktion, keine Abhängigkeit.

## Definition of Done

Gilt für jede Aufgabe, vollständig in `CLAUDE.md` §7. Kurzfassung: ausgeführt, getestet, gelintet, gemessen, dokumentiert, Status aktualisiert, committet.
