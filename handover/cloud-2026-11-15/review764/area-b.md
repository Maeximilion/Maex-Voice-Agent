# Area B review notes (ledger #764): #744, #745, #749, #750 on main 69ef3678

Probes live in `probes-b/`; copy one into `api/tests/` of a checkout on main and run
`python3 -m pytest -q -p no:cacheprovider api/tests/<probe>.py` with testenv.sh sourced.

## PR #744 (modal wish word in the complaint gap)

### [P1] The order's own wish verb starts the complaint match; the order is lost
PR: #744 · File: api/agent/escalation.py:164 (`_WANTS_TO_COMPLAIN`), used at api/agent/loop.py:868 · Verified: probe
Trigger: guest at call start, phone known, a remedy rule on "kalt": "Ich möchte die 23, beschweren muss ich mich aber auch, die Pizza war kalt." (also "Ich nehme die 23, aber beschweren muss ich mich auch, ..."; and via the model's `transfer_to_team(reason=complaint)`: "Ich möchte die 23, außerdem beschwere ich mich, die Pizza war kalt.", "Ich bestelle die 23, außerdem reklamiere ich die Pizza, die war kalt.")
Wrong: `_OTHER_WISH` is only checked for the gap words. The leftmost match starts on the order's own verb ("möchte", "nehme", "bestelle", "hätte"), the dish and a connector fill at most four gap words, and the whole "möchte die 23, beschweren" is cut as the complaint's own wish. The remainder holds no wish word, `only_a_complaint` returns True, `_offer_remedy` offers the rule, and "Ja." books the remedy and ends the call (`ended=True`, audit row written): the 23 is never ordered and never reaches the team. Same impact as #738 (the issue #744 closes), the fix covers only a second wish word inside the gap. Hard rule 5 (the order must reach the team).
Required: a wish word whose own object stands between it and "beschwer"/"reklam" (anything but "mich/uns", reflexive filler, "gern/bitte", "über ...") must not be the complaint's wish; e.g. allow only reflexive/filler/"über"-phrase words in the gap, or require the gap to hold "mich|uns|sich" for a "beschwer" match. Verification case: the four sentences above go to the team (`SAY_ESCALATION`, no `remedy_offer`), while "Ich möchte mich über die Lieferung beschweren" and "Ich möchte das reklamieren" still get the offer.
Probe: probes-b/test_probe_b_744_order_verb.py (5 red: 2 core-path, 2 model-path, 1 full yes-ends-call)

## PR #745 (address forms cut before the yes and no readers)

No finding. Probed `is_yes`, `consent_answer`, `recording_answer`, `bare_recording_answer` on main
with 80 German variants (inline probe, results in "Checked and clean"). Nothing but an assent word plus
filler reads as a yes; "ja, äh, nein", "Herr Müller, ja", "nein danke, das passt so", "Ja, junge Frau,
ohne Zwiebeln", "Ja, für die junge Frau", "Ja, die junge Frau kommt mit", "Ja, guter Mann, aber zwei"
all stay no yes. The cut only removes words, so it can only make `asks()` detect more questions
("Ist junge Frau das richtig" now reads as a question, before it did not) and cannot glue a
`_HARMLESS` phrase ("nicht junge frau schlecht" leaves two spaces, no match).

## PR #749 (table read back kept on the model's own handovers)

No finding. `_card` takes `self._open_table(state) or table`, so the turn-start table is only a
fallback when the state no longer shows one. Every path the PR touched (model `transfer_to_team`,
ladder end, `_callback_without_number` card and its ladder end) gets it; the remaining handoffs without
it are the UNHEARD_TURNS end (state unchanged by definition, so the state's own table is there) and
the failed-tool-promises-team path (excluded on purpose, tool may have cancelled the table).

## PR #750 (after-midnight table named by its night)

### [P2] A draft made after midnight still says "heute"/"morgen" by the calendar
PR: #750 · File: api/domain/reservations/spoken.py:81-94 (`spoken_when`, its `today`), api/domain/reservations/create.py:159 · Verified: probe (wording), impact by reading
Trigger: the restaurant's evening runs past midnight (the #681 test assumes until 01:00). A guest calls on Wednesday 16.09.2026 at 00:15 local and books for the coming evening. The draft's `created_at` is Wednesday 00:15, so a table Wednesday 19:00 reads "Ein Tisch für zwei Personen heute um sieben Uhr abends", a table Thursday 19:00 reads "morgen um sieben Uhr abends".
Wrong: the PR's own rule ("the hours after midnight belong to the night that began the evening before", `NIGHT_ENDS`) is applied to the table's hour only, never to the hour the draft was made. At 00:15 most callers still live in Tuesday: "morgen" means Wednesday to them. A caller who says "morgen um sieben" is resolved by the model to the calendar Thursday, hears his own word "morgen" back, and the yes books Thursday, 24 hours late. The mirror of #742: the read-back, which is the consent (rule 3), echoes the ambiguous word and cannot catch the misunderstanding. #742 rated the same class P2 counted as P1 on this path (#681 convention).
Required: when the draft is made between 00:00 and NIGHT_ENDS local, name the day without "heute"/"morgen" (weekday and date, "am Mittwoch, den 16. September um sieben Uhr abends"). Verification case: `created_at` 00:15 Wednesday, tables Wednesday 19:00 and Thursday 19:00 read back with weekday and date; a 20:00 Tuesday draft keeps "morgen".
Probe: probes-b/test_probe_b_750_call_after_midnight.py (2 red)

DST (no finding): `local = reserved_for.astimezone(zone)` on an aware UTC instant gives the correct
local hour on both switch nights. 2026-10-25 (last Sunday of October): 22:30 UTC Sat -> 00:30 CEST
"in der Nacht von Samstag auf Sonntag um halb eins"; 00:30 UTC and 01:30 UTC both read "halb drei"
(the repeated hour), outside any service. 2026-03-29: 23:30 UTC Sat -> 00:30 CET, 01:30 UTC -> 03:30 CEST,
both named "Nacht von Samstag auf Sonntag"; 21:30 UTC Sun -> 23:30 CEST "morgen um halb zwölf nachts"
(today 28.03). The weekday arithmetic is on `date`, so no DST effect. 23:30 stays the day's evening
("halb zwölf nachts"), 00:00 reads "in der Nacht von ... um zwölf Uhr", 05:00 is "morgens".

## Checked and clean

#744
- Monotonic (by reading): adding words to the gap's negative lookahead only shortens the match or moves it right, so no sentence that went to the team before can now get the offer; 31 sentences probed on main.
- The four #738 sentences plus "Gerne die 23, möchte mich beschweren", "Bitte die 23, will mich beschweren", "Wir möchten zwei Pizzen, wollen uns beschweren", "Ich nehme die 23 möchte mich beschweren" go to the team.
- A stutter "möchte, möchte mich beschweren" goes to the team (safe side).
- ReDoS: 10k to 36k character inputs of wish words, commas and filler run in 26 to 50 ms.
- Known, documented shortcuts not counted as findings: a dish with no wish word ("Zweimal die 23 und ich möchte mich beschweren"), the colloquial "möcht" missing from `_WANTS`.

#745
- `is_yes`: "ja genau", "Ja, junge Frau", "Passt, junger Mann", "Ja, passt so, junge Frau, danke" yes; "nee", "ja, äh, nein", "Herr Müller, ja", "nein danke, das passt so", "Nein, das passt so", "Ja, ne", "ja nee", "Jein", "Ja, aber", "Genau, nein", "Ja passt nicht", "Ja so nicht", "Ja sicher nicht", "Ja, in Ordnung, nicht", "Ja, junge Frau?", "Ist das so, junge Frau", "Junge Frau.", "Ja Frau", "Ja, gut, Frau" no yes.
- An address next to content stays no yes: "Ja, junge Frau, ohne Zwiebeln", "Ja, für die junge Frau", "Ja, die junge Frau kommt mit", "Ja, das Fräulein auch", "Ja, guter Mann, aber zwei".
- `consent_answer`/`recording_answer`: "Nein, junger Mann" a no, "Ja, junge Frau, nein" unclear, "nee" a no; unclear stays no consent.
- The cut cannot build a `_HARMLESS` phrase or hide a question: removing words only adds `asks()` hits.
- NFKC folds the no-break space and the decomposed "ä"; the list equals `escalation._ADDRESSED_AS_A_PERSON` (drift test exists).
- Loop: `addressed` is read before `after_recording_answer` cuts the clause; the truthful sentence comes first on the bare and the non-bare recording answer; the recording is asked only at call start (telephony/handler.py:463), so no read-back can wait while the recording answer is read.
- `confirm` still needs `begin_turn`'s `readback_heard` and `_may_confirm`; a yes to any other last sentence books nothing.

#749
- `_card` prefers the state's own table, so the turn-start table cannot override a new draft made in the turn.
- Model `transfer_to_team`, ladder end in `_converse`, `_callback_without_number` card and ladder end all carry `open_table`.
- UNHEARD_TURNS handoff without `table`: the state is unchanged by definition, so it still names the table.
- The failed-tool `SAY_TO_THE_TEAM` handoff without `table` is deliberate (the tool may have cancelled the table on the tablet).
- `confirm` ok returns before any later handover in the same turn, so a booked table is never read as open.

#750
- DST on both 2026 switch nights, 23:30 / 00:00 / 00:30 / 01:30 / 04:30 / 05:30 boundaries, the date added beyond six days, past or future via `abs`, `created_at` aware (`utcnow`), the replay keeping the creation day.
- Other spoken times (`slots.py` alternatives, `_booked_say`) name no day, so they cannot contradict the read-back.
