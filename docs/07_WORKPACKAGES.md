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
| T-0.7 | #20 | Slash-Befehle in `.claude/commands/` einmal durchspielen, CI-Workflow grün bekommen | 12, 13 §6 | T-0.4 | fertig 17.09.2026: alle sieben Befehle durchgespielt; CI-Schritte (`ruff check`, `ruff format --check`, `pytest`) lokal gegen echtes Postgres grün, die volle Suite (721 Tests zum Zeitpunkt des Commits, danach mit jedem Merge aus main mehr). Gefunden und behoben: `/gate` zeigte auf einen Abschnitt "Gate Workflow", den es in `docs/15_README_STRATEGY.md` nicht gibt (heißt "Ablauf bei einem Gate"); CI lief bei Push nur auf `main` und `task/**`, jetzt auch auf `dev/**`. `api/tests/test_commands.py` hält die Befehle künftig ehrlich (Pfade, Make-Ziele, Emojis). Blockiert bleiben `/eval` und Schritt 2 von `/bug`, solange `evals/runner.py` fehlt (T-5.1) |
| T-0.8 | #21 | `domain/menu/numberwords.py`: deutsche Zahlwörter und Mengen, ≥ 100 Unit-Tests, ohne DB | 11 §menu | T-0.6 | fertig 17.09.2026: `parse_cardinal` (ganzer Text ist die Zahl, 0 bis 999, Ziffern und Wörter, zusammen- und auseinandergeschrieben), `find_numbers`, `find_item_number` (ausdrückliches "Nummer …" schlägt alles andere; zwei Zahlen ohne Marker ergeben `None` statt der ersten), `find_quantity` (nur mit Marker: zweimal, 2x, drei Portionen). Umlaute in beiden Schreibweisen, Erkennungsformen wie "vierzig sieben" und "sechszehn". 375 Tests: Rundlauf 0 bis 199 und jede siebte bis 999 gegen einen unabhängig geschriebenen Sprecher, dazu die Telefon-Fälle. `sim/scripted_llm.py` benutzt jetzt dieses Modul statt seiner provisorischen Tabelle. Gemessen: 3 bis 5 Mikrosekunden je Aufruf. 716 Tests gesamt. Codex-Review PR #105 (P1x2, P2x1) im Folge-PR behoben, weil er erst nach dem Merge kam: Zahl waechst nicht mehr ueber Satzzeichen oder ueber den Artikel einer Mengenangabe ("Nummer 20, eine Portion" war die 21 mit Menge 21), ein Artikel am Anfang einer Zahl zaehlt mit ("die ein und zwanzig" war die 20), und eine Zahl an einem Mengen-Marker gilt nicht mehr als zweite Gerichtnummer ("2 x die 23" fragte ohne Not zurueck). Zahlen tragen jetzt ihre Lage im Satz (`_Span`). 8 neue Tests, 724 Tests gesamt |

---

