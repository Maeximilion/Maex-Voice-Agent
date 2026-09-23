# Tool-Beschreibungen v2 – Stand nach T-4.5 und confirm für Bestellungen

> Für die Sprach-Plattform (D1 offen) und fürs Function-Calling des Modells. Vollständiger
> Vertrag inklusive Fehlercodes: `docs/04_API_TOOLS.md`. Diese Datei ist die knappe Fassung
> je Tool, wie sie das Modell zum Aufrufen braucht.
>
> Basis: `POST /v1/tools/<name>`, Auth `Authorization: Bearer <AGENT_API_TOKEN>`. Jeder Request
> trägt `call_id` und `tenant_id`, jede Antwort `{ ok, data, say }` bzw. `{ ok: false, error, say }`.

## get_service_status
**Wann:** immer als erster Aufruf im Gespräch.
**Eingabe:** `call_id`, `tenant_id` — keine weiteren Felder.
**Liefert:** ob offen, bis wann, Wartezeiten, Betriebsmodus.

## check_slot
**Wann:** bevor eine Reservierung angelegt wird, und erneut nach jedem neuen Terminwunsch.
**Eingabe:** `reserved_for` (Datum/Uhrzeit), `party_size`.
**Liefert:** `available`; bei „nein" bis zu zwei `alternatives`.

## create_reservation
**Wann:** sobald Datum, Uhrzeit, Personenzahl, Name und Rufnummer vorliegen und `check_slot` frei meldet.
**Eingabe:** `idempotency_key`, `guest_name`, `phone`, `party_size`, `reserved_for`, optional `note`.
**Liefert:** `reservation_id`, Status `draft`, `readback` — der Satz, der vorgelesen wird.
**Schreibend:** braucht `idempotency_key`.

## search_menu
**Wann:** für jedes Gericht, das der Gast nennt, bevor es in eine Bestellung kommt.
**Eingabe:** `query` (das Gesagte, so wie es kam), optional `max_results`.
**Liefert:** `match_type` (`exact_number` · `alias` · `fuzzy_single` · `ambiguous`) und `results` mit `menu_item_id`, Nummer, Name, Preis, `sold_out`, `option_groups`.
**Mehrere Gerichte in einem Satz:** über HTTP `ok: false`, `error.code: "ambiguous"`, `say` bittet um eins nach dem anderen, `message` nennt die Teile — dann je Teil neu fragen. Nur im eigenen Gesprächskern (`agent/dispatch.py`) zerlegt das Tool selbst und liefert `match_type: "positions"` mit einem Eintrag je Teil (`query`, `ok`, Ergebnis oder `error_code` und `say`).
**Regel:** bei `ambiguous` nachfragen, nie selbst wählen. `not_found`: `say` sprechen.

## get_item_details
**Wann:** bei Fragen zu Optionen, Beschreibung oder Allergenen eines gefundenen Gerichts.
**Eingabe:** `menu_item_id`, `allergen_question` (Pflicht, `true` nur bei einer Allergenfrage).
**Liefert:** Beschreibung, `option_groups`, `allergens` (`known: false` heißt: keine Auskunft, nicht „frei davon").

## draft_order
**Wann:** sobald alle Gerichte gefunden, Pflichtoptionen gewählt und Name und Rufnummer bekannt sind. Nach jeder Änderung erneut.
**Eingabe:** `idempotency_key`, `type: "pickup"`, `customer` (`name`, `phone`), `items` (je `menu_item_id`, `quantity`, optional `options` als `{group, name}` und `note`).
**Liefert:** `order_id`, Summe, `ready_at`, `readback` — der Satz, der vorgelesen wird.
**Schreibend:** über HTTP ist `idempotency_key` Pflicht — gleicher Schlüssel, gleiche Antwort. Im eigenen Gesprächskern bildet der Code ihn aus den Angaben des Anrufs, das Modell lässt ihn weg.

## confirm
**Wann:** nachdem der Gast das `readback` von `create_reservation` oder `draft_order` mit Ja bestätigt hat.
**Eingabe:** `entity` (`"reservation"` oder `"order"`), `entity_id`, `idempotency_key`.
**Liefert:** `status: "confirmed"`; bei Bestellungen `pickup_code` und `handover` (`queued` oder `awaiting_approval`).
**Schreibend:** braucht `idempotency_key`. Ein zweiter Aufruf auf denselben Vorgang ist unschädlich.

## create_callback
**Wann:** Verständnis-Leiter Stufe 6, `check_slot` findet nichts, eine Allergenauskunft ist nicht gepflegt, oder ein Anliegen liegt außerhalb dessen, was das System heute kann (Lieferung).
**Eingabe:** `phone`, `reason` (`complaint` · `not_understood` · `human_requested` · `out_of_scope`), `summary` (ein Satz).
**Liefert:** `callback_id`, Status `open`.
**Schreibend:** kein `idempotency_key` nötig, ein Anruf hat höchstens einen offenen Rückruf.

## transfer_to_team
**Wann:** sofort bei Beschwerde, Wunsch nach einem Menschen oder Storno — ohne nachzufragen.
**Eingabe:** `reason` (`complaint` · `not_understood` · `human_requested` · `out_of_scope` · `cancellation`).
**Liefert:** `transfer_to` (Durchwahl), `available`. Ist `available: false`, stattdessen `create_callback` aufrufen.
**Schleifenschutz:** höchstens einmal je Anruf; ein zweiter Aufruf liefert dasselbe Ergebnis zurück.

---

**Noch nicht verfügbar (Stufe 3):** `find_customer`, `check_delivery`, Lieferung in
`draft_order`. Kommen mit den zugehörigen Aufgaben (Block 4) dazu — dann auch eine neue
Prompt-Version.
