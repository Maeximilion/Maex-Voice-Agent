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
| max_call_seconds | INT | Kostenbremse, Default 420 |

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
| max_guests | INT | Gäste gesamt im Fenster |
| slot_minutes | INT | Raster, Default 30 |

### `reservations`
| Feld | Typ | Bemerkung |
|---|---|---|
| id | UUID PK | |
| tenant_id | UUID FK | |
| call_id | UUID FK | Pflicht |
| status | TEXT | `draft` / `confirmed` / `cancelled` |
| guest_name | TEXT | |
| phone | TEXT | E.164 |
| party_size | INT | |
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
| outcome | TEXT | `completed` / `transferred` / `callback` / `abandoned` / `error` |
| transfer_reason | TEXT NULL | |
| cost_cents | INT NULL | von der Plattform, sobald verfügbar |
| model | TEXT NULL | welches Modell lief |
| tool_calls | JSONB | Liste mit Name, Dauer, Ergebnis |
| delete_after | DATE | Löschfrist |

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
| phone | TEXT | E.164, unique je Mandant |
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

| Daten | Frist | Umsetzung |
|---|---|---|
| Anrufaufnahmen | 30 Tage (Vorschlag) | täglicher Job, harte Löschung |
| Anrufprotokoll ohne Ton (`imports/anrufprotokoll.csv`, docs/17) | 90 Tage (entschieden 24.09.2026, D10) | wöchentlich `scripts/call_log.py --frist-tage 90 --loeschen`; Papierbögen nach dem Abtippen vernichten |
| Transkripte | 90 Tage (Vorschlag) | täglicher Job |
| `calls` ohne personenbezogene Felder | 24 Monate | Statistik bleibt, `caller_id` wird genullt |
| Kunden ohne Bestellung | 24 Monate (Vorschlag) | `delete_after`, täglicher Job |
| Bestellungen | nach steuerlicher Aufbewahrungspflicht | Master ist ohnehin die Kasse |

Fristen sind Vorschläge und gehören in den Rechts-Check (`docs/09_OPERATIONS_LEGAL.md`).

---

## Migrationsreihenfolge

| Migration | Inhalt |
|---|---|
| 001 | `tenants`, `service_config`, `opening_hours`, `special_days`, `capacity`, `reservations`, `calls`, `callbacks`, `outbox`, `audit_log` |
| 002 | Extension `pg_trgm` · `menu_items`, `item_options`, `item_allergens`, `item_aliases` (Trigram-Index auf `name` und `alias`), `orders`, `order_items` |
| 003 | `item_options.price_reason`: warum eine Option mehr kostet (T-4.10) |
| 004 | `customers`, `addresses`, `delivery_zones`, `orders.address_id` |
| 005 | `eval_cases`, `eval_runs` |
