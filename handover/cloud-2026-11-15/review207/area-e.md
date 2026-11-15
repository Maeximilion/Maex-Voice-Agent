# Area E: menu matching and wishes (#720, #721, #722, #735), main @ 69ef3678

Summary: P0 0, P1 9, P2 4 (one P2 counts as P1 on rule 2). Every finding is backed by a red probe.
P1: #720 x3 (addition behind a removal without mit/extra is a free note; count behind dafür/sondern dropped;
per-portion addition on every portion), #721 x3 (guard only at comma/und/sowie; a wish bypasses the guard;
regression "die 23, ne, die 24" books both), #722 x2 (prefixed/category number with a unit read as 40+2,
"Sushi vierzig zwei Portionen" is SM1; "die 23 x die 24" silent), #735 x1 (allergy before the denial dropped).

Probes live in `probes-e/` (copy into `api/tests/` of a worktree and run with
`python3 -m pytest -q -p no:cacheprovider api/tests/<file>`).

## PR #720 (#671): a priced addition in a wish never rides free in the note

### [P1] An addition after a removal without mit/extra/statt/dafür is a free note
PR: #720 · File: api/domain/menu/wishes.py:1876-1882 (`_classify` lead `_REMOVE`), :1952-1968 (`_split_addition`) · Verified: probe
Trigger: dish 47 with option Extra/Tofu +1,50. "die 47 ohne Zwiebeln, plus Tofu", "..., dazu Tofu",
"..., zusätzlich Tofu", "..., stattdessen Tofu", "..., aber Tofu", "..., Tofu dazu"; and behind an option:
"die 47 mit Nudeln, ohne Zwiebeln, plus Tofu" / "..., dazu Tofu".
Wrong: `_split_addition` only cuts at mit/extra/statt/anstatt and the three new `_SWAP` words, so
everything else behind "ohne" stays one removal. Search returns kind `note` text "ohne Zwiebeln, plus Tofu"
(or option Nudeln +300 with note "ohne Zwiebeln, plus Tofu"); dispatch treats a note as understood, nothing
in `ordering/` checks a note against the dish's options, so the Tofu reaches kitchen and read-back unpriced
(rule 1). The fix closed the example words of #671, not the class "a priced option of the dish named in the
removal part without a removal word in front of it".
Required: a note (also the note of an option) that names an option of the dish outside the reach of an
"ohne/kein" is `unknown`/asked, whatever joiner precedes it; verification: the parametrised probe below,
plus "ohne Zwiebeln und Tofu" (genuinely ambiguous: two removals or an addition) must not become an option.
Probe: probes-e/test_probe_e_720.py::test_priced_option_never_rides_in_a_note (8 of 9 red; the "2x Tofu"
case raises Ambiguous via has_number and holds)

### [P1] A count behind the new swap words is dropped: two Tofu charged once, nothing asked
PR: #720 · File: api/domain/menu/wishes.py:1352-1364 (`_drop_quantity.dish_count`) · Verified: probe
Trigger: "die 47 ohne Zwiebeln, dafür 2x Tofu", "..., dafür zweimal Tofu", "die 47 kein Huhn, sondern 2 Stück Tofu".
Wrong: `dish_count` keeps a count only when the word before it is in `_ADD` ("mit", "extra"); the same PR
made "dafür/sondern/lieber" open an addition (`_SWAP`) but did not add them there, so "2x"/"zweimal" is
stripped as a dish quantity before `has_number`, and the result is option Tofu +150 once, note "ohne
Zwiebeln", no ask-back. The count is also not reliably the dish quantity (the caller meant two tofu).
Same root as #671 point 3, which the PR fixed only behind "mit"/"extra".
Required: a count right behind any word that opens an addition (`_ADD | _SWAP`, and the joiners of the
finding above) stays in the wish and is asked; verification: probe below.
Probe: probes-e/test_probe_e_720.py::test_count_behind_a_swap_word_is_asked (3 of 3 red)

