# Area A review notes (allergens and menu), main @ 69ef3678

PRs: #747, #748, #751, #760. Notes appended as I go.

## PR #747 (guest hears the core's allergen sentence)

### [P1] Core allergen sentence replaces the model's only when the model set the flag
PR: #747 · File: api/agent/loop.py:628 and :840-847 (with api/agent/guards.py:693, api/agent/escalation.py:224) · Verified: probe
Trigger: (a) guest "Ist in den Frühlingsrollen Gluten?" (no allergy word, so `asks_about_allergies` is False and `flag_the_allergy_question` does not force the flag); the model calls `get_item_details` with `allergen_question: false`, reads `data.allergens` in the payload (`_tool_result_as_input` always sends it) and says "Die 23 enthält Gluten und Soja." (23 holds only A). (b) Turn 2 after a lookup in turn 1: "Und ist da auch Soja drin?" with no lookup in that turn; model says "Ja, die 23 enthält auch Soja."
Wrong: `allergen_says` is filled only from `told.startswith(SAY_ALLERGENS_LEAD)`, which exists only when the flag is true, and only for a lookup in the same turn. In both cases the guest hears the model's allergen list (an added soy here; a dropped one the same way). `allergen_claim` does not judge positive "enthält" sentences and `unread_allergy_question` is gated on `asks_about_allergies`, which needs an allergy word, so a plain allergen question ("Ist da Milch drin?", "Hat die 13 Nüsse?" all read False) is answered from the model with no lookup at all. Rule 1: allergen words heard that do not come from the DB. The #747 guarantee ("the allergens a guest hears come from the table of the core, whatever the model makes of the result") holds only on the model's cooperation.
Required: any successful `get_item_details` with `allergens.known` in the turn yields the core sentence when the final say names an allergen (build it from `result.data`, not from `say`), and a guest question that names an allergen next to a dish (`names_an_allergen` + `is_question`) forces the flag / counts for `unread_allergy_question`. Verification case: the two probe tests; plus "Ist in der Pho Bo Milch?" with no tool call and the model saying "Die Pho Bo enthält Soja." must not reach the guest.
Probe: probes-a/test_probe_a_747_flag.py (2 red)

## PR #748 (absence forms, health words, plural on cards)

