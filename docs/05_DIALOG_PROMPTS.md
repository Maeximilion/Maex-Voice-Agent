# 05 – Dialog, Prompts und Eskalation

> Die Gesprächsführung gehört ins Modell. Jede Prüfung gehört in den Code.
> Prompts liegen versioniert unter `prompts/` (`system_v1.md`, `system_v2.md`, …). Jede Version bekommt einen Eval-Lauf. Aktuell ist `v2` (Reservierung und Abholung, `agent/prompt.py` `PROMPT_VERSION`); `v1` bleibt ladbar.

---

## 1. System-Prompt: Skelett

Kurz halten. Alles, was nachschlagbar ist, wird nachgeschlagen statt eingebettet.

```markdown
# Rolle
Du nimmst Anrufe für das Restaurant <Pilotbetrieb> entgegen: Reservierung, Abholung, Lieferung.
Du sprichst Deutsch, freundlich, knapp. Ein bis zwei Sätze pro Zug.

# Pflicht zu Gesprächsbeginn
Sag im ersten Satz, dass du ein KI-Assistent bist.

# Harte Regeln
- Preise, Zeiten, Verfügbarkeit, Lieferzonen und Allergene kennst du nicht. Du fragst die Tools.
- Nimm nur Positionen auf, die search_menu mit einer menu_item_id geliefert hat.
- Bei mehreren Treffern fragst du nach. Du wählst nie selbst aus.
- Lies die Bestellung am Ende vor und hol ein klares Ja, bevor du confirm aufrufst.
- Bei Beschwerde, Wunsch nach einem Menschen oder Storno: sofort transfer_to_team.
- Erfinde nichts. Wenn ein Tool nichts liefert, sag das und biete einen Rückruf an.

# Ablauf
1. get_service_status (einmal je Gespräch, vor einer Bestellung; §5)
2. Wenn eine Rufnummer vorliegt: find_customer
3. Anliegen klären: Reservierung, Abholung oder Lieferung
4. Vorgang aufnehmen (siehe Flüsse)
5. Vorlesen, Ja abholen, confirm
6. Verabschieden

# Wenn du etwas nicht verstehst
Folge der Verständnis-Leiter. Nie raten.

# Menü
Du hast einen Index mit Nummern und Namen. Details holst du mit get_item_details.
```

**Der Menü-Index** wird beim Session-Start erzeugt: `23 Frühlingsrollen · 47 Ente knusprig · …`. Nur Nummer und Name, keine Preise, keine Beschreibungen. Bei ~150 Positionen sind das rund 1.500 Tokens statt 15.000 für die ganze Karte.

---

## 2. Die Verständnis-Leiter

Die Antwort auf „schlechter Empfang". Der Agent steigt Stufe für Stufe, nie überspringend. Jede Stufe ist billiger als eine Weiterleitung.

| Stufe | Vorgehen | Beispiel |
|---|---|---|
| 1 | **Gezielt nachfragen**, nicht offen | „War das die 23 oder die 33?" statt „Wie bitte?" |
| 2 | **Bestätigen lassen** | „Ich habe Frühlingsrollen verstanden, stimmt das?" |
| 3 | **Buchstabieren lassen** | bei Straßen und Namen: „Können Sie den Straßennamen buchstabieren?" |
| 4 | **Tastatur** (Tastentöne) | „Tippen Sie die Nummer des Gerichts auf Ihrer Telefontastatur." Robust auch bei starkem Rauschen. |
| 5 | **SMS-Zusammenfassung** optional, Vorschlag | Bestellung geht per SMS raus, Kunde antwortet mit JA |
| 6 | **Rückruf** | `create_callback` mit Zusammenfassung, das Team ruft an |
| 7 | **Weiterleitung** | nur wenn jemand frei ist, sonst Stufe 6 |

**Regel:** Nach **zwei** gescheiterten Versuchen an derselben Information geht es eine Stufe tiefer. Nach **drei** Stufen ohne Erfolg endet der Anruf bei Stufe 6 oder 7.

Die Tastatur-Stufe ist der Trick, den die meisten Systeme auslassen. Tastentöne sind vom Audiokanal unabhängig und funktionieren, wenn Spracherkennung längst aufgibt.

---

## 3. Gesprächsflüsse

### Reservierung
Pflicht: **Datum und Uhrzeit · Personenzahl · Name · Rufnummer**

