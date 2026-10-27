# 11 – Modularer Aufbau

> Kein Monolith. Jedes Modul hat **eine** Verantwortung, eigene Tests und eine klare Richtung, in die es Abhängigkeiten haben darf.
> Regel für Claude Code: Bevor du eine Datei anlegst, prüfst du hier, in welches Modul sie gehört. Passt sie nirgends, fehlt ein Modul, nicht eine Ausnahme.

---

## 1. Die Schichten

```text
┌──────────────────────────────────────────────────────────────────────┐
│  EINGÄNGE          tools/ (HTTP-Vertrag)             gui/   sim/     │
│                    telephony/ (own voice layer, docs/20)             │
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

Warum diese Härte: Wenn in `domain/ordering/` nie ein Anbietername vorkommt, kannst du die Technik davor tauschen, ohne eine Preisregel anzufassen. Since 07.10.2026 that means a speech engine or the telephone access. Und die Evals testen dieselbe Fachlogik, die im Betrieb läuft — nicht eine Kopie.

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
- `routing.py` — whether the agent answers a call (only in `overflow` and `primary`), the team extension and the tenant name for the greeting; the mode is read fresh, so the emergency stop holds for the next call (T-1.13); `tenant_named`, the one tenant of a name, which the service `voice` serves (T-1.11)
- `consent.py` — `record_consent`: one `consent_log` row per answer to a consent question of the agent (call, time, question, answer), committed before anything is stored on the strength of it (T-11.1, L13)

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
| `loop.py` | Kundenzug → Modell → Tool-Aufrufe → Antwort; Abbruch bei `max_call_seconds`, and on the phone line when one guest turn takes longer than `turn_deadline_seconds` |
| `dispatch.py` | Tool-Name → `domain`-Funktion, mit Zeitmessung. `search_menu` zerlegt hier einen Satz mit mehreren Positionen (`split_positions`) und sucht je Teil; `draft_order` bekommt den Schlüssel aus den Angaben des Anrufs |
| `ladder.py` | die Verständnis-Leiter als Zustandsmaschine: zählt Fehlversuche, steigt die Stufe |
| `escalation.py` | die Auslöser aus 05 §4, prüft **vor** dem Modell; also the question "Sind Sie ein Mensch?" (`without_human_question`), whose truthful answer the loop gives without the model (docs/09 L1, T-11.5); an allergen question beyond the maintained values (`asks_beyond_the_allergens`, T-11.1, L17), which `loop.py` hands to the team like an unmaintained value after `get_item_details` |
| `guards.py` | what the core enforces whatever the model returns (05 §5): `confirm` only after an explicit yes to the draft read back before the turn, `draft_order` only with dishes from a clear match or from candidates the guest heard by name, no booking word or code from the model except the core's own `booking_say` word for word (`false_booking`), and no claim about allergens beyond the database in a call that speaks of allergies (`allergen_claim`, L17: the loop hands the call to the team); the core says the booking after `confirm` in `loop.py`). A refused call goes back to the model as a failed result with a `hint` |
| `guest_details.py` | whether the guest said a name or a phone number (`name_said`, `number_known`, `names_a_phone_number`): whole words or a spelled name; the caller ID, or the last digits of a turn that names them as a number to call, in E.164. `state.py` keeps the words and numbers of the call, writes the number the guest gave into `slots` itself and drops an unsaid name or number a model writes; `guards.py` refuses `create_reservation`, `draft_order` and `create_callback` with one, or under another number than the one in `slots` |
| `consent.py` | the yes detector (`is_yes`), shared by `guards.py` and `evals/recorder.py`; the question for the allergy as a kitchen note (`ALLERGY_QUESTION`) and its answer (`consent_answer`: `yes`, `no` or `unclear`, T-11.1) |
| `allergies.py` | the caller's allergies (T-11.1, L13): read from the guest's own sentence every turn into the state (ingredient, whose, dish; `note_allergies`), the question the core still has to ask before a draft (`question`: "Wogegen?", then the consent), and the draft with the allergies written onto its lines, its order or its table (`noted_call`). `guards.py` refuses a model note that names an allergy; `loop.py` holds the draft while a question waits |
| `intent.py` | what the guest called for, pickup or table, heard in their own sentence **before** the model (`heard`); `state.py` keeps the first wish of a call as `intent` until a draft exists |
| `llm.py` | Modellanbindung, austauschbar, mit Token-Zählung |
| `outcome.py` | outcome and intent of a call from the state, shared by `sim/session.py` and `telephony/handler.py` |

**Why our own core?** Evals need it, the simulator needs it, the shadow measurement needs it, and since D7 (decided 07.10.2026) it also runs in operation, called by the voice layer (docs/20 §2). Test and operation are identical.

### `telephony/` – der einzige Ort, der den Anbieter kennt
| Datei | Verantwortung |
|---|---|
| `port.py` | the interface in two protocols: `CallEvents` (`on_call_started`, `on_user_turn`, `on_dtmf`, `on_call_ended`) and `TelephonyPort` (`caller_id`, `say`, `transfer`, `hangup`, `start_recording`); since T-10.4 with `on_silence` and `on_voice_failure`, and `say` takes whether the sentence may be interrupted (docs/20 §4); since T-10.5 `TelephonyPort.expect`, the key terms for the speech engine (docs/20 §6) |
| `handler.py` | provider-neutral call handler (T-1.13): opens the call log, says the AI disclosure, sends the call straight to the team in `paused` and `shadow`, runs each turn through `agent/loop.py`, transfers when the state says so, or says goodbye (unless the agent just did) and hangs up, closes the call log; every failure ends with the outage sentence and a transfer |
| `adapters/fake.py` | Testadapter, spielt Anrufe aus Dateien ab |
| `app.py` | the process of the service `voice` (T-1.11, docs/20 §2): one call handler for the tenant of `TENANT_NAME`, a call session per connection with its own ear and mouth (`EARS`, `MOUTHS` by the name in `.env`: `whisper`, `piper`, the drill pair `fake`), and the endings the dialplan asks for. Refuses to start without token, own number, engines or model, with the drill engines outside dev and test, with an engine it cannot load, or with the team on its own number. Since T-1.11 part 2 each engine runs once at start, a `Watchdog` ends the process when an engine call hangs (only the time it works counts, 30 s; a load 120 s; started before the engines load), and the call handler's database sessions end a statement after `DB_TIMEOUT_MS` (`db.bounded_sessions`). Started by uvicorn with `--factory` |
| `session.py` | call session of the own voice layer, one per call (T-10.4, docs/20 §4): one worker and one queue, feeds the ear, decides what is the caller's turn, plays what the core says through the mouth; keeps a read-back uninterrupted and its answer behind it, drains speech before a transfer or hangup, buffers the keypad, counts silence, turns a failure of ear or mouth into `on_voice_failure`. `Lines` is the `TelephonyPort` over all sessions; `broken` turns a line that cannot go on into a failure of the voice layer (T-1.11). Knows no engine and no telephone system |
| `speech.py` | the ports of the voice layer (T-10.4): `Ear` (speech-to-text, reports `Heard`), `Mouth` (text-to-speech) and `Line` (the caller's leg: answer, play, flush, transfer, hangup) |
| `adapters/asterisk.py` | media channel of Asterisk: the wire format (T-10.1), `call_of` (which call a connection is, from the variables `MEDIA_START` reports) and `MediaLine` (T-1.11), the `speech.Line` on one connection: answers with the first sentence, reports a sentence played by its own mark, keeps at most about 6 s unplayed at Asterisk because a full queue drops audio and still reports the mark behind it, records the ending for the dialplan before `HANGUP`. Checked against frames recorded from Asterisk 22.11.0 (22.10.1 behaved the same) (`api/tests/fixtures/asterisk_media_*.jsonl`). Standard library only |
| `drill.py` | kit of the outage drills (T-1.11): starts the product's `asterisk` and `voice` against a stand-in for the router (`deploy/asterisk/drill/`), places one call per drill, kills, stops or freezes `voice` and stops the database, and judges from the containers' output whether the team rang and how fast. `scripts/voice_drill.py` is only its entry. Standard library only |
| `audio.py` | test tones in raw call audio: make one, find its frequency and how long it sounds (T-10.1). Standard library only |
| `probe.py` | measuring endpoint of the spike T-10.1: stands where `voice` will stand, plays a tone per dialed extension, reacts to keys, logs every control frame. Plain ASGI, no database, no settings. Driven by `spike.py`; the Asterisk configuration of the spike is `deploy/asterisk/` (docs/20 §10) |
| `spike.py` | kit of the spike T-10.1: starts the test PBX, the scripted softphone and the probe as containers, places the calls and judges them from the probe's logs and the softphone's recordings. In `telephony/` because it drives the PBX; `scripts/voice_spike.py` is only its entry. Standard library only |
| `speech_bench.py` | bench of the spike T-10.3, runs inside the image of `deploy/speech/`: downloads the open engines' model files, speaks the sentences of a plan with every voice, sends them through the band of a telephone line (`telephone`), lets every ear transcribe them and writes what was heard with the timings. Judges nothing and knows nothing of the menu. The only file here that needs numpy; the engine packages are imported where they are used and live in that image only |
| `speech_spike.py` | kit of the spike T-10.3: names the candidates (ears, voices), builds the plan from the eval files, runs bench and judge (`evals/heard.py`) as containers, the last two without a network, and prints the tables and the pick. With `--orders` the plan holds spoken orders of several dishes instead, each judged as one order. `scripts/speech_spike.py` is only its entry. Standard library only |
| `adapters/stt_<engine>.py` | one speech-to-text engine each (docs/20 §2). `stt_whisper.py` since T-10.5: `SegmentingEar`, the silence detector that turns an engine which only transcribes into an `Ear` (partials while the caller speaks, the end of an utterance after 704 ms of silence, on a thread of its own), and `Whisper`, the engine loaded once per process. Its packages are imported where they are used and live in the image of the voice service |
| `adapters/tts_<engine>.py` | one text-to-speech engine each (docs/20 §2). `tts_piper.py` since T-10.5: a Piper voice as a `Mouth`, converted to the rate of the line |
| `adapters/fake_speech.py` | fake ear, mouth and line for tests (T-10.4): the ear says what a test tells it, the mouth returns the sentence as its audio, the line records what it was asked to do |
| `router.py` | the doors of `voice` (T-1.11): `/media`, Asterisk's media channel, one WebSocket per call, its session made at `MEDIA_START` (a connection that names no call, or a call that is live, is refused), frames to the call's `MediaLine` and out through one writer that closes a connection which stalls for 2 s; `/calls/{id}/ending`, what the dialplan asks when our leg is gone. Both want the media token as Basic credentials |

The port has two directions (T-1.13): `CallEvents` is what the media side of the voice layer reports (`on_call_started`, `on_user_turn`, `on_dtmf`, `on_call_ended`), implemented once by `handler.py`; `TelephonyPort` is what we make the line do (`caller_id`, `say`, `transfer`, `hangup`, `start_recording`), implemented by each adapter. Calls are named by the session id of the media side (`calls.external_session_id`). `start_recording` is called nowhere before the legal check in docs/09. `fake.py` reads the eval case format (docs/08 §1) plus customer entries with `dtmf` or `hangup`, so every case in `evals/cases/` plays as a phone call. The speech engines are the planned files left (T-10.5).

**Rule:** a different speech engine changes one file in `adapters/`, its name in `app.py`, and `.env`. A different access (the pilot's router or a SIP trunk) changes the Asterisk configuration under `deploy/asterisk/` and `.env`. Nothing else.

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
- `retention.py` — deletion job after docs/03 "Löschkonzept" (T-11.2): `purge(session, now)` strips the personal fields whose period is over and logs one line per pass; `cold_path.py` runs it in a thread of its own every `RETENTION_INTERVAL_SECONDS`, `python -m api.jobs.retention` runs one pass by hand
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

Damit gibt es den **Durchstich ohne Telefon**: Terminal → Agent → Fachlogik → DB → Tablet zeigt die Bestellung. Everything is testable without a telephone.

### `evals/`
- `runner.py` (Lauf, Wegwerf-Datenbank, CLI), `judge.py` (Abgleich mit dem Datenbankzustand), `recorder.py` (Beobachter am Modell für geratene Positionen und `confirm` ohne Ja), `report.py` (JSON, Markdown, Vergleich mit dem letzten Lauf), `scratch_db.py` (Wegwerf-Datenbanken, auch für die Tests) — siehe 08
- `heard.py` - judges what a speech-to-text engine heard against what was said: a number sentence by the rule that resolves it on the phone (`number_eval.resolve`) as right, asked or silently wrong, a dish name letter by letter. Knows no engine; written for the spike T-10.3, the audio evals of T-10.7 judge the same way
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
