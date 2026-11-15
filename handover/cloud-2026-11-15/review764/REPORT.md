# Ledger #764 review report (`/code-review max` level), main @ 69ef3678

Skeptic verdict wins over the reviewer's severity. "Eff." = effective severity under CLAUDE.md section 6 ("a P2 counts as P1" on xhigh paths). All reviewer probes re-run by the skeptics; red as claimed unless noted. No P0 anywhere. Placeholders: `I-ALLERGEN`, `I-DIALOG`, `I-STAFFWD`, `I-OUTAGE` (one issue per fix PR), `I-D751`, `I-D757` (decision issues), `I-757FU`, `I-PARKED`.

## 1. Summary per PR

| PR | Finding | Final | Verdict | Where it goes |
|---|---|---|---|---|
| #743 | Hang-up card of a failed call drops the booking that stands (handler.py:659-661, `_fail`; root cause predates #743) | P2 (eff. P1) | CONFIRMED, corrected trigger (real path: DB error after `confirm`, `broken()` during confirmation, missed-leg card; not "a later turn") | fix/764-outage-path |
| #744 | Order's own wish verb starts the complaint match, order lost, remedy booked (escalation.py:164-169) | P1 | CONFIRMED | fix/764-dialog-batch |
| #745 | clean | - | - | - |
| #747 | Core allergen sentence replaces the model's only when the model set the flag (loop.py:628, :842-847; guards.py:668) | P1 | CONFIRMED, plus skeptic's cross-turn path ("Ich habe eine Erdnussallergie." then "Ist in der 13 Erdnuss?"), 2 more red | fix/764-allergen-guard |
| #748 | Absence forms pass `allergen_claim`: "In der 13 ist keine Erdnuss." (guards.py:245-260; also notes via `claims_in_a_note`) | P1 | CONFIRMED (10/10 unit, 3/3 loop red) | fix/764-allergen-guard |
| #748 (regressed by #788) | Short ingredients (Kiwi, Mais, Zimt, Mohn, Lamm) go on the callback card (loop.py:1576, `_stems` drops words under 5 letters) | P1 | CONFIRMED; Zöliakie sub-case split off as UNCLEAR P3 | fix/764-allergen-guard |
| #749 | clean | - | - | - |
| #750 | Draft made after midnight says "heute"/"morgen" by the calendar (create.py:150-166, spoken.py:81-94) | P2 (eff. P1, read-back/confirm path) | CONFIRMED | fix/764-dialog-batch |
| #751 (regressed by #788) | Second person's allergy dropped when a health note exists but omits her | P2 | DOWNGRADE from P1: deliberate #788 design, pinned by test_agent_allergy_notes.py:251-262 | Decision D1 |
| #754 | Close then reopen today drops "wie Sonntag" without the owner (staff_changes.py:292-297) | P2 (eff. P1: hours, seats) | CONFIRMED | fix/764-staff-weekday |
| #754 | Outage path reads the team number before the outage sentence plays (handler.py:666-677; docs/20:227 false) | P2 (eff. P1: outage fallback) | CONFIRMED; delay about 0 to 6 s, 30 s limit not broken, "about 4 s" is a derivation | fix/764-outage-path |
| #755 | clean | - | - | - |
| #757 | Today's closure lists tables already past (staff_changes.py:406, router.py:1612) | P3 | DOWNGRADE from P2 | Decision D3 |
| #757 | Shortening lists override tables it never touched (staff_changes.py:428-454) | P3 | DOWNGRADE from P2 | I-757FU |
| #757 (with #754) | Stranded guest list lives only on an open held card; owner's keep erases it, waiting card never lists (router.py:1595-1622, staff_changes.py:332-403) | P2 | CONFIRMED (cross-cutting) | Decision D2 |
| #760 | Reason filler without comma lands in the ingredient (wishes.py:111-120, :558) | P3 | DOWNGRADE from P2: only reachable via a model-built `search_menu` query, no allergen lost, root cause predates #760 | I-PARKED |
| #774 | clean | - | - | - |
| #783 | clean | - | - | - |

