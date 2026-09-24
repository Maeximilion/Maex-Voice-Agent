# 11 – Modularer Aufbau

> Kein Monolith. Jedes Modul hat **eine** Verantwortung, eigene Tests und eine klare Richtung, in die es Abhängigkeiten haben darf.
> Regel für Claude Code: Bevor du eine Datei anlegst, prüfst du hier, in welches Modul sie gehört. Passt sie nirgends, fehlt ein Modul, nicht eine Ausnahme.

---

## 1. Die Schichten

```text
┌──────────────────────────────────────────────────────────────────────┐
│  EINGÄNGE          tools/ (HTTP für die Plattform)   gui/   sim/     │
│                    telephony/ (Webhooks der Plattform)               │
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
- `telephony/` kennt den Anbieter. **Sonst niemand.**

Warum diese Härte: Wenn in `domain/ordering/` nie ein Anbietername vorkommt, kannst du die Voice-Plattform tauschen, ohne eine Preisregel anzufassen. Und die Evals testen dieselbe Fachlogik, die im Betrieb läuft — nicht eine Kopie.

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
| `numberwords.py` | „dreiundzwanzig" → 23, „zweimal" → Menge 2, „Nummer vierzig sieben" → 47. Reine Funktion, hundert Tests. |
| `normalize.py` | Kleinschreibung, Umlaute, Füllwörter raus („einmal die … bitte") |
| `search.py` | die Auflösungsreihenfolge aus 04: Nummer → Alias → Trigram, mit Schwellen aus der Konfiguration |
| `aliases.py` | Alias anlegen, Treffer zählen, Vorschläge aus Anrufen |
| `details.py` | Optionen, Allergene mit der Regel „unbekannt ≠ keine" |
| `importer.py` | CSV nach 14 lesen, prüfen, einspielen, Bericht |

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
| `llm.py` | Modellanbindung, austauschbar, mit Token-Zählung |

**Warum ein eigener Kern, wenn die Plattform einen hat?** Drei Gründe: Evals brauchen ihn, der Simulator braucht ihn, die Schattenmessung braucht ihn. Ob er auch im Betrieb läuft, ist Entscheidung **D7** (`docs/01_STATUS.md`). Läuft er, sind Test und Betrieb identisch. Läuft die Plattform ihren eigenen Loop, bleibt ein Rest Abweichung, den die Rollenspiele auffangen.

### `telephony/` – der einzige Ort, der den Anbieter kennt
| Datei | Verantwortung |
|---|---|
| `port.py` | das Interface: `on_call_started`, `on_user_turn`, `on_dtmf`, `transfer`, `hangup`, `start_recording`, `caller_id` |
| `adapters/<anbieter>.py` | übersetzt Webhooks und API des Anbieters auf das Interface |
| `adapters/fake.py` | Testadapter, spielt Anrufe aus Dateien ab |
| `router.py` | Webhook-Endpunkte, Signaturprüfung, Session-Zuordnung |

**Regel:** Wechselt der Anbieter, ändert sich genau eine Datei in `adapters/` und `.env`. Sonst nichts.

### `tools/` – dünne HTTP-Hülle
Ein Modul je Endpunkt, jeweils fünf bis fünfzehn Zeilen: Request parsen, `domain` aufrufen, Hülle zurück, Dauer loggen. **Keine Fachlogik hier.** Wenn ein Tool wächst, ist die Logik in `domain/` falsch abgelegt.

### `events/` – der kalte Pfad
- `outbox.py` — Ereignis schreiben (in derselben Transaktion wie der Fachvorgang)
- `dispatcher.py` — alle paar Sekunden: `pending` lesen, an n8n senden, `sent` oder Wiederholung mit Backoff, nach N Versuchen `failed` plus Alarm
- `types.py` — `order.confirmed`, `reservation.confirmed`, `callback.created`, `order.handover_failed`, `daily.report`

Outbox statt direktem Aufruf: Fällt n8n aus, ist die Bestellung trotzdem gebucht und die GUI zeigt sie. Das Ereignis wartet, bis n8n zurück ist. Kein Vorgang geht verloren, keiner wird doppelt gesendet.

### `jobs/`
- `retention.py` — täglicher Löschjob nach 03
- `menu_diff.py` — Abgleich Kasse gegen Agent-DB
- `daily_report.py` — Kennzahlen des Tages als Outbox-Ereignis
- `holidays.py` — Feiertage Baden-Württemberg jährlich in `special_days` vorschlagen

### `gui/`
- `router.py` — Seiten und HTMX-Fragmente
- `sse.py` — Ereignisstrom für Live-Updates (neue Bestellung, Rückruf, Modus)
- `templates/` — `base.html`, `betrieb/`, `admin/`, `fragments/`
- `static/` — Pico.css, HTMX lokal, eigene Töne
- `dev/console.html` — der Simulator als Webseite, nur in `ENV=dev`

### `sim/` – das Text-Telefon
- `cli.py` — Gespräch im Terminal: du tippst den Kunden, der Agent antwortet, Tool-Aufrufe werden angezeigt
- `replay.py` — spielt ein Transkript aus `evals/cases/` ab
- `session.py` — die gemeinsame Mechanik beider Eingänge: Anruf-Zeile öffnen und schließen, Zustand halten, Tool-Protokoll und Ausgabe
- `scripted_llm.py` — regelbasierter Modell-Ersatz bis T-2.4: erkennt Reservierung und Abholung aus `prompts/system_v2.md`, rät nie, meldet Unverstandenes an die Leiter
- `scripted_order.py` — der Abholfluss des Modell-Ersatzes: Gerichte, Pflichtoptionen, Name, `draft_order`, vorlesen, `confirm`
- `noise.py` — verrauscht Eingaben absichtlich (Buchstabendreher, abgeschnittene Wörter), um die Leiter zu testen

Damit gibt es den **Durchstich ohne Telefon**: Terminal → Agent → Fachlogik → DB → Tablet zeigt die Bestellung. Alles vor der Anbieterentscheidung testbar.

### `evals/`
- `runner.py`, `compare.py`, `report.py` — siehe 08
- `cases/<bereich>_<nr>_<name>.json`

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
| 7 | `tools/` für die Stufe-1-Tools | die Plattform kann anklopfen | 1 |
| 8 | `agent/` + `sim/` | **erstes Gespräch im Terminal** | 1 |
| 9 | `gui/` Betrieb v0 | die Reservierung wird sichtbar | 1 |
| 10 | `telephony/port` + `fake` | Interface steht, bevor der Anbieter feststeht | 1 |
| 11 | `telephony/adapters/<anbieter>` | erst nach D1 | 1 |
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
| `telephony/adapters` | gegen aufgezeichnete Webhooks | fertig |
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