## Block 1 – Stufe 1: Reservierung  ·  Sammel-Issue #6

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-1.1 | #22 | Alembic einrichten, Migration 001 (Stufe-1-Tabellen), `up`/`down` getestet | 03 | T-0.2 | fertig 16.09.2026: `db/alembic.ini`, `env.py`, `versions/001_stufe1.py`, Modelle `api/models/`; up/down/up gegen Wegwerf-DB getestet, Modelle und Schema ohne Diff |
| T-1.2 | #23 | Seed-Skript `scripts/seed.py`: Mandant <Pilotbetrieb>, Öffnungszeiten, Kapazität, Testkonfiguration | 03 | T-1.1 | fertig 16.09.2026: `scripts/seed.py`, idempotent, Live-Schalter bleibt unangetastet, Werte sind Platzhalter bis C1 |
| T-1.3 | #24 | Tool `get_service_status` inkl. Sondertage und Wartezeiten | 04 | T-1.2 | fertig 16.09.2026: `domain/status/` + `tools/service_status.py`, 16 Tests, p95 weit unter 300 ms |
| T-1.4 | #25 | Tool `check_slot`: Verfügbarkeit plus bis zu 2 Alternativen | 04 | T-1.2 | fertig 16.09.2026: `domain/reservations/{capacity,slots,spoken}.py` + `tools/check_slot.py`, 23 Tests, p95 rund 13 ms |
| T-1.5 | #26 | Tool `create_reservation` als `draft`, mit `readback` und Idempotenz | 04 | T-1.4 | fertig 16.09.2026: `domain/reservations/create.py` + `tools/create_reservation.py`, `core/ids.py`, `domain/customers/phone.py` (E.164), 40 Tests, p95 rund 15 ms |
| T-1.6 | #27 | Tool `confirm` generisch (Reservierung und Bestellung), `audit_log`, Ereignis-Warteschlange | 04 | T-1.5 | fertig 17.09.2026: `domain/confirm.py` + `tools/confirm.py`, Zeilensperre gegen doppelte Ereignisse, Outbox-Eintrag `reservation.confirmed`, 17 Tests, p95 rund 10 ms. Bestellungen seit 23.09.2026: `domain/ordering/confirm.py`, Abholcode je Betriebstag, Übergabe nur im Modus `primary` sofort, sonst Freigabe im Tablet (T-4.7) |
| T-1.7 | #28 | Tool `create_callback` | 04 | T-1.1 | fertig 17.09.2026: `domain/callbacks/create.py` + `tools/create_callback.py`, Idempotenz über den Zustand (ein offener Rückruf je Anruf), `audit_log` und Outbox-Ereignis `callback.created`, 18 Tests, p95 rund 11 ms. Nebenbefund behoben: `normalize_phone` schluckte die geklammerte (0) hinter der Landesvorwahl |
| T-1.8 | #29 | Tool `transfer_to_team` inkl. Schleifenschutz und Erreichbarkeitsprüfung | 04 | T-1.1 | fertig 17.09.2026: `domain/callbacks/transfer.py` + `tools/transfer_to_team.py`, Zeilensperre und Zustand auf `calls.transfer_reason` gegen Mehrfachauslösung je Anruf, Erreichbarkeit aus Öffnungszeiten (Annahme, siehe docs/01), 17 Tests, p95 deutlich unter 300 ms. Codex-Review PR #98 (P1, P2) behoben: eigener `TransferReason` mit `cancellation`, kein Zustand/Audit bei Nicht-Erreichbarkeit |
| T-1.9 | #30 | Anruf-Log: `POST /v1/calls/start` und `/end`, Tool-Aufrufe mit Dauer | 03 | T-1.1 | fertig 17.09.2026: `domain/calls/{start,end}.py`, Endpunkte in `main.py`, Zustands-Idempotenz auf `external_session_id` bzw. wie `domain/confirm.py`, Löschfrist 24 Monate. `core/tool_log.py` schreibt `calls.tool_calls` generisch für jeden `/v1/tools/*`-Aufruf. 27 Tests, alle bestehenden Latenz-Tests bleiben grün. Codex-Review PR #99 (P1, P2) behoben: `tool_calls`-Update jetzt zusätzlich nach `tenant_id` gefiltert, `start_call` mit Advisory-Sperre je `(tenant_id, external_session_id)` gegen gleichzeitige Plattform-Retries. 232 Tests gesamt |
| T-1.10 | #31 | System-Prompt `prompts/system_v1.md` plus Tool-Beschreibungen für die Plattform | 05 | T-1.3…T-1.8 | fertig 17.09.2026: `prompts/system_v1.md` (nur Stufe-1-Tools, ~440 Token) + `prompts/tools_v1.md` (Kurzreferenz je Tool für Function-Calling, docs/04 bleibt der volle Vertrag). Noch nicht gebaute Tools (`search_menu` u. a.) bewusst ausgespart. 6 Tests: Pflichtabschnitte, alle sechs Tools genannt, Token-Budget eingehalten, keine Stufe-2/3-Tools erwähnt. 238 Tests gesamt |
| T-1.11 | #38 | **Anbieter-Adapter** `telephony/adapters/<anbieter>.py` gegen aufgezeichnete Webhooks | 11 §telephony, 12 S7 | T-1.13, D1 entschieden | blockiert |
| T-1.12 | #32 | `events/`: Outbox schreiben in `confirm`, Dispatcher mit Backoff, Fake-n8n im Test, Alarm bei `failed` | 11 §events, 03 | T-1.6 | fertig 17.09.2026: `events/` mit `types.py`, `outbox.py` (enqueue in der Transaktion des Fachvorgangs) und `dispatcher.py`; ein Ereignis je Transaktion mit `FOR UPDATE SKIP LOCKED`, Backoff 5 s / 30 s / 2 min / 10 min, danach `failed` plus Alarm als ERROR-Log; eigener Container-Dienst; 12 Tests mit Fake-n8n |
| T-1.13 | #33 | `telephony/port.py` Interface + `adapters/fake.py`, der Anrufe aus Dateien abspielt | 11 §telephony | T-1.10 | offen |

