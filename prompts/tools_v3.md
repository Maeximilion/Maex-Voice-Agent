# Tool-Beschreibungen v3 – Stand nach T-6.5 (Lieferung)

> Fürs Function-Calling des Modells und jeden Client über HTTP. Vollständiger Vertrag
> inklusive Fehlercodes: `docs/04_API_TOOLS.md`. Diese Datei ist die knappe Fassung je Tool,
> wie sie das Modell zum Aufrufen braucht.
>
> Basis: `POST /v1/tools/<name>`, Auth `Authorization: Bearer <AGENT_API_TOKEN>`. Jeder Request
> trägt `call_id` und `tenant_id`, jede Antwort `{ ok, data, say }` bzw. `{ ok: false, error, say }`.

## get_service_status
**Wann:** vor einer Bestellung, wenn `service` im Zustand fehlt; `service` sagt nur, ob offen ist und ob Abholung und Lieferung angenommen werden. Fragt der Gast, bis wann offen ist oder wie lange es dauert: erneut aufrufen. Für eine Reservierung nicht nötig: `check_slot` kennt die Öffnung.
**Eingabe:** keine Felder.
**Liefert:** ob offen, bis wann, Wartezeiten, Betriebsmodus.

## check_slot
**Wann:** bevor eine Reservierung angelegt wird, und erneut nach jedem neuen Terminwunsch.
**Eingabe:** `reserved_for` (Datum/Uhrzeit), `party_size`.
**Liefert:** `available`; bei „nein" bis zu zwei `alternatives` aus demselben Service und einen `say`. Ohne `alternatives` den `say` vorlesen: außerhalb der Öffnung fragt er nach einer anderen Uhrzeit, kein `create_callback`.

## create_reservation
**Wann:** sobald Datum, Uhrzeit, Personenzahl, Name und Rufnummer vorliegen und `check_slot` frei meldet. Nach jeder Änderung erneut `check_slot`, dann `create_reservation` — nie `draft_order`.
**Eingabe:** `guest_name`, `phone`, `party_size`, `reserved_for`, optional `note` (ohne Allergie).
**Liefert:** `reservation_id`, Status `draft`, `readback` — der Satz, der vorgelesen wird.

## search_menu
**Wann:** sofort, wenn der Gast ein Gericht oder eine Nummer nennt, mit seinem ganzen Satz: bevor du etwas dazu sagst, nachfragst oder es in eine Bestellung kommt.
**Eingabe:** `query` (das Gesagte, so wie es kam).
**Liefert:** `match_type` (`exact_number` · `alias` · `fuzzy_single` · `ambiguous`) und `results` mit `menu_item_id`, Nummer, Name, Preis, `sold_out`, `option_groups`. Nennt der Satz einen Wunsch („ohne Karotten", „mit Nudeln statt Reis"), dazu `wish`: `kind` `note` (Weglassen), `option` (steht auf der Karte, mit `group`, `option`, `price_delta_cents`, `reason`), `allergy` (notiert der Kern, keine Zusage), `unknown` (nicht anbieten, `say` sprechen) oder `open` (nachfragen: Gericht wählen lassen oder, mit `groups`, die Gruppe). Ein Weglassen zur Zugabe steht in `wish.note` und gehört ebenfalls in `note`. Fragt `say` „Wogegen …?", liest der Kern die Antwort; bei mehreren Gerichten eines nach dem anderen.
**Mehrere Gerichte in einem Satz:** das Tool zerlegt selbst und liefert `match_type: "positions"` mit einem Eintrag je Teil (`query`, `ok`, Ergebnis oder `error_code` und `say`).
**Regel:** bei `ambiguous` nachfragen, nie selbst wählen. `not_found`: `say` sprechen. Das `say` eines eindeutigen Treffers wiederholt das Verstandene: sprich es, dann weiter.

## get_item_details
**Wann:** bei Fragen zu Optionen, Beschreibung oder Allergenen eines gefundenen Gerichts.
**Eingabe:** `menu_item_id`, `allergen_question` (Pflicht, `true` nur bei einer Allergenfrage).
**Liefert:** Beschreibung, `option_groups` (je Option `reason`: warum sie mehr kostet, aus der Karte; leer: Preis steht so in der Karte), `allergens` (`known: false`: keine Auskunft, nicht „frei davon"). Allergene nur aus `say`.

## find_customer
**Wann:** bei einer Lieferung, wenn `customer` im Zustand fehlt und `phone` in `slots` steht. War `found` false und nennt der Gast danach seinen Namen, noch einmal.
**Eingabe:** optional `guest_name`, nur wie der Gast ihn gesagt hat. Die Nummer kennt der Kern.
**Liefert:** nur `found`, Ids und in `say` die Frage nach der Adresse.

## check_delivery
**Wann:** sobald der Gast eine Postleitzahl nennt, auch auf die Frage, ob wir dorthin liefern.
**Eingabe:** `postal_code`, dazu `street`, `house_number`, `city`, wenn genannt.
**Liefert:** `zone_name`, `fee_cents`, `min_order_cents`, `eta_minutes`. `out_of_zone`, `invalid_input`: `say` sprechen.

## draft_order
**Wann:** sobald alle Gerichte gefunden, Pflichtoptionen gewählt und Name und Rufnummer bekannt sind, bei einer Lieferung auch die Adresse. Nach jeder Änderung erneut.
**Eingabe:** `type` (`"pickup"` oder `"delivery"`), `customer` (`name`, `phone`; bei `delivery` dazu `address` mit `street`, `house_number`, `postal_code`, `city`, optional `floor_note`, oder `address_id` aus `customer` im Zustand, dann `name` nur, wenn der Gast ihn genannt hat), `items` (je `menu_item_id`, `quantity`, optional `options` als `{group, name}` und `note`), optional `note` für die ganze Bestellung (ohne Allergie).
**Liefert:** `order_id`, Summe, Liefergebühr, `ready_at`, `readback` — der Satz, der vorgelesen wird. `below_minimum`: `say` nennt den Fehlbetrag. `closed`: `say` sprechen.

## confirm
**Wann:** nachdem der Gast das `readback` von `create_reservation` oder `draft_order` mit Ja bestätigt hat.
**Eingabe:** `entity` (`"reservation"` oder `"order"`), `entity_id`.
**Liefert:** `status: "confirmed"`; bei Bestellungen `pickup_code` und `handover` (`queued` oder `awaiting_approval`).

## create_callback
**Wann:** Verständnis-Leiter Stufe 6, `check_slot` findet nichts frei (nicht bei „geschlossen"), oder ein Anliegen liegt außerhalb dessen, was das System heute kann.
**Eingabe:** `phone`, `reason` (`complaint` · `not_understood` · `human_requested` · `out_of_scope`), `summary` (ein Satz).
**Liefert:** `callback_id`, Status `open`.

## transfer_to_team
**Wann:** sofort bei Beschwerde, Wunsch nach einem Menschen oder Storno — ohne nachzufragen.
**Eingabe:** `reason` (`complaint` · `not_understood` · `human_requested` · `out_of_scope` · `cancellation`).
**Liefert:** `transfer_to` (Durchwahl), `available`. Ist `available: false`, stattdessen `create_callback` aufrufen.