### [P2] A removal swapped into a removal keeps the first one only in `.note` of a kind-`note` wish
PR: #720 · File: api/domain/menu/wishes.py:1883-1887 · Verified: probe (classify_wish), read-back by reading
Trigger: "die 47 ohne Koriander, lieber ohne Zwiebeln", "kein Koriander, dafür ohne Zwiebeln".
Wrong: `_swaps` now splits there; the added part "ohne Zwiebeln" classifies as kind `note`, and the
model_copy puts "ohne Koriander" into `.note` of a kind-`note` wish. Every reader of a note wish uses
`.text` only (`removal_with` returns `wish.text`, `say_understood._with_wish` says `f"{item}, {wish.text}"`),
so the read-back says "Nummer 47, ohne Zwiebeln" and the head removals of `_with_allergy` lose "ohne Koriander".
Required: when the swapped part is itself a removal, the result is one note with both removals in `.text`
(or the swap is not cut); verification: classify_wish("ohne Koriander, lieber ohne Zwiebeln") has
kind note and text containing both.

## PR #721 (#676): a clear card number is never lost or invented in a split

### [P1] The "card number beside more" guard cuts only at comma/und/sowie: "Pho Bo äh die 23" takes Pho Bo
PR: #721 · File: api/domain/menu/search.py:785-792 (guard), split.py:44 `_SEPARATOR` · Verified: probe
Trigger (sushi test menu): "Pho Bo äh die 23", "die 23 äh Pho Bo", "die dreiundzwanzig äh Pho Bo",
"Die 23. Pho Bo.", "Pho Bo. Die 23.", "Pho Bo? Die 23", "die 23 - Pho Bo", "die 23 also Pho Bo",
"Pho Bo dazu die 23".
Wrong: each returns `fuzzy_single` 13 Pho Bo, no question, the 23 silently gone. #676 item 2 required
"a sentence with a card number that is kept whole asks"; the fix tests only for pieces cut at the
`_SEPARATOR` (comma, und, sowie), so a hesitation the ear writes without commas (the eval data has
"Nummer äh 23", "und äh zwanzig") or a full stop/question mark/dash between the parts bypasses it. A
number *behind* the name ("Pho Bo äh die 23") cannot be the quantity reading that excuses "die 23 Pho Bo".
Required: a sentence whose name search would strip a card number (any `_number_spans` token with article
or marker, not a quantity) asks; verification: probe below.
Probe: probes-e/test_probe_e_721.py::test_a_card_number_beside_a_name_without_comma_asks (9 of 9 red)

### [P1] A wish in the sentence bypasses the guard: the corrected dish ends up in the kitchen note
PR: #721 · File: api/domain/menu/search.py:712-723 (`_search_with_wish` returns before the guard at :785) · Verified: probe
Trigger: "die 23 ohne Zwiebeln, nee, Pho Bo", "die 23 ohne Zwiebeln, äh, Pho Bo", "die 23 ohne Zwiebeln,
oder Pho Bo", "S12 ohne Wasabi, äh, Pho Bo"; and "die 23 äh Pho Bo ohne Koriander" (13 clear, 23 lost).
Wrong: `exact_number` 23 with wish kind `note`, text "ohne Zwiebeln, nee, Pho Bo": the caller's
correction to Pho Bo is booked as the 23 with a kitchen note naming another dish; the dispatch repeats
it as understood. Same root as #676 item 2 ("Pho Bo, äh, die 23"), only with a removal in front; the
whole() change returns `[query]` whenever `wish_candidates(query)` is non-empty, so the PR explicitly
routes these sentences into the unguarded wish branch.
Required: a removal/wish segment that contains a correction or hesitation followed by a dish name or
card number is not a note; the sentence asks. Verification: probe below.
Probe: probes-e/test_probe_e_721.py::test_a_wish_does_not_bypass_the_card_number_guard (5 of 5 red)