---

## Block 1b – Stufe 1: Gesprächs-Kern und Simulator  ·  Sammel-Issue #7

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-2.1 | #34 | `agent/`: Prompt-Aufbau mit Menü-Index, Zustand, Loop, Dispatch auf `domain`, Fake-LLM für Tests | 11 §agent, 05 | T-1.10, T-0.6 | fertig 17.09.2026: `prompt.py`, `state.py`, `dispatch.py` (Tool → `domain` ohne HTTP-Umweg, eigener `idempotency_key`, schreibt `calls.tool_calls` selbst), `loop.py` (Zug → Modell → Tools → Antwort, Abbruch bei `max_call_seconds`/zu vielen Tool-Hops), `llm.py` (`LLMClient`-Schnittstelle + `FakeLLM`). `core/tool_log.py` refaktoriert (`append_tool_call()` gemeinsam mit der HTTP-Middleware). 27 Tests. p95 5,5 ms je Dispatch-Aufruf. Nebenbei behoben: `db/migrations/env.py` überschrieb bislang den App-Log-Pegel (siehe docs/01). 265 Tests gesamt. Codex-Review PR #101 (P1×3, P2×2) noch am selben Tag behoben: `state_patch` auf `LLMTurn`, `reservation_id` im Prompt-Zustand, Zeitlimit bei jedem Tool-Hop erneut geprüft, Tool-`say` ans Modell weitergereicht, und vor allem: Abbruch bei Zeitlimit/Hop-Grenze übergibt jetzt wirklich an `transfer_to_team`/`create_callback` statt es nur anzukündigen. 7 neue Tests, 272 Tests gesamt |
| T-2.2 | #35 | `agent/ladder.py` + `escalation.py`: Verständnis-Leiter als Zustandsmaschine, Eskalations-Auslöser vor dem Modell | 05 §2, §4 | T-2.1 | fertig 17.09.2026: `ladder.py` (`UnderstandingLadder`, Fehlversuche je Feld, eine Stufe tiefer nach zwei Fehlversuchen, `should_end_call` nach drei Stufenwechseln), `escalation.py` (Stichwort-Erkennung Beschwerde/Mensch-Wunsch/Storno aus 05 §4, läuft vor dem Modell). In `loop.py` verdrahtet: `LLMTurn.understanding_failure`, Eskalations-Prüfung als Erstes in `run_turn`, Leiter-Hinweis im Prompt-Zustand, gemeinsames `_handoff()` mit echtem Grund statt Platzhalter. 20 Tests, 292 Tests gesamt. Codex-Review PR #102 (P2×3) noch am selben Tag behoben: `understanding_failure` je `run_turn`-Aufruf entdoppelt, `"storn"` erkennt auch das Substantiv "Storno", `_handoff()` gibt den auslösenden Kundentext in die Rückruf-Zusammenfassung mit. 3 neue Tests, 295 Tests gesamt |
| T-2.3 | #36 | `sim/cli.py` + `sim/replay.py`: erstes Gespräch im Terminal, Reservierung landet in der DB | 11 §sim | T-2.1, T-1.5 | fertig 17.09.2026: `cli.py` (Gespräch im Terminal, `:quit` beendet, `--noise`/`--seed`/`--now`/`--tenant`), `replay.py` (Transkript aus `evals/cases/` im Format docs/08 §1, danach Datenbankzustand statt Modelltext), `session.py` (gemeinsame Mechanik: `start_call`/`end_call`, Zustand, Anzeige der Tool-Aufrufe aus `calls.tool_calls`, Ausgang aus dem Gesprächszustand statt aus dem Gefühl des Modells), `noise.py` (Buchstabendreher, abgeschnittene und verschluckte Wörter, mit Saat reproduzierbar), `scripted_llm.py` (regelbasierter Modell-Ersatz bis T-2.4: Personenzahl, Tag und Uhrzeit, Name, Rufnummer; ohne Erkennung ein gemeldeter Fehlversuch statt einer Vermutung). Erster Fall `evals/cases/reservierung_0001_tisch_fuer_vier.json`. Durchstich gemessen: vier Züge, Tool-Aufrufe 7 bis 15 ms, Reservierung steht danach `confirmed` in der DB. Codex-Review PR #104 (P1x4, P2x2) noch am selben Tag behoben: Ja und Nein nur noch an Wortgrenzen (sonst galt "Im Januar" als Zustimmung), Anliegen ausserhalb von Version 1 vor der Statusabfrage geprüft und bis zum Rückruf gemerkt, eine Antwort nur mit Uhrzeit behält den schon genannten Tag, unmögliches Datum gilt als nicht verstanden statt als heute (und verlässt den Loop nicht mehr als ValueError), `finish()` schliesst den Anruf zur aktuellen Zeit statt zur Startzeit, und eine Korrektur beim Vorlesen baut einen neuen Entwurf, statt den alten stehen zu lassen (ein spaeteres Ja bestaetigte sonst die korrigierte Zahl nicht). 46 Tests, 341 Tests gesamt |
| T-2.4 | #37 | `agent/llm.py` gegen ein echtes Modell, Token-Zählung, Kosten je Gespräch im Log | 05 §5 | T-2.3 | offen |
| T-2.5 | #39 | Sim-Konsole in der GUI (`gui/dev/console.html`), nur `ENV=dev` | 06, 11 §gui | T-2.3, T-3.1 | offen |

