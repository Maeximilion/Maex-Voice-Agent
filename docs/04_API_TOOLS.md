# 04 – Agent-API und Tool-Verträge

> Diese Endpunkte ruft die Voice-Plattform auf, während der Kunde in der Leitung wartet.
> Basis: `POST /v1/tools/<name>` · Auth: `Authorization: Bearer <AGENT_API_TOKEN>` · `Content-Type: application/json`

---

## Gemeinsame Regeln

**Jeder Request enthält:**
```json
{ "call_id": "uuid", "tenant_id": "uuid", "...": "toolspezifisch" }
```

**Jede Antwort hat dieselbe Hülle:**
```json
{ "ok": true, "data": { }, "say": "kurzer Satz für den Agenten oder null" }
```
```json
{ "ok": false, "error": { "code": "not_found", "message": "…" }, "say": "…" }
```

- `say` ist ein **Vorschlag**, kein Zwang. Es hält die Formulierung heikler Fälle (ausverkauft, außerhalb der Zone, Allergene) im Code statt im Modell.
- **Fehlercodes:** `invalid_input` · `not_found` · `ambiguous` · `closed` · `out_of_zone` · `below_minimum` · `conflict` · `service_unavailable`
- **Nie** ein Stacktrace, nie ein HTTP 500 ohne JSON-Körper. Der Agent muss jede Antwort vorlesen können.
- **Latenzbudget:** 300 ms p95 bei lokaler DB. Wird in den Tests gemessen (`p95_ms` in `api/tests/conftest.py`, 20 Aufrufe je Messreihe).
  - **Messverfahren:** bis zu 3 Messreihen. p95 gilt über alle bisher gemessenen Aufrufe (20, 40, dann 60), keine Reihe wird verworfen; liegt es unter dem Budget, endet die Messung. Die Grenze ist lokal und in CI dieselbe, 300 ms.
  - **Begründung:** Auf geteilten CI-Runnern laufen Push- und PR-Lauf plus `docker-smoke` gleichzeitig. Einzelne Ausreißer hoben p95 dort auf 560 ms (18.09.2026), lokal liegt es bei 10 bis 21 ms. Bei 20 Aufrufen reichen 2 Ausreißer für Rot, bei 60 sind bis zu 3 erlaubt; das ist weiter echtes p95. Ein Überschreiten in mehr als 5 Prozent der Aufrufe bleibt rot, auch wenn es nur zeitweise auftritt (Codex-Review PR #110).
  - Nicht erlaubt: Latenztests überspringen oder die Grenze anheben. 300 ms ist die Zusage an den Telefonpfad.
- **Schreibende Tools** brauchen `idempotency_key`. Gleicher Schlüssel → gleiche Antwort, kein zweiter Vorgang.
- Jeder Aufruf landet mit Dauer und Ergebnis in `calls.tool_calls`.

---

## `get_service_status`
Immer der erste Aufruf. Klärt, ob überhaupt etwas geht.

**Request**
```json
{ "call_id": "…", "tenant_id": "…" }
```
**Response**
```json
{
  "ok": true,
  "data": {
    "is_open": true,
    "closes_at": "2026-09-15T20:00:00Z",
    "pickup_enabled": true,
    "delivery_enabled": true,
    "pickup_wait_minutes": 25,
    "delivery_wait_minutes": 50,
    "sold_out": [{ "number": "23", "name": "Frühlingsrollen" }],
    "call_mode": "primary"
  },
  "say": null
}
```
Geschlossen → `is_open: false` plus `say` mit den nächsten Öffnungszeiten.

---

## `find_customer`
Nur sinnvoll, wenn die Nummer übermittelt wurde.

**Request** `{ "call_id": "…", "tenant_id": "…", "caller_id": "+4972215551234" }`

**Response**
```json
{
  "ok": true,
  "data": {
    "found": true,
    "customer_id": "…",
    "name": "Herr Müller",
    "addresses": [
      { "address_id": "…", "label": "Musterstraße 1, 12345 Musterstadt", "is_default": true }
    ],
    "order_count": 12
  },
  "say": "Wieder an die Rheinstraße 54?"
}
```
Unbekannt → `found: false`, `say: null`. **Nie** raten, welcher Kunde gemeint sein könnte.

---

## `search_menu`
Das wichtigste Tool. Hier entsteht der meiste Fehler-Spielraum, deshalb strenge Regeln.

**Request**
```json
{ "call_id": "…", "tenant_id": "…", "query": "einmal die Nummer dreiundzwanzig", "max_results": 3 }
```
**Response**
```json
{
  "ok": true,
  "data": {
    "match_type": "exact_number",
    "results": [
      {
        "menu_item_id": "…", "number": "23", "name": "Frühlingsrollen (4 Stück)",
        "price_cents": 690, "sold_out": false,
        "option_groups": [
          { "group": "Sauce", "required": false,
            "options": [{ "name": "süßsauer", "price_delta_cents": 0, "default": true },
                        { "name": "Erdnuss", "price_delta_cents": 50, "default": false }] }
        ]
      }
    ]
  },
  "say": null
}
```

**Auflösungsreihenfolge**
1. Zahl im Text → exakter Treffer auf `menu_items.number` → `match_type: "exact_number"`
2. Alias-Tabelle, exakt → `match_type: "alias"`
3. Unscharfe Suche über Name und Alias (Trigram) → nur Treffer über Schwelle
   - genau ein Treffer über der hohen Schwelle → `match_type: "fuzzy_single"`
   - mehrere → `match_type: "ambiguous"`, bis zu 3 Vorschläge, der Agent **muss** nachfragen
   - keiner → `ok: false`, `error.code: "not_found"`

Vor Schritt 2 und 3 fallen Füllwörter des Bestellsatzes weg („einmal … bitte"), damit der Trigram-Vergleich am Gericht hängt und nicht am Satz. Die beiden Schwellen stehen in der Konfiguration (`MENU_FUZZY_THRESHOLD_HIGH`, `MENU_FUZZY_THRESHOLD_LOW`): sie stellen sich erst am echten Gespräch ein.

**Harte Regel:** Der Agent darf nur eine Position übernehmen, die eine `menu_item_id` aus diesem Tool trägt. Bei `ambiguous` wird nachgefragt, nicht gewählt.

---

## `get_item_details`
Für Rückfragen zu Optionen, Extras und Allergenen.

**Request** `{ "call_id": "…", "tenant_id": "…", "menu_item_id": "…" }`

**Response**
```json
{
  "ok": true,
  "data": {
    "menu_item_id": "…", "number": "23", "name": "Frühlingsrollen (4 Stück)",
    "price_cents": 690, "sold_out": false,
    "description": "mit Gemüsefüllung, dazu süßsaure Sauce",
    "allergens": { "known": true, "codes": ["A", "F"], "confirmed_at": "2026-08-01" },
    "option_groups": [ ]
  },
  "say": null
}
```
**Allergene:** Ist `known: false`, lautet `say` wörtlich „Das lasse ich Ihnen vom Team bestätigen." (aus dem Code, `domain/menu/details.py`), und der Agent legt einen Rückruf an. Der Agent formuliert hier **nichts** selbst. `codes` ist dann leer und `confirmed_at` `null`: keine Auskunft, nicht „frei davon". Die Codes kommen in der Reihenfolge der LMIV-Liste, damit jeder Anruf dieselbe Reihenfolge hört.

Inaktive Gerichte liefert weder `search_menu` noch `get_item_details`; ausverkaufte liefern beide, mit `sold_out: true`.

---

## `check_delivery`

**Request**
```json
{ "call_id": "…", "tenant_id": "…",
  "postal_code": "12345", "street": "Musterstraße", "house_number": "1", "city": "Musterstadt" }
```
**Response**
```json
{
  "ok": true,
  "data": { "deliverable": true, "zone_id": "…", "zone_name": "Zone 1",
            "fee_cents": 250, "min_order_cents": 2000, "eta_minutes": 50 },
  "say": null
}
```
Außerhalb → `ok: false`, `error.code: "out_of_zone"`, `say` bietet Abholung an.

---

## `check_slot`

**Request** `{ "call_id": "…", "tenant_id": "…", "reserved_for": "2026-09-20T18:30:00Z", "party_size": 4 }`

**Response**
```json
{
  "ok": true,
  "data": { "available": false,
            "alternatives": ["2026-09-20T18:00:00Z", "2026-09-20T19:30:00Z"] },
  "say": "Um halb sieben ist leider voll. Sechs Uhr oder halb acht ginge."
}
```

---

## `create_reservation`

**Request**
```json
{ "call_id": "…", "tenant_id": "…", "idempotency_key": "…",
  "guest_name": "Müller", "phone": "+49…", "party_size": 4,
  "reserved_for": "2026-09-20T18:00:00Z", "note": "Kinderstuhl" }
```
**Response**
```json
{
  "ok": true,
  "data": {
    "reservation_id": "…", "status": "draft",
    "reserved_for": "2026-09-20T18:00:00Z", "party_size": 4,
    "guest_name": "Müller", "phone": "+4972215551234", "note": "Kinderstuhl",
    "readback": "Ein Tisch für vier Personen am Sonntag, den 20. September um acht Uhr, auf den Namen Müller, mit dem Hinweis: Kinderstuhl. Passt das so?"
  },
  "say": null
}
```
Legt die Reservierung als `draft` an und gibt `readback` zurück — den Satz, den der Agent vorliest. Erst `confirm` macht sie gültig.

**Prüfungen im Code, nicht im Modell:** Anruf bekannt (`call_id` muss in `calls` stehen, sonst `not_found`) · Rufnummer nach E.164 normalisierbar (sonst `invalid_input` mit `say`) · Zeitpunkt in der Zukunft · Slot frei nach denselben Regeln wie `check_slot` (sonst `conflict`, `say` nennt die Alternativen). Der Entwurf zählt sofort gegen die Kapazität und landet im `audit_log`. Gleicher `idempotency_key` → dieselbe Antwort ohne neue Prüfung; Schlüssel eines anderen Mandanten → `conflict`.

---

## `draft_order`
Rechnet und prüft. Die einzige Stelle, an der eine Summe entsteht.

**Request**
```json
{ "call_id": "…", "tenant_id": "…", "type": "delivery",
  "customer": { "name": "Müller", "phone": "+49…", "address_id": "…" },
  "items": [
    { "menu_item_id": "…", "quantity": 2,
      "options": [{ "group": "Sauce", "name": "Erdnuss" }], "note": "ohne Zwiebeln" }
  ] }
```
**Response**
```json
{
  "ok": true,
  "data": {
    "order_id": "…", "status": "draft",
    "items_total_cents": 1480, "delivery_fee_cents": 250, "total_cents": 1730,
    "ready_at": "2026-09-15T18:50:00Z",
    "warnings": [],
    "readback": "Zweimal Nummer 23 Frühlingsrollen mit Erdnusssauce, ohne Zwiebeln. Macht 17,30 Euro, Lieferung in etwa 50 Minuten an die Rheinstraße 54. Passt das so?"
  },
  "say": null
}
```

**Prüfungen im Code, nicht im Modell:** Öffnungszeit · Position aktiv und nicht ausverkauft · Optionen gültig · Pflichtgruppen gewählt · Zone auflösbar · Mindestbestellwert erreicht · Summe korrekt · Lieferzeit aus aktueller Wartezeit.
Verstoß → `ok: false` mit passendem Code und `say`.

---

## `confirm`
Der einzige Übergang von `draft` nach `confirmed`.

**Request** `{ "call_id": "…", "tenant_id": "…", "entity": "order", "entity_id": "…", "idempotency_key": "…" }`

**Response** `{ "ok": true, "data": { "status": "confirmed", "handover": "queued", "pickup_code": "A17" }, "say": null }`

**Wirkung:** Status setzen, `audit_log` schreiben, Ereignis an n8n legen, GUI aktualisieren. Im Modus `overflow` wird `approved` erst durch das Team gesetzt; die Küche startet dann später.

**Idempotenz trägt der Zustand, nicht der Schlüssel:** ein zweiter Aufruf auf denselben Vorgang liest `confirmed` und antwortet gleich, ohne ein zweites Ereignis anzulegen — auch mit einem anderen `idempotency_key`. Der Schlüssel landet im `audit_log`. Ein stornierter Vorgang ergibt `conflict`, ein unbekannter oder fremder `not_found`. `entity: "order"` steht im Vertrag, antwortet aber bis Stufe 2 mit `not_found`; `pickup_code` bleibt bei Reservierungen `null`.

---

## `create_callback`

**Request**
```json
{ "call_id": "…", "tenant_id": "…", "phone": "+49…",
  "reason": "not_understood", "summary": "Möchte eine große Bestellung für Samstag, Leitung war schlecht." }
```
Legt die Aufgabe an, GUI meldet sie mit Ton. `reason`: `complaint` · `not_understood` · `human_requested` · `out_of_scope`

**Response**
```json
{ "ok": true,
  "data": { "callback_id": "…", "status": "open", "phone": "+497215551234",
            "reason": "not_understood", "summary": "…" },
  "say": "Ich habe Ihre Nummer notiert. Das Restaurant ruft Sie so bald wie möglich zurück." }
```

**Wirkung:** Aufgabe mit `status: open` anlegen, `audit_log` schreiben (`callback.created`), Ereignis `callback.created` in die Outbox legen.

**Idempotenz trägt der Zustand, nicht der Schlüssel:** je Anruf gibt es höchstens **einen offenen** Rückruf. Prüfen und Anlegen sind je Anruf durch eine Advisory-Sperre serialisiert (`pg_advisory_xact_lock`), sonst finden zwei gleichzeitige Erstaufrufe beide nichts und legen beide an. Zum Vertrag gehören drei Punkte: jeder schreibende Pfad auf `callbacks` nimmt dieselbe Sperre; die Datenbank fährt `READ COMMITTED` (in `api/db.py` festgelegt, nicht dem Serverdefault überlassen), damit der Wartende den fremden Commit auch sieht; und ein zweiter Aufruf **aktualisiert den bestehenden Rückruf nicht** — abweichende `summary`, `phone` oder `reason` werden verworfen, die Antwort trägt die Werte des ersten Aufrufs. Ist der erste Rückruf `done`, entsteht ein neuer. Ein zweiter Aufruf im selben Anruf liefert den bestehenden zurück, ohne zweite Aufgabe und ohne zweites Ereignis — sonst bekommt das Team nach einem Zeitüberlauf der Plattform zwei Zettel für denselben Gast. Ist der erste Rückruf erledigt (`done`), entsteht wieder ein neuer.

**Fehler:** unbekannter Anruf oder fremder Mandant → `not_found` mit Störungssatz · unbrauchbare Rufnummer → `invalid_input` mit Nachfrage-Satz · leeres `summary` → `invalid_input` · unbekannter `reason` → `invalid_input`.

Die Rufnummer wird nach E.164 normalisiert. Eine geklammerte `(0)` hinter der Landesvorwahl entfällt dabei (`+49 (0)7221 5551234` → `+4972215551234`).

---

## `transfer_to_team`

**Request** `{ "call_id": "…", "tenant_id": "…", "reason": "complaint" }`

**Response** `{ "ok": true, "data": { "transfer_to": "+49…", "available": true }, "say": null }`

Ist niemand erreichbar → `available: false`, der Agent legt stattdessen einen Rückruf an. **Schleifenschutz:** Ziel ist immer die Durchwahl, nie die Hauptnummer, und `transfer_to_team` wird je Anruf höchstens einmal ausgeführt.

---

## Weitere Endpunkte (nicht vom Agenten aufgerufen)

| Endpunkt | Zweck |
|---|---|
| `GET /health` | Liveness für Monitoring und Compose |
| `POST /v1/calls/start` · `/end` | Anruf-Log von der Plattform |
| `GET /v1/gui/...` | Daten für die Oberfläche |
| `POST /v1/gui/orders/{id}/approve` | Freigabe im Überlauf-Betrieb |
| `POST /internal/events/replay` | hängengebliebene Übergaben erneut senden |