### [P1] Regression: a correction word outside the list now books both numbers ("die 23, ne, die 24")
PR: #721 · File: api/domain/menu/search.py:395-421 (`whole()` cut at card numbers) · Verified: probe, old tree compared
Trigger: "die 23, ne, die 24", "die 23, quatsch, die 24", "die 23, sorry, die 24", "die 23, Moment, die 24"
(also "Entschuldigung", "Korrektur", "falsch", "Pardon", "nö"); "die 23, doch die 24" cuts out a clear 23 too.
Wrong: on af3883b3^1 these gave parts ['die 23, ne', 'die 24'] and the first part's search was NotFound
(asked). The new cut in `whole()` makes every card-number piece its own position, so they now give
['die 23', 'ne', 'die 24']: dispatch searches each, 23 and 24 are both `exact_number`, `say_understood`
repeats "Nummer 23 und Nummer 24", and the corrected 23 is in the order (rule 2; only the final
read-back can still catch it). `ALTERNATIVE_WORDS` gained only "nee"; a closed list cannot hold every
spoken correction, so the cut must not turn an unknown piece between numbers into a separator.
Required: a non-dish, non-number piece between two card numbers keeps them together (ask), as before
the PR; at minimum "ne"/"nö"/"Moment"/"sorry"/"quatsch"/"Korrektur" count as corrections. Verification: probe.
Probe: probes-e/test_probe_e_721.py::test_a_correction_between_two_numbers_is_not_two_positions (4 of 5 red;
"doch" passes the assert only because "doch die 24" is NotFound, the 23 is still taken)

### [P2] "die 23 statt Pho Bo" / "die 23 oder Pho Bo" asks only about Pho Bo
PR: #721 · File: api/domain/menu/search.py (fuzzy step after the guard) · Verified: probe
Trigger: "die 23 statt Pho Bo", "die 23 oder Pho Bo", "die 23 oder Ente", "die 23 plus Pho Bo", "Pho Bo mit der 23".
Wrong: `ambiguous` with only 13 ("Meinen Sie Nummer 13, Pho Bo?"): the guard does not fire (one piece),
the name search strips the 23 and offers the name hit alone; for "statt" the caller asked for the 23
*instead of* Pho Bo, so a "ja" books the dish they replaced. Asked, so not silent, but the number said is
missing from the question.
Required: the same `SAY_WHICH_NUMBER` ask (or both offered); verification: search on each raises
Ambiguous with "Welche Nummer".

## PR #722 (#677): a quantity and a card number never read into each other