**→ Meilenstein „Durchstich ohne Telefon"** nach T-2.3 und T-3.3: Terminal → Agent → DB → Tablet.

## Block 2 – Stufe 1: GUI-Grundlage  ·  Sammel-Issue #8

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-3.1 | #40 | GUI-Gerüst: Jinja2, HTMX, `app.css` aus den Variablen und Klassen von `gui/mockups/admin-desktop.html`, Layout mit Kopfzeile | 06, gui/mockups | T-0.2, D6 | fertig |
| T-3.2 | #41 | Kopfzeile live: KI-Modus, Lieferung an/aus, Wartezeit, Not-Aus-Knopf | 06 §3 | T-3.1, T-1.3 | fertig 18.09.2026: `domain/status/config.py` (pausieren, einschalten mit Modus von davor aus `audit_log`, Lieferung an/aus, Wartezeit +15/+30 gedeckelt auf 180 Min, Zeilensperre, Audit), Knoepfe in `fragments/kopfzeile.html`, Ereignis `header` im Strom haelt alle Tablets gleich, Schreiben nur mit `HX-Request` |
| T-3.3 | #42 | Spalte „Heute": Reservierungen mit Live-Aktualisierung über SSE | 06 §3 | T-3.1, T-1.5 | fertig |
| T-3.4 | #43 | Spalte „Rückrufe" mit Ton und Erledigt-Knopf | 06 §3 | T-3.1, T-1.7 | fertig 18.09.2026: `domain/callbacks/board.py` (offene Rückrufe, Beschwerden oben, dann der älteste; `mark_done` mit Zeilensperre und Audit), `fragments/rueckrufe.html` mit Anrufen (`tel:`) und Erledigt, Ereignis `callbacks` im Strom, Ton aus WebAudio bei neuer Karte, eigener Ton für Beschwerden |
| T-3.5 | #44 | Bedientest: ein Teammitglied bedient 5 Minuten ohne Erklärung, Protokoll | 06 §1 | T-3.2…T-3.4 | offen |

**→ Gate G1** nach T-1.11 und T-3.5: 20 Rollenspiel-Anrufe, Latenz, Kosten, Ausfalltest.

---