### [P1] Common absence forms still pass `allergen_claim` ("In der 13 ist keine Erdnuss.")
PR: #748 · File: api/agent/guards.py:245-260 · Verified: probe
Trigger: model sentence, unit and loop level: "In der 13 ist keine Erdnuss.", "In der Pho Bo ist keine Milch.", "Milch ist da keine drin.", "Soja ist keins drin.", "Die 13 enthält Sesam nicht.", "Nein, Sesam hat die 13 nicht.", "Die 13 enthält weder Nüsse noch Sesam.", "Da kommen keine Nüsse rein.", "Nüsse? Nein, die sind nicht drin." (allergen in the sentence before the "?" split), "Da ist nix von Sesam drin.". In the loop: guest "Ist in der Pho Bo Milch?" (no lookup, `unread_allergy_question` silent, see #747 finding) and model "Nein, in der Pho Bo ist keine Milch." reaches the guest.
Wrong: `_ABSENT_IN_SENTENCE` lists `hat|haben|gibt|sind` but not `ist` (the most common form), `_ABSENT`'s `kein\w* \w+ (drin|darin|dabei)` needs a word between "keine" and "drin", "nicht" after the object ("enthält Sesam nicht", "hat die 13 nicht") is not read, "weder ... noch", "rein", "nix" are not read, and the "?" sentence split loses the allergen named in the guest-echo half. Absence of an allergen said by the model = rule 1 break (L17).
Required: these forms count as a claim; verification: the parametrized unit cases plus the three loop cases in the probe (13 red).
Probe: probes-a/test_probe_a_748_absence.py (13 red)
Note: after a same-turn flagged lookup the #747 replacement hides most of these from the guest, so the reachable path is the no-lookup / flag-false path of the #747 finding; fixing either closes the loop cases, the unit gap stays for notes (`claims_in_a_note` reuses `allergen_claim`: a draft note "In der 13 ist keine Erdnuss" is read back unrefused).

## PR #751 (a second person's allergy behind the caller's)

#788 deleted `api/agent/allergies.py` (`note_allergies`) and `wishes.theirs_behind`, the whole #751 fix. The kitchen note is now the model's (`guards.unnoted_allergy` + `with_the_guests_words`).

### [P1] #788 undid #751: second person's allergy dropped from the kitchen note
PR: #751 (regressed by #788) · File: api/agent/guards.py:719-735 · Verified: probe
Trigger: "Einmal die Pho Bo auf Müller. Ich bin allergisch gegen Erdnüsse, meine Frau gegen Sesam." and the model drafts with note "WICHTIG !!! Allergie: Erdnüsse"; same for "ich hab ne Nussallergie und mein Sohn verträgt keine Milch" / note "WICHTIG !!! Nussallergie"; and across turns: turn 1 "Ich habe eine Erdnussallergie.", turn 2 "... Und meine Tochter verträgt kein Sesam." / note "WICHTIG !!! Erdnussallergie".
Wrong: the net passes a draft as soon as any note matches `HEALTH_NOTE_RE` ("allergi", "wichtig !!!"); it never checks that the note covers each allergy in `state.allergy_said`. The draft is stored with only the caller's allergy; Sesam / Milch never reach the kitchen. Exactly the #751 defect (#670 point 2, #718), now model-dependent again; rule 2 spirit and the kitchen-safety purpose of the net. The same hole covers any later sentence (a second allergy of the caller told after a first note) because a redraft carrying the old note passes.
Required: the net accepts a draft only if its notes name every allergen / ingredient said (e.g. `allergens_named(_said(state)) <= allergens_named(notes)` plus the stem match `_names_health` already uses), else the hint / `with_the_guests_words`. Verification: the three probe cases must store Sesam / Milch.
Probe: probes-a/test_probe_a_751_second_person.py (3 red)

## PR #748, part 3 (plural on cards), as reworked by #788

#788 replaced #748's `_names_health` (stems of the noted `state.allergies` ingredient, any length >= 2) with stems of the guest's own sentences (`state.allergy_said`) that skip every word under five letters, plus `allergens_named` for LMIV names. #748's plural tests (`test_a_noted_plural_matches_its_singular_on_a_card`) were dropped with `state.allergies`. The plural cases themselves still hold (Erdnüsse/Erdnuss, Nüsse/Nuss, Ei, Haselnuss: checked).

### [P1] #788 undid #748 for short ingredients: the allergy goes on the callback card
PR: #748 (regressed by #788) · File: api/agent/loop.py:1556-1587 · Verified: probe
Trigger: turn 1 "Ich bin allergisch gegen Kiwi." (no team, CLOSED), turn 2 "Ich möchte bitte mit jemandem sprechen, ist im Obstsalat Kiwi?" and the model calls `transfer_to_team`. Stored callback card: "Kunde wollte mit einem Menschen sprechen. Kunde sagte: Ich möchte bitte mit jemandem sprechen, ist im Obstsalat Kiwi?". Unit level the same for Mais, Zimt, Mohn, Lamm, Reis, Hefe (any non-LMIV ingredient of four letters or fewer; LMIV names are caught by `allergens_named`). Weaker case, same function: "Mein Sohn hat Zöliakie." then a card "Rückruf: Gluten in der 13" also passes.
Wrong: `_stems` drops words under five letters, so the guest's ingredient is never matched; the card, kept for the callback period, carries the guest's allergy (health data, L13), while the kitchen note is deleted the night after. Same path for model-written callback summaries (`_card_without_allergy`), line notes and the order note on cards (`_order_lines`). The brief allows P0 for confidentiality; I rate P1 because the card stays inside the team's tablet, but it is a retention breach of Art. 9 data.
Required: the ingredient words of `allergy_said` (the word after "gegen/auf/kein", the stem before "allergie") are matched at any length; verification: the six unit cases and the handover-card case in the probe.
Probe: probes-a/test_probe_a_748_card_short.py (7 red)

## PR #760 (reason filler after an allergy list)

Fix intact on main: `_CLAUSE_WORDS` still holds deshalb/deswegen/daher/darum, its tests pass. `classify_wish` still feeds the search wish (`search.py` `say_for_wish`, `_with_wish`) and the wish text "WICHTIG !!! Allergie: {ingredient}" the model is offered for the note.

### [P2] Reason filler without a comma (speech output) still lands in the ingredient
PR: #760 · File: api/domain/menu/wishes.py:111-120, :558 · Verified: probe (direct calls)
Trigger: "ich bin allergisch gegen Nüsse deswegen nehme ich die 5" -> ingredient "Nüsse deswegen nehme ich die 5"; "allergisch gegen Nüsse und Sesam deshalb die 5" -> "Nüsse und Sesam deshalb die 5". With comma, other reason words: "drum die 5", "also die 5", "weshalb ich die 5 nehme", "weil ich die 5 nehme", "halt deshalb die 5", "sonst die 5", "dafür die 5" all stay in the ingredient.
Wrong: `_ends_list` reads clause words only after "und" or a comma; the speech engine sets commas as it likes (the code says so for "?" in `is_question`). The note offered to the model and read back says "Allergie: Nüsse deswegen nehme ich die 5"; the allergen itself is kept, so no allergen is lost (hence P2), but the dish "die 5" is swallowed by the wish text and the read-back is garbled. Shared with the older clause words ("dann", "aber" without comma behave the same), so the root cause predates #760; #760's fix covers only the comma form.
Required: a reason word (deshalb, deswegen, daher, darum, drum, also, weshalb, weil) ends the list also without a preceding comma; verification: the no-comma cases above give "Nüsse" / "Nüsse und Sesam".
Probe: none as file (direct `classify_wish` calls, reproduced in the notes above)

## Cross-cutting observation for the #788 reviewer (not one of my PRs)
`escalation.arms_the_net` (the only trigger of the kitchen-note net since #788) is False for "Mein Mann kann keine Milch ab.", "Mei Frau vertragt koa Sesam.", "Meine Tochter verträgt nix mit Nüssen.", "Meine Frau kriegt einen anaphylaktischen Schock bei Nüssen.", "Nüsse sind für meine Frau lebensgefährlich." (verified by direct call). Then the net is silent and the kitchen note is wholly the model's.

## Checked and clean

#747
- Same-turn flagged lookup: the model's dropped / added / renamed allergen is replaced by the core sentence (existing tests green on main).
- Two dishes in one turn: one sentence per dish, duplicates of the same lookup removed (`told not in allergen_says`).
- Sold-out text appended after the allergen sentence keeps the `SAY_ALLERGENS_LEAD` prefix, so it is still recognised.
- Guard order: `allergen_claim`, `unread_allergy_question`, `false_booking` all run before the replacement, so a claim beyond the table still goes to the team.
- Rule 3: a read-back the model said in the same turn is replaced, `note_said` records only the allergen sentence, so a later "ja" triggers `reread_on_yes` (safe side).
- Unknown allergens on the flagged path still hand over (`SAY_ALLERGENS_UNKNOWN` branch untouched).
- #788 did not touch the replacement logic (loop.py:628-633, :840-847 intact).

#748
- The forms #748 added are read: "Die 13 hat keine Nüsse", "Da gibt es kein Gluten", "Nüsse sind da nicht drin", "Da haben wir keine Nüsse drin", "keinerlei", "gar keine".
- `_ABSENT_IN_SENTENCE` stays per sentence: "Die Bestellung hat keine Getränke. Die 13 ohne Erdnüsse, notiert." is no claim (existing test green).
- `_food` strips the guest's own "keine Nussallergie" acknowledgement before the absence read.
- Plural to singular on cards (Erdnüsse/Erdnuss, Nüsse/Nuss, Ei, Haselnuss, Mandeln) still matched after #788 via `allergens_named` and the 5-letter stems.
- Health words (moved to #763 by #748): #788 added them to `HEALTH_NOTE` (Zöliakie, darf kein, meiden, reagier), so a note naming them is deleted by retention.

#751
- Bare "Nein." to "Haben Sie selbst eine Allergie?" keeps another person's sentence in `allergy_said` (#788 `_note_allergy_said`).
- Second person's sentence arms the net in the common forms ("meine Frau gegen Sesam", "darf keine", "reagiert auf"): the draft goes back once without any health note, and the core writes the guest's words on the second try (`with_the_guests_words`); the open hole is only "a health note exists but omits a person" (finding above).

#760
- Comma forms "deshalb / deswegen / daher / darum" and "und deshalb" end the list; "darum hätte ich gern die 5", "daher keine 13", "deswegen frag ich" give "Nüsse".
- The new words are also in `_NOT_STEM` via `_CLAUSE_WORDS`: no ingredient named like them, no side effect on compound stems found.
- "Ich habe eine Erdnussallergie und meine Frau Sesam" still leaves the ingredient open (test green); after #788 this sentence arms the net, so the note must name a health word.
