# Skeptic E: menu matching and wishes, main @ 69ef3678

Worktree /home/user/rev-n. Every probe in probes-e/ re-run: 56 red, 6 green, as the notes say.

## Does the input reach the parser on the real path?

- `prompts/tools_v3.md` (PROMPT_VERSION v3, `api/agent/prompt.py:14`) for search_menu: "sofort, wenn der Gast ein Gericht
  oder eine Nummer nennt, mit seinem ganzen Satz", "`query` (das Gesagte, so wie es kam)". The core parses the caller's
  raw ASR sentence as the model forwards it. Nothing in `loop.py` or `guards.py` rewrites the query.
- The reviewer's probes call `domain.menu.search.search_menu`. The agent goes through `agent/dispatch._search_menu`, which
  runs `position_parts` first and adds the echo (`say_understood`). I re-ran every input through
  `dispatch(..., "search_menu", {"query": ...})`: probes-e/test_skeptic_e_dispatch.py. Every finding holds there with
  the same outcome. Echo texts are quoted below.
- Read-back (`domain/ordering/readback.py:_spoken_line`): "<N>mal Nummer <n> <name> mit <option names>, <note>." plus the
  total. It names options without their price and reads a note word for word. Nothing in `ordering/` or `guards.py` checks
  a note against the dish's options (`guards.notes_an_additive` only looks for additives). `unnoted_allergy`
  (`guards.py:719`) passes any note that matches `HEALTH_NOTE_RE`.
- Pre-merge trees (git archive of each merge's first parent, run in scratchpad): pre-#720 14 of 18 red (the swap-count
  cases were green), pre-#721 14 of 19 red (the 5 correction cases were green, so that one is a regression), pre-#722
  15 of 19 red, pre-#735 0 of 6 red (a regression). Most findings are fixes that stayed incomplete, not regressions.
- Line refs in the notes for `_classify`/`_split_addition` are about 50 lines off. They sit at wishes.py:1928 and :2004.

