# Tool-Beschreibungen v1 – Stand nach T-1.9

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

## confirm
**Wann:** nachdem der Gast das `readback` von `create_reservation` mit Ja bestätigt hat.
**Eingabe:** `entity: "reservation"`, `entity_id`, `idempotency_key`.
**Liefert:** `status: "confirmed"`.
**Schreibend:** braucht `idempotency_key`. Ein zweiter Aufruf auf denselben Vorgang ist unschädlich.

## create_callback
**Wann:** Verständnis-Leiter Stufe 6, `check_slot` findet nichts, oder ein Anliegen liegt außerhalb dessen, was das System heute kann (Abholung, Lieferung, Speisekarte, Allergien).
**Eingabe:** `phone`, `reason` (`complaint` · `not_understood` · `human_requested` · `out_of_scope`), `summary` (ein Satz).
**Liefert:** `callback_id`, Status `open`.
**Schreibend:** kein `idempotency_key` nötig, ein Anruf hat höchstens einen offenen Rückruf.

## transfer_to_team
**Wann:** sofort bei Beschwerde, Wunsch nach einem Menschen oder Storno — ohne nachzufragen.
**Eingabe:** `reason` (`complaint` · `not_understood` · `human_requested` · `out_of_scope` · `cancellation`).
**Liefert:** `transfer_to` (Durchwahl), `available`. Ist `available: false`, stattdessen `create_callback` aufrufen.
**Schleifenschutz:** höchstens einmal je Anruf; ein zweiter Aufruf liefert dasselbe Ergebnis zurück.

---

**Noch nicht verfügbar (Stufe 2/3):** `find_customer`, `search_menu`, `get_item_details`,
`check_delivery`, `draft_order`. Kommen mit den zugehörigen Aufgaben (Block 3/4) dazu — dann
auch eine neue Prompt-Version.