### [P1] A prefixed or category-word number with a unit is read as 40+2: "Sushi vierzig zwei Portionen" is SM1
PR: #722 · File: api/domain/menu/numberwords.py:382-394 (`_scan` guard), :528-535 (`dish_ends`) · Verified: probe
Trigger: menu with S20, S22, S40, S42, SM1 (docs/14: S1-S53, SM1-SM6). "Sushi vierzig zwei Portionen" and
"Sushi zwanzig zwei Portionen" and "Sushi vierzig 2x" -> `fuzzy_single` SM1 "Sushi-Menü klein" (a wrong
dish, clear, no question). "S40 zwei Portionen", "die S40 zwei Portionen", "S40 zwei mal", "S20 2x" ->
NotFound; "S 20 2 Portionen" -> ambiguous 12/13/23.
Wrong: both new guards need `tokens[start-1] in _ITEM_NUMBER_MARKERS | _LINK_ARTICLES`, but in "S40" /
"Sushi vierzig" the token in front of the digits is the prefix or category word, so `_quantity_spans`
still reads "40 zwei Portionen" as quantity 42 (`find_quantity` = 42, also with "Nummer S40 zwei
Portionen"), the number span overlaps it and is dropped, `sole_item_number` returns (None, False), and the
name search runs on "sushi portionen". #677 item 1 required "a digit followed by a quantity unit is never
joined into the card number"; the T-4.12 prefix forms are the menu's main card format.
Required: a number with a menu prefix or category word in front counts as named as a dish for both guards;
verification: probe below (S40/S20 found or asked, never SM1, never NotFound).
Probe: probes-e/test_probe_e_722.py::test_prefixed_number_with_a_unit_is_the_dish (6 red),
::test_category_word_number_with_a_unit_never_takes_another_dish (3 red, 4 green)

### [P1] "die 23 x die 24" takes the 24 with quantity 23, nothing asked
PR: #722 · File: api/domain/menu/numberwords.py:1401-1418 (`_bare_number_at` condition) · Verified: probe
Trigger: "die 23 x die 24", "die 13 x die 12", "die dreizehn mal die zwölf", "die 13 mal Nummer 12".
Wrong: `exact_number` of the second number. #677 item 2 required "with an article or as the first number,
'<n> mal <m>' is ambiguous and asks"; the Codex follow-up (`_bare_number_at`) lets only a bare second number
ask, so an article or "Nummer" on the second number silently makes the first the quantity. Both carry the
card article here, so either can be the dish (rule 2).
Required: a second number with an article or marker behind "mal"/"x", after a first number with an article,
asks; "2 x 7up", "zwei mal Acht Schätze" stay name searches. Verification: probe below.
Probe: probes-e/test_probe_e_722.py::test_article_number_times_article_number_asks (4 of 4 red)

### [P2, counts as P1] "Nummer 40 zwei bitte" is dish 42 (a count without a unit word)
PR: #722 · File: api/domain/menu/numberwords.py:382-394 (`_counts` needs a `_QUANTITY_NOUNS` word) · Verified: probe
Trigger: "Nummer 40 zwei bitte", "die 40 zwei, bitte" -> `exact_number` 42 (with a comma after 40 it asks).
Wrong: the break needs a unit word after the digit; an elliptic count ("zwei bitte") keeps the
"vierzig sieben" join, so 40 + 2 = 42 without a question. Same root as #677 item 1; how often the ear drops
the comma in "Nummer vierzig, zwei bitte" is not checked against transcripts.
Required: behind a marked tens number, a unit digit followed only by filler is asked, not joined;
verification: probe below.
Probe: probes-e/test_probe_e_722.py::test_tens_number_with_a_bare_count_is_not_42 (2 of 2 red)

## PR #720, continued (found later in the run)

### [P1] A per-portion addition ("zweimal die 47, einmal mit Tofu") lands on every portion
PR: #720 · File: api/domain/menu/wishes.py:1352-1364 (`_drop_quantity`), wish_candidates segment · Verified: probe
Trigger: dish 47 with Extra/Tofu +1,50 and Beilage/Nudeln +3,00. "zweimal die 47, einmal mit Tofu",
"zweimal die 47, eine mit Nudeln".
Wrong: `exact_number` 47 with wish option Tofu +150 (Nudeln +300), no question: "einmal"/"eine" in front of
"mit" is the addition's count (one of the two portions), but `dish_count` keeps a count only *behind*
`_ADD`, so it is dropped as a dish quantity; the line of two then carries the option, and
`pricing.unit_cents * quantity` charges it twice and the kitchen makes both with it. Same class as #671
item 3 ("a count inside an addition reaches `has_number` and is asked about").
Required: a count or article-count right in front of an addition after a dish quantity asks;
verification: probe below.
Probe: probes-e/test_probe_e_720.py::test_a_per_portion_addition_is_not_put_on_every_portion (2 red)

### [P2] #671's own example "die 23 mit 2x Ei" ends NotFound, not asked
PR: #720 · File: api/domain/menu/search.py:712-723 with wishes.has_number · Verified: probe
Trigger: "die 47 mit 2x Tofu", "die 47 mit zweimal Tofu", "die 47 mit 2 Stück Tofu" (no "Nummer").
Wrong: `has_number` now skips the wish branch (intended), but without a marker `sole_item_number` returns
(None, False) and the name search over "mit tofu" raises NotFound "Das habe ich auf der Karte nicht
gefunden": the clear 47 is lost for this turn instead of the Rule-A ask. The PR's search test uses only
"Nummer 23 mit 2x Ei"; the issue's example is the article form.
Required: Ambiguous with SAY_WHICH_NUMBER (or the dish kept and the count asked); verification: probe.
Probe: probes-e/test_probe_e_720.py::test_the_issue_example_without_nummer_is_asked_not_lost (3 red)

## PR #735 (#719): an allergy behind the caller's own denial, as the code stands after #788

Context: since #788 the agent writes allergy notes itself; the core net (`guards.unnoted_allergy`) only
checks that *some* note matches `HEALTH_NOTE_RE`. The search's wish (`kind=allergy`, text
"WICHTIG !!! Allergie: X", say `SAY_ALLERGY_NOTE`, repeated by `say_understood`) is what the model is given
as the reading, so a confident wrong reading here is not caught by the net.

