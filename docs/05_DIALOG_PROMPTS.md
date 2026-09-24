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
   nichts    → create_callback
```

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
| System-Prompt unter 800 Tokens | wird bei jedem Zug mitgeschickt |
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

---

## 6. Ansagetexte (Entwurf, C1 prüft rechtlich)

**Begrüßung**
> „Guten Tag, hier ist der KI-Assistent von <Pilotbetrieb>. Was kann ich für Sie tun?"

**Mit Aufzeichnung** (nur wenn der Rechts-Check das trägt)
> „Guten Tag, hier ist der KI-Assistent von <Pilotbetrieb>. Das Gespräch wird zur Qualitätssicherung aufgezeichnet. Wenn Sie das nicht möchten, verbinde ich Sie mit einem Mitarbeiter. Was kann ich für Sie tun?"

**Weiterleitung**
> „Ich verbinde Sie mit einem Mitarbeiter, einen Moment bitte."

**Rückruf**
> „Ich habe Ihr Anliegen notiert. Ein Mitarbeiter ruft Sie unter dieser Nummer zurück."

**Ausfall**
> „Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie direkt mit dem Restaurant."

Hinweis: Alle Texte gehen vor dem ersten echten Anruf durch den Rechts-Check (`docs/09_OPERATIONS_LEGAL.md`).