Counts: P1 4 (#744, #747, #748 x2), P2 5 (#743, #750, #751, #754 x2) plus the cross-cutting #757 P2, P3 3, clean 5 PRs (#745, #749, #755, #774, #783).

## 2. Fixed on branches in this session (coordinator fills)

Not merged, not pushed separately. Each branch needs: red test first, ruff, evals where the conversation path is touched (rule 4), independent review of the exact head.

| Branch | Findings | PR / issue | Commit(s) | Tests (red to green) | Evals / CI | Status |
|---|---|---|---|---|---|---|
| fix/764-allergen-guard | #747 flag; #748 absence forms; #748 short ingredients on cards. Skeptic's fix: (1) `unread_allergy_question` fires when `state.allergy_talk`/`allergy_said` and the guest sentence names an allergen; (2) successful `get_item_details` with `allergens.known` builds the core sentence from `result.data` whatever the flag; (3) `_ABSENT` / `_ABSENT_IN_SENTENCE` forms (ist, kein.. drin with 0-2 words, nix, weder/noch, "<allergen> nicht", allergen before "?"); (4) ingredient word of `allergy_said` kept at any length | | | probes-a 25 + skeptic cross-turn probe | | |
| fix/764-dialog-batch | #744 order verb (gap may bridge only reflexive/filler/"über"-phrase words); #750 night read-back (weekday and date when draft hour < NIGHT_ENDS) | | | probes-b 4+2 | | |
| fix/764-staff-weekday | #754 "wie Sonntag": `_drops_weekday(hours, holding) or _drops_weekday(hours, origin)` | | | probe plus "normale Zeiten" after close, plain-day close then reopen still holds | | |
| fix/764-outage-path | #754 team number read after outage sentence (docs/20:227 and handler.py:671-672 comment, drill re-run to record in docs/20 section 5); #743 card keeps `booking_say` / open draft in `_fail`, `team_missed` path | | | probes-c order and card wire probe | | |

## 3. Decisions for Maxi

**D1 (#751, second person's allergy; design of #788 vs your wording).** Recommended A.
Evidence: #788 removed `allergies.py`; the net passes a draft once any note matches `HEALTH_NOTE` (docs/05:264-266, pinned by test_agent_allergy_notes.py:251-262), so "Ich bin allergisch gegen Erdnüsse, meine Frau gegen Sesam" with note "WICHTIG !!! Allergie: Erdnüsse" stores no Sesam (3 probes red). Your decision text "no note of the draft mentions it" reads per allergy as well; no eval has two persons, #787 (open) should measure it.
- A (recommended): add a coverage check without a parser, `allergens_named(_said(state)) <= allergens_named(notes)` plus the `_names_health` stem match, else hint and `with_the_guests_words`; the pinning test changes; add a two-person eval case.
- B: keep #788 as is, rely on model plus #787 measurement, add only the eval case.
- C: ask the model for one note line per person (prompt rule only, no core check).

**D2 (#757, where the stranded guest list lives; cross-cutting).** Recommended B.
Evidence: the list is built only for an open held card whose holding differs from `before` (router.py:1614); the owner's keep closes the card and the list vanishes, and a waiting "wie Sonntag" card never lists, so the 12:00 guest is told nothing (2 probes red through the GUI routes). STATUS v2.44.38 and docs/06:162 promise the call-list only for held cards.
- A: the card stays on the board as "Gäste anrufen" until the team ticks each guest off (state in card JSONB or new column, one audited write route, migration).
- B (recommended): standing block "Tische außerhalb der Öffnungszeit" per date from today, computed at render from holding hours and confirmed tables; stateless, no migration; a row goes when the table is cancelled or moved; a waiting card previews the tables its change would strand. Caveat: tablet overrides outside every window stay listed for good, so it needs a "kept on purpose" mark or an override exclusion (override is not stored on the reservation today).
- C: minimal: list on waiting cards too and keep a closed card's list visible until its date has passed, no tick-off.

**D3 (#757 P3, how long after its start is a table still listed on today's closure).** Recommended A.
Evidence: `bookings_outside_hours` takes no `now`; closing at 15:00 lists the 12:00 lunch party too (probe red `['LunchDone', 'Evening']`); no cancellation or message happens, rows are time-ordered. If D2 = B, this becomes the render-time rule of the standing block, so decide together.
- A (recommended): keep tables with `reserved_for >= now - 60 min` (still arriving or just seated), drop earlier ones.
- B: only tables at or after now.
- C: leave the whole business day listed (docs/06:162 as written), accept the long list.

No other finding carries "Needs Maxi: yes". The #757 P3 override-table finding needs none (rule-bound fix) but interacts with D2 B (see I-757FU).

## 4. Issue drafts (not on a fix branch, not a decision)

Labels per issue: `code-review` + kind + `priority:` label (use the repo's exact names; `priority:p2` / `priority:p3` assumed).

**Decision issues** (own design, filed regardless): "#751 second person's allergy: coverage check or not (D1)", labels `code-review`, `decision`, `priority:p2`, body = D1; "#757 stranded guest list: where it lives (D2) and how long past tables stay (D3)", labels `code-review`, `decision`, `priority:p2`, body = D2, D3.

**I-757FU** title: "Staff board: follow-ups to the stranded-guest list (#757)", labels `code-review`, `bug`, `priority:p3`. Do after D2/D3 so one PR covers it.
- [ ] api/domain/status/staff_changes.py:428-454, P3: a shortening lists override tables that no old window held (probe `['Override','Late']`); list only `any(a <= t < b for a, b in dinein(before))` incl. neighbour-day windows; a closure still lists them (interacts with D2 B "kept on purpose")
- [ ] staff_changes.py:406 / router.py:1612, P3: past tables on today's closure (rule from D3)

**I-PARKED** title: "Parked P3 and unverified items from ledger #764", labels `code-review`, `bug`, `priority:p3` (or note for #207).
- [ ] api/domain/menu/wishes.py:111-120, :558, P3 (#760): a reason word (deshalb, deswegen, daher, darum, weil, weshalb) ends the allergy list also without a comma; "ich bin allergisch gegen Nüsse deswegen nehme ich die 5" gives "Nüsse deswegen nehme ich die 5"; test: the no-comma cases give "Nüsse" / "Nüsse und Sesam"
- [ ] api/agent/loop.py card redaction, P3 question (#748 sub-case): "Mein Sohn hat Zöliakie." then card "Rückruf: Gluten in der 13" needs a disease-to-allergen mapping; UNCLEAR, decide whether a card must hide it
- [ ] api/agent/escalation.py (`arms_the_net`), no verdict: False for "Mein Mann kann keine Milch ab.", "Meine Tochter verträgt nix mit Nüssen.", "Meine Frau kriegt einen anaphylaktischen Schock bei Nüssen.", "Nüsse sind für meine Frau lebensgefährlich." (verified by direct call, reviewer gave no severity, #788 lane); the kitchen note is then wholly the model's; needs a skeptic verdict, link to D1 / #787

## 5. Ledger tick commands

Replace placeholders with the filed issue numbers (a PR whose findings sit in a fix-branch issue references that issue).

```
python3 scripts/ultrareview_ledger.py done 743 --level max --findings I-OUTAGE
python3 scripts/ultrareview_ledger.py done 744 --level max --findings I-DIALOG
python3 scripts/ultrareview_ledger.py done 745 --level max
python3 scripts/ultrareview_ledger.py done 747 --level max --findings I-ALLERGEN
python3 scripts/ultrareview_ledger.py done 748 --level max --findings I-ALLERGEN I-PARKED
python3 scripts/ultrareview_ledger.py done 749 --level max
python3 scripts/ultrareview_ledger.py done 750 --level max --findings I-DIALOG
python3 scripts/ultrareview_ledger.py done 751 --level max --findings I-D751
python3 scripts/ultrareview_ledger.py done 754 --level max --findings I-STAFFWD I-OUTAGE
python3 scripts/ultrareview_ledger.py done 755 --level max
python3 scripts/ultrareview_ledger.py done 757 --level max --findings I-D757 I-757FU
python3 scripts/ultrareview_ledger.py done 760 --level max --findings I-PARKED
python3 scripts/ultrareview_ledger.py done 774 --level max
python3 scripts/ultrareview_ledger.py done 783 --level max
```
(If the script wants a findings count or refuses an empty `--findings` for clean PRs, drop the flag.)

## 6. Checked and clean (condensed)

- #743: `caller_hung_up` only for a closed, answered session; no double card with the missed-leg card; outcome stays `error`; withheld number gives no card; leg closed before first sentence gives no card.
- #744: monotonic (extra gap guard only shortens or moves the match); the four #738 sentences go to the team; ReDoS 36k chars in 50 ms; documented shortcuts ("dish with no wish word", "möcht") not counted.
- #745: `is_yes`, `consent_answer`, `recording_answer` with 80 variants; address forms never build a yes; NFKC folds; `confirm` still needs `readback_heard`.
- #747 (apart from the flag): same-turn flagged lookup replaced correctly; two dishes one sentence each; sold-out suffix keeps the lead; guard order ok; unknown allergens still hand over.
- #748 (apart from findings): added absence forms read; per-sentence scope keeps "Die Bestellung hat keine Getränke. Die 13 ohne Erdnüsse, notiert." no claim; plural-to-singular matches on cards.
- #749: `_card` prefers state's table; model transfer, ladder ends and callback card carry it; failed-tool handoff without table deliberate.
- #750: DST both 2026 switch nights, 23:30/00:00/00:30/01:30/04:30/05:30, date beyond six days, replay keeps creation day.
- #751: bare "Nein." keeps another person's sentence in `allergy_said`; common second-person forms arm the net and the core writes the guest's words on the second try.
- #754: `_team_phone` never raises, falls back to the call's number; `_hhmm` refuses offsets; `sheet_numbers` E.164 only; later dates with closed old windows wait.
- #755: migration 038 up/down with a live owner row; resolve needs `admin_until`, `admin_active_at`, 15 min; every owner-rights end clears both; `mark_admin_active` cannot reopen rights; only `require_admin` moves the clock.
- #757: only `confirmed`, not deleted; tablet tables included; Berlin day bounds and after-midnight windows; last-start rule kept on purpose; autoescape, no `tel:` for costly numbers; SSE token follows cancels and moves; read-only, no new write.
- #760: comma forms of deshalb/deswegen/daher/darum end the list; no stem side effects.
- #774: `outage` excluded from model transfer reasons, mapped to `not_understood` in handoffs; no CHECK constraint on `transfer_reason`; drill attributes the crash to the right scenario.
- #783: FROM/ENDING/CARD filtered before any `$[...]`; empty or failed CURL goes to the team; every branch ends at the team or by design.
- Cross-cutting: races (`_locked` FOR UPDATE, advisory locks, one card per call), time (aware UTC everywhere), data (call_id, audit_log, no personal data in new logs), CSRF (27 POST routes keep `_require_htmx`), autoescape. Not reported: #755 owner GET pages without same-origin check (no concrete attacker path).
