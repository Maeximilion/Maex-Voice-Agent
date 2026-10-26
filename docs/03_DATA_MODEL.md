# 03 – Datenmodell

> PostgreSQL 16. Jede Änderung als Alembic-Migration, `up` und `down` getestet.

---

## Grundregeln

- **Geld** immer `INTEGER` in Cent. Kein `FLOAT`, kein `NUMERIC` mit Nachkommastellen im Code.
- **Telefonnummern** als E.164-Text (`+4972215551234`), beim Eingang normalisiert.
- **Zeiten** als `TIMESTAMPTZ` in UTC. Anzeige in `Europe/Berlin`.
- **IDs** als `UUID` (`gen_random_uuid()`), außer bei Menüpositionen: dort zusätzlich die **Kartennummer** als eindeutiger fachlicher Schlüssel.
- **Mandantenfähig**: jede betriebsbezogene Tabelle trägt `tenant_id`. Aktuell genau ein Mandant, aber das Schema bleibt offen.
- **Weich löschen** bei allem, was ein Vorgang ist (`deleted_at`). Hart löschen nur bei personenbezogenen Daten nach Frist.
- Jede Tabelle hat `created_at` und `updated_at`.

---

## Stufe 1 – Reservierung

### `tenants`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| name | TEXT | „<Firmenname>" |
| timezone | TEXT | `Europe/Berlin` |

### `service_config`
Eine Zeile je Mandant. Der Live-Schalter des Betriebs.

