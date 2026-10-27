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

The envelope also carries what the framework answers itself (#252): an unknown path is `not_found` with status 404, a wrong method `invalid_input` with 405. A `reserved_for` that no calendar holds (the first and last days of the year range, where adding a time zone or a day overflows) is `invalid_input` like any other malformed field, in the tools and in the conversation core.

Below `/gui/` the same envelope keeps an error status (repo audit, 27.10.2026): an unhandled error is 500, a malformed path or field 422, an `AppError` no view catches 500. The tablet's htmx swaps every answer below 400 into the page, so a 200 put the JSON where the header and its pause button stood (`api/main.py` `_status`, `api/gui/static/app.js`). The agent's paths keep 200.

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
   Kartennummern tragen Buchstaben: eine Endung a bis g („23a", „25g", auch
   „fünfundzwanzig G" oder „25 g") und ein Präfix, wenn die Karte es hat
   („S12", „SM1", T-4.12). Welche Präfixe gelten, liest die Suche aus den
   aktiven Nummern der Karte (`items.card_format`), nie aus dem Code. Gesagt
   wird das Präfix als Buchstabe („S 12"), buchstabiert („Es zwölf", „Es Em
   eins") oder als Kategoriewort („Sushi zwölf"), wenn alle Nummern mit diesem
   Präfix in einer einwortigen Kategorie stehen und diese Kategorie keine Nummer ohne Präfix hat. Ein Kategoriewort kann mehrere
   Präfixe meinen („Sushi" trägt S und SM): die Suche schlägt alle nach, genau
   ein Treffer ist `exact_number`, zwei sind `ambiguous` („Sushi eins" → S1 oder
   SM1), keiner `not_found`. Nie wird aus einer Präfixnummer die Zahl allein:
   „S 13" findet nicht Gericht 13, „Nummer 23g" nicht die 23. Ein Präfix vor
   einer Zahl über 999 („S1000", „Sushi tausend") ist auch ohne „Nummer" eine
   genannte Nummer und `not_found`, nie eine Namenssuche. Eine Nummer mit
   Präfix ist nie eine Menge: direkt neben einem Namen („S12 Lachs") oder
   hinter einer Ziffer bzw. „die 23" fragt die Suche nach der einen Nummer; mit
   „und" oder Komma getrennt („S12 und Pho Bo") sind es zwei Positionen wie bei
   einer Zahl ohne Präfix. Nur ein Zahlwort davor ist die Menge („zwei S
   zwölf"). Buchstabiert („Es zwölf") zählt nur am Satzanfang, auch nach Wunsch
   oder Gruß („Ich hätte gern Es zwölf", „Hallo, Es zwölf"), nie nach einem Verb
   („nehme ich es zwei" ist kein S2). A word the speech recognition writes for a
   letter adds a candidate: every engine measured writes „S zwölf" as „ist zwölf"
   (T-10.3), so „ist" directly before a number, set apart from it, keeps the S
   number next to the plain one („12 oder s12"), after any lead („Ich nehme ist
   zwölf"). Such a number is never taken without a question, whichever of the two
   the menu has (Codex PR #257): with both the search asks by name („Meinen Sie
   Nummer 12, Wan-Tan-Suppe oder Nummer S12, Thunfisch Maki?"), with one it offers
   that one („Meinen Sie Nummer S7, Gurken Maki?", `ambiguous` with one result),
   because the dish that exists may not be the one that was said. With none it is
   `not_found`. Next to another dish („Ist zwölf und Pho Bo") it is a position of
   its own, like the S12 said clearly. Only where the menu has the prefix S, and only in a
   pure number sentence: next to a name the sentence is what it was („das ist 5g
   Zucker"). The same behind the number: the speech recognition writes a trailing
   letter as a word („26d" as „sechsundzwanzig der", „30a" as „dreißig ja"; run
   on the real menu, docs/20 §11), and each of those words is filler in a number
   sentence. Where such a word (`ja` for a, `war` for b, `der` and `dann` for d,
   `die` for e or g, `äh`, `eine` and `einen` for e, `ist` for f) stands
   directly behind a number, plain or with a prefix („S sieben ja" is S7 or S7a,
   Codex PR #282), at most a comma or a hesitation in between („die 26, ähm,
   der"), with nothing but filler or the quantity behind it („die 26 der noch",
   „die 26 der zweimal", „Nummer 35 ja genau"; „die 26 eine Portion" is the
   quantity alone), and the menu has that number with that letter, the lettered
   number is a second candidate („26 oder 26d") and never taken without a
   question: the search asks by name („Meinen Sie Nummer 26, ... oder Nummer 26d,
   ...?"), or offers the one dish the menu has (eval `abholung_0080`). A full stop
   behind the number ends the order: „Die 26. Ja, gerne." is the 26 and an
   answer. Without that card number on the menu nothing changes: „Nummer 26 der"
   stays the 26, and „die 26 bitte" stays it on every menu. Eine genannte, nicht
   vorhandene Nummer
   als Wort wird abgesetzt vorgelesen („s tausend"). Präfixe, die schon Menge,
   Marker oder gesprochenes Wort sind (`x`, `st`, `nr`, `no`, `hm`, `ja`, `es`,
   `so`, `um`, `zu`, `da`, `du`, `er`, `ob`), lehnt der Import mit eigener
   Meldung ab.
   Eine Zahl wird nur dann direkt als Kartennummer genommen, wenn der ganze Satz
   genau diese eine Nummer ist (Regel A): Marker („Nummer", „Nr."), Füllwörter,
   Zögerlaute und **eine** Menge dürfen daneben stehen, sonst nichts.
   „Einmal die Nummer 23 bitte" und „zweimal die 23" sind damit `exact_number`,
   ebenso „Hallo, ich würde gern die 13" und „dazu die 24". Ein „und" ganz vorn
   setzt die Bestellung fort („Und noch die 24"). Steht davor schon eine Menge
   („zweimal und die 24") oder ein Marker („Nummer und 24"), folgt ein Zehner
   („und zwanzig", abgeschnitten aus „drei und zwanzig") oder steht es zwischen
   zwei Zahlen („die 23 und die 24"), ist der Satz mehrdeutig. The tens count
   in digits as well („und 20", „und 20 mal die 13") and across a pause, which
   is punctuation, a hesitation or „bitte" („und äh zwanzig", „und... 20");
   an article, opening word, marker or prefix keeps the continuation („und
   noch die zwanzig" is the 20, Codex PR #147).
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
   A part the sentence cut off gets the same check with the menu (eval
   `abholung_0081`): of „die 25, zweimal die 12 und die S12" the sentence alone
   cuts off „zweimal die 12 und die S12", because „die S12" (a prefix, no
   quantity) and a name without a quantity („… und Pho Bo") open no position
   without the menu; checked like a sentence of its own, the part gives its two
   positions. A piece that is one card number is a position whether the menu has
   it or not (eval `abholung_0082`): „zweimal die 99 und Pho Bo" is the 99,
   answered with `not_found`, and Pho Bo once. Kept whole, the name search found
   Pho Bo alone and took it twice. Any other piece that hits nothing still makes
   the „und" part of a name. A piece with an unclear number makes the whole
   sentence `ambiguous` with the question for the number (Codex PR #147): before,
   the sentence went whole to the name search, which strips the numbers, so
   „einmal Pho Bo, und 30" was Pho Bo alone. The same holds for the dish part in
   front of a wish („und 30 mit Reis" asks; before, „mit Reis" was searched as a
   name). A pause at the „und" of a spoken number („die drei und äh zwanzig",
   „die drei äh und zwanzig") keeps it one piece, and the sentence asks whether
   it is 23 or the 3 and the 20, also next to a prefix, a letter or a name. A
   comma on either side of the „und" is the same pause („die drei, und zwanzig
   Frühlingsrollen", „die drei und, zwanzig"), and so is a sentence that starts
   with the cut-off half („und äh, zwanzig", #328). A unit right before a tens
   word without any „und" („drei zwanzig", „die 3 20") is two numbers or a 23
   whose „und" the ear lost: the sentence asks as well (#325), also with a
   hesitation between them („Nummer drei äh zwanzig Portionen"), while a bare
   comma separates („die 3, zwanzig Portionen" is 20 of the 3). „die eine
   zwanzig" is the 20, the „eine" its article; before a tens quantity it
   cannot be one, and „eine zwanzig Portionen" asks. The tens half of such a number is
   no quantity („drei und äh zwanzig Portionen" is not 20 portions, #328). A
   hesitation counts however the ear spells it („äh", „ähh", „äääh", „öh",
   „eh", „hmm"); „ah" and „oh" do not, the ear writes them for the letters a
   and o (#327). When a name before the unclear number hits several dishes
   („einmal Suppe, und 30"), the sentence still asks for the number instead of
   searching the name alone (#328). In a sentence of several parts only the part
   with the unclear number asks („die 13 und die 24, und 30": the 13 is taken).
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
| `allergy` | eine eigene Allergie oder Unverträglichkeit („ich vertrage keine Erdnüsse", „Erdnussallergie", „Nuss- und Sesamallergie", „Laktoseintoleranz"); jede genannte Zutat kommt in den Hinweis | `text` ist der Küchenhinweis im festen Wortlaut „WICHTIG: Keine <Zutat>. Grund: Allergie" (E14), `ingredient` die Zutat; als `note` in `draft_order`, im `readback` wiederholt; `say` ohne Zusage, dass das Gericht frei davon ist. Ohne erkennbare Zutat fragt `say`, wogegen; „weiß ich nicht" oder „nein" ist keine Zutat, die Frage bleibt offen. Mehrere Gerichte ohne Zutat in einem Satz: `say` fragt nach dem ersten und nennt es („Wogegen sind Sie bei Pho Bo allergisch?"), die anderen folgen einzeln. A removal said before the allergy, or after it with „ohne", stays (#252): `text` ends with it („WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln"); while the ingredient is still asked for, it waits in `wish.note` and goes after the note made from the answer. An addition next to an allergy is not taken yet (docs/01 open point). A phrase that only denies an allergy („ich habe keine Erdnussallergie", „ich bin nicht gegen Erdnüsse allergisch", „gegen nichts allergisch") is no allergy: only the wishes around it count („ich habe keine Allergie, aber bitte ohne Koriander"), without one there is no `wish`. A negation mixed with more („keine Allergie gegen Erdnüsse, aber gegen Sesam") gives an allergy without `ingredient`: `say` asks what it is against, the ingredient is never read from such words. Only a closing „sonst keine (weiteren) Allergien" after a named allergy does not count as such a negation. A denial does not reach over a bare „und" to an allergy adjective with an ingredient („ich habe keine Allergie und allergisch auf Erdnüsse"): that is asked about; a second noun stays denied („keine Allergien und Unverträglichkeiten gegen Laktose"). An ingredient behind the same „gegen" or „auf" again belongs to the list („allergisch gegen Erdnüsse und gegen Sesam"), a time or a clause of its own does not („und gegen 18 Uhr", „und auf Diät", „und möchte bestellen"). The ingredient is read from the whole sentence, so a removal said right before the allergy may also be listed as an allergy, the safe side. Nothing is noted with a gap (#207, PR #361): where more follows than the parser reads, the ingredient stays open and `say` asks what it is against; the words it did not read never go on the note. That is a list behind the allergy word that closes a phrase („gegen Erdnüsse allergisch und gegen Sesam", „Erdnussallergie, außerdem gegen Sesam", „Erdnussallergie und Sesam", „Erdnussallergie, leider"), „aber auch" with more („allergisch gegen Erdnüsse, aber auch gegen Sesam"), a „gegen" with one word that no list holds („und meine Frau gegen Sesam"), and an allergy word that names an allergen the parser does not read („laktoseintolerant", „Erdnussallergiker"). A time, an order verb, a new subject or dish, „danke" or a new sentence behind the allergy word asks nothing („und gegen 18 Uhr", „und möchte bestellen", „. Und Cola"); a bare word behind a word that names no allergen („mit Allergie und Frühlingsrollen") asks as before. A „keine" that an „aber" or „bitte" leads to opens a removal like „ohne" („aber bitte keine Zwiebeln", #331), unless it denies an allergy word („aber keine weiteren Allergien"). A removal waiting in `wish.note` that is longer than an order line allows (200 characters) is dropped, as on the kitchen note (#331) |
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
**Allergene:** Ist `known: false` **und** `allergen_question: true`, lautet `say` wörtlich „Das kann ich Ihnen nicht sicher sagen, dazu ist bei uns nichts hinterlegt." (aus dem Code, `domain/menu/details.py`). The own core hands the call to the team right after it, or sets a callback when nobody answers, and the call ends before any `confirm` (T-11.1, legal check L17: allergen information belongs before the contract). Over HTTP a client has to hand over or set the callback itself; the model's tool texts (`prompts/tools_v2.md`) no longer ask for it, since in the own core the loop does it. Ohne Allergenfrage bleibt der Satz weg: sonst bekäme die Frage nach der Sauce den Rückruf statt einer Antwort. `known: false` steht trotzdem in den Daten - der Agent nennt nie ein Allergen, das dort nicht steht. Der Agent formuliert hier **nichts** selbst. `codes` ist dann leer und `confirmed_at` `null`: keine Auskunft, nicht „frei davon". Gepflegte Codes kommen in der Reihenfolge der LMIV-Liste, damit jeder Anruf dieselbe Reihenfolge hört; `confirmed_at` ist der jüngste Nachweis, als **Ortsdatum** des Mandanten (CLAUDE.md §8) und nicht als UTC-Datum: ein Nachweis vom 19.09. um 00:30 Ortszeit liegt als 18.09. 22:30 UTC in der Zeile.

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
            "alternatives": ["2026-09-20T18:00:00Z", "2026-09-20T19:30:00Z"],
            "team_callback": false },
  "say": "Um halb sieben ist leider voll. Sechs Uhr oder halb acht ginge."
}
```

`alternatives`: höchstens zwei Termine im Raster, der nächstgelegene zuerst, **nur unter sechs Stunden Abstand zum Wunsch**. Der Satz nennt die Uhrzeit ohne Tag, und der Gast hört die Deutung, die dem Wunsch am nächsten liegt; unter sechs Stunden ist das die richtige (die andere Hälfte der Uhr liegt ±12 h weg, ein anderer Tag ±24 h). Am Ruhetag gibt es deshalb nichts vom Vortag (Befund T-5.2, `reservierung_0027`), nachts um halb eins keinen Mittag als „halb zwei" (Review PR #152). Ohne Alternative sagt der Satz „auch nicht kurz davor oder danach", nicht „an dem Tag": weiter weg kann noch etwas frei sein. Ist der Tag geschlossen (Ruhetag, geschlossener Sondertag) und liegt der Wunsch nicht im Fenster des Vorabends, heißt es „Am Montag haben wir leider geschlossen."

**The asking call's own drafts do not count (#207, 14.10.2026):** the seats are counted without the drafts of the `call_id` in the request. A guest who corrects the time or the party size gets a new draft next to the old one; the old one must not make the evening full for the same guest (30 of 40 confirmed, a draft of six, the correction asks for six: free, not 36 + 6). Drafts of every other live call count, and so do the confirmed tables of the asking call: a second table in the same call is not a correction.

**Service-Fenster (T-1.14, D12, Maxi 26./27.09.2026):** Ein Service ist ein Öffnungsfenster für `dinein` (Mittag, Abend, an Sondertagen die Sonderzeit). Gäste bleiben im selben Service:
- Beginn frühestens `service_config.reservation_lead_minutes` nach Öffnung (15) und spätestens `reservation_last_start_minutes` vor Schluss (30), beide Grenzen buchbar, die Schlusszeit selbst nie (auch bei 0). Mittag 11–14 und Abend 17–22 ergeben 11:15–13:30 und 17:15–21:30, im 30-Minuten-Raster angeboten ab 11:30 und 17:30. Gilt nur für Reservierungen (Annahme D12).
- Abendwunsch → Alternativen nur am selben Abend, Mittagswunsch → nur am selben Mittag, weiter unter sechs Stunden Abstand. Liegt der Wunsch im Service, aber vor dem frühesten oder nach dem spätesten Beginn (17:00, 21:45): „Um fünf Uhr können wir leider keinen Tisch reservieren. Halb sechs oder sechs Uhr ginge."
- Außerhalb der Öffnung (15:00 zwischen zwei Services, 23:00 nach dem letzten, 01:00 nach einem Nachtfenster): keine Alternative, „Um drei Uhr haben wir leider geschlossen. Zu welcher anderen Uhrzeit passt es Ihnen?"
- Morgens vor der ersten Öffnung des Tages (näher an ihr als am letzten Schluss davor; nur ohne Schluss am Vortag gilt Mitternacht): Alternativen aus dem ersten Service desselben Tages mit freiem Platz, auch abends. Ab sechs Stunden Abstand nennt der Satz die Tageszeit: „Um neun Uhr haben wir noch geschlossen. Abends um halb sechs oder um sechs ginge."
- Wunsch für einen anderen, genannten Tag: keine Sonderlogik, es gilt der Tag des Gastes.

**Limits for a reservation by phone (#252, Maxi 12.10.2026),** all three from `service_config` (migration 007):
- **How far ahead:** a wish more than `reservation_max_days_ahead` days ahead (90) is `available: false` without alternatives: „So weit im Voraus kann ich noch keinen Tisch reservieren, höchstens 90 Tage im Voraus. Welcher frühere Tag passt Ihnen?" Counted in calendar days of the restaurant, not in hours: late in the evening the last day can still be booked for its whole evening. Checked before opening hours, capacity and party size, so it holds for a large party too; only a moment in the past is refused before it (`invalid_input`).
- **Party size:** a table that is free by the rules above is booked by the agent only up to `reservation_max_party_size` guests (8), on the same day up to `reservation_max_party_size_same_day` (5; the smaller of the two counts). Above that the answer is `{ "available": false, "alternatives": [], "team_callback": true }` with the sentence „Für neun Personen kann ich den Tisch nicht selbst zusagen, das bespricht das Team persönlich mit Ihnen." (same day: „Für sechs Personen noch heute …"). What follows is a callback for the team (`create_callback`, `reason: out_of_scope`, the wish with party size, day, time and name in `summary`); the sentence that promises it comes from `create_callback`, once the callback exists. A client over HTTP creates it itself. **In the conversation core the loop does it, not the model** (`agent/loop.py`): the turn that learns of `team_callback` ends with the tool's sentence, the callback and its sentence, and the call ends. A model answers with a sentence or a tool call, never both, so the sentence would end its turn, and a guest who hangs up then would be lost (Codex PR #280). The model never sees this result, and the system prompt carries nothing about it. Without a number to call back (caller ID withheld), or when the callback fails, the call goes the way of every handover: a transfer where the team is reachable, otherwise the sentence that nobody can be reached. Such a caller is not asked for a number; that is the open point of every handover (docs/01_STATUS.md).
- `team_callback` replaces only an `available: true`: a closed day stays closed and a full service full, whatever the size of the party, and the guest can name another time. `create_reservation` runs the same check and answers `conflict` with the same sentence, so no draft exists for such a table; in the conversation core that conflict carries the same signal (`core/errors.py` `TeamDecides`), and a model that skipped `check_slot` ends with the callback too.

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

**Prüfungen im Code, nicht im Modell:** Anruf bekannt (`call_id` muss in `calls` stehen, sonst `not_found`) · Rufnummer nach E.164 normalisierbar (sonst `invalid_input` mit `say`) · Zeitpunkt in der Zukunft · Slot frei nach denselben Regeln wie `check_slot` (sonst `conflict`, `say` nennt die Alternativen). Der Entwurf zählt sofort gegen die Kapazität und landet im `audit_log`. It counts for every other call; against its own call it does not, as in `check_slot` (#207): a corrected draft is checked without the one it replaces. Gleicher `idempotency_key` → dieselbe Antwort ohne neue Prüfung; Schlüssel eines anderen Mandanten → `conflict`.

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
  ],
  "note": null }
```
`note` (optional, at most 200 characters) is the note of the whole order (T-11.1, D24): the guest's allergy said without a dish, or when it is unclear which dish it belongs to. It is stored in `orders.note`, read back after the dishes („Für die ganze Bestellung: …."), sent on the kitchen ticket and printed bold above the dishes. In the own core the model never writes an allergy into it: the core writes it there from its state.

**Allergy notes in the own core (T-11.1, legal check L13, redesign of 26.10.2026):** an allergy never travels in a note the model writes. A `draft_order` or `create_reservation` (or a `cart`) whose note names an allergy is refused with a hint (`agent/guards.py` `HINT_ALLERGY_NOTE`). The core reads the guest's allergy from the guest's own sentence and writes it onto the draft itself before it is dispatched (`agent/allergies.py` `noted_call`): onto the line of the dish it was said with when a clear `search_menu` hit found that dish, otherwise onto `note` of the whole order, and onto `note` of a table. Its wording is „WICHTIG: Keine <Zutat>. Grund: Allergie" after the caller's explicit „ja" to the consent question, „ohne <Zutat>" otherwise and always for another adult, in front of what the model wrote („WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln"). The first draft that needs it waits for the core's questions (docs/05 §Allergie). Over HTTP the tool stores the note as it comes; the consent is a step of the conversation, not of the tool.
**Response**
```json
{
  "ok": true,
  "data": {
    "order_id": "…", "status": "draft",
    "items_total_cents": 1480, "delivery_fee_cents": 250, "total_cents": 1730,
    "ready_at": "2026-09-15T18:50:00Z",
    "warnings": [],
    "readback": "Zweimal Nummer 23 Frühlingsrollen mit Erdnusssauce, ohne Zwiebeln. Macht 17 Euro 30, Lieferung in etwa 50 Minuten an die Rheinstraße 54. Passt das so?"
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
- `readback` je Position ein Satz („Zweimal Nummer 23 Frühlingsrollen, ohne Zwiebeln."), dann Summe, Abholzeit, Name. The total is spoken as two plain numbers, never with a decimal comma: „17 Euro 30", a round amount „7 Euro", below one euro „50 Cent", single cents with their unit („17 Euro 5 Cent"), a lone one as a word („ein Euro 50"; a voice reads „1 Euro" as „eins Euro"). A voice that does not know prices reads „17,30 Euro" as „siebzehn Komma drei null Euro" (Maxi, 12.10.2026); the surcharge in the `say` of `search_menu` uses the same form („50 Cent Aufpreis"). Ein Replay mit demselben `idempotency_key` liefert dieselbe Antwort: Nummer und Name je Position (Stand der Karte) und `warnings` stehen im `audit_log`-Eintrag `order.draft_created` und werden von dort gelesen, nicht aus Karte und Öffnungszeiten neu gerechnet, die sich seitdem geändert haben können. Menge, Optionen, Hinweise, Name und Telefon kommen aus `orders`/`order_items`: `audit_log` hält keine Personendaten, weil es länger bleibt als die Bestellung.

---

## `confirm`
Der einzige Übergang von `draft` nach `confirmed`.

**Request** `{ "call_id": "…", "tenant_id": "…", "entity": "order", "entity_id": "…", "idempotency_key": "…" }`

**Response** `{ "ok": true, "data": { "status": "confirmed", "handover": "queued", "pickup_code": "A17" }, "say": null }` — `handover` ist `"queued"` oder `"awaiting_approval"` (siehe unten)

**Wirkung:** Status setzen, `audit_log` schreiben, Ereignis an n8n legen, GUI aktualisieren. Im Modus `overflow` wird `approved` erst durch das Team gesetzt; die Küche startet dann später.

**Idempotenz trägt der Zustand, nicht der Schlüssel:** ein zweiter Aufruf auf denselben Vorgang liest `confirmed` und antwortet gleich, ohne ein zweites Ereignis anzulegen — auch mit einem anderen `idempotency_key`. Der Schlüssel landet im `audit_log`. Ein stornierter Vorgang ergibt `conflict`, ein unbekannter oder fremder `not_found`. `pickup_code` bleibt bei Reservierungen `null`.

**Reservations, seats checked again (#250):** a draft holds its seats only while its call can confirm it: the call has not ended and the draft is younger than `service_config.max_call_seconds`. An older draft no longer counts in `check_slot`, so its seats may be given to someone else. `confirm` therefore checks the window again, under the same day lock as `create_reservation` and without the drafts of its call (the draft itself and any a correction replaced, #207), and answers `conflict` with `say` "Dieser Tisch ist in der Zwischenzeit leider vergeben worden. Zu welcher anderen Uhrzeit passt es Ihnen?" when the party no longer fits. Seats only: opening hours and lead times were checked when the draft was made. Within a live call the check passes, because the draft still holds what it asks for; the one exception is a call that holds two drafts for the same window and confirms both, where every confirmed seat counts and the second may no longer fit. In the conversation core a draft that `confirm` refuses with `conflict` leaves the state (`agent/state.py`): it can never be confirmed as it is, and a "Ja" to the next question must not try again. The intent stays; `invalid_input` and `not_found` keep the draft, and so does the core's own refusal for a missing yes.

**Bestellungen** (`domain/ordering/confirm.py`, seit 23.09.2026):

- `pickup_code` ist „A" plus laufende Nummer je Mandant und Betriebstag (A1, A2, …; Betriebstag ab 05:00, `core/time`). Der Tag kommt aus dem Anlagezeitpunkt der Bestellung. Zwei gleichzeitige Bestätigungen zählen nacheinander (Sperre je Mandant und Tag), nie zweimal derselbe Code.
- `handover` hängt am Modus beim Bestätigen: nur `primary` gibt `"queued"`, setzt `handover_state = pending` und legt `order.confirmed` in die Outbox (Bon für die Küche). In `overflow`, `shadow` und `paused` antwortet es `"awaiting_approval"`: die Bestellung ist bestätigt, aber nichts geht an die Küche, bis das Team im Tablet freigibt (T-4.7, docs/02 §Modus). Ein späterer Moduswechsel ändert die Antwort auf einen erneuten Aufruf nicht.
- **The amount holds a ticket back too, in every mode (#252, 13.10.2026):** an order whose `total_cents` (items plus delivery fee, what the guest was read) is above `service_config.order_approval_above_cents` answers `"awaiting_approval"` in `primary` as well: confirmed, pickup code assigned, no `order.confirmed` in the outbox until "Passt" on the tablet. Above the amount, not from it: with the default of 15000 an order of exactly 150,00 euros goes to the kitchen. A limit of 0 holds every order, one that costs nothing included. The limit is read at the moment of the confirm; a limit changed afterwards does not change the answer to a repeated call. The `audit_log` entry `order.confirmed` says why the ticket waited: `waits_for` is `"mode"`, `"amount"` or `null`.
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

**Second writer, not a tool:** after a team leg nobody answered, the line's process writes the card through the same function, the same lock and the same rule of one open card per call (`callbacks.team_missed`, T-10.8). The number is the one the guest named in the call or the caller id; the reason is the transfer's (a cancellation becomes `human_requested`, a transfer without a reason, paused or the outage path, `out_of_scope`); the audit actor is `telephony`; the call's outcome becomes `callback` (an `error` stays). The text is "Ans Team weitergeleitet, dort hat niemand abgenommen." and the reason's sentence, or, where the core wrote a card of its own for the handover (an allergen question beyond the database, T-11.1, L17), that card, which the line's process keeps with the named number. Without a usable number no card is written.

Die Rufnummer wird nach E.164 normalisiert. Eine geklammerte `(0)` hinter der Landesvorwahl entfällt dabei (`+49 (0)7221 5551234` → `+4972215551234`).

---

## `transfer_to_team`

**Request** `{ "call_id": "…", "tenant_id": "…", "reason": "complaint" }`

**Response** `{ "ok": true, "data": { "transfer_to": "+49…", "available": true }, "say": null }`

Ist niemand erreichbar → `available: false`, der Agent legt stattdessen einen Rückruf an. `available` is the opening hours, not a ping (Maxi, 20.10.2026): outside them the agent's handover rings nobody and the agent writes the callback (the outage paths of the phone line ring the team at any hour); inside them the call is handed to the team, and on the phone line the dial result decides: a leg nobody answers at `TEAM_PHONE` becomes a callback card written by the line's process (`callbacks.team_missed`, docs/20 §5, T-10.8). **Schleifenschutz:** Ziel ist immer die Durchwahl, nie die Hauptnummer, und `transfer_to_team` wird je Anruf höchstens einmal ausgeführt.

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