### [P1] An addition after a removal without mit/extra/statt/dafür is a free note
Verdict: CONFIRMED P1
Evidence: dispatch "die 47 ohne Zwiebeln, dazu Tofu" gives exact_number 47, wish note "ohne Zwiebeln, dazu Tofu". The echo
is "Nummer 47, ohne Zwiebeln, dazu Tofu, sehr gern." With Nudeln in front it gives option Nudeln +300 and note "ohne
Zwiebeln, plus Tofu". The read-back reads the note ("..., ohne Zwiebeln, dazu Tofu."), so the guest hears the Tofu and says
yes. The total lacks the 1,50 and nothing names it. The read-back does not catch it. docs/04 §Wünsche says it plainly:
"Weglassen zusammen mit einer Zugabe ... wird nie zur freien Notiz". This was already red before #720 (incomplete fix).
Fix: in `_classify`, when the removal part (or an option's note) names an option of the dish that no ohne/kein governs,
the wish becomes `unknown` or `open` (asked), whatever joiner stands in front. Keep "ohne Zwiebeln und Tofu" as a note or
ask, never an option. · Test: the parametrised probe plus "ohne Zwiebeln und Tofu". · Needs Maxi: no

### [P1] A count behind the new swap words is dropped: two Tofu charged once, nothing asked
Verdict: CONFIRMED P1 (new in #720: before it the whole tail was a free note, which was wrong too)
Evidence: dispatch "die 47 ohne Zwiebeln, dafür 2x Tofu" gives option Tofu +150 and note "ohne Zwiebeln". The echo is
"Nummer 47, ohne Zwiebeln, mit Tofu, ein Euro 50 Aufpreis." No count reaches the wish. draft_order cannot hold Tofu twice
(docs/04: the same option twice is `invalid_input`), so the only correct outcomes are an ask or unknown. The read-back
catches it only partly. If the model turns the "2x" into the dish quantity, "Zweimal Nummer 47" is audible. If not, "mit
Tofu" hides the count and the order is 1,50 short. docs/04: "Steht im Wunsch eine Zahl ..., gilt Regel A".
Fix: `_drop_quantity.dish_count` keeps a count behind `_ADD | _SWAP` (and the joiners of the finding above) in the wish,
so `has_number` asks. · Test: probe test_count_behind_a_swap_word_is_asked. · Needs Maxi: no

### [P2] A removal swapped into a removal keeps the first one only in `.note` of a kind-`note` wish
Verdict: DOWNGRADE to P3
Evidence: dispatch "die 47 ohne Koriander, lieber ohne Zwiebeln" gives wish note text "ohne Zwiebeln" and note "ohne
Koriander". The echo is "Nummer 47, ohne Zwiebeln." There are two readings. "lieber ohne Zwiebeln" is a natural
self-correction ("rather without onions"), and the echo matches that reading. Under the other reading, tools_v3 tells the
model "Ein Weglassen zur Zugabe steht in `wish.note` und gehört ebenfalls in `note`", so both can reach the draft. Nothing
is priced and no allergy is involved. The `_with_allergy` head loss needs an allergy in the same sentence too. Rare and
ambiguous input. The defect is only the echo.
Fix: when the swapped part is itself a removal, do not cut, and keep one note with both. · Test:
classify_wish("ohne Koriander, lieber ohne Zwiebeln").text holds both. · Needs Maxi: no

### [P1] The "card number beside more" guard cuts only at comma/und/sowie: "Pho Bo äh die 23" takes Pho Bo
Verdict: CONFIRMED P1 (AGENTS.md:93, code that picks a best-guess item). Lower impact: the echo names the wrong dish.
Evidence: dispatch, 9 of 9 give fuzzy_single 13. Example: "Pho Bo äh die 23" gives "Alles klar, Nummer 13 Pho Bo." The 23
is dropped without a word, but the echo and the read-back both say "Nummer 13 Pho Bo", so a guest who meant the 23 can
object. Inputs are realistic: evals/ hold "Nummer äh 23" and "und äh zwanzig" without commas, and ASR punctuation gives
"Die 23. Pho Bo.". Already red before #721: #676 item 2 was fixed only at the separators, so this is not a regression.
Fix: in `search_menu`, before the name search, ask (SAY_WHICH_NUMBER) whenever the stripped query held a card-number span
with an article or marker that is not the quantity, whatever stands between. · Test: probe
test_a_card_number_beside_a_name_without_comma_asks. · Needs Maxi: no

### [P1] A wish in the sentence bypasses the guard: the corrected dish ends up in the kitchen note
Verdict: CONFIRMED P1. Lower impact: the echo reads the odd note aloud.
Evidence: dispatch "die 23 ohne Zwiebeln, nee, Pho Bo" gives exact_number 23, note "ohne Zwiebeln, nee, Pho Bo". The echo
is "Nummer 23, ohne Zwiebeln, nee, Pho Bo, sehr gern." "die 23 äh Pho Bo ohne Koriander" gives fuzzy_single 13 with the
note "ohne Koriander", and the 23 is gone. The echo and the read-back say the note word for word, which a guest is likely
to notice, but the kitchen note names a second dish. The reviewer says #721 "explicitly routes" these here. That part is
overstated: all 5 cases were already red before #721. The defect holds.
Fix: `_search_with_wish` (or `wish_candidates`) treats a removal segment that holds a correction or alternative word or a
hesitation followed by a dish name or card number as no note. The sentence asks. · Test: probe
test_a_wish_does_not_bypass_the_card_number_guard. · Needs Maxi: no

### [P1] Regression: a correction word outside the list now books both numbers ("die 23, ne, die 24")
Verdict: CONFIRMED P1 (regression of #721)
Evidence: pre-#721 tree: 5 of 5 green. Now dispatch gives positions [23 exact, "ne" not_found, 24 exact] and the echo
"Alles klar, Nummer 23 und Nummer 24." (same for quatsch, sorry, Moment). "die 23, doch die 24" gives the 23 alone, echo
"Nummer 23, sehr gern.", so the correction is lost. "nein" and "nee" still ask. Partial catch: the "ne" part carries its
own not_found say, which the model should ask about, and the read-back lists both dishes.
Fix: in `_position_parts.whole`, a non-dish, non-number piece between two card-number pieces keeps them together (ask), as
before. Do not rely on growing ALTERNATIVE_WORDS. · Test: probe test_a_correction_between_two_numbers_is_not_two_positions,
plus "doch". · Needs Maxi: no

### [P2] "die 23 statt Pho Bo" / "die 23 oder Pho Bo" asks only about Pho Bo
Verdict: CONFIRMED P2 (docs/21 lifts it to P1 as xhigh code. Coordinator's call: it asks, nothing is silent.)
Evidence: dispatch "die 23 statt Pho Bo", "die 23 oder Pho Bo", "die 23 plus Pho Bo" and "Pho Bo mit der 23" all give
ambiguous [13] "Meinen Sie Nummer 13, Pho Bo?". For "plus" and "mit der" the guest meant two dishes, so a "ja" drops the
23. The read-back lists Pho Bo alone, which can still catch it. Already red before the PR.
Fix: the same card-number-beside-more ask as above (the guard fix covers it). · Test: each raises Ambiguous with "Welche
Nummer". · Needs Maxi: no

### [P1] A prefixed or category-word number with a unit is read as 40+2: "Sushi vierzig zwei Portionen" is SM1
Verdict: CONFIRMED P1 for the category-word cases. The prefix-letter cases are P2 (safe side).
Evidence: dispatch "Sushi vierzig zwei Portionen", "Sushi zwanzig zwei Portionen" and "Sushi vierzig 2x" give fuzzy_single
sm1, echo "Alles klar, Nummer sm1 Sushi-Menü klein." That is a wrong clear dish (16,90 against 4,50), which the echo and
the read-back name. "S 20 2 Portionen" gives ambiguous with 12/13/23 offered: wrong candidates, and a "ja" books one.
"S40 zwei Portionen", "die S40 ...", "S40 zwei mal" and "S20 2x" give not_found "Können Sie mir die Nummer sagen?" (asked,
nothing booked). "Nummer S40 zwei Portionen", "zweimal S40" and "zwei Portionen S40" are correct. The S prefix is the
menu's main format (docs/14). Already red before #722.
Fix: in `_scan`/`_counts`/`dish_ends`, a menu prefix or a category word directly in front of the digits counts as a dish
marker for both guards. · Test: the two probe tests (never sm1, never not_found). · Needs Maxi: no

### [P1] "die 23 x die 24" takes the 24 with quantity 23, nothing asked
Verdict: CONFIRMED P1. Lower impact and low plausibility.
Evidence: dispatch "die 13 mal Nummer 12" gives exact_number 12, echo "Gern, Nummer 12." All four probe forms do the same.
The echo drops the 13 without a word. If the model takes 13 as the quantity, the read-back "13 mal Nummer 12" makes it
audible (cap 30). If it takes 1, the 13 is lost. The phrasing "die N mal die M" is rare in speech, but #677 item 2 asked
for exactly this class, and `_bare_number_at` (Codex f25451ba) narrowed it. Already red before #722.
Fix: in `sole_item_number`, after "mal"/"x" behind an article-number, a second number with an article or marker also asks.
Keep "2 x 7up" and "zwei mal Acht Schätze" as they are. · Test: probe test_article_number_times_article_number_asks.
· Needs Maxi: no

### [P2, counts as P1] "Nummer 40 zwei bitte" is dish 42 (a count without a unit word)
Verdict: CONFIRMED P2 (counts as P1 by docs/21. The wrong dish is named in the echo.)
Evidence: on a menu with 40 and 42, dispatch gives exact_number 42, echo "Alles klar, Nummer 42.". On a menu without a 42
it is not_found "Die Nummer 42 habe ich nicht auf der Karte" (asked). The caller said 40 and hears 42, so the echo and the
read-back can catch it. "vierzig zwei" as 42 is a deliberate join, so the reading is genuinely two-way. How often the ear
drops the comma is unmeasured.
Fix: in `_scan`, behind a marked tens number, a unit digit followed only by filler or bitte asks. · Test: probe
test_tens_number_with_a_bare_count_is_not_42. · Needs Maxi: no

### [P1] A per-portion addition ("zweimal die 47, einmal mit Tofu") lands on every portion
Verdict: CONFIRMED P1
Evidence: dispatch gives exact_number 47, option Tofu +150, echo "Gern, Nummer 47 mit Tofu, ein Euro 50 Aufpreis.". The
model sets quantity 2 from "zweimal", and the read-back "Zweimal Nummer 47 Ente knusprig mit Tofu." does not say that only
one portion takes it. The guest is charged 1,50 too much and the kitchen adds Tofu to both portions. The read-back does
not catch it. "zwei 47er, einmal mit Tofu" is not_found (asked). Already red before #720.
Fix: `_drop_quantity`: a count or article-count ("einmal", "eine") right before an addition, after a dish quantity, stays
in the wish so it asks. · Test: probe test_a_per_portion_addition_is_not_put_on_every_portion. · Needs Maxi: no

### [P2] #671's own example "die 23 mit 2x Ei" ends NotFound, not asked
Verdict: CONFIRMED P2 (safe side: nothing booked wrong. The guest hears "nicht gefunden" for a clear number.)
Evidence: dispatch "die 47 mit 2x Tofu" gives not_found "Das habe ich auf der Karte nicht gefunden. Können Sie mir die
Nummer sagen?". "Nummer 47 mit 2x Tofu" asks "Welche Nummer meinen Sie?". It costs one wasted turn and is likely to repeat.
Fix: in `search_menu`, when `has_number` skips the wish branch and an article-number is present, raise the Rule-A
Ambiguous, not the name search. · Test: probe test_the_issue_example_without_nummer_is_asked_not_lost. · Needs Maxi: no

### [P1] An allergy said before the denial is dropped: "Ich habe eine Nussallergie! Ich habe keine andere Allergie, nur meine Frau gegen Sesam" is Sesam only
Verdict: CONFIRMED P1 (regression of #735: pre-#735 tree 6 of 6 green, the open allergy was asked)
Evidence: dispatch "die 13, ich vertrage keine Nüsse. Ich habe keine Allergie, nur meine Frau gegen Sesam" gives
exact_number 13, wish allergy "WICHTIG !!! Allergie: Sesam", ingredient Sesam. The same holds for the Nussallergie,
Erdnuss and Zöliakie variants. tools_v3 tells the model to put the `ingredient` in the note and "nie die Worte roh
notieren". That note passes `unnoted_allergy` (guards.py:729: any HEALTH_NOTE_RE match), so the kitchen never hears of the
nuts or the coeliac disease. The read-back says "Allergie: Sesam" only. A guest might notice, but nothing prompts them.
Fix: `_theirs_behind_a_denial` returns None when any text before the denial's clause (every sentence) `mentions_allergy`
or `speaks_of_health`. · Test: probe test_probe_e_735.py plus the health-word cases. · Needs Maxi: no

## Pattern

12 findings are confirmed or downgraded, not rejected: 9 P1 and 3 P2 (one P2 to P3). All 12 sit in functions that the #546
batch already patched. Rounds: round 1 is the ultrareview #546 issues (#671, #676, #677, #719), round 2 is the fix PRs and
their Codex rounds (#720 to #722, #735), round 3 is this review. Function names come from commit hunk headers, so treat
them as approximate.

- wishes.py `_classify` / `_split_addition`: #671 named them, #720 1e36272a and Codex afeadd33 patched them, now the free
  note returns (finding 1, and the P3). 3 rounds.
- wishes.py `_drop_quantity.dish_count` (with `has_number`): #671 named it, #720 1e36272a patched it, now 2 findings (swap
  count, per-portion), plus the NotFound P2 through `has_number`. 3 rounds.
- search.py `search_menu` (the beside-more guard), `_search_with_wish` and `_position_parts.whole`, with split.py
  `separator_pieces`: #676 named `whole()`, `_separable` and `search_menu`. #721 a018c2b5 plus Codex e5fe74ce and
  df685214 patched them. Now 4 findings (no-comma guard, wish bypass, the correction regression, statt/oder). 3 rounds,
  and one finding undoes behaviour the PR changed.
- numberwords.py `_scan` / `_counts` / `_quantity_spans` / `sole_item_number._bare_number_at`: #677 named them. #722
  e18eee12 plus Codex 3c29ca9d, 1c1aefcd, f25451ba and 699d229f patched them. Now 3 findings. 3 rounds.
- wishes.py `_with_allergy` / `_theirs_behind_a_denial`: #668 (#546 batch 2), #718/#719, #735 a23c477a plus 6 Codex
  commits, later eb96d204 and aa479aaf. Now 1 regression. At least 4 rounds.

So all five function groups meet docs/21's "same function 3 rounds in a row" trigger.