| Feld | Typ | Bemerkung |
|---|---|---|
| tenant_id | UUID PK FK | |
| call_mode | TEXT | `shadow` / `overflow` / `primary` / `paused` |
| delivery_enabled | BOOL | Schalter „Lieferung pausieren" |
| pickup_wait_minutes | INT | aktuelle Wartezeit Abholung |
| delivery_wait_minutes | INT | aktuelle Wartezeit Lieferung |
| team_phone | TEXT | Durchwahl für `transfer_to_team` |
| max_call_seconds | INT | Kostenbremse, Default 420. The phone line reads it with the routing at the start of every call, the text phone, the dev phone and the evals when their session starts, and each ends the call by it (`domain/calls/routing.py`, `telephony/handler.py`, `sim/session.py`); a change applies from the next call. The setting `MAX_CALL_SECONDS` is only what the seed writes here. Also how long a reservation draft holds its seats without a confirmation (`domain/reservations/capacity.py`) |
| reservation_lead_minutes | INT ≥ 0 | frühester Reservierungsbeginn nach Öffnung eines Service, Default 15 (D12, Migration 005) |
| reservation_last_start_minutes | INT ≥ 0 | spätester Reservierungsbeginn vor Schluss eines Service, Default 30 (D12, Migration 005) |
| reservation_max_days_ahead | INT ≥ 0 | how many days ahead a table can be booked by phone, counted in calendar days of the restaurant, default 90 (#252, migration 007). 0: today only |
| reservation_max_party_size | INT ≥ 0 | largest party the agent books itself, default 8 (#252, migration 007). Above it `check_slot` answers `team_callback` and the team calls back. 0: the team confirms every table |
| reservation_max_party_size_same_day | INT ≥ 0 | the same limit for a table on the same day, default 5 (#252, migration 007). The smaller of the two counts on that day |
| order_approval_above_cents | INT ≥ 0 | an order whose total is above this amount waits for the team's approval on the tablet before a slip prints, whatever the mode; default 15000, 0 holds every order (#252, migration 007). Read by `confirm` since 13.10.2026 (`domain/ordering/confirm.py`, docs/04 §confirm) |
| turn_deadline_seconds | INT ≥ 1 | how long a guest waits in silence for the answer to one sentence before the call goes to the team; default 10 (#252, migration 007). Read by the phone line with the routing at the start of every call (`domain/calls/routing.py`, `telephony/handler.py`) and kept by `agent/loop.py`; a changed value applies from the next call. The text phone and the evals have no deadline |
| order_approval_overdue_minutes | INT ≥ 1 | how long an order may wait for "Passt" on the tablet before its card turns red; default 5 (#252, migration 008). Counted from the guest's yes, the audit entry `order.confirmed`. Read by the order board (`domain/ordering/board.py`); a changed value shows within the next poll of the tablet |

### `opening_hours`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id | UUID FK | |
| weekday | SMALLINT | 0 = Montag … 6 = Sonntag |
| opens_at | TIME | |
| closes_at | TIME | |
| service | TEXT | `dinein` / `pickup` / `delivery` |

Mehrere Zeilen je Wochentag erlaubt (Mittag und Abend getrennt).

### `special_days`
Feiertage, Urlaub, Sonderzeiten. Schlägt `opening_hours` für dieses Datum.

| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id | UUID FK | |
| date | DATE | |
| closed | BOOL | |
| opens_at / closes_at | TIME NULL | nur wenn `closed = false` |
| note | TEXT | erscheint in der GUI |

### `capacity`
Tischkapazität je Zeitfenster, Grundlage für `check_slot`.

| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id | UUID FK | |
| weekday | SMALLINT | |
| slot_start / slot_end | TIME | z. B. 18:00–20:00 |
| max_guests | INT | Gäste gesamt im Fenster, `>= 0` (0 closes the window for reservations; migration 006) |
| slot_minutes | INT | Raster, Default 30, `> 0` (migration 006: a grid of zero minutes never ends) |

### `reservations`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id | UUID FK | |
| call_id | UUID FK | Pflicht |
| status | TEXT | `draft` / `confirmed` / `cancelled` |
| guest_name | TEXT | |
| phone | TEXT | E.164 |
| party_size | INT | `> 0` (migration 006) |
| reserved_for | TIMESTAMPTZ | |
| note | TEXT | Kinderstuhl, Allergie, Anlass |
| idempotency_key | TEXT UNIQUE | |

### `calls`
Ein Eintrag je Anruf. Grundlage für KPIs und Kostenkontrolle.

| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id | UUID FK | |
| external_session_id | TEXT | ID der Voice-Plattform |
| caller_id | TEXT NULL | NULL bei unterdrückter Nummer |
| started_at / ended_at | TIMESTAMPTZ | |
| duration_seconds | INT | |
| intent | TEXT | `reservation` / `pickup` / `delivery` / `info` / `complaint` / `unknown` |
| outcome | TEXT | `completed` / `transferred` / `callback` / `abandoned` / `error`. A transferred call whose team leg nobody answered becomes `callback` once its card is written, also when that comes before the log of our leg; `error` stays, logged before or after the card (T-10.8) |
| transfer_reason | TEXT NULL | |
| cost_cents | INT NULL | von der Plattform, sobald verfügbar. From the own core (text phone, T-2.4): the model cost of the call in whole cents, rounded up, from the counted tokens and the prices in `.env`; empty when no real model ran, a price is missing or the tokens of one request are unknown, never 0 for unknown. The exact tokens are in the log line "model usage", not in this table |
| model | TEXT NULL | welches Modell lief |
| tool_calls | JSONB | Liste mit Name, Dauer, Ergebnis |
| delete_after | DATE | the day the caller number goes, `RETENTION_CALLER_ID_DAYS` (90) after `started_at` (T-11.2; 24 months before). Informational: the deletion job goes by `started_at`, so a shortened period also reaches older rows |

### `callbacks`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id / call_id | UUID FK | |
| phone | TEXT | |
| reason | TEXT | `complaint` / `not_understood` / `human_requested` / `out_of_scope` |
| summary | TEXT | was der Kunde wollte, in einem Satz |
| status | TEXT | `open` / `done` |
| done_by / done_at | TEXT / TIMESTAMPTZ | |

### `outbox`
Der kalte Pfad beginnt hier. Wird in **derselben Transaktion** wie der Fachvorgang geschrieben.

| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | dient n8n als Idempotenz-Schlüssel; `order.confirmed` holt die Druckbrücke ab, nicht der Dispatcher (T-4.6) |
| tenant_id | UUID FK | |
| event_type | TEXT | `order.confirmed` / `reservation.confirmed` / `callback.created` / `order.handover_failed` / `daily.report` |
| payload | JSONB | vollständiger Vorgang, damit n8n nicht zurückfragen muss |
| status | TEXT | `pending` / `sent` / `failed` |
| attempts | INT | |
| next_attempt_at | TIMESTAMPTZ | Backoff: 5 s, 30 s, 2 min, 10 min, dann `failed` + Alarm |
| last_error | TEXT NULL | |
| sent_at | TIMESTAMPTZ NULL | |

### `audit_log`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | BIGSERIAL PK | |
| tenant_id | UUID | |
| at | TIMESTAMPTZ | |
| actor | TEXT | `agent` / `gui:<user>` / `system` |
| action | TEXT | `order.confirm`, `config.update`, … |
| entity / entity_id | TEXT / UUID | |
| payload | JSONB | vorher/nachher bei Änderungen |

---

## Stufe 2 – Abholung

### `menu_items`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id | UUID FK | |
| number | TEXT | Kartennummer, z. B. „23", eindeutig je Mandant |
| name | TEXT | |
| category | TEXT | Vorspeise, Suppe, Wok, Sushi … |
| price_cents | INT | |
| active | BOOL | |
| sold_out_until | TIMESTAMPTZ NULL | Schalter „Gericht aus" |
| description | TEXT | |
| pos_code | TEXT NULL | Artikelnummer genau wie in der Kasse („35B"), `number` ist klein für die Suche. Eingabezettel und Kassenübergabe sollen sie drucken (Umstellung mit T-4.6, heute steht dort noch `number`); leer bei Gerichten, die nicht aus der Kasse kommen (T-4.11, Migration 004) |

**Eindeutigkeit:** `(tenant_id, number)` ist unique. Die Nummer ist der robusteste Weg durch eine schlechte Leitung.

### `item_options`
Varianten und Extras mit Preisdifferenz.

| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| menu_item_id | UUID FK | |
| group_name | TEXT | „Fleisch", „Schärfe", „Größe" |
| option_name | TEXT | „Huhn", „mittel", „groß" |
| price_delta_cents | INT | kann negativ sein |
| is_default | BOOL | |
| required | BOOL | Gruppe muss gewählt werden |
| price_reason | TEXT NULL | warum die Option mehr kostet („zweite Station in der Küche"). Der Agent nennt nur diesen Satz, nie eine eigene Begründung. Leer: der Preis steht so in der Karte (Migration 003, T-4.10) |

### `item_allergens`
| Feld | Typ | Bemerkung |
|---|---|---|
| menu_item_id | UUID FK | |
| allergen_code | TEXT | LMIV-Kennbuchstabe, nur `A`–`H`, `L`–`P`, `R` (14 Hauptallergene; kein I, J, K, Q). Die DB lehnt alles andere ab. |
| confirmed_by | TEXT | wer den Wert gepflegt hat |
| confirmed_at | TIMESTAMPTZ | |

**Regel:** Kein Eintrag bedeutet *keine Auskunft*, nicht *kein Allergen*. Der Agent sagt dann: Rückruf durch das Team.

### `item_aliases`
Der Übersetzer zwischen Kundensprache und Karte. Wächst aus echten Anrufen.

| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| menu_item_id | UUID FK | |
| alias | TEXT | „Frühlingsrollen", „die knusprigen", „Nummer 23" |
| source | TEXT | `manual` / `call` / `import` |
| hits | INT | wie oft er getroffen hat |

### `orders`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id / call_id | UUID FK | |
| type | TEXT | `pickup` / `delivery` |
| status | TEXT | `draft` / `confirmed` / `approved` / `handed_over` / `cancelled` |
| customer_id | UUID NULL FK | Fremdschlüssel erst mit 004 (`customers`), bis dahin nur das Feld |
| phone | TEXT | |
| customer_name | TEXT | |
| address_id | UUID NULL FK | nur bei Lieferung |
| items_total_cents | INT | |
| delivery_fee_cents | INT | |
| total_cents | INT | |
| ready_at | TIMESTAMPTZ | zugesagte Zeit |
| note | TEXT | |
| idempotency_key | TEXT UNIQUE | |
| pickup_code | TEXT NULL | Abholcode („A17"), ab `confirm` gesetzt (docs/04 confirm, docs/06 §3) |
| handover_state | TEXT NULL | `pending` / `sent` / `failed` — Übergabe an Küche/Kasse; leer, solange Entwurf, und leer nach `confirm` außerhalb von `primary`, bis das Team freigibt. `sent` setzt die Rückmeldung der Druckbrücke, `failed` heißt „Küche hat den Bon nicht" (Druckfehler oder 60 s unabgeholt) und wird wieder `sent`, sobald der Bon doch gedruckt ist (T-4.6, docs/04 §Küchenbon) |

**Datenbank prüft mit:** `total_cents = items_total_cents + delivery_fee_cents`, Beträge ≥ 0, Menge > 0. Ein Rechenfehler im Code scheitert an der Tabelle, nicht auf dem Bon.

`approved` heißt: das Team hat die Bestellung im Tablet mit „Passt" abgehakt (T-4.7). Außerhalb von `primary` ist das zugleich die Freigabe an die Küche - vorher gibt es keinen Bon (docs/04 §confirm). Im Überlauf-Betrieb (Stufe 5) ist genau das der Freigabe-Schritt; T-8.2 macht ihn abschaltbar.

Das Tablet schreibt ins `audit_log`: `order.approved` (Passt, `released` sagt, ob damit der Bon losging), `order.resent` (Nochmal senden) und `order.corrected` (Korrektur, mit `reason`, `before`/`after` je Position, `labels` und `note_dropped`, aber ohne Name, Telefon und Hinweistext). Die jüngste `labels`-Liste aus `order.draft_created` oder `order.corrected` nennt Nummer und Name je Position in der Reihenfolge von `created_at` (`domain/ordering/labels.py`). Die Zahl der `order.corrected`-Zeilen ist die Revision des Bons.

### `order_items`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id | UUID FK | Mandant der Position; Bestellung und Gericht werden je über `(id, tenant_id)` referenziert, damit beide im selben Mandanten liegen |
| order_id | UUID FK | |
| menu_item_id | UUID FK | **Pflicht.** Ohne ID keine Position. |
| quantity | INT | |
| unit_price_cents | INT | Preis zum Bestellzeitpunkt, eingefroren |
| options | JSONB | gewählte Optionen mit Preisdifferenz |
| note | TEXT | „ohne Zwiebeln", „WICHTIG: Keine Erdnüsse. Grund: Allergie" (E14) |

---

## Stufe 3 – Lieferung

### `customers`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id | UUID FK | |
| phone | TEXT | E.164; not unique: a shared number can belong to several customers, and an objection to recognition is kept per number (legal check L12, T-6.1) |
| name | TEXT | |
| last_order_at | TIMESTAMPTZ | |
| order_count | INT | |
| blocked | BOOL | Spam- oder Scherzanrufer |
| delete_after | DATE | Löschfrist |

### `addresses`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| customer_id | UUID FK | |
| street / house_number | TEXT | getrennt, die Hausnummer kommt oft über die Tastatur |
| postal_code / city | TEXT | |
| floor_note | TEXT | „2. OG, Klingel Müller" |
| zone_id | UUID NULL FK | beim Anlegen aufgelöst und gespeichert |
| lat / lon | DOUBLE NULL | nur bei Polygon-Zonen |
| is_default | BOOL | |

### `delivery_zones`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id | UUID FK | |
| name | TEXT | „Zone 1 <Ort>" |
| match_type | TEXT | `postal_code` oder `polygon` |
| postal_codes | TEXT[] | bei `postal_code` |
| polygon | JSONB | GeoJSON bei `polygon` |
| fee_cents | INT | Lieferpauschale |
| min_order_cents | INT | Mindestbestellwert |
| eta_minutes | INT | |
| active | BOOL | |

**Start:** `postal_code` genügt und ist ohne Geo-Erweiterung testbar. Polygone erst, wenn die Liefergebiets-Kalkulation sie liefert. Dann ist PostGIS eine Option, `shapely` im Python-Code reicht aber für diese Datenmenge.

---

## Stufe 4 – Qualität

### `eval_cases`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| name | TEXT | |
| transcript | TEXT | Kundenseite, ggf. mit Störungen |
| expected | JSONB | erwartetes Ergebnis-JSON |
| tags | TEXT[] | `menu`, `address`, `noise`, `dialect`, `escalation` |
| source | TEXT | `roleplay` / `real_call` / `handcrafted` |

### `eval_runs`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| at | TIMESTAMPTZ | |
| prompt_version / model | TEXT | |
| passed / failed | INT | |
| accuracy | NUMERIC | |
| details | JSONB | je Fall: erwartet, bekommen, Abweichung |
| git_sha | TEXT | |

---

## Löschkonzept

Decided by Maxi on 20.10.2026 with the legal check (`docs/09_OPERATIONS_LEGAL.md` §1, L8, L13, L15). Every period is a setting (`.env` or `service_config`), never a number in the code. **Built:** the call log deletion (`scripts/call_log.py`) and, since T-11.2 (26.10.2026), the daily deletion job `api/jobs/retention.py` for every row below that names it. It runs in the dispatcher process once an hour (`RETENTION_INTERVAL_SECONDS`, first pass at the start of the process) or by hand with `python -m api.jobs.retention`; a pass only does what is due, writes one log line "Retention pass" with a count per period, and repeating it changes nothing. Rows stay and lose their personal fields: required text columns become empty strings, so no migration was needed. The periods are the settings `RETENTION_*_DAYS` (`.env.example`), each at least one day. Open: T-11.7, T-6.7 and T-9.3.

| Data | Period | How |
|---|---|---|
| Call recordings | 30 days; at once on withdrawal | daily job, hard delete (T-11.3) |
| Transcripts | 90 days; no real wording ever goes into the repository, eval cases are synthetic (L14) | daily job (T-7.1) |
| Call log without audio (`imports/anrufprotokoll.csv`, docs/17) | 90 days (D10); the hour of the call only, not the minute (L15) | weekly `scripts/call_log.py --frist-tage 90 --loeschen`; paper sheets destroyed after typing |
| `calls.caller_id` | 90 days after the call (was 24 months), `RETENTION_CALLER_ID_DAYS` | daily job nulls it; the statistics in `calls` stay |
| Name, phone, address, notes and the links `customer_id` and `address_id` on orders and reservations | 90 days after the order or reservation date (`ready_at` of an order, its creation while it has none; `reserved_for`), `RETENTION_GUEST_DETAILS_DAYS` | daily job strips the fields and nulls the links, so the row cannot be traced through the customer record; items, prices, times and `call_id` stay. The notes of the positions (`order_items.note`) go with them. `address_id` joins with T-6.1 |
| A caller's allergy in `order_items.note`, `orders.note` or `reservations.note` | end of the business day of the order (the night after `ready_at`, 05:00 local time, `core/time.py`), collected or not; for a reservation the night after its date; `RETENTION_ALLERGY_NOTE_DAYS` business days later (0) | daily job empties the note text (L13), also its copies in the outbox payloads of the same orders and reservations (`note`, `items[].note`; Codex PR #371), so it does not wait for the payload period. A note counts as an allergy when it contains "Allergi…" (the kitchen wording "Grund: Allergie" included), "Unverträglich…" or "Intoleran…", the words the call log refuses too; the whole note goes. Any other note stays until the guest details go |
| `callbacks.phone` and `callbacks.summary` | 90 days after the callback is done (the window of orders and reservations; not named in the check, set with it), `RETENTION_CALLBACK_DAYS` | daily job strips both fields (T-11.2); an open callback keeps them, the team still has to call back |
| Customers and their addresses | 24 months after the last contact | daily job (T-6.7) |
| Outbox payloads | 7 days after a successful dispatch (`RETENTION_OUTBOX_SENT_DAYS`); any event not sent at most 30 days after it was written (`RETENTION_OUTBOX_UNSENT_DAYS`) | daily job cuts the payload to its ids (`order_id`, `reservation_id`, `callback_id`, `call_id`, `revision`): the column is not nullable, and the kitchen finds every slip of an order by id and revision. An event still pending at 30 days becomes `failed` with it, so a slip without the guest is never printed or sent. The event row goes after 90 days (`RETENTION_OUTBOX_ROW_DAYS`) |
| Container and Caddy logs | 14 days | cleanup by age, size limits alone do not hold the period (T-11.7) |
| n8n executions | 7 days | `EXECUTIONS_DATA_PRUNE`, `EXECUTIONS_DATA_MAX_AGE=168` (T-11.7) |
| Backups | rolling 30 days | retention of `scripts/backup.sh` and a lifecycle rule on the storage; an erasure ledger outside the database and its backups is replayed after a restore (T-9.3) |
| `audit_log` | 12 months, `RETENTION_AUDIT_LOG_DAYS` (365) | daily job deletes the entries; they carry no names, phone numbers or notes |
| Request log of data subjects (`docs/09_OPERATIONS_LEGAL.md` §1 U2) | **Assumption, awaiting Maxi (21.10.2026):** the verification details (name, order or reservation date, delivery address as the requester gave them) are stripped 3 months after the request is answered; the row without them (request id, received and answered date, kind, outcome) stays until the end of the third calendar year after the year of the answer, then the row is deleted. Reasoning: the details are needed only to answer and to defend the answer if the requester complains; the thin row is the proof of the one-month deadline (Art. 12(3), accountability Art. 5(2)) for as long as a claim can arise (regular limitation of 3 years, § 195 BGB, which under § 199(1) BGB starts at the end of the year in which the claim arose). A legal choice: Maxi confirms or changes it | Place: table `data_subject_requests`, planned with T-11.8 (id, `tenant_id` (business table, so every lookup and the cleanup are scoped by it), `received_at`, `answered_at`, `kind`, `verification_note`, `outcome`, `delete_after`); until it exists the manual procedure keeps the log in a file outside the repository (no customer data in the repo), cleaned by hand in the monthly routine (docs/09 §3). Cleanup once the table exists: the daily job of T-11.2 (it strips `verification_note` and deletes expired rows, a log line per run). The erasure ledger (U7) is a separate list of row ids without personal data and is not touched by this period |
| Order rows without personal fields | as long as the tax adviser asks (L18); the register is the master | kept |

---

## Migrationsreihenfolge

| Migration | Inhalt |
|---|---|
| 001 | `tenants`, `service_config`, `opening_hours`, `special_days`, `capacity`, `reservations`, `calls`, `callbacks`, `outbox`, `audit_log` |
| 002 | Extension `pg_trgm` · `menu_items`, `item_options`, `item_allergens`, `item_aliases` (Trigram-Index auf `name` und `alias`), `orders`, `order_items` |
| 003 | `item_options.price_reason`: warum eine Option mehr kostet (T-4.10) |
| 004 | `menu_items.pos_code`: Artikelnummer genau wie in der Kasse (T-4.11) |
| 005 | `service_config.reservation_lead_minutes`, `reservation_last_start_minutes`: Reservierungsbeginn je Service (T-1.14, D12) |
| 006 | CHECK on `capacity.slot_minutes`, `capacity.max_guests`, `reservations.party_size` (#252) |
| 007 | `service_config.reservation_max_days_ahead`, `reservation_max_party_size`, `reservation_max_party_size_same_day`: limits for a reservation by phone; `order_approval_above_cents` (read by `confirm` since 13.10.2026) and `turn_deadline_seconds` (read by the phone line since 13.10.2026): the two other limits of #252, in the same migration |
| 008 | `service_config.order_approval_overdue_minutes`: after how many minutes without "Passt" the card of a waiting order turns red (5); the downgrade drops the column |
| planned, next free number | `customers`, `addresses`, `delivery_zones`, `orders.address_id` (T-6.1) |
| planned, next free number | `eval_cases`, `eval_runs` |
