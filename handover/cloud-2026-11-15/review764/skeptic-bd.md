# Skeptic verdicts, areas B and D (ledger #764, main @ 69ef3678)

Worktree /home/user/rev-f; probes copied to api/tests/ and run with testenv.sh: 10 red, 1 green
(the green one is the third ORDER_IN_FRONT sentence, "Ich hätte gern zwei Pizzen, außerdem eine
Reklamation", which already goes to the team and is no part of the claim).

## Area B (dialog, consent, read-back, escalation)

### [P1] The order's own wish verb starts the complaint match; the order is lost
Verdict: CONFIRMED (P1)
Evidence: probe test_probe_b_744_order_verb.py: 2 core-path + 2 model-path red, the yes-ends-call test
reaches `ended and booked(session)` before its closing assert. Through the real entry point
(`loop.run_turn`). api/agent/escalation.py:164-169: `_OTHER_WISH` lookahead guards only the gap words,
so the leftmost `_WANTS` match starts on the order's "möchte"/"nehme"/"bestelle"; :281 `sub` then removes
"möchte die 23, beschweren" and `_WANTS_ANYTHING` finds nothing. No later guard: `_offer_remedy`
(api/agent/loop.py:852-870) checks only stage/slots/cart/seen_items, all empty because the core answers
before the model ever sees the dish. Not documented as a shortcut (escalation.py:140-142 and the notes
only name "a dish with no wish word"). Same impact class as #738, rated P1 in
api/tests/test_agent_escalation.py:289.
Fix: let the complaint wish only bridge reflexive/filler/"über"-phrase gap words (e.g. for "beschwer"
require "mich|uns|sich" in or right after the gap, or forbid a digit/article+noun object in the gap) ·
Test: the four probe sentences to SAY_ESCALATION with no remedy_offer; "Ich möchte mich über die
Lieferung beschweren" and "Ich möchte das reklamieren" still get the offer · Needs Maxi: no

### [P2] A draft made after midnight still says "heute"/"morgen" by the calendar
Verdict: CONFIRMED (defect P2; effective P1 under the xhigh rule, read-back/confirm path, CLAUDE.md section 6 "a P2 counts as P1")
Evidence: probe test_probe_b_750_call_after_midnight.py 2 red through the real `create.readback`
(api/domain/reservations/create.py:150-166, the sentence the guest's yes confirms). `today` is
`created_at` local date (:159); `spoken_when` (spoken.py:81-94) applies NIGHT_ENDS to the table's hour
only. The model gets the bare calendar `now` ("Mittwoch, 2026-09-16T00:15", api/agent/loop.py:1071-1077)
and the prompts (prompts/system_v3.md, tools_v3.md) say nothing about the night tail, so nothing
upstream resolves the guest's "morgen" by the night rule. No doc accepts it: HISTORY.md:1132 fixes
`created_at` as reference only for replay stability. The silent-harm case is "morgen": if the model
maps it to Thursday, the read-back echoes "morgen" and the yes books 24 h late; "heute" (Wed 19:00) is
mostly confusing (Tuesday 19:00 is already past), but a "Nein, morgen" correction can drive it to
Thursday too. Why only P2 as a defect: needs a booking call between 00:00 and 04:59 (the pilot closes
at 22:00, HISTORY.md:1112), and it hinges on the model's reading of "morgen". Note: not a regression of
#750; the calendar heute/morgen from `created_at` predates it, #750 just left this half of the class open.
Fix: in `spoken_when`/`spoken_date`, when the draft's local hour is < NIGHT_ENDS, drop "heute"/"morgen"
and say weekday plus date · Test: the probe (created 00:15 Wed, tables Wed 19:00 and Thu 19:00 read
"am Mittwoch, den 16. September ..." / "am Donnerstag, den 17. September ..."), plus a 20:00 Tuesday
draft keeping "morgen" and a 05:00 draft keeping "heute" · Needs Maxi: no (same class and direction as the
#742 fix; the wording stays the D4 read-back format)

## Area D (access, staff, closures)

### [P2] Today's closure lists tables whose time has already passed
Verdict: DOWNGRADE to P3
Evidence: probe test_757_today_closure_lists_tables_already_past red (`['LunchDone', 'Evening']`)
through `change_day_hours` then the same `bookings_outside_hours` call the board makes
(api/gui/router.py:1612-1622). The behaviour holds, but it is over-inclusion on the safe side: no table
is cancelled or lost, nobody is messaged, and every row prints its time first
(api/gui/templates/fragments/pruefen.html:25, "12:00 Uhr, ...") in time order (staff_changes.py:445), so
at 15:00 the team sees the lunch party is past and the evening rows stand last. docs/06_GUI.md:162 and
the docstring (staff_changes.py:414-417, "all of them when closed") describe exactly this; no test pins
the past rows on purpose either. Cost: a needless call or a longer list on a busy day.
Fix: pass the request's `now` and, for `day == today`, keep only `reserved_for >= now - grace` · Test:
the probe (closed at 15:00, 12:00 and 19:00 tables lists only 19:00) plus a later date unaffected ·
Needs Maxi: yes, small (what grace: is a table seated 20 minutes ago still "to call"?)

### [P2] A shortening lists override tables that were outside the hours before too
Verdict: DOWNGRADE to P3
Evidence: probe test_757_shortening_lists_an_override_table_it_never_touched red (`['Override',
'Late']`). Real: span = business day + old windows (staff_changes.py:428) and the filter only asks
whether a new window holds the table (:452-453); tablet overrides outside every window exist
(api/domain/reservations/manual.py:11, 61). But docs/06_GUI.md:162 says the card lists "the confirmed
tables outside the new hours", which the 15:30 table literally is; only the docstring's "would leave"
implies change-caused. Again over-inclusion: the row shows 15:30, visibly outside the old 17:00 start
too, nothing is cancelled; the harm is a needless call to a guest the team booked on its own word.
Needs an override table outside every window on a date that is later shortened, a rare combination.
Fix: on a shortening (not a closure) list a table only when an old window held it,
`any(a <= t < b for a, b in dinein(before))` with neighbour-day windows · Test: the probe lists only
"Late"; a closure still lists the override table · Needs Maxi: no

### [P2] Close then reopen today drops "wie Sonntag" without the owner
Verdict: CONFIRMED (defect P2; effective P1 under the xhigh rule, since it decides opening hours and
seats/availability for today, CLAUDE.md section 6)
Evidence: probe test_754_close_then_reopen_today_keeps_like_weekday red (`card.held is True`) through
`change_day_hours`, the domain entry the staff route uses. staff_changes.py:292-297: `origin` (card.before,
"wie Sonntag") is only used for `hours in (holding, origin)`; the weekday clause checks `holding`, which
after the held closure is `closed` with like_weekday None, so `_drops_weekday` (:587-595) is False and the
times hold at once. This is the exact bypass of the #710 rule ("a staff change for today on a 'wie
Sonntag' date waits for the owner", docs/01_STATUS.md:312, test
`test_shorter_times_today_on_a_day_like_another_weekday_wait_too`), reached by a natural sequence
(kitchen down, then back). Mitigation only: the owner gets the push and the card and can undo, but today
runs on the plain weekday's seats, call mode and call limit meanwhile (more seats means overbooking).
Fix: `or (day == today and not (_drops_weekday(hours, holding) or _drops_weekday(hours, origin)))` ·
Test: the probe (not held, row stays closed), plus "normale Zeiten" after the close also waits, and a
close then reopen on a plain day still holds · Needs Maxi: no (applies his #710 decision)

## Cross-cutting

### [P2] Guest list to call lives only on an open held card: owner's keep erases it
Verdict: CONFIRMED (P2; no xhigh upgrade: the list decides no hours, seats or booking, it is the
follow-up the team does by hand)
Evidence: probe test_probe_cross_757.py, 2 red through the real GUI routes (POST /gui/tageszeiten,
POST /gui/pruefen/{id}/uebernehmen, GET /gui/fragments/pruefen). (a) `decide` (staff_changes.py:332-386)
closes the card on keep, `open_cards` (:389-403) returns OPEN only, so `_check_rows`
(router.py:1595-1622) never lists those tables again. (b) the list is built only when the holding differs
from `card.before` (router.py:1614); a waiting card holds `before`, and the keep writes the hours and
closes the card in one step (:368-372). `bookings_outside_hours` has that one caller (grep api/gui), and
the DAY_HOURS_CHANGED push carries no tables. Not intended: STATUS v2.44.38 (docs/01_STATUS.md:359) and
docs/06_GUI.md:162 say "a held closure or shortening lists ... so the team calls those guests itself";
neither covers the owner's keep, and the waiting case was simply not reached. Harm: a guest of a closed
or shortened date arrives at a closed door, the case #710 exists for.
Fix: product decision for Maxi (where the list lives once the card closes, and who says "called"):
- A: the card stays on the board after the owner's keep, as "Gäste anrufen", until the team ticks each
  guest off (state in the card's JSONB or a new column; one write route, audited).
- B (recommended): a standing block "Tische außerhalb der Öffnungszeit" per date from today, computed at
  render from the hours that hold and the confirmed tables, independent of any card; a row goes once the
  table is cancelled or moved. Stateless, no migration, also covers the owner's own hour changes; a waiting
  card additionally previews the tables its change would strand. Caveat: tablet overrides outside every
  window would stand there for good (area D's 757b), so B needs either a "kept on purpose" mark or the
  override excluded (it is not stored on the reservation today).
- C: minimal: list on waiting cards too, and keep a closed card's list visible until its date has passed
  (no tick-off, the list just survives the keep).
Test: the two probes green; a cancelled table leaves the list; a waiting card names the 12:00 guest
before the keep · Needs Maxi: yes (board behaviour and who confirms the calls were made)