### [P1] An allergy said before the denial is dropped: "Ich habe eine Nussallergie! Ich habe keine andere Allergie, nur meine Frau gegen Sesam" is Sesam only
PR: #735 · File: api/domain/menu/wishes.py:1857-1925 (`_theirs_behind_a_denial`, called first in `_with_allergy` :1790) · Verified: probe (classify_wish and search_menu), net by reading
Trigger: "Pho Bo, ich habe eine Nussallergie! Ich habe keine andere Allergie, nur meine Frau gegen Sesam";
"die 13, meine Tochter hat eine Erdnussallergie. Ich habe keine Allergie, nur meine Frau gegen Sesam";
"Mein Sohn ist gegen Erdnuss allergisch. Ich habe keine Allergie, aber meine Frau gegen Sesam";
"Erdnussallergie. Ich habe keine Allergie, ..."; also health words: "Ich habe Zöliakie. Ich habe keine
Allergie, nur meine Frau gegen Sesam", "Ich vertrage keine Nüsse. ...", "Ich bin laktoseintolerant. ...".
Wrong: wish `allergy`, ingredient Sesam, text "WICHTIG !!! Allergie: Sesam" (through search: `exact_number`
13 / `fuzzy_single` 13 with that wish). The denial is judged only on `_SENTENCE_END.split(before)[-1]`, the
head removals are taken from `_clause_start(words, named[0])` of the *earlier* allergy word, and nothing
checks that the text in front of the denial's sentence holds no allergy or health word. With the path
switched off (monkeypatched to None) every one of these is an open allergy (ingredient None, whole text,
asked) - so #735 turned an ask into a confident reading that drops the caller's own nut allergy, the
daughter's peanut allergy or a coeliac disease. A draft with "WICHTIG !!! Allergie: Sesam" passes
`unnoted_allergy`, so the kitchen never hears of the nuts.
Required: `_theirs_behind_a_denial` returns None when any text before the denial's own clause (all
sentences, not only the last) mentions an allergy or health word (`mentions_allergy`/`speaks_of_health`);
verification: probe below plus the health-word cases.
Probe: probes-e/test_probe_e_735.py (6 of 6 red)

## Checked and clean

PR #720
- The four #671 examples and the Codex follow-up ("ohne Huhn, dafür Tofu, ohne Zwiebeln") give option or unknown, never a free note.
- "mit Nudeln, ohne Zwiebeln, mit Tofu, ohne Koriander" (recursion through both splits) ends unknown, no endless recursion.
- `_swaps` needs a non-comma word behind it: "ohne Zwiebeln, dafür" stays a note; "ohne Zwiebeln, lieber scharf" is unknown.
- `has_number` on "2x", "2 Stück", "dreimal", "drei Stück" true; "ohne eine Zwiebel" (article) still false; a count at the end ("mit Ei, zweimal", "mit Ei 2x") stays the dish quantity as designed.
- "zweimal die 23 ohne Zwiebeln mit extra Käse" and "die 23 zweimal ohne Zwiebeln mit extra Käse": option Käse +120, note "ohne Zwiebeln", quantity not in the wish - correct.
- "Tofu statt Huhn", "Tofu anstatt Huhn", "Tofu statt Huhn, ohne Zwiebeln": option Tofu with its price.