## Block 3 – Stufe 2: Abholung  ·  Sammel-Issue #9

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-4.1 | #45 | Migration 002 (Menü, Bestellungen) | 03 | T-1.1 | fertig 18.09.2026: `db/migrations/versions/002_stufe2.py` + `api/models/menu.py`, `orders.py`; `pg_trgm` mit GIN-Index auf `name` und `alias`, CHECKs (Cent ≥ 0, Summe geht auf, Menge > 0, Statuswerte), `orders.pickup_code` ergänzt; up/down und Modell-Diff getestet |
| T-4.2 | #46 | Menü-Import `scripts/import_menu.py` nach Format 14, `--dry-run`, Prüfregeln, idempotent | 14, 11 §menu | T-4.1, CSV aus dem Chat | fertig 18.09.2026 (Code; echte CSV aus dem Chat steht aus): `domain/menu/importer.py` (`parse` ohne DB, `apply` in einer Transaktion), `normalize.py`, `scripts/import_menu.py`; alle Prüfregeln aus 14, Preisänderung nur mit `--apply-price-changes`, idempotent, Audit-Eintrag |
| T-4.3 | #47 | Tool `search_menu` mit der Auflösungsreihenfolge aus 04, Trigram-Index, Schwellen konfigurierbar | 04 | T-4.2 | fertig 18.09.2026: `domain/menu/search.py` + `tools/search_menu.py`; Nummer → Alias → Trigram, Schwellen aus `MENU_FUZZY_THRESHOLD_HIGH/LOW`; unbekannte Nummer ist `not_found` statt Ausweichen, Zahl ohne "Nummer" neben einem Namen ist Menge; nur aktive Gerichte, ausverkauft mit Satz; p95 < 300 ms bei 200 Gerichten |
| T-4.4 | #48 | Tool `get_item_details` inkl. Allergen-Regel „unbekannt ≠ keine" | 04 | T-4.2 | fertig 22.09.2026: `domain/menu/details.py` + `tools/get_item_details.py`; Allergene nur aus `item_allergens`, ohne Zeile `known: false` und - nur auf die Allergenfrage, Pflichtfeld `allergen_question` - der Satz fürs Team aus dem Code, Codes in LMIV-Reihenfolge, `confirmed_at` als Ortsdatum; Optionen und „heute aus" über den gemeinsamen Baustein `domain/menu/items.py`, den auch `search_menu` benutzt; p95 ~16 ms |
| T-4.5 | #49 | Tool `draft_order` mit allen Prüfungen und `readback`; dabei den Satz je Position zerlegen, bevor `search_menu` gefragt wird - Regel A macht aus „die 23 und einmal Pho Bo" sonst eine Rückfrage statt zweier Positionen (`docs/01_STATUS.md` § Open Points, `docs/04` §search_menu) | 04 | T-4.3, T-4.4 | erledigt 23.09.2026 (Abholung; Lieferung T-6.5) |
| T-4.6 | #50 | Übergabe über n8n mit `handover_state`: **B** Netzwerk-Bondrucker (ESC/POS) zuerst, **C** <Kassensystem>-Bestell-Eingang als zweiter Adapter nach Antwort von <Kassenanbieter>; Idempotenz, Wiederholung | 02 §2, 01 D2 | T-1.6 | offen |
| T-4.7 | #51 | GUI Spalte „Neue Bestellungen" mit Passt/Korrigieren und Korrekturgründen | 06 §3 | T-3.1, T-4.5 | offen |
| T-4.8 | #52 | GUI „Gericht aus" | 06 §3 | T-4.1 | offen |
| T-4.10 | - | Wünsche zu Positionen: Gericht und Wunsch im selben Satz trennen („die 23 ohne Karotten" findet heute nichts), Wunsch als Option der Karte (Preis aus `item_options`, Aufpreis beim Wiederholen genannt) oder als Hinweis nur für Weglassen („ohne", „kein"); alles, was nicht auf der Karte steht, bietet der Agent nicht an. Begründung für einen Aufpreis nur aus der Karte (neue optionale Spalte im Options-Import, Migration). Allergie ist kein Wunsch: Hinweis an die Küche plus Allergenpfad aus T-4.4, keine Zusage | 04, 05, 14 | T-4.5 | fertig 24.09.2026: `domain/menu/wishes.py` (`split_wish`, `classify_wish`), `search_menu` liefert `wish` (note, option mit Aufpreis und Grund, allergy ohne Zusage, unknown nicht angeboten, open bei mehreren Treffern); Wiederholung mit Aufpreis im Gesprächskern; Migration 003 `item_options.price_reason` mit optionaler Importspalte; Schlüssel aus der geprüften Anfrage (offene Punkte PR #127); Latenz gemessen |
| T-4.9 | #53 | Preis-Abgleich Kasse gegen Agent-DB als Skript; Abweichung als rotes Badge am Gericht in der Admin-Liste, übernehmen oder verwerfen | 02 §6, 06 §4 | T-4.2 | offen |
| T-5.1 | #54 | Eval-Runner `evals/runner.py`, Fall-Format, Report | 08 | T-4.5 | offen |
| T-5.2 | #55 | Eval-Suite v1: mindestens 100 Fälle (Menü, Mengen, Optionen, Störgeräusche, Eskalation) | 08 | T-5.1 | offen |
| T-5.3 | #56 | Modellvergleich über die Eval-Suite, Kosten je Anruf, Empfehlung | 05 §5 | T-5.2 | offen |

**→ Gate G2**

---

## Block 4 – Stufe 3: Lieferung  ·  Sammel-Issue #10

| ID | Issue | Aufgabe | Spec | Hängt ab von | Status |
|---|---|---|---|---|---|
| T-6.1 | #57 | Migration 004 (Kunden, Adressen, Zonen) | 03 | T-4.1 | offen |
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
