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
1. get_service_status
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
```
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

### Allergie
```text
get_item_details mit allergen_question: true
   known: true  → Codes nennen, aber keine medizinische Aussage
   known: false → say aus dem Code vorlesen → create_callback

Jede andere Rückfrage (Optionen, Extras, Beschreibung):
   get_item_details mit allergen_question: false

Gast nennt eine eigene Allergie ("ich vertrage keine Erdnüsse")
   → Hinweis an Position oder Vorgang, Wortlaut fest (E14):
     "WICHTIG: Keine <Zutat>. Grund: Allergie"
   → beim Vorlesen wiederholen, nie zusagen, das Gericht sei frei davon
```

---

## 4. Eskalation: die Auslöser

Sofort und ohne Diskussion:
- Kunde sagt Beschwerde, Reklamation, Ärger, „will jemanden sprechen"
- Änderung oder Storno einer laufenden Bestellung
- Große oder ungewöhnliche Bestellung (Vorschlag: über 150 € oder über 20 Positionen)
- Catering, Feier, Sonderwunsch außerhalb der Karte
- Allergie ohne gepflegten DB-Wert
- Dritter gescheiterter Versuch auf derselben Verständnis-Stufe
- Anrufdauer über `max_call_seconds`

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

**What a real model gets (T-2.4)** — the own core does not use the function calling of a
platform. The system message is `prompts/system_vN.md`, then the tool reference (the
sections of `prompts/tools_vN.md`, one per tool, without the file's notes for the voice
platform), then the answer format below. The compact state carries `now`, the local date
with weekday ("Dienstag, 2026-09-15T18:00+02:00"): a model has no clock, and "morgen um
sieben" or "am Samstag" need one. It is the same clock the tools get. While a guest turn
runs over several tool calls, the input of a request is the last tool result; the state
then also carries `guest_said`, the sentence the turn began with, and `called`, the names
of the tools the model has called in this turn (a refused call included). Both are gone
with the next turn. Without `called` a real model asked `get_service_status` again on its
own result until the hop limit (seen 07.10.2026).

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
| `slots` | what the guest named in this turn, under the names the core takes (`guest_name`, `phone`, `party_size`, `reserved_for`, `note`; the format for an order lists them, since a real model wrote `customer_name` there and lost the name); goes into the compact state, anything not written here is gone on the next turn. Empty values (`null`, `""`) are dropped, so a model that fills unused fields cannot erase a known phone number |
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
| 2, for the order across turns | the `cart` of an answer is taken whole or not at all: every line must have the form of an order item and name a dish as above, and none that was sold out at its last search (a clear hit that is sold out is still a hit; in the state it would stand like any other line until `draft_order` refuses at the end). Otherwise a dish nobody searched for would stand in the state, where the next turn reads it as found | nothing of the answer is carried out, neither its sentence nor its tool call; the order in the state stays as it was |
| 3, for a changed order | a `cart` that differs from the order that was read back (the sequence of lines and of options does not count, nor case and spacing of an option, as in `draft_order`: a model may repeat the order the other way round or write "huhn" for "Huhn"), or one that was refused, is a correction: the draft that waits for its yes is dropped and `stage` goes back to `collecting`. That holds for a reservation that was read back as well: the guest who goes on with the order has moved on, as with a menu search after a readback (Codex PR #237) | a later yes can confirm neither the order the guest just changed nor a table the conversation has left |
| 1, facts from the database | a model writes only guest details into `slots` (`GUEST_SLOTS`: party size, date and time, name, phone, note). A tool result copied there (`open`, `closes_at`, a price) is dropped | the field never reaches the next prompt |

A refused call goes back to the model as a failed tool result with `error_code` and a
`hint` that says why (a refused order under the name `cart`, which is no tool); it stands in `calls.tool_calls` like any failed call and costs one
tool hop, so a model that insists ends in the handoff to the team. Which of several
offered dishes the guest's answer means is left to the model and measured by the evals;
the readback and its yes come after it. The call time limit is read again when the
model has answered: an answer that arrives too late is neither spoken nor dispatched,
but what it heard (a phone number) is taken first, for the callback of the handoff.

---

## 6. Ansagetexte (Entwurf, C1 prüft rechtlich)

**Begrüßung**
> „Guten Tag, hier ist der KI-Assistent von <Pilotbetrieb>. Was kann ich für Sie tun?"

On the phone the code says this sentence before the first turn (`telephony/handler.py`, name from `tenants.name`), so the disclosure never depends on the model. The state then carries `greeted: true`, and the model does not greet a second time (T-1.13).

**Mit Aufzeichnung** (nur wenn der Rechts-Check das trägt)
> „Guten Tag, hier ist der KI-Assistent von <Pilotbetrieb>. Das Gespräch wird zur Qualitätssicherung aufgezeichnet. Wenn Sie das nicht möchten, verbinde ich Sie mit einem Mitarbeiter. Was kann ich für Sie tun?"

**Weiterleitung**
> „Ich verbinde Sie mit einem Mitarbeiter, einen Moment bitte."

**Rückruf**
> „Ich habe Ihr Anliegen notiert. Ein Mitarbeiter ruft Sie unter dieser Nummer zurück."

**Ausfall**
> „Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie direkt mit dem Restaurant."

**Nobody reachable** (`agent/loop.py` `SAY_NOBODY_REACHABLE`, draft): the core gives up, the team is not reachable and there is no number for a callback. No sentence that promises the team is spoken then.
> „Ich kann Ihnen gerade leider nicht weiterhelfen und erreiche im Restaurant niemanden. Bitte rufen Sie später noch einmal an."

**Verabschiedung**
> „Vielen Dank für Ihren Anruf. Auf Wiederhören."

The line is hung up only after a goodbye (Maxi, 06.10.2026). The code says this sentence before every hangup unless the agent's last sentence already parts ("bis dann", "bis gleich", "Auf Wiederhören"), so it is never said twice (`telephony/handler.py`). A transfer needs no goodbye: the transfer sentence comes before it.

Hinweis: Alle Texte gehen vor dem ersten echten Anruf durch den Rechts-Check (`docs/09_OPERATIONS_LEGAL.md`).