PR #721
- "die 23 und Ente", "S12 und Ente", "SM1 und Ente", "25G und Ente", "die 25 G und Ente", "die dreiundzwanzig und Ente", "Ente, die 23", lists of three: card numbers stay their own positions.
- The guard asks for "die 23, äh, Pho Bo", "Pho Bo, ähm, die 23", "die 23, nee, Pho Bo", "die 23, nein Pho Bo", "Nummer 23 Pho Bo", "S12 Pho Bo", "S12 äh Pho Bo" (prefix forms ask by design).
- Decimal comma: "Eine Cola 0,5", "die 23 für 8,50", "die 23,24" (asks), "Eine Cola 0,5 und eine Pho Bo" (two positions), "die 25,G" (asks 25/25g); "Pho Bo,23" still splits.
- "drei und zwanzig Portionen Pho Bo" (Codex) not cut by `cut_at_separators`; "Die 30, A." stays whole and asks.
- `_alias_items` loose match: "Sake 200 ml" with an alias only on 100 ml asks; exact alias still wins; an exact alias plus a loose one on another dish over-asks (safe side).
- `ALTERNATIVE_WORDS` now shared by split.py and numberwords.py (no drift); "nee" handled with and without commas.

PR #722
- "Nummer 40 zwei Portionen", "Nummer 40 2 Portionen", "Nummer 20 2 Portionen", "Nummer hundert zwei Portionen", "die 40 zweimal", "die vierzig zwei mal", "Nummer 40 zwei Stück/Stk/St/Portion", "zwo": dish 40/20/100, quantity 2.
- "vierzig zwei mal" without marker stays 42 portions (Codex PR #722 intent); "Nummer 40 zwei Teller/davon" asks.
- "die dreizehn mal zwei", "die 13 x 2", "13 x 2", "13x2", "die zwölf mal drei", "S12 x 2", "Nummer 13 x 2", "und/noch/dann die 13 x 2" ask; "zwei mal die 13", "zweimal 13", "die 13 2x", "2 x S12" take 13/S12 with quantity 2.
- Letters: "die 30, ah", "Die 30, A.", "die 30, a", "die 30, äh, a", "die 26, d", "die 25, G." offer both (heard); "die 30 a" and "die 25 g" are the lettered dish; "die 30, A, und die 26" asks.
- "hundertundfünfzehn", "einhundertundzehn", "zweihundertundeins", "hundertunddrei" read; "hundertund" is no number.
- "zwei 23", "zwei die 23", "die 2 und die 3", "die zwei und die drei": ask (no silent pick).

PR #735 (current code)
- The issue sentence and its variants (aber/nur/auch, "hat eine Sesamallergie", "ist gegen Erdnuss allergisch", child, removal before/after) give the right single allergen.
- A second allergen, a second person, "ich gegen Nüsse", a negation in the clause ("nicht gegen", "gegen keinen", "gegen Sesam nicht"), "gegen alles/Nichts", a dangling comma, "danke"/"glaube ich"/"oder?" and person words outside `_PERSONS` (Freundin, Freund, Kollegin) all fall back to the open allergy (asked).
- A removal behind ("ohne Erdnuss", "keine Nüsse bitte", "ohne Nüsse für sie") stays in the kitchen note text; "ohne Nüsse wegen ihr" and "..., ohne Gluten, sie hat Zöliakie" fall back (open).
- `_THEIRS.fullmatch` on bounded `[a-z]+` groups, no nested quantifiers: no backtracking risk.
