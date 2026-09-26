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
- **Schreibende Tools** brauchen `idempotency_key`. Gleicher Schlüssel → gleiche Antwort, kein zweiter Vorgang. Nur im selben Anruf: gehört der Schlüssel zu einem Vorgang eines anderen Anrufs, ist das `conflict`, nie dessen Antwort. Im eigenen Gesprächskern bildet der Code den Schlüssel immer selbst, einen Schlüssel vom Modell gibt es nicht - aus der **geprüften** Anfrage mit allen gespeicherten Feldern: eine geänderte Notiz ist ein neuer Entwurf, `options: []` und ein weggelassenes Feld sind derselbe (T-4.10).
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
1. Zahl im Text → exakter Treffer auf `menu_items.number` → `match_type: "exact_number"`.
   Eine Zahl wird nur dann direkt als Kartennummer genommen, wenn der ganze Satz
   genau diese eine Nummer ist (Regel A): Marker („Nummer", „Nr."), Füllwörter,
   Zögerlaute und **eine** Menge dürfen daneben stehen, sonst nichts.
   „Einmal die Nummer 23 bitte" und „zweimal die 23" sind damit `exact_number`,
   ebenso „Hallo, ich würde gern die 13" und „dazu die 24". Ein „und" vor der
   ersten Zahl setzt die Bestellung fort („Und noch die 24"), erst zwischen zwei
   Zahlen („die 23 und die 24") macht es den Satz mehrdeutig.
   Steht **Inhalt** daneben, trennen sich zwei Fälle: mit Marker („die Nummer 23
   und einmal Pho Bo") ist es `ambiguous` mit der Frage nach der einen Nummer.
   Ein Wunsch daneben („Nummer 23 mit Erdnusssauce", „die 23 ohne Karotten")
   wird vorher abgetrennt, siehe **Wünsche zur Position** unten. **Ohne** Marker ist es gar kein Nummernsatz, und
   die Namenssuche liefe über den ganzen Satz und fände nur Pho Bo. Deshalb
   prüft `search_menu` vorher mit `split_positions`, ob der Satz mehrere
   Positionen nennt: dann `ok: false`, `error.code: "ambiguous"`, `say` („Einen
   Moment, ich nehme das der Reihe nach auf.") lässt den Gast warten, statt ihn
   um eine Wiederholung zu bitten, `message` nennt die Teile („mehrere
   Positionen: die 23 | einmal Pho Bo"), der Aufrufer fragt je Teil. Keine
   Position fällt mehr still weg. Der eigene
   Gesprächskern (`agent/dispatch.py`) fragt nicht nach, sondern zerlegt selbst
   und sucht je Teil: Antwort `match_type: "positions"` mit einem Eintrag je Teil
   (`query`, `ok`, Treffer oder `error_code` und `say`). Trennt der Satz allein
   nicht („die 23 und Pho Bo": „Pho Bo" ohne Menge), prüfen beide Wege mit der
   Karte (`position_parts` in `domain/menu/search.py`): trifft jedes Stück an
   den Trennern für sich eindeutig ein anderes Gericht, sind es mehrere
   Positionen - über HTTP die Rückfrage, im Gesprächskern die Suche je Teil;
   sonst war das „und" Teil eines Namens. Steht der ganze Satz selbst auf der
   Karte („Fisch und Chips" als Alias oder als Name, auch mit Menge und
   Füllwort: „einmal Fisch und Chips, bitte"), bleibt er ein Gericht, auch wenn
   die Stücke einzeln treffen. Ein Stück mit „mit", „ohne", „extra" vorn ist ein
   Hinweis zur Position davor („die 23, mit Reis"), nie eine eigene. Dasselbe
   Gericht mit denselben Worten zweimal („Pho Bo und Pho Bo") sind zwei
   Positionen; mit anderen Worten („Pho und Pho Bo") bleibt der Satz ganz.
   Hängt der ganze Satz als Alias an mehreren Gerichten, fragt die Suche nach.
   Dasselbe gilt für benachbarte Stücke mitten in einer Aufzählung: „Fisch und
   Chips und Pho Bo" sind zwei Positionen, geprüft von links, das längste Stück
   zuerst. Eine Spanne ist nie länger als der längste Name oder Alias der Karte
   (in Stücken an den Trennern): ohne Namen mit „und" gibt es keine
   Spannenprüfung, eine lange Aufzählung bleibt im Latenzbudget.
   **Ein Satz, eine Position:** wer mehrere Positionen in einem Satz aufnehmen
   will, zerlegt ihn **vor** der Suche und fragt `search_menu` je Position. Die
   Zerlegung gehört zum Bestellfluss, nicht in `search_menu`: sie steht in
   `domain/menu/split.py` (`split_positions`, T-4.5). Getrennt wird an „und",
   „sowie" und Komma, aber nie bei Korrektur oder Alternative („23, nein 24",
   „23 oder 24") und nie, wenn ein Teil nur Zögerlaut ist („23, äh, 24") -
   dann bleibt der Satz ganz und `search_menu` fragt laut nach. Jeder
   Teil muss mit einer eigenen Position beginnen (Nummer, Menge oder „ein/eine";
   „Schärfe 2" beginnt keine) und
   darf nach Menge und Nummer nicht mit „ohne", „mit" oder „extra" weitergehen
   („2 x ohne Koriander"); „Ente süß und sauer" und
   „Nummer 23, ohne Zwiebeln" bleiben so ein Satz. Ein solcher Teil hängt
   wieder an dem davor, die übrigen Grenzen bleiben: „die 23 und eine Ente süß
   und sauer" ergibt zwei Positionen. Nach „hundert" hängt „und" nur eine Zahl
   an („hundert und eins"), keine neue Position („die hundert und eine Cola").
   Einleitende Wörter („dazu", „außerdem") vor einer Position stören nicht.
2. Alias-Tabelle, exakt → `match_type: "alias"`
3. Unscharfe Suche über Name und Alias (Trigram) → nur Treffer über Schwelle
   - genau ein Treffer über der hohen Schwelle → `match_type: "fuzzy_single"`
   - mehrere → `match_type: "ambiguous"`, bis zu 3 Vorschläge, der Agent **muss** nachfragen
   - keiner → `ok: false`, `error.code: "not_found"`

**Heute aus (T-4.8).** Ein Treffer, den das Team am Tablet auf „Gericht aus" gestellt hat, kommt mit `sold_out: true` und einem `say`: „Frühlingsrollen ist heute leider aus. Stattdessen hätte ich Nummer 24 Sommerrollen." Die Alternativen sind bis zu zwei aktive, nicht ausverkaufte Gerichte derselben Kategorie, die nächsten Nummern nach dem ausverkauften, dann von vorn; gibt es keine, bleibt nur der erste Satz. Nie etwas, das nicht auf der Karte steht (D8). `get_item_details` und `draft_order` nennen weiter nur „heute aus".

**Formen von „nicht eindeutig".** Sie unterscheiden sich darin, ob es etwas
vorzuschlagen gibt:

| Lage | Antwort |
|---|---|
| Mehrere Gerichte passen (Alias oder Trigram) | `ok: true`, `match_type: "ambiguous"`, bis zu 3 Vorschläge in `results` |
| Der Satz nennt keine eine Nummer („23 oder 24", „Nummer 23, nein", „Nummer 47, die Ente") | `ok: false`, `error.code: "ambiguous"`, kein `results`, `say` fragt nach der einen Nummer |
| Der Satz nennt mehrere Positionen ohne Marker („die 23 und einmal Pho Bo") | `ok: false`, `error.code: "ambiguous"`, kein `results`, `say` „Einen Moment, ich nehme das der Reihe nach auf.", `message` nennt die Teile |

**Wiederholen, was verstanden wurde (nur Gesprächskern, Maxi PR #127):** jeder
eindeutige Treffer (`exact_number`, `alias`, `fuzzy_single`, nicht ausverkauft)
kommt im Agenten mit einem `say`, das ihn sofort wiederholt, bei `positions`
alle eindeutigen Teile in einem Satz. Hat der Gast eine Nummer genannt, nur die
Nummer („Gern, Nummer 23 und Nummer 13."); hat er das Gericht beschrieben, der
Name der Karte mit Nummer („Alles klar, Nummer 48 Ente süß-sauer.") - nicht das
Gesagte, sondern das, was das System daraus gemacht hat, damit ein falscher
Treffer sofort auffällt. Ohne Menge, die kommt mit dem `readback`. Die
Einleitung wechselt, gewählt aus dem Gesagten, nicht zufällig: ein Replay sagt
dasselbe. Unklare Teile behalten ihr eigenes `say` und werden nacheinander
gefragt. Über HTTP bleibt `say` bei eindeutigen Treffern leer.

Die zweite Form hat bewusst keine Vorschläge: welche Gerichte gemeint sein
könnten, ist nicht entscheidbar, solange die Nummer nicht feststeht. Der Agent
liest `say` vor und fragt nach. Eine Nummer, die es nicht gibt, bleibt
`not_found` — die Suche weicht nie auf ähnliche Namen aus.

**Wünsche zur Position (T-4.10, Entscheidung D8):** steht ein Wunsch im Satz
(„die 23 ohne Karotten", „die knusprige Ente mit Nudeln statt Reis"), trennt
`domain/menu/wishes.py` ihn vom Gericht; gesucht wird das Gericht, der Wunsch
kommt als `wish` mit:

| `wish.kind` | Bedeutung | Was der Agent tut |
|---|---|---|
| `note` | Weglassen („ohne", „kein") | als `note` der Position in `draft_order`, ohne Preis |
| `option` | steht als Option des Gerichts auf der Karte; `group`, `option`, `price_delta_cents` und `reason` aus `item_options` | in `options` von `draft_order`; der Aufpreis wird sofort mit wiederholt |
| `allergy` | eine eigene Allergie oder Unverträglichkeit („ich vertrage keine Erdnüsse", „Erdnussallergie", „Nuss- und Sesamallergie", „Laktoseintoleranz"); jede genannte Zutat kommt in den Hinweis | `text` ist der Küchenhinweis im festen Wortlaut „WICHTIG: Keine <Zutat>. Grund: Allergie" (E14), `ingredient` die Zutat; als `note` in `draft_order`, im `readback` wiederholt; `say` ohne Zusage, dass das Gericht frei davon ist. Ohne erkennbare Zutat fragt `say`, wogegen; „weiß ich nicht" oder „nein" ist keine Zutat, die Frage bleibt offen. Mehrere Gerichte ohne Zutat in einem Satz: `say` fragt nach dem ersten und nennt es („Wogegen sind Sie bei Pho Bo allergisch?"), die anderen folgen einzeln |
| `unknown` | steht nicht auf der Karte | nicht anbieten: `say` („Den Wunsch … kann ich leider nicht anbieten"), das Gericht bleibt wie auf der Karte |
| `open` | bei mehreren Treffern, oder die Option steht in zwei Gruppen (`groups`, „Reis" als Beilage und als Extra) | nachfragen: erst das Gericht wählen lassen, bzw. `say` fragt nach der Gruppe |

Weglassen zusammen mit einer Zugabe („ohne Zwiebeln, dafür mit Nudeln") wird nie
zur freien Notiz: die Zugabe ist `option` oder `unknown`, das Weglassen steht in
`wish.note` und gehört als `note` in `draft_order` - sonst bekäme die Küche eine
Zugabe ohne Preis. Eine Menge im Wunsch („ohne Zwiebeln, zweimal") bleibt bei
der Position, nicht im Hinweis. Eine Frage nach den Allergenen („welche
Allergene sind drin") ist kein Wunsch, sondern der Allergenpfad.
Gehört der „Wunsch" zum Namen („Sommerrollen mit Garnelen"), ist er keiner.
Steht im Wunsch eine Zahl („Nummer 23 mit 2 Soßen"), gilt Regel A und die Suche
fragt nach der einen Nummer. Nennt der Satz mehrere Positionen, wird zuerst
zerlegt, der Wunsch gehört dann zu seinem Teil (`positions[].wish`). Der Satz
zur Allergie ist ein Entwurf und wird vor dem Echtbetrieb mit dem Rechts-Check
abgestimmt (docs/09); der Wortlaut des Küchenhinweises steht fest (E14).

**Harte Regel:** Der Agent darf nur eine Position übernehmen, die eine `menu_item_id` aus diesem Tool trägt. Bei `ambiguous` wird nachgefragt, nicht gewählt.

---

## `get_item_details`
Für Rückfragen zu Optionen, Extras und Allergenen.

**Request** `{ "call_id": "…", "tenant_id": "…", "menu_item_id": "…", "allergen_question": false }`

`allergen_question` ist **Pflicht** und hat keine Vorgabe: nur der Agent weiß, ob der Gast nach Allergenen gefragt hat oder nach der Sauce. Fehlt das Feld, antwortet das Tool `invalid_input` - das fällt auf, ein stiller Vorgabewert nicht.

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
**Allergene:** Ist `known: false` **und** `allergen_question: true`, lautet `say` wörtlich „Das kann ich Ihnen nicht sicher sagen. Das Team ruft Sie dazu zurück." (aus dem Code, `domain/menu/details.py`), und der Agent legt einen Rückruf an. Ohne Allergenfrage bleibt der Satz weg: sonst bekäme die Frage nach der Sauce den Rückruf statt einer Antwort. `known: false` steht trotzdem in den Daten - der Agent nennt nie ein Allergen, das dort nicht steht. Der Agent formuliert hier **nichts** selbst. `codes` ist dann leer und `confirmed_at` `null`: keine Auskunft, nicht „frei davon". Gepflegte Codes kommen in der Reihenfolge der LMIV-Liste, damit jeder Anruf dieselbe Reihenfolge hört; `confirmed_at` ist der jüngste Nachweis, als **Ortsdatum** des Mandanten (CLAUDE.md §8) und nicht als UTC-Datum: ein Nachweis vom 19.09. um 00:30 Ortszeit liegt als 18.09. 22:30 UTC in der Zeile.

Die `menu_item_id` kommt aus `search_menu`, ein anderer Weg in die Karte existiert nicht. Inaktive Gerichte liefert das Tool nicht (`not_found`), ausverkaufte schon, mit `sold_out: true` und demselben Satz wie die Suche - es sei denn, es ging um Allergene und die Auskunft fehlt, die wiegt dann schwerer.

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
{ "call_id": "…", "tenant_id": "…", "idempotency_key": "…", "type": "delivery",
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

**Stand T-4.5 (nur Abholung):** `type: "delivery"` antwortet `invalid_input` mit der Frage, ob der Gast abholen möchte; Zone, Pauschale und Mindestbestellwert kommen mit T-6.5. Im Code (`domain/ordering/`):

| Lage | Antwort |
|---|---|
| Abholung gerade nicht offen | `closed` |
| Gericht unbekannt, inaktiv oder von einem anderen Mandanten | `not_found` |
| Gericht ausverkauft | `conflict`, `say` wie bei `search_menu` |
| Option gibt es an diesem Gericht nicht, doppelt, oder Pflichtgruppe mehrfach | `invalid_input` mit `say` |
| Pflichtgruppe ohne Wahl | `invalid_input`, `say` fragt nach der Gruppe. Die Voreinstellung wird **nicht** still eingesetzt - sie wäre geraten |
| Menge über 30 oder mehr als 30 Positionen | `invalid_input` (Schutz gegen Hörfehler, nicht gegen Großbestellungen) |

- Dieselbe Option zweimal ergibt `invalid_input` mit `say` („Erdnuss zu Ente knusprig habe ich schon. Einmal Erdnuss, richtig?"). Zwei Kartenzeilen oder Gruppen, die sich nur in der Schreibweise unterscheiden, ergeben `service_unavailable` mit Übergabe ans Team statt einer geratenen Zeile; der Import lehnt solche Zeilen ab. Ebenso eine Optionswahl, die den Preis unter null drückt: ein Kartenfehler, den der Gast nicht korrigieren kann.
- Optionen kommen als `{group, name}`, verglichen ohne Groß-/Kleinschreibung; Preise kommen nur aus der Karte. `order_items.unit_price_cents` friert den Kartenpreis ein, die Optionen tragen ihre Differenz selbst.
- `ready_at` = jetzt + `service_config.pickup_wait_minutes`, auf die volle Minute **aufgerundet** (in UTC): angesagt wird nie weniger als die Wartezeit. Liegt es nach Schluss der Abholung, steht `ready_after_close` in `warnings`; der Entwurf entsteht trotzdem.
- `readback` je Position ein Satz („Zweimal Nummer 23 Frühlingsrollen, ohne Zwiebeln."), dann Summe, Abholzeit, Name. Ein Replay mit demselben `idempotency_key` liefert dieselbe Antwort: Nummer und Name je Position (Stand der Karte) und `warnings` stehen im `audit_log`-Eintrag `order.draft_created` und werden von dort gelesen, nicht aus Karte und Öffnungszeiten neu gerechnet, die sich seitdem geändert haben können. Menge, Optionen, Hinweise, Name und Telefon kommen aus `orders`/`order_items`: `audit_log` hält keine Personendaten, weil es länger bleibt als die Bestellung.

---

## `confirm`
Der einzige Übergang von `draft` nach `confirmed`.

**Request** `{ "call_id": "…", "tenant_id": "…", "entity": "order", "entity_id": "…", "idempotency_key": "…" }`

**Response** `{ "ok": true, "data": { "status": "confirmed", "handover": "queued", "pickup_code": "A17" }, "say": null }` — `handover` ist `"queued"` oder `"awaiting_approval"` (siehe unten)

**Wirkung:** Status setzen, `audit_log` schreiben, Ereignis an n8n legen, GUI aktualisieren. Im Modus `overflow` wird `approved` erst durch das Team gesetzt; die Küche startet dann später.

**Idempotenz trägt der Zustand, nicht der Schlüssel:** ein zweiter Aufruf auf denselben Vorgang liest `confirmed` und antwortet gleich, ohne ein zweites Ereignis anzulegen — auch mit einem anderen `idempotency_key`. Der Schlüssel landet im `audit_log`. Ein stornierter Vorgang ergibt `conflict`, ein unbekannter oder fremder `not_found`. `pickup_code` bleibt bei Reservierungen `null`.

**Bestellungen** (`domain/ordering/confirm.py`, seit 23.09.2026):

- `pickup_code` ist „A" plus laufende Nummer je Mandant und Betriebstag (A1, A2, …; Betriebstag ab 05:00, `core/time`). Der Tag kommt aus dem Anlagezeitpunkt der Bestellung. Zwei gleichzeitige Bestätigungen zählen nacheinander (Sperre je Mandant und Tag), nie zweimal derselbe Code.
- `handover` hängt am Modus beim Bestätigen: nur `primary` gibt `"queued"`, setzt `handover_state = pending` und legt `order.confirmed` in die Outbox (Bon für die Küche). In `overflow`, `shadow` und `paused` antwortet es `"awaiting_approval"`: die Bestellung ist bestätigt, aber nichts geht an die Küche, bis das Team im Tablet freigibt (T-4.7, docs/02 §Modus). Ein späterer Moduswechsel ändert die Antwort auf einen erneuten Aufruf nicht.
- Das Ereignis trägt die ganze Bestellung (Positionen mit Nummer, Name, Menge, Optionen, Hinweis, Preis). Nummer und Name kommen aus dem Schnappschuss des Entwurfs: auf dem Bon steht, was dem Gast vorgelesen wurde - nach einer Korrektur im Tablet der korrigierte Stand.
- **Bon aus dem Tablet (T-4.7):** „Passt" auf einer wartenden Bestellung, „Nochmal senden" und eine Korrektur, nachdem die Küche schon einen Bon hat, legen ebenfalls `order.confirmed` mit der ganzen Bestellung ab (`domain/ordering/ticket.py`). Zwei Felder kommen dazu: `revision` (Anzahl der Korrekturen, 0 beim ersten Bon) und `correction_reason` (`wrong_item` / `wrong_quantity` / `wrong_address` / `other`, nur wenn die Küche schon einen Bon hatte - dann druckt sie „KORREKTUR"; sonst `null`).
- **Vertrag für den Bon-Empfänger (T-4.6):** Der Dispatcher stellt nach `next_attempt_at` zu, nicht nach Bestellung. Deshalb (a) überschreibt ein neuer Stand einen Bon, den noch niemand zu senden versucht hat (`pending`, `attempts = 0`), statt einen zweiten anzulegen; (b) verwirft der Empfänger einen Bon, dessen `revision` kleiner ist als eine für dieselbe `order_id` schon gedruckte; (c) folgt `handover_state` nur dem Ereignis mit der aktuellen Revision. Umgesetzt in T-4.6: (b) und (c) prüft der Server selbst beim Abholen und bei der Rückmeldung, die Druckbrücke verwirft zusätzlich jede schon gedruckte Revision (§Küchenbon).
- Eine Bestellung aus einem anderen Anruf ergibt `not_found`, eine stornierte `conflict`.

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
| `POST /v1/kitchen/claim` · `/ack` | Küchenbon für die Druckbrücke im Lokal (T-4.6, §Küchenbon) |

## Küchenbon (Druckbrücke, T-4.6)

Nur für die Druckbrücke (`printbridge/`), mit eigenem Token `KITCHEN_BRIDGE_TOKEN` statt des Agent-Tokens. Das Token gilt für genau einen Betrieb (`KITCHEN_BRIDGE_TENANT_ID`); jeder andere `tenant_id` ist `unauthorized`. Fehlt Token oder Betrieb, ist der Eingang zu. Je Betrieb läuft genau eine Brücke: ihr Druckprotokoll ist lokal, eine zweite Brücke könnte einen Bon nach verlorener Rückmeldung noch einmal drucken. Antworten in der Hülle aus §1. Architektur: docs/02 §2a.

**`POST /v1/kitchen/claim`** `{tenant_id, limit}` (1–10, Standard 5) → `{"tickets": [{"id", "attempt", "ticket", "ready_time", "print_time"}]}`

- Fällige `order.confirmed` des Betriebs, älteste zuerst. `ticket` ist der Inhalt aus §confirm (Positionen, `revision`, `correction_reason`).
- `ready_time` und `print_time` sind „HH:MM" in der Zeitzone des Betriebs: die Brücke rechnet nicht selbst um, der Bon stimmt auch auf einem Rechner mit UTC.
- Abholen zählt als Versuch und leiht den Bon für 60 s aus. Ohne Rückmeldung ist er danach wieder fällig.
- Ein Bon, zu dem es schon eine höhere Revision gibt, wird nicht ausgeliefert, sondern als erledigt verbucht (`last_error` sagt „überholt von Revision n").
- Gemessen: p95 5,8 ms mit lokaler DB.

**`POST /v1/kitchen/ack`** `{tenant_id, event_id, ok, error?}` → `{"status": "sent" | "pending" | "failed"}`

- Erst wird die Bestellung gesperrt, dann die Revision geprüft: eine gleichzeitige Korrektur überholt die Rückmeldung nicht.
- `ok = true`: Bon `sent`; ist er die neueste Revision, wird `handover_state` `sent` (auch aus `failed`: die Karte wird wieder normal).
- `ok = false`: Fehlversuch mit Backoff (docs/03 §outbox), nach dem letzten `failed` plus Alarm. Ist er die neueste Revision und `handover_state` noch `pending`, wird die Karte sofort rot, Alarm im Log und `order.handover_failed` an n8n.
- Jeder Wechsel der Karte steht im `audit_log` (`order.handover_sent` / `order.handover_failed`, Akteur `system`, ohne Personendaten).
- Wiederholte Rückmeldung für einen erledigten Bon ändert nichts. Unbekannter oder fremder Bon: `not_found`.

**Wächter** (eigener Faden im Dispatcher-Prozess, `python -m api.jobs.cold_path`, unabhängig davon, wie lange ein Versand an n8n hängt): liegt ein fälliger Bon 60 s unabgeholt, wird die Karte rot wie oben. Ein Bon ohne Versuche mehr wird `failed`, ein überholter stattdessen still erledigt. Er sperrt erst die Bestellung, dann den Bon, wie das Tablet: „Nochmal senden" während eines Durchlaufs legt keinen zweiten Bon an und wird nicht gleich wieder rot.
