# 11 – Modularer Aufbau

> Kein Monolith. Jedes Modul hat **eine** Verantwortung, eigene Tests und eine klare Richtung, in die es Abhängigkeiten haben darf.
> Regel für Claude Code: Bevor du eine Datei anlegst, prüfst du hier, in welches Modul sie gehört. Passt sie nirgends, fehlt ein Modul, nicht eine Ausnahme.

---

## 1. Die Schichten

```text
┌──────────────────────────────────────────────────────────────────────┐
│  EINGÄNGE          tools/ (HTTP-Vertrag)             gui/   sim/     │
│                    telephony/ (eigene Sprachschicht, docs/20)        │
├──────────────────────────────────────────────────────────────────────┤
│  GESPRÄCH          agent/   Loop, Prompt-Aufbau, Zustand, Dispatch   │
├──────────────────────────────────────────────────────────────────────┤
│  FACHLOGIK         domain/  menu · ordering · reservations · calls · │
│                             delivery · customers · status · callbacks│
├──────────────────────────────────────────────────────────────────────┤
│  AUSGÄNGE          events/  Outbox → n8n · jobs/ Löschen, Berichte   │
├──────────────────────────────────────────────────────────────────────┤
│  BASIS             models/  schemas/  db.py  core/ (Hülle, Log, Auth)│
└──────────────────────────────────────────────────────────────────────┘
```

**Abhängigkeitsrichtung: nur nach unten.**
- `tools/`, `gui/`, `sim/`, `telephony/` dürfen `agent/` und `domain/` benutzen.
- `agent/` darf `domain/` benutzen, nie `tools/` (es ruft die Fachlogik direkt, ohne HTTP-Umweg).
- `domain/` kennt nur `models/`, `schemas/`, `core/`. Es weiß nichts von HTTP, Telefon, HTMX oder n8n.
- `events/` wird von `domain/` nur durch **Schreiben in die Outbox-Tabelle** angestoßen. Kein direkter Aufruf nach außen.
- `telephony/` knows the telephone access and the speech engines (the voice layer, docs/20). **Nobody else.**

Warum diese Härte: Wenn in `domain/ordering/` nie ein Anbietername vorkommt, kannst du eine Sprach-Engine oder den Telefonzugang tauschen, ohne eine Preisregel anzufassen. Und die Evals testen dieselbe Fachlogik, die im Betrieb läuft — nicht eine Kopie.

---

## 2. Modul für Modul

