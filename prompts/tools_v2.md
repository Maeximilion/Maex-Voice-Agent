# Tool-Beschreibungen v2 – Stand nach T-4.5 und confirm für Bestellungen

> Für die Sprach-Plattform (D1 offen) und fürs Function-Calling des Modells. Vollständiger
> Vertrag inklusive Fehlercodes: `docs/04_API_TOOLS.md`. Diese Datei ist die knappe Fassung
> je Tool, wie sie das Modell zum Aufrufen braucht.
>
> Basis: `POST /v1/tools/<name>`, Auth `Authorization: Bearer <AGENT_API_TOKEN>`. Jeder Request
> trägt `call_id` und `tenant_id`, jede Antwort `{ ok, data, say }` bzw. `{ ok: false, error, say }`.

## get_service_status
**Wann:** vor einer Bestellung, wenn `service` im Zustand fehlt; `service` sagt nur, ob offen ist und ob Abholung angenommen wird. Fragt der Gast, bis wann offen ist oder wie lange es dauert: erneut aufrufen. Für eine Reservierung nicht nötig: `check_slot` kennt die Öffnung.
**Eingabe:** keine Felder.
**Liefert:** ob offen, bis wann, Wartezeiten, Betriebsmodus.

## check_slot
**Wann:** bevor eine Reservierung angelegt wird, und erneut nach jedem neuen Terminwunsch.
**Eingabe:** `reserved_for` (Datum/Uhrzeit), `party_size`.
**Liefert:** `available`; bei „nein" bis zu zwei `alternatives` aus demselben Service und einen `say`. Ohne `alternatives` den `say` vorlesen: außerhalb der Öffnung fragt er nach einer anderen Uhrzeit, kein `create_callback`.

## create_reservation
**Wann:** sobald Datum, Uhrzeit, Personenzahl, Name und Rufnummer vorliegen und `check_slot` frei meldet. Nach jeder Änderung erneut `check_slot`, dann `create_reservation` — nie `draft_order`.
**Eingabe:** `idempotency_key`, `guest_name`, `phone`, `party_size`, `reserved_for`, optional `note` (nie eine Allergie: die schreibt der Kern selbst).
**Liefert:** `reservation_id`, Status `draft`, `readback` — der Satz, der vorgelesen wird.
**Schreibend:** braucht `idempotency_key`.

## search_menu
**Wann:** sofort, wenn der Gast ein Gericht oder eine Nummer nennt, mit seinem ganzen Satz: bevor du etwas dazu sagst, nachfragst oder es in eine Bestellung kommt.
**Eingabe:** `query` (das Gesagte, so wie es kam).
**Liefert:** `match_type` (`exact_number` · `alias` · `fuzzy_single` · `ambiguous`) und `results` mit `menu_item_id`, Nummer, Name, Preis, `sold_out`, `option_groups`. Nennt der Satz einen Wunsch („ohne Karotten", „mit Nudeln statt Reis"), dazu `wish`: `kind` `note` (Weglassen), `option` (steht auf der Karte, mit `group`, `option`, `price_delta_cents`, `reason`), `allergy` (die Allergie des Gastes: der Kern notiert sie selbst, nie in `note`; keine Zusage), `unknown` (nicht anbieten, `say` sprechen) oder `open` (nachfragen: Gericht wählen lassen oder, mit `groups`, die Gruppe). Ein Weglassen zur Zugabe steht in `wish.note` und gehört ebenfalls in `note`. Fragt `say` nach einer Allergie („Wogegen …?"), sprich die Frage; die Antwort liest der Kern selbst. Bei mehreren Gerichten ohne Zutat eines nach dem anderen.
**Mehrere Gerichte in einem Satz:** über HTTP `ok: false`, `error.code: "ambiguous"`, `say` lässt den Gast warten („der Reihe nach"), `message` nennt die Teile — dann je Teil neu fragen. Nur im eigenen Gesprächskern (`agent/dispatch.py`) zerlegt das Tool selbst und liefert `match_type: "positions"` mit einem Eintrag je Teil (`query`, `ok`, Ergebnis oder `error_code` und `say`).
**Regel:** bei `ambiguous` nachfragen, nie selbst wählen. `not_found`: `say` sprechen. Im Gesprächskern wiederholt das `say` eines eindeutigen Treffers, was verstanden wurde (Nummer oder Name der Karte) — sprich es, dann weiter.

## get_item_details
**Wann:** bei Fragen zu Optionen, Beschreibung oder Allergenen eines gefundenen Gerichts.
**Eingabe:** `menu_item_id`, `allergen_question` (Pflicht, `true` nur bei einer Allergenfrage).
**Liefert:** Beschreibung, `option_groups` (je Option `reason`: warum sie mehr kostet, aus der Karte; leer heißt, der Preis steht so in der Karte), `allergens` (`known: false` heißt: keine Auskunft, nicht „frei davon").

## draft_order
**Wann:** sobald alle Gerichte gefunden, Pflichtoptionen gewählt und Name und Rufnummer bekannt sind. Nach jeder Änderung erneut.
**Eingabe:** `idempotency_key`, `type: "pickup"`, `customer` (`name`, `phone`), `items` (je `menu_item_id`, `quantity`, optional `options` als `{group, name}` und `note`), optional `note` für die ganze Bestellung. Eine Allergie steht in keinem `note`: der Kern schreibt sie selbst auf die Position oder die ganze Bestellung.
**Liefert:** `order_id`, Summe, `ready_at`, `readback` — der Satz, der vorgelesen wird.
**Schreibend:** über HTTP ist `idempotency_key` Pflicht — gleicher Schlüssel, gleiche Antwort. Im eigenen Gesprächskern bildet der Code ihn aus den Angaben des Anrufs, das Modell lässt ihn weg.

## confirm
**Wann:** nachdem der Gast das `readback` von `create_reservation` oder `draft_order` mit Ja bestätigt hat.
**Eingabe:** `entity` (`"reservation"` oder `"order"`), `entity_id`, `idempotency_key`.
**Liefert:** `status: "confirmed"`; bei Bestellungen `pickup_code` und `handover` (`queued` oder `awaiting_approval`).
**Schreibend:** braucht `idempotency_key`. Ein zweiter Aufruf auf denselben Vorgang ist unschädlich.

## create_callback
**Wann:** Verständnis-Leiter Stufe 6, `check_slot` findet nichts frei (nicht bei „geschlossen"), eine Allergenauskunft ist nicht gepflegt, oder ein Anliegen liegt außerhalb dessen, was das System heute kann (Lieferung).
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