Die Rufnummer kommt aus der Rufnummernerkennung: `agent/state.py` legt sie zu
Gesprächsbeginn in `slots.phone`, der Agent fragt nicht danach. Nennt der Gast von
sich aus eine andere, gilt diese. Nur bei unterdrückter Nummer wird gefragt (Maxi,
PR #127). Das gilt für Reservierung und Abholung.
```text
→ check_slot
   frei      → create_reservation → vorlesen → Ja → confirm
   belegt    → Alternativen aus dem Tool anbieten (max. 2) → erneut check_slot
   geschlossen → `say` vorlesen (fragt nach anderer Uhrzeit, keine Alternative) → erneut check_slot
   nichts    → create_callback
   team_callback → the core creates the callback itself (the tool's sentence, then the callback's), the call ends; without a number it hands over like every handover (transfer, or the sentence that nobody can be reached)
   too far ahead → read out `say` (names the limit, asks for an earlier day) → check_slot again
```
`team_callback` and the horizon are the restaurant's limits for a reservation by phone (#252, `service_config`, docs/04 §check_slot): above 8 guests, and on the same day above 5, the agent books nothing and the team calls back to confirm and talk it through; no table more than 90 days ahead. The callback after `team_callback` is created by the conversation core in the same turn, not by the model (`agent/loop.py`, docs/04 §check_slot): the model never sees that result, and neither the system prompt nor the tool reference says anything about it.

`check_slot` bietet nur Termine im selben Service an (T-1.14, docs/04 §check_slot). Außerhalb der Öffnung gibt es keine Alternative: der Satz sagt "geschlossen" und fragt nach einer anderen Uhrzeit, das ist kein Fall für `create_callback`. Morgens vor der ersten Öffnung kommen Termine aus einem späteren Service desselben Tages, ab sechs Stunden Abstand mit Tageszeit ("abends um sieben").

### Abholung
Pflicht: **Positionen mit menu_item_id · Name · Rufnummer**
```text
je Position → search_menu
   exact_number / alias / fuzzy_single → übernehmen
   ambiguous → nachfragen, max. 3 Optionen vorlesen
   not_found → Verständnis-Leiter
   wish → note / option übernehmen, unknown nicht anbieten, allergy ohne Zusage
→ draft_order → readback vorlesen → Ja → confirm → Abholzeit und Code nennen
```

**Wünsche (T-4.10, D8):** Was die Karte als Option kennt, nimmt der Agent auf
und sagt den Aufpreis gleich mit („Nummer 47 Ente knusprig mit Nudeln, 3 Euro
Aufpreis"). Weglassen („ohne Karotten") wird notiert und wiederholt. Alles
andere bietet er am Telefon nicht an. Fragt der Gast, warum etwas mehr kostet,
nennt er nur den Grund aus der Karte (`reason`) - fehlt er, steht der Preis so
in der Karte. Er erklärt einmal, sachlich, und verhandelt nicht; besteht der
Gast darauf, ist es eine Beschwerde und geht ans Team.

### Lieferung
Zusätzlich: **Adresse**
```text
find_customer hat Adresse  → "Wieder an …?" → Ja genügt
sonst                      → PLZ → Straße (buchstabieren) → Hausnummer (Tastatur)
→ check_delivery
   out_of_zone    → Abholung anbieten
   below_minimum  → Fehlbetrag nennen, nachbestellen lassen
→ draft_order → vorlesen → Ja → confirm
```

### Auskunft
Öffnungszeiten, Liefergebiet, Wartezeit → aus `get_service_status` und `check_delivery`. Danach: „Möchten Sie gleich bestellen?"

### Beschwerde, Mensch-Wunsch, Storno
```text
sofort → transfer_to_team
   niemand frei → create_callback mit Zusammenfassung → zusagen, wann zurückgerufen wird
```
Kein Beschwichtigen, kein Ausfragen, keine Zusagen. Der Agent nimmt auf und gibt ab.

### "Sind Sie ein Mensch?"
```text
question for a human alone → fixed sentence of the core, no model call: "Ich bin ein KI-Assistent, kein Mensch. Möchten Sie lieber mit jemandem aus dem Team sprechen?"
   yes in the next sentence → transfer_to_team (human_requested)
   anything else            → the call goes on, the model sees the offer in last_say
question with a request   → "Ich bin ein KI-Assistent, kein Mensch." and the model's answer to the request, no offer
```
The AI Act asks for a truthful answer every time (docs/09 §1 L1), so the code gives it before the model, like the greeting (T-11.5, `agent/escalation.py` `without_human_question`, `agent/loop.py`). Only the question order counts, and a statement with "oder?" behind it ("Sind Sie ein Mensch?", "Spreche ich mit einer Maschine oder mit einem Menschen?", "Ist da ein Mensch?", "Sie sind ein Roboter, oder?"); a phrasing the list misses goes to the model, which says in its first sentence that it is an AI (the prompt has no room for more, `test_prompts_v2.py` budget). Greetings around the question ("Hallo, sagen Sie mal, ...") are no request; any other word or a digit is, and the model answers it after the truthful sentence. "Ich will mit einem Menschen sprechen" stays the wish of §4 and goes to the team at once. The triggers of §4 are read in the rest of the sentence, without the question's own words: a complaint, a cancellation or the wish for a human next to the question goes to the team as well, with the truthful sentence first. The offer stands while it is the last thing the guest heard: any sentence after it, the phone line's "Sind Sie noch da?" too, takes it back, and a "Ja" then answers that sentence. The question gets the answer only within the call limit; past `max_call_seconds` the truthful sentence comes before the handover of the timeout.

### Allergie
```text
get_item_details mit allergen_question: true
   known: true  → Codes nennen, aber keine medizinische Aussage
   known: false → the core says the say of the tool and hands the call to
                  the team, a callback when nobody answers; the call ends,
                  no confirm (T-11.1, L17); with no number known, the core
                  asks for one first, and the answer makes the callback

Gast fragt über die gepflegten Werte hinaus (L17, D24): Spuren,
Kontamination, Kontakt, Fritteuse oder dasselbe Öl, in jedem Anruf; "wirklich
sicher", "hundertprozentig", "garantieren" in einem Anruf, der von Allergien
spricht
   → the core, before the model: the same sentence, the same handover;
     an order that stands unplaced goes onto the card and the guest hears
     "Ihre Bestellung ist damit noch nicht aufgegeben."; the card names the
     question and the dish, never the guest's allergy

Jede andere Rückfrage (Optionen, Extras, Beschreibung):
   get_item_details mit allergen_question: false

Gast nennt eine eigene Allergie ("ich vertrage keine Erdnüsse")
   → Hinweis an Position oder Vorgang, Wortlaut fest (E14):
     "WICHTIG: Keine <Zutat>. Grund: Allergie"
   → beim Vorlesen wiederholen, nie zusagen, das Gericht sei frei davon
   → a removal said with it stays, after it (#252):
     "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln"
   → the core reads it from the guest's own sentence, every turn, and
     writes it onto the draft itself (T-11.1); the model never writes an
     allergy into a note, a draft or cart with one is refused
   → on the dish when a clear search found the dish of its sentence; said
     without a dish, with an unclear one, or the dish no longer ordered:
     `note` of draft_order for the whole order (D24); on a table: its `note`
   → not read in full ("gegen Erdnüsse allergisch und gegen Sesam", or a dish
     in front that read whole would count as an allergen): "Wogegen sind Sie
     allergisch?"; the core reads the answer, "Keine." takes it back

Before the first draft that needs it (T-11.1, L13), the core holds
draft_order or create_reservation and asks itself:
   an allergy not read in full → "Wogegen sind Sie allergisch?"
     an answer it reads  → on to the next question or the draft
     anything else       → the draft is dropped, the model reads the
                           sentence; the next draft is held again
   an allergy of the caller or their child, consent not asked yet →
     "Darf ich die Allergie als Hinweis für die Küche notieren?"
   explicit "ja"     → the draft with the fixed wording, read out by the core
   "nein"            → the draft with "ohne <Zutat>" only, read out by the core
   anything else     → no consent; the draft is dropped, the model reads the
                       sentence, and the next draft carries "ohne <Zutat>"
   asked once per call, the answer holds for every note of the call and is
   logged (`consent_log`: call, time, question, answer); it counts only as
   the answer to the question itself: after "Sind Sie noch da?" the
   question is put again

Another adult's allergy ("meine Frau hat eine Erdnussallergie")
   → "ohne Erdnuss" only, never the reason, never asked (L13);
     the caller's own child counts as the caller ("mein Sohn ...")

Gast verneint eine Allergie ("ich habe keine Erdnussallergie")
   → no note (#252)
```

The allergy and the consent are the core's, not the model's (`agent/allergies.py`, `agent/loop.py`, redesign of 26.10.2026 after five review rounds on PR #368 that each found another note wording which lost a restriction or put it on the wrong person): the core keeps each allergy as structured state (ingredient, whose, dish) read from the guest's sentence with the parser of the menu search, any sentence of the call, also one said while a draft waits for a question. The model's notes carry only other wishes. A yes to the consent question is as strict as the yes to a read-back, with the words that say "note it" besides ("Ja, notieren Sie das"); "Ja, und noch eine Cola" is no explicit consent. A handover card never quotes a guest sentence that names an allergy. Whose allergy it is, is read on the words of the allergy's clause (`of_another_adult`): a person after "mein", "unser" ("meine Frau"), one on their own ("ein Freund", "jemand") or the subject "er", "sie", "wir"; a list of persons, closed, so a person it does not name keeps the allergy as the caller's own.

---

## 4. Eskalation: die Auslöser

Sofort und ohne Diskussion:
- Kunde sagt Beschwerde, Reklamation, Ärger, „will jemanden sprechen"
- Änderung oder Storno einer laufenden Bestellung
- Große oder ungewöhnliche Bestellung (Vorschlag: über 150 € oder über 20 Positionen)
- Catering, Feier, Sonderwunsch außerhalb der Karte
- Allergie ohne gepflegten DB-Wert, and any allergen question beyond the maintained values (traces, contact, a promise of safety): the core hands over before the model and before any `confirm` (T-11.1, L17)
- Dritter gescheiterter Versuch auf derselben Verständnis-Stufe
- Anrufdauer über `max_call_seconds` (on the phone line the tenant's value from `service_config`, read at the start of the call)
- The guest waits longer than `turn_deadline_seconds` for the answer to one sentence (#252; the tenant's value, read with the same routing; phone line only). The guest hears the outage sentence of §6, not a new one. Once the voice layer can say "Bitte warten Sie einen Moment" while a turn runs (T-1.11), the value goes from 10 to 15 in the database (Maxi, 12.10.2026); the code does not change for that

---

## 5. Token-Disziplin

| Hebel | Wirkung |
|---|---|
| Menü als Index, Details per Tool | größter Hebel, spart ~90 % des Menü-Anteils |
| System-Prompt unter 800 Tokens | wird bei jedem Zug mitgeschickt. The own core sends more than the file: system prompt, tool reference and answer format are about 2,240 estimated tokens together in a call about a table and about 2,430 in a call about an order, where the answer format also explains the cart and names the slots (07.10.2026, two formats, see below), with a budget of 2,600 for the whole message (Maxi, 06.10.2026; `agent/prompt.py` `CORE_TOKEN_BUDGET`, tested). To be shrunk with the numbers of the first eval run |
| Kompakter Bestellstatus statt Gesprächsverlauf | der Verlauf wächst linear, der Status nicht |
| Formulierungen für heikle Fälle als `say` aus dem Code | kürzere Antworten, konstante Wortwahl |
| Prompt-Caching, falls die Plattform es kann | System-Prompt und Index werden zwischengespeichert |
| Kleinstes Modell, das die Evals besteht | Modellwahl ist ein Messergebnis, keine Meinung |

**Bestellstatus statt Verlauf** — nach jedem Zug wird der Stand kompakt zusammengefasst:
```json
{ "intent": "delivery", "customer": "bekannt, Rheinstraße 54",
  "items": [{ "id": "…", "n": 2, "opt": "Erdnuss" }],
  "open": ["Rufnummer bestätigen"], "stage": "readback_pending" }
```

Nur ein vorgelesener Entwurf ist bestätigbar. Korrigiert der Gast nach dem Vorlesen
und scheitert die Korrektur (`draft_order` oder `create_reservation` mit Fehler) oder
beginnt eine neue Slotprüfung (`check_slot`) oder eine neue Suche in der Karte
(`search_menu`), fällt der alte Entwurf aus dem Zustand
und `stage` geht zurück auf `collecting`: ein späteres Ja kann ihn nicht mehr
bestätigen (`agent/state.py`, Codex PR #127). Eine Frage zu einem Gericht
(`get_item_details`, etwa nach Allergenen) ändert nichts: der Entwurf bleibt
bestätigbar, eine andere Option geht nur über `draft_order`.

**A correction at the read-back (Maxi, 12.10.2026).** The read-back is where every card
number is confirmed before anything is booked, so a wrong one has to be correctable there:
"Falsch, ich meinte die Nummer 25c". One card number named clearly replaces the one dish
it can stand for, the one with the same digits ("25c" for a 25d that was read back, "S12"
for the 12) or the only dish of the order. Quantity and note for the kitchen stay, a
quantity said with the correction counts ("nein, zweimal die 24", "nein, zwei Nummer 24"),
the rest of the order stays, a mandatory choice of the new dish is asked, and the new order is
read back in full before a yes. The draft that was read back
first stays a draft. Behind the no the sentence has to be the card number alone: anything
else (two numbers, a number that is not on the menu, two dishes the number could stand for,
a time such as "um 13 Uhr", one more dish with "und die 13" or "und noch die 13", a dish
taken away with "nicht", a name, a bare no) starts the order again, as before: never a guess.
A corrected dish that is out today, or a number the menu does not have, is said with the
sentence of the search before the order starts again, and a lookup that fails without an
answer hands the call to the team (rule 5). The scripted stand-in does this since 12.10.2026
(`sim/scripted_order.py` `_correction`, eval `abholung_0079`); a real model is told the same
by the system prompt ("Ändert der Gast etwas, lege den Vorgang neu an"), and the core drops
the old draft whichever of them drives the call.

**Read back means spoken, and a question is no yes (#251, 12.10.2026).** Both are held by the core, not by the prompt:

- A draft counts as read back when the last thing the guest heard was its `readback` and the question for the yes, and nothing else (`guards.note_said`, `consent.read_out`). Three parts, on the words alone, case and punctuation do not count: the words of the draft in order and without a gap (a read-back that leaves the name out or says another number is not the draft); behind them the closing question, the draft's own ("Passt das so?") or one of a few fixed forms ("Ist das so richtig?", "Stimmt das?"; not "Ist es sicher?", not "Ist es richtig sicher?", not "Möchten Sie noch einen Hochstuhl?", and not nothing at all); in front of them nothing, polite words ("Gerne, ...") and at most one whole lead-in phrase ("Ich wiederhole: ...", "Zur Kontrolle: ..."), never a single word of one ("Noch ein Tisch ..." is another table) and never a sentence of its own ("Das ist falsch: ..."). Only the last thing said counts: after a side question the model answered, the draft has to be read out again, on its own. Until it is heard `confirm` is refused, and while the draft waits the state the model gets carries the sentence as `readback`, because a model keeps nothing between turns; once heard, and once booked, it is not sent again. A new draft has to be read out again.
- `is_yes` (`agent/consent.py`) refuses a sentence with a question mark and the word order of a question without one: "Ist das richtig", "Stimmt das so", "Passt das", "Ist alles richtig". Speech recognition often drops the mark. The verb has to open its clause for that: after an assent or a thanks ("Ja, ist das richtig") it still does, behind any other word it comes second and the sentence is a statement ("Natürlich ist das richtig", "Das ist es"). "Das stimmt so", "Passt." and "Ja, passt alles." stay a yes. "Alles richtig" has no verb and "Ist so richtig" no subject: each is a question as often as an answer, and a yes only next to a word that cannot ask ("Ja, alles richtig", "Ja, ist okay").
- After a refused yes the core reads the draft out itself (12.10.2026, finding on PR #258). A `confirm` on a yes for a draft that is not what the guest heard last is refused, and the turn ends right there with the draft's sentence (`guards.reread_on_yes`, `agent/loop.py`); the yes to it books. The model is not asked again: the stand-in handed such a call to the team, and a model that answers "gebucht" or asks in its own words would keep the guest in refused yeses. Nothing that stands next to that `confirm` is taken (a `cart`, a state patch, a report to the understanding ladder): the guest said yes and nothing else, and each of them could drop the draft or end the call before the question is put. The call's time limit is still checked first. On any other sentence than a yes the refusal goes back to the model as before ("Was soll ich ändern?"). The eval recorder knows the case by the same rule: such a `confirm` is a refused attempt, and the guest's next sentence shows that the draft was read out; a result that comes back for it instead is judged as usual. Eval cases `reservierung_0032` and `reservierung_0033`.
- The eval recorder (`evals/recorder.py`) measures with the same two functions: a `confirm` after a draft that was not read out last is a hard violation when it went through and a refused attempt when the core stopped it.

**What a real model gets (T-2.4)** — the own core does not use the function calling of a
platform. The system message is `prompts/system_vN.md`, then the tool reference (the
sections of `prompts/tools_vN.md`, one per tool, without the file's notes for the voice
platform), then the answer format below. The compact state carries `now`, the local date
with weekday ("Dienstag, 2026-09-15T18:00+02:00"): a model has no clock, and "morgen um
sieben" or "am Samstag" need one. It is the same clock the tools get. While a guest turn
runs over several tool calls, the input of a request is the last tool result; the state
then also carries `guest_said`, the sentence the turn began with, and `called`, the names
of the tools the model has called in this turn (a refused call and a refused `cart` included). Both are gone
with the next turn. Without `called` a real model asked `get_service_status` again on its
own result until the hop limit (seen 07.10.2026).

**What stays across turns for a model that keeps nothing (first full eval run, 13.10.2026).**
Two fields of the state, both of constant size, so the state still never grows into a
transcript:

- `last_say`: the one sentence that ended the last turn, at most 300 characters, a longer
  one with its end and the closing question kept
  (`agent/state.py` `LAST_SAY_MAX`, set in `guards.note_said`), replaced with every turn.
  The guest's next words answer it. Without it a real model searched "Auf den Namen
  Mueller" on the menu, said goodbye on "Nein, das wars" with an empty order, and answered
  "Ja, passt" with a confirmation and a pickup code it made up. A state without `last_say`
  is the start of a call, which is the only place the model greets (and not at all with
  `greeted`). An offer of several dishes is therefore in sight for the one turn that
  answers it, by name and card number, never with an id; the order still takes only dishes
  the core knows (`guards.take_cart`).
- `service`: `{"is_open": true, "pickup_enabled": true}` once `get_service_status` answered in this call, both flags or nothing (pickup can be off while the restaurant is open, Codex PR #291). A model
  asked for the status at the start of every turn, one request each time, and then answered
  from the status instead of calling `check_slot`. The prompt asks for the status before an order
  when `service` is missing, again on a question about closing time or waiting time (the
  state keeps only the two flags, Codex PR #291), and not for a table: `check_slot` knows
  the opening hours.

**Answer format of the own core (T-2.4)** — a real model behind `agent/llm.py` gets the
system prompt, the compact state and one input per call, never a transcript. It answers
with exactly one JSON object; the instruction for it (`OUTPUT_FORMAT`) is appended to the
system prompt by the client and is not part of `prompts/system_vN.md`:
```json
{ "say": null, "tool": "check_slot", "args": { "party_size": 4, "reserved_for": "…" },
  "slots": { "party_size": 4 }, "not_understood": null }
```

| Field | Meaning |
|---|---|
| `say` or `tool` + `args` | the sentence for the guest, or a tool call. The format asks for exactly one. When a model sends both, the tool call wins and the sentence is dropped (Maxi, 06.10.2026): a real model does it on its first turn, and the model speaks again once it has the tool result. A readback with `confirm` in the same answer is stopped by the core (guards below), not by the format |
| `slots` | what the guest named in this turn, under the names the core takes (`guest_name`, `phone`, `party_size`, `reserved_for`, `note`; the format for an order lists them, since a real model wrote `customer_name` there and lost the name); goes into the compact state, anything not written here is gone on the next turn. Empty values (`null`, `""`) are dropped, so a model that fills unused fields cannot erase a known phone number. A `guest_name` or `phone` the guest did not say is dropped as well (guards below) |
| `cart` | only in the format for an order (below). The whole order as it stands after this turn, lines in the form of `items` of `draft_order` (`menu_item_id`, `quantity`, optional `options` and `note`); `null` when nothing changed. A field left blank (`null`, `""`, `{}`) says nothing; a list is the order, also the empty one: `[]` empties it, for the guest who removes the last dish. Anything else (`false`, `0`, a text) is refused like a cart in a wrong form (Codex PR #237). The core takes it only when every dish is one an order may name (guards below). The format says that a cart is no order yet: only `draft_order` creates one and delivers the readback, and `confirm` needs an `order_id` in the state. Without that sentence a real model called `confirm` on its cart as soon as the guest had nothing more to add |
| `not_understood` | the name of the detail that was not understood; the code counts it on the understanding ladder (§2) |

**Two formats, chosen by the state (07.10.2026)** — `OUTPUT_FORMAT` is the short contract
above without `cart`. `ORDER_FORMAT` is the same text with the field `cart`, its rules and
the names of the slots. The client sends `ORDER_FORMAT` only while the state carries
`cart`, which it does from the first menu search of a call on, also while the order is
still empty. A call about a table never searches the menu and gets the short format, as
before the cart existed. The reason is a measurement on a local model
(`qwen3.6-35b-a3b`, deterministic for a fixed prompt): the six reservation cases passed
6 of 6 with the short format, 4 with the slot names added, 3 with the cart text and 1
with both. A reservation needs neither addition, so it does not pay for them, in tokens
(about 2,240 estimated against 2,430) or in cases. The format stands at the end of the
system message, so the long part before it stays the same for a provider's prompt cache.

**The order in the compact state (T-2.4)** — a model keeps nothing between turns, so the
order it has understood so far has to stand in the state it gets back:
```json
{ "stage": "start", "open": [], "slots": { "phone": "+4972215551234" },
  "cart": [{ "menu_item_id": "…", "quantity": 2, "number": "47", "name": "Ente knusprig",
             "open": [{ "group": "Fleisch", "options": ["Ente", "Huhn"] }] }] }
```

The model writes dish, quantity, options and note of a line; the state adds `number`,
`name` and `open` from the search result. `open` names mandatory option groups nothing was
chosen from yet, with the names to choose from and no price. A `name` or a price a model
writes into a line is dropped. A successful `draft_order` makes its `items` the cart; a
confirmed order empties it.

Only the order goes back to the model, not what else a search delivered. A first version
also showed the dishes a search had found that were not in the order yet (the candidates
of an open question, a clear hit not taken up). On a real model the candidate the guest
had **not** chosen stayed in sight, and two turns later the model ordered it
(`abholung_0072`, 07.10.2026). The price: an answer like "die erste" to an offer cannot be
resolved from the state, the model searches again with what the guest said, or asks.

**What the guest called for in the compact state (T-2.4, 08.10.2026)** — `intent` says
what the call is about, `pickup` or `reservation`. A model cannot write it (it is no guest
slot), and until a draft existed the state of the next turn did not carry it: on a real
model "Es zwölf bitte", one turn after "ich möchte etwas zum Abholen bestellen", was read
as a table for twelve. The core hears the wish in the guest's own sentence before the
model is asked (`agent/intent.py`), the way the escalation triggers of §4 are heard:

| The sentence names | `intent` |
|---|---|
| a pickup ("abholen", "Abholung", "abgeholt", "mitnehmen") | `pickup` |
| a table ("Tisch" and its plurals as a whole word, "reservieren" and "Reservierung" anywhere; "Asiatisches", a guest named Tischler or an address at a "Platz" name none) | `reservation` |
| an order and nothing else ("bestellen") | `pickup`, unless something table-like is named, compounds included ("einen Vierertisch bestellen" says nothing). With "Tisch" it is a reservation. Until delivery is built an order by phone is a pickup (T-6.5) |
| both (a pickup word next to anything table-like, compounds such as "Vierertisch" and "Platz" included), neither, a delivery ("liefern"; an order "nach Hause" without a pickup or table word), or a word that rules something out ("nicht", "kein", "nein", "ohne", "statt" as whole words; "ohnehin" rules nothing out) | nothing |

Only the first wish of a call is taken: it fills an empty `intent`, and a word in a later
sentence never replaces it ("dürfen wir unseren Hund mitnehmen?" in a call about a
table). It is never taken against what the state shows already: with an order in the
state (`cart`) no table is heard, with a party size or a time in `slots` no pickup, and
after a menu search without either no table. The wish is noted before a call is handed
over on an escalation trigger of §4, so a call that goes to the team in that sentence is
logged with it; not on a complaint, where the order word would make a complaint about an
order a pickup. From the draft on `intent` follows the tools as before, and it is what the call
log says the call was about. A guest who changes their mind before a draft keeps the
first wish in the state until a tool of the other flow drafts; the model reads the
change in the sentence itself. It is a hint for the model,
not a decision: which tool is called stays with the model, and the guards below do not
read it. It costs no prompt text; the field stood in the state before. The rule is strict
on purpose: a wish that is wrongly heard stands in every turn after it, a wish that is
missed ("ich hole es dann ab", "haben Sie noch Platz?") leaves the state as it was.
Words are not understanding: "ich wollte meine Jacke abholen" as the first such
sentence of a call is heard as a pickup. If the call log shows that often, the model
writes the wish instead, which needs a line in the answer format and a run against the
baseline. Not measured on
a real model yet: due with the first full run, against the baseline of `main`.

An answer outside this format, a timeout or an unreachable model is an outage: the core
says the outage sentence (§6) and hands the call to the team, it does not ask the model
again. An answer is limited to `LLM_MAX_OUTPUT_TOKENS`; one that is cut off there is no
valid JSON and counts as outside the format. Tool names and arguments are checked by `agent/dispatch.py`, not by the format.

**The core holds the hard rules itself** (`agent/guards.py`, `agent/state.py`) — the prompt
asks the model to follow them, the code does not rely on it. Before a tool call is
dispatched, the loop checks:

| Rule | What the core checks | If not |
|---|---|---|
| 3, nothing without a yes | `confirm` needs an explicit yes (`agent/consent.py`) in the guest's current sentence, to the draft that was read back **before** this turn. A draft built or replaced inside the turn was never read to the guest; a yes in the same sentence does not count. A yes is a sentence that is nothing but the assent: an assent word plus words that carry no order ("Ja, gerne", "Passt so, danke"). "Ja, und noch eine Cola" or "Ich hätte gerne noch eine Suppe" is the start of a change | `confirm` is not dispatched, the draft stays a draft |
| 2, never guess | `draft_order` takes a `menu_item_id` only from a clear match of `search_menu` in this call (`exact_number`, `alias`, `fuzzy_single`), or from a candidate of an unclear result that the guest heard: the sentence that ended a turn named it by its full name, as whole words ("Reis" is not named by "Preis"), or as "Nummer <card number>". A model that keeps the offer to itself makes no candidate usable | `draft_order` is not dispatched |
| 2, for the order across turns | the `cart` of an answer is taken whole or not at all: every line must have the form of an order item and name a dish as above, and none that was sold out at its last search (a clear hit that is sold out is still a hit; in the state it would stand like any other line until `draft_order` refuses at the end). The options of a line are checked the way `draft_order` checks them, against what the search delivered for the dish: each must exist, none twice, at most one from a mandatory group (a wrong one would stand as the choice and `open` would be gone). And a `draft_order` in the same answer must draft that cart: with other items it is refused. Otherwise a dish nobody searched for would stand in the state, where the next turn reads it as found | nothing of the answer is carried out, neither its sentence nor its tool call; the order in the state stays as it was |
| 3, for a changed order | a `cart` that differs from the order that was read back (the sequence of lines and of options does not count, nor case and spacing of an option, as in `draft_order`: a model may repeat the order the other way round or write "huhn" for "Huhn"), or one that was refused, is a correction: the draft that waits for its yes is dropped and `stage` goes back to `collecting`. That holds for a reservation that was read back as well: the guest who goes on with the order has moved on, as with a menu search after a readback (Codex PR #237) | a later yes can confirm neither the order the guest just changed nor a table the conversation has left |
| 2, for the guest's name and phone number | a name or a number a model writes is taken only as the guest said it in this call (`agent/guest_details.py`; the rules and their limits are below the table). A name: every word of it was a whole word of a guest sentence, as often as the name holds it, or was spelled letter by letter in one sentence (§2, rung 3), and the name holds nothing but those words. A number: it is the caller ID, or the last digits of a guest turn that names them as a number to call or answers the agent's question for one; compared in E.164, so another spelling counts and a missing or changed digit does not. The core hears both in the guest's sentence before the model is asked and keeps the words and numbers of the call, no sentence. The number the guest gave is written into `slots` by the core itself, and `create_reservation`, `draft_order` and `create_callback` book only under the number that stands there | in `slots` a name or number the guest did not say is dropped without a hint, the slot stays as it was (an invented number never replaces the caller ID) and it is no success on the ladder; a tool call is not dispatched, and the hint says to ask the guest (for a number: under which number with its area code they can be reached), or to take `phone` from `slots` digit for digit |
| 1, facts from the database | a model writes only guest details into `slots` (`GUEST_SLOTS`: party size, date and time, name, phone, note). A tool result copied there (`open`, `closes_at`, a price) is dropped | the field never reaches the next prompt |
| 3, for what the guest hears | the core says every booking itself: when `confirm` goes through, the turn ends with a sentence built from the confirm result (what was booked, waiting for the team or not, the name the order runs under, never the pickup code (Maxi, 23.10.2026); `loop._booked_say`) and a goodbye, and the model is not asked. That sentence stays in the state as `booking_say`. A model sentence with a booking word (bestätigt, gebucht, reserviert, Abholcode, Abholnummer, any ending) or a code ("Code" with a token like A1, s12, 8842, "B 99") is refused, also denied or asked, unless it is `booking_say` word for word; a new draft or a cancelled replay takes `booking_say` back (`guards.false_booking`). Fifteen Codex rounds on PR #347 showed that every rule that judged whether a model sentence about a booking was true let a false one through | the sentence is not spoken; the model gets it back as a failed result under the name `say` with a hint |

A refused call goes back to the model as a failed tool result with `error_code` and a
`hint` that says why (a refused order under the name `cart`, a refused sentence under `say`, neither a tool; a `confirm` with no draft at all names the tool that makes one, a booking without a phone number asks for it; a callback with nobody's number known goes to the team where it is reachable, and otherwise counts on the ladder for `phone` once the model has asked for the number); it stands in `calls.tool_calls` like any failed call and costs one
tool hop, so a model that insists ends in the handoff to the team. Which of several
offered dishes the guest's answer means is left to the model and measured by the evals;
the readback and its yes come after it. A `transfer_to_team` the model calls and the team takes ends the call at once, as the core's own handoff does. Points in time of a tool result reach the model in the restaurant's local time, as `now` does. The other way round, a `reserved_for` the model writes without an offset is read as the restaurant's local time (`dispatch.in_local_time`, like `core/time.to_utc` for the GUI); the HTTP tools still require an offset. The call time limit is read again when the
model has answered: an answer that arrives too late is neither spoken nor dispatched,
but what it heard (a phone number) is taken first, for the callback of the handoff.

**How a name and a number are heard, and the limits (T-2.4, 12.10.2026)** — it is words,
not understanding, and strict on purpose: what it misses costs the guest one more
question, what it wrongly took would book under a stranger.

*The name.* Every word of it was a whole word of a guest sentence, as often as the name
holds it, or the guest spelled it: a row of single letters with nothing but blanks, commas,
hyphens or periods between them ("M E I E R", "M-E-I-E-R", "M. E. I. E. R.", "M wie
Martha, A wie Anton"). Another word ends the row, and a letter that belongs to a card
number or hangs on an apostrophe is none of it (the a of "23a" and of "23 a", the s of
"ich bin's"). The name a model writes must be those words and nothing else, with one
separator between two of them: a blank, a hyphen, an apostrophe in either form, a comma or
a period before a blank. "Müller 999", "---Müller---", "Müller.", "Müller Müller" after
one "Müller", and a name with a line break in it are refused. Letters are compared in one
Unicode form, so an "ü" that arrives as u plus a mark of its own is the same letter. Which
of the said words is the name stays with the model, the readback follows. A title the
guest did not say ("Herr Müller" after "Müller") is refused like any added word.

*The number.* Digits are a number to call only when the guest's sentence says so, by a
word for it ("meine Nummer ist", "Telefon", "Handy", "erreichbar", "Rückruf", "Vorwahl")
or by "unter" right before them ("bitte unter 0171 2345678"), or when the agent's last
sentence asked for the number; then the answer needs no such word. "Zu meiner alten
Bestellung 0171 2345678 habe ich eine Frage" names no number to call, and neither does a
"Bestellnummer". Of such a turn only the last digits can be the number, and only when they
begin with the zero of an area code or the plus of a country code and are a valid number.
Digits that are followed by more digits are none: the number may go on behind them ("0171
234 äh 5678", "0171 234. 5678", "0721 555 und dann 1234" begin with a valid number that
has digits missing) or it ended and they are something else ("0171 2345678 - 19 Uhr
passt" read as one is a number nobody said). The digits do not say which, so the guest is
asked again and says the number alone. The same holds when other digits of the turn may
be the head of the number, because they begin with a plus, with a zero and have three
digits or more, or are six or more ("0171 20, 034567" and "0043, 0664 1234567" end with a
valid German number nobody said; of two numbers in one turn the core picks neither), and
when one or two digits stand behind a hyphen, a slash or a closing bracket after what is a
complete number already ("0171 2345678-19 Uhr": an extension or an hour). A head it does
not tell as one leaves the tail standing ("49 171, 0345678", or a head in the turn before;
Open Points). A count, a date, an hour or a dish before the number does no harm ("für 4 am
16.09., meine Nummer ist 0171 2345678", "am 01 10 2026, meine Nummer ist ..."). The trunk
zero in brackets belongs to its number ("+43 (0)664 1234567"). Inside a number may stand
blanks, a slash or a hyphen without blanks, brackets around the area code and a period
between digits; a comma, a period before a blank, and a dash or slash next to a blank end
it. A date (also one written with blanks, "01 10 2026"), a time, a run too short or too
long, and a number without its area code are no phone number.

*Whose decision the number is.* The number the guest gave goes into `slots` by the core's
own hand, in E.164, before the model is asked: a guest who names another number than the
one they call from is reached under that one (§3), whether or not the model writes it
down. A tool books under the number in `slots`, so a model can neither leave the guest's
number out and book under the caller ID nor book under one it made up. A model may still
set `phone` to the caller ID or to a number the guest gave, for a guest who says "nehmen
Sie doch die Nummer, von der ich anrufe". When the number changes while a draft waits for
its yes, the draft is dropped like after any correction: it was made under the other
number and read out without it, so a later yes would book under the one the guest
replaced.

*Not heard, each costing one more question and never a booking:* a number said in words
("null eins sieben ..."), a number without its area code or with a country code and no
plus, a number named without a word for it before the agent has asked, a number followed
by other digits in the same turn or named next to a second one, one written with a dash or
a slash between blanks ("0721 / 5551234") or named in two turns, an extension of one or
two digits behind a hyphen ("0721 5551234-0"), letter names when spelling ("Doppel-L",
"Ypsilon"), a name with another accent than it was said with, and a spelling the agent
proposed and the guest only agreed to ("Maier mit a-i?" - "Ja"). How numbers and letters
arrive as text is decided by the ear of the voice layer (T-10.5); the check is extended
there.

*Not stopped:* blanks alone do not tell where a number ends, since numbers are said in
groups: "0171 2345678 19 Uhr" is one number of thirteen digits and stands in `slots` as
that. And a number the guest names for another reason, with a word for it ("meine alte
Nummer war ..."), is taken for the one to call. A number taken from speech is not read
back today, which would close both (Open Points in `docs/01_STATUS.md`). A name is known
from guest sentences only: once a known caller's name comes from the database
(`find_customer`, stage 3), that name has to count as well. No prompt text changed: the
model learns of the rule from the hint of a refused call.

---

## 6. Ansagetexte (Entwurf, C1 prüft rechtlich)

**Begrüßung**
> „Guten Tag, hier ist der KI-Assistent von <Pilotbetrieb>. Hinweise zum Datenschutz finden Sie unter example.com/datenschutz. Was kann ich für Sie tun?"

On the phone the code says this sentence before the first turn (`telephony/handler.py`, name from `tenants.name`), so the disclosure never depends on the model. The state then carries `greeted: true`, and the model does not greet a second time (T-1.13). The middle sentence names where the full privacy notice is (docs/09 §1 L4, T-11.5): the address comes from `service_config.privacy_notice_address`, never from the code; `example.com/datenschutz` is the repository placeholder. While the field is empty the greeting leaves that sentence out. The text phone and the evals keep the model's own first sentence.

**Answer to „Sind Sie ein Mensch?"** (`agent/loop.py` `SAY_NOT_A_PERSON`, `SAY_TEAM_OFFER`, draft; §3)
> „Ich bin ein KI-Assistent, kein Mensch. Möchten Sie lieber mit jemandem aus dem Team sprechen?"

**Mit Aufzeichnung** (nur wenn der Rechts-Check das trägt)
> „Guten Tag, hier ist der KI-Assistent von <Pilotbetrieb>. Das Gespräch wird zur Qualitätssicherung aufgezeichnet. Wenn Sie das nicht möchten, verbinde ich Sie mit einem Mitarbeiter. Was kann ich für Sie tun?"

**Weiterleitung**
> „Ich verbinde Sie mit einem Mitarbeiter, einen Moment bitte."

**Rückruf**
> „Ich habe Ihr Anliegen notiert. Ein Mitarbeiter ruft Sie unter dieser Nummer zurück."

**Ausfall**
> „Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie direkt mit dem Restaurant."

As the first sentence of a call, before the greeting (the database down at the start, a call the process does not know; `telephony/handler.py` `SAY_OUTAGE_FIRST`, draft), the AI says it is one first (L1 of docs/09), without the restaurant's name, which may not be readable then:
> „Hier ist der KI-Assistent des Restaurants. Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie direkt mit dem Restaurant."

**Nobody reachable** (`agent/loop.py` `SAY_NOBODY_REACHABLE`, draft): the core gives up, the team is not reachable and there is no number for a callback. No sentence that promises the team is spoken then.
> „Ich kann Ihnen gerade leider nicht weiterhelfen und erreiche im Restaurant niemanden. Bitte rufen Sie später noch einmal an."

**Nobody answered the team** (`telephony/handler.py` `ANNOUNCEMENTS`, drafts, T-10.8): the AI handed the call to the team, and nobody answered at `TEAM_PHONE`. Asterisk plays it from a file `voice` renders at its start, so it is heard with `voice` down too, and only to a caller the AI answered: one who never heard the disclosure above (paused, shadow, `voice` not reached) hears nothing (docs/20 §5). The first only once the callback card is written, the second on anything else:
> „Im Restaurant hat gerade leider niemand abgenommen. Ich habe Ihre Nummer notiert. Das Restaurant ruft Sie so bald wie möglich zurück. Vielen Dank für Ihren Anruf. Auf Wiederhören."
> „Im Restaurant hat gerade leider niemand abgenommen. Bitte rufen Sie später noch einmal an. Vielen Dank für Ihren Anruf. Auf Wiederhören."

**Silent line** (`telephony/handler.py` `STILL_THERE`, drafts): the caller has said nothing since the agent's last sentence ended. The code asks twice, then says the farewell and hangs up (docs/20 §4). A yes to one of these questions confirms no draft.
> „Sind Sie noch da?"
> „Ich höre Sie leider nicht. Sind Sie noch dran?"

**Verabschiedung**
> „Vielen Dank für Ihren Anruf. Auf Wiederhören."

The line is hung up only after a goodbye (Maxi, 06.10.2026). The code says this sentence before every hangup unless the agent's last sentence already parts ("bis dann", "bis gleich", "Auf Wiederhören"), so it is never said twice (`telephony/handler.py`). A transfer needs no goodbye: the transfer sentence comes before it.

Hinweis: Alle Texte gehen vor dem ersten echten Anruf durch den Rechts-Check (`docs/09_OPERATIONS_LEGAL.md`).