### `core/` – Querschnitt
| Datei | Verantwortung |
|---|---|
| `envelope.py` | `ok()` / `fail()`, die Antwort-Hülle aus 04 §1 |
| `errors.py` | Fehlerklassen mit Code (`NotFound`, `Ambiguous`, `OutOfZone` …) → werden zentral in die Hülle übersetzt |
| `logging.py` | JSON-Logs, jede Zeile trägt `call_id` und `request_id` |
| `tool_log.py` | Middleware: jeder `/v1/tools/*`-Aufruf mit Dauer und Ergebnis in `calls.tool_calls`, best-effort |
| `auth.py` | Token-Prüfung als FastAPI-Dependency |
| `ids.py` | UUID, Idempotenz-Schlüssel, Abholcode („A17") |
| `time.py` | UTC ↔ `Europe/Berlin`, „heute" im Sinne des Betriebstags |

**Regel:** Fachcode wirft `core.errors`, nie `HTTPException`. Die Übersetzung passiert einmal, in `tools/`.

### `domain/status/`
Öffnungszeiten, Sondertage, Wartezeiten, Modus. Beantwortet: *Geht gerade etwas, und was?*
- `hours.py` — ist offen? nächste Öffnung? je Service
- `config.py` — `service_config` lesen und schreiben, mit `audit_log`
- Tests: Mitternacht, Sondertag schlägt Wochentag, Sommerzeit-Umstellung

### `domain/menu/`
Der fehleranfälligste Bereich, deshalb am stärksten zerlegt.
| Datei | Verantwortung |
|---|---|
| `numberwords.py` | „dreiundzwanzig" → 23, „zweimal" → Menge 2, „Nummer vierzig sieben" → 47, „Es zwölf" → S12 mit dem `CardFormat` der Karte (T-4.12). Reine Funktion, hundert Tests; welche Präfixe gelten, gibt der Aufrufer aus der DB mit (`items.card_format`). |
| `normalize.py` | Kleinschreibung, Umlaute, Füllwörter raus („einmal die … bitte") |
| `search.py` | die Auflösungsreihenfolge aus 04: Nummer → Alias → Trigram, mit Schwellen aus der Konfiguration |
| `aliases.py` | Alias anlegen, Treffer zählen, Vorschläge aus Anrufen |
| `details.py` | Optionen, Allergene mit der Regel „unbekannt ≠ keine" |
| `sold_out.py` | Schalter „Gericht aus": bis Ende des Betriebstags, `audit_log`, Alternativen derselben Kategorie (T-4.8) |
| `importer.py` | CSV nach 14 lesen, prüfen, einspielen, Bericht; mit `deactivate_missing` werden fehlende Gerichte inaktiv |
| `pos_dbf.py` | dBase-Tabellen der Kasse lesen, nur lesend, Bytes rein, Zeilen raus (T-4.11) |
| `pos_convert.py` | Kassenartikel in die CSV nach 14: Größen, Extras, Allergen-Umsetztabelle, Bericht (T-4.11) |

### `domain/ordering/`
| Datei | Verantwortung |
|---|---|
| `pricing.py` | Summe aus Positionen, Optionen, Pauschale. Cent-Integer, sonst nichts. |
| `validation.py` | offen? aktiv? nicht ausverkauft? Pflichtgruppen? Mindestbestellwert? |
| `draft.py` | Entwurf anlegen oder aktualisieren |
| `readback.py` | der Vorlesetext, deterministisch aus dem Entwurf |
| `confirm.py` | `draft → confirmed`, Idempotenz, `audit_log`, Outbox-Eintrag. Für Bestellung **und** Reservierung. |
| `ready_time.py` | zugesagte Zeit aus Wartezeit und Auslastung |

### `domain/reservations/`
- `capacity.py` — freie Plätze je Fenster
- `slots.py` — verfügbar? bis zu zwei Alternativen, nahe am Wunsch
- `create.py` — Entwurf mit `readback`

### `domain/delivery/`
- `zones.py` — PLZ-Match, später Polygon (`shapely`), ein Interface für beides
- `fees.py` — Pauschale, Mindestbestellwert, Lieferzeit je Zone
- `address.py` — Straße, Hausnummer, PLZ normalisieren, Zone auflösen und an der Adresse speichern

### `domain/customers/`
- `phone.py` — E.164-Normalisierung deutscher Nummern, inkl. unterdrückter Nummer
- `lookup.py` — Kunde und Adressen zur Nummer, `blocked` beachten
- `retention.py` — Löschfristen setzen und anwenden

### `domain/callbacks/`
- `create.py` — Rückruf-Aufgabe mit Zusammenfassung
- `transfer.py` — Durchwahl, Erreichbarkeit, Schleifenschutz (einmal je Anruf)

### `domain/calls/`
Anruf-Log, nicht Gesprächsführung — das bleibt `agent/`. Eigenes Modul statt Teil
von `callbacks/`, weil es kein Eskalationspfad ist, sondern der Rahmen um jeden
Anruf, ganz gleich wie er ausgeht.
- `start.py` — `calls`-Zeile anlegen, Rufnummer normalisieren, Löschfrist setzen; Zustands-Idempotenz über `external_session_id`
- `end.py` — Dauer und Ergebnis schreiben, Zustands-Idempotenz wie `domain/confirm.py`
- `routing.py` — whether the agent answers a call (only in `overflow` and `primary`), the team extension and the tenant name for the greeting; the mode is read fresh, so the emergency stop holds for the next call (T-1.13)

Die Zeile pro einzelnem Tool-Aufruf (`calls.tool_calls`, docs/04 §Gemeinsame
Regeln) kommt nicht von hier: `core/tool_log.py` schreibt sie generisch für jeden
`/v1/tools/*`-Aufruf, damit kein einzelnes Tool-Modul dafür Fachlogik-fremden Code
braucht.

### `agent/` – der Gesprächs-Kern
Läuft unabhängig vom Telefon. Text rein, Text raus, Tools dazwischen.
| Datei | Verantwortung |
|---|---|
| `prompt.py` | System-Prompt aus `prompts/system_vN.md` plus Menü-Index bauen |
| `state.py` | kompakter Gesprächszustand (05 §5) statt wachsendem Verlauf |
| `loop.py` | Kundenzug → Modell → Tool-Aufrufe → Antwort; Abbruch bei `max_call_seconds` |
| `dispatch.py` | Tool-Name → `domain`-Funktion, mit Zeitmessung. `search_menu` zerlegt hier einen Satz mit mehreren Positionen (`split_positions`) und sucht je Teil; `draft_order` bekommt den Schlüssel aus den Angaben des Anrufs |
| `ladder.py` | die Verständnis-Leiter als Zustandsmaschine: zählt Fehlversuche, steigt die Stufe |
| `escalation.py` | die Auslöser aus 05 §4, prüft **vor** dem Modell |
| `guards.py` | what the core enforces whatever the model returns (05 §5): `confirm` only after an explicit yes to the draft read back before the turn, `draft_order` only with dishes from a clear match or from candidates the guest heard by name. A refused call goes back to the model as a failed result with a `hint` |
| `consent.py` | the yes detector (`is_yes`), shared by `guards.py` and `evals/recorder.py` |
| `llm.py` | Modellanbindung, austauschbar, mit Token-Zählung |
| `outcome.py` | outcome and intent of a call from the state, shared by `sim/session.py` and `telephony/handler.py` |

**Why our own core?** Evals need it, the simulator needs it, the shadow measurement needs it, and since D7 (decided 07.10.2026) it also runs in operation, called by the voice layer (docs/20 §2). Test and operation are identical.

### `telephony/` – der einzige Ort, der den Anbieter kennt
| Datei | Verantwortung |
|---|---|
| `port.py` | the interface in two protocols: `CallEvents` (`on_call_started`, `on_user_turn`, `on_dtmf`, `on_call_ended`) and `TelephonyPort` (`caller_id`, `say`, `transfer`, `hangup`, `start_recording`); docs/20 §4 plans additions (whether a sentence may be interrupted, silence, failure of the voice layer) |
| `handler.py` | provider-neutral call handler (T-1.13): opens the call log, says the AI disclosure, sends the call straight to the team in `paused` and `shadow`, runs each turn through `agent/loop.py`, transfers when the state says so, or says goodbye (unless the agent just did) and hangs up, closes the call log; every failure ends with the outage sentence and a transfer |
| `adapters/fake.py` | Testadapter, spielt Anrufe aus Dateien ab |
| `app.py` | planned (docs/20 §2): the process of the service `voice` |
| `session.py` | planned (docs/20 §2): call session, provider neutral |
| `speech.py` | planned (docs/20 §2): the ports for ear (speech-to-text) and mouth (text-to-speech) |
| `adapters/asterisk.py` | planned (docs/20 §2): media protocol of Asterisk, implements the port |
| `adapters/stt_<engine>.py` | planned (docs/20 §2): one speech-to-text engine each |
| `adapters/tts_<engine>.py` | planned (docs/20 §2): one text-to-speech engine each |
| `adapters/fake_speech.py` | planned (docs/20 §2): fake ear and mouth for tests |
| `router.py` | planned (docs/20 §2): the WebSocket endpoint for call audio and the dialplan callback |

The port has two directions (T-1.13): `CallEvents` is what the media side of the voice layer reports (`on_call_started`, `on_user_turn`, `on_dtmf`, `on_call_ended`), implemented once by `handler.py`; `TelephonyPort` is what we make the line do (`caller_id`, `say`, `transfer`, `hangup`, `start_recording`), implemented by each adapter. Calls are named by the session id of the media side (`calls.external_session_id`). `start_recording` is called nowhere before the legal check in docs/09. `fake.py` reads the eval case format (docs/08 §1) plus customer entries with `dtmf` or `hangup`, so every case in `evals/cases/` plays as a phone call. `router.py` and the other planned files come with the media adapter of our own voice layer (T-1.11, T-10.4).

**Rule:** a different speech engine or a different access changes one file in `adapters/` and `.env`. Nothing else.

### `tools/` – dünne HTTP-Hülle
Ein Modul je Endpunkt, jeweils fünf bis fünfzehn Zeilen: Request parsen, `domain` aufrufen, Hülle zurück, Dauer loggen. **Keine Fachlogik hier.** Wenn ein Tool wächst, ist die Logik in `domain/` falsch abgelegt.

### `events/` – der kalte Pfad
- `outbox.py` — Ereignis schreiben (in derselben Transaktion wie der Fachvorgang)
- `dispatcher.py` — alle paar Sekunden: `pending` lesen, an n8n senden, `sent` oder Wiederholung mit Backoff, nach N Versuchen `failed` plus Alarm
- `types.py` — `order.confirmed`, `reservation.confirmed`, `callback.created`, `order.handover_failed`, `daily.report`; `KITCHEN` nennt die Ereignisse, die die Druckbrücke abholt statt n8n (T-4.6)

### `kitchen/` – Eingang der Druckbrücke (T-4.6)
- `router.py` — `POST /v1/kitchen/claim` und `/ack`, eigenes Token `KITCHEN_BRIDGE_TOKEN`, dünne Hülle um `domain/ordering/handover.py` (Abholen, Rückmeldung, Wächter)

### `printbridge/` – die Druckbrücke im Lokal (T-4.6)
Eigenständig, nur Standardbibliothek, kennt keinen Code aus `api/` und spricht mit dem Server nur über HTTPS. Läuft auf einem Rechner im Lokal (`printbridge/README.md`).
- `bridge.py` — Durchlauf: abholen, drucken, ins Protokoll, zurückmelden; `--test` für einen Probebon
- `escpos.py` — Bonlayout als ESC/POS (PC858, 48 Zeichen), Korrektur oben, Hinweis fett, keine Telefonnummer
- `transport.py` — Netzwerkdrucker (TCP 9100 mit Statusabfrage) oder Windows-Warteschlange (pywin32, wartet bis gedruckt)
- `state.py` — Druckprotokoll: welche Bestellung in welcher Revision gedruckt ist, gegen doppelte und veraltete Bons
- `client.py` — `/v1/kitchen/*`, nur https außer localhost

Outbox statt direktem Aufruf: Fällt n8n aus, ist die Bestellung trotzdem gebucht und die GUI zeigt sie. Das Ereignis wartet, bis n8n zurück ist. Kein Vorgang geht verloren, keiner wird doppelt gesendet.

### `jobs/`
- `cold_path.py` — Prozess des Dienstes `dispatcher`: Dispatcher Richtung n8n plus Wächter für den Küchenbon in eigenem Faden (T-4.6), damit ein hängendes n8n die rote Karte nicht verzögert; steckt beide zusammen, damit `events/` die Fachlogik nicht kennen muss
- `retention.py` — täglicher Löschjob nach 03
- `menu_diff.py` — Abgleich Kasse gegen Agent-DB
- `daily_report.py` — Kennzahlen des Tages als Outbox-Ereignis
- `holidays.py` — Feiertage Baden-Württemberg jährlich in `special_days` vorschlagen

### `gui/`
- `router.py` — Seiten und HTMX-Fragmente
- `sse.py` — Ereignisstrom für Live-Updates (neue Bestellung, Rückruf, Modus)
- `dev.py` — router of the simulator console (`/gui/dev/console`), mounted by `mount_gui` only when `ENV=dev`; keeps the open calls in the process, the logic stays in `sim/session.py` (the one place where `gui` imports `sim`, because the console is the simulator page)
- `templates/` — `base.html`, `betrieb/`, `admin/`, `fragments/`
- `static/` — Pico.css, HTMX lokal, eigene Töne
- `dev/console.html` — der Simulator als Webseite, nur in `ENV=dev`

### `sim/` – das Text-Telefon
- `cli.py` — Gespräch im Terminal: du tippst den Kunden, der Agent antwortet, Tool-Aufrufe werden angezeigt
- `replay.py` — spielt ein Transkript aus `evals/cases/` ab
- `session.py` — die gemeinsame Mechanik beider Eingänge: Anruf-Zeile öffnen und schließen, Zustand halten, Tool-Protokoll und Ausgabe
- `scripted_llm.py` — regelbasierter Modell-Ersatz bis T-2.4: erkennt Reservierung und Abholung aus `prompts/system_v2.md`, rät nie, meldet Unverstandenes an die Leiter
- `scripted_order.py` — der Abholfluss des Modell-Ersatzes: Gerichte, Pflichtoptionen, Name, `draft_order`, vorlesen, `confirm`; what a card number is on the open allergy question it takes from the active menu (`MenuNumbers`, handed in by `session.py`), not from the import grammar
- `noise.py` — verrauscht Eingaben absichtlich (Buchstabendreher, abgeschnittene Wörter), um die Leiter zu testen

Damit gibt es den **Durchstich ohne Telefon**: Terminal → Agent → Fachlogik → DB → Tablet zeigt die Bestellung. Alles ohne Telefon testbar.

### `evals/`
- `runner.py` (Lauf, Wegwerf-Datenbank, CLI), `judge.py` (Abgleich mit dem Datenbankzustand), `recorder.py` (Beobachter am Modell für geratene Positionen und `confirm` ohne Ja), `report.py` (JSON, Markdown, Vergleich mit dem letzten Lauf), `scratch_db.py` (Wegwerf-Datenbanken, auch für die Tests) — siehe 08
- `menu/` — Evalkarte im Importformat (docs/14), Testdaten
- `cases/<bereich>_<nr>_<name>.json` — optional `now`, `caller_id`, `sold_out`, `repeat_confirm`, `pending` (bekannte Lücke mit Aufgabe, docs/08 §3)

---

## 3. Bauplan je Modul

| Reihenfolge | Modul | Warum jetzt | Stufe |
|---|---|---|---|
| 1 | `core/` | alles andere braucht die Hülle und das Logging | 0 |
| 2 | `models/` + Migration 001 | ohne Tabellen keine Fachlogik | 1 |
| 3 | `domain/status/` | kleinstes Modul, erster echter Test | 1 |
| 4 | `domain/reservations/` | Vehikel für den Durchstich | 1 |
| 5 | `domain/callbacks/` | Eskalation muss vom ersten Tag funktionieren | 1 |
| 6 | `domain/ordering/confirm` + `events/` | ein `confirm`, das für alles gilt | 1 |
| 7 | `tools/` für die Stufe-1-Tools | the HTTP contract stands (docs/04); not on the call path of the own voice layer (docs/20 §2) | 1 |
| 8 | `agent/` + `sim/` | **erstes Gespräch im Terminal** | 1 |
| 9 | `gui/` Betrieb v0 | die Reservierung wird sichtbar | 1 |
| 10 | `telephony/port` + `fake` | interface stands before the first real adapter | 1 |
| 11 | `telephony/adapters/asterisk` | media adapter of the own voice layer, T-1.11 after T-10.4 (D1 is decided: no platform vendor) | 1 |
| 12 | `domain/menu/numberwords` + `search` | Herz von Stufe 2, isoliert testbar | 2 |
| 13 | `domain/menu/importer` + `domain/ordering/` komplett | Bestellung ende-zu-ende | 2 |
| 14 | `evals/` vollständig | Gate G2 braucht Zahlen | 2 |
| 15 | `domain/delivery/` + `domain/customers/` | Stufe 3 | 3 |
| 16 | `jobs/` | Löschfristen vor echten Daten, Berichte für den Betrieb | 3–5 |

---

## 4. Tests je Modul

| Modul | Testart | Ohne DB? |
|---|---|---|
| `numberwords`, `normalize`, `pricing`, `readback`, `phone`, `ladder`, `escalation` | reine Unit-Tests, hunderte Fälle, Millisekunden | fertig |
| `domain/*` mit DB | Tests gegen Test-Postgres, Fixtures aus `seed.py` | offen |
| `tools/` | HTTP-Tests, prüfen nur Hülle und Auth | offen |
| `agent/` | mit Fake-LLM (vorgegebene Antworten), prüft Loop und Dispatch | fertig |
| `events/` | Outbox schreiben, Dispatcher gegen Fake-n8n, Retry-Verhalten | offen |
| `telephony/adapters` | against the fake adapter; later against recorded media events and fake speech engines (docs/20) | fertig |
| Ende-zu-Ende | `sim/replay` gegen echte API mit Test-DB | offen |

**Ziel:** Die schnellen Tests laufen bei jedem Speichern, die DB-Tests vor jedem Commit, die Evals vor jedem Merge.

---

## 5. Eine Datei, eine Verantwortung

Faustregeln, die Claude Code beim Anlegen anwendet:
- Über 300 Zeilen → aufteilen
- Zwei Gründe, die Datei zu ändern → zwei Dateien
- Ein Modulname, der „und" enthält → zwei Module
- `domain/` importiert `fastapi`, `httpx` oder `jinja2` → falsche Schicht
- Ein Anbietername außerhalb von `telephony/` → Verstoß
