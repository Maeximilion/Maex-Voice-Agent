# Operating test protocol (T-3.5)

> One page per test. Print it or fill it in on a second device. Goal from `docs/06_GUI.md` section 1: a team member uses the operations view for 5 minutes, without any explanation, with greasy fingers, on the tablet, while the phone rings.
> Filled sheets stay out of the repository (they hold the team member's impressions). Only the verdict and the list of changes it caused go to `docs/01_STATUS.md`. No names: write "team member 1", "team member 2".

---

## 0. Before the test (the one who runs it, not the tester)

- [ ] Tablet is the real device, held the way it hangs in the kitchen or at the counter, logged in, sound on, brightness as in operation
- [ ] Operations view reachable from the tablet through Caddy (docs/13); the header shows the green dot "KI nimmt an"
- [ ] **Isolated stack**: run on a throwaway stack (workbench or local Compose, invented data) with the print bridge off, never on the working stack of the restaurant; test orders in `primary` mode would queue real slips that cannot be retracted. Starting waiting times (only if this is not a throwaway stack): pickup ____ min, delivery ____ min, to restore afterwards
- [ ] **No red cards by timeout**: stop the dispatcher (or its handover sweep) for the run; with the print bridge off, the sweep would turn every unclaimed slip of B, C and D red after 60 s. The only red card is the prepared one (set `handover_state` to failed by hand)
- [ ] **Approval threshold**: keep the totals of A, B, C, D and E below `order_approval_above_cents` (or set it high) and `order_approval_overdue_minutes` at its default 5, so no card waits for approval, turns red by a wait timer or sounds the complaint alarm during the run
- [ ] **Open day**: run the test on a day and time when pickup is open (opening hours), otherwise the text phone call that creates order A ends abandoned and task 1 has no card
- [ ] **Language**: the spoken prompts below are English for the repository; prepare one translation in the tester's working language before the test and use the same wording for both testers
- [ ] Test data only, invented customers: **B, C, D and the red-card order exist before the test; A does not (it is created during section 2a)** - order A: the live arrival of section 2a (a pickup, created during the test); order B: a pickup (write its pickup code here: ____) used for task 2 (Passt on B does not remove order C); order C: a pickup with a dish that has a required choice and a note (write its pickup code here: ____), used for task 3; order D: a dish with two alternative choices of the same group (for example two sauces), used for task 12; **its dish appears on no other card** (A, B, C, and the dish chosen in task 3, use other dishes), and it carries only that one choice group, created with the recorded old choice selected (any other selected group would be cleared by the same-number swap); write its pickup code here: ____ so task 12 names it. The replacement dish of task 3 has exactly one required choice group, containing the named choice. Write the concrete numbers here before the test: replacement dish ____ with choice ____ (task 3); dish ____ with choice ____ changed to choice ____ (task 12); one **separate** red card ("Zettel nicht gedruckt", a fifth order, not A to D, because task 3 saves a correction that turns a failed handover normal); one complaint callback; one reservation today **at least one hour after the planned end of the test** (otherwise task 11 has no correct answer)
- [ ] A second device or a text phone (`sim.cli`) ready to create a new order (a **pickup**, with a pickup code, so task 1 can be done on it) and a new callback while the test runs; these are the live arrivals of section 2a
- [ ] Operating conditions of the goal above: a phone nearby rings during the test (ring it from another phone every 60 s, ringtone at working volume), and the tester's fingers are damp or lightly oiled (a drop of cooking oil rubbed in, or wet hands dried once); note in the table below if this was not done
- [ ] **Reset before every tester** (the second tester sees the same start as the first): the AI is on (green dot), pickup waiting time set to 25 minutes and delivery waiting time to 50 minutes (both leave at least 15 minutes of headroom below the 180-minute limit, so task 7 can be done), dish 23 available ("Wieder da" not shown for it), the fixtures **rebuilt from scratch, not reopened**: delete or close the cards of the previous run (A, E, the live callback, and the changed B, C, D), then create B, C, D, the red-card order, the complaint callback and the reservation again with the recorded values (old dish, old choice, note, pickup codes) and set the red state again; reload the tablet so nothing is left from the first run
- [ ] A clock or the phone's stopwatch; this sheet; a pen

| Field | Entry |
|---|---|
| Date and time | |
| Tester | team member 1 / 2 / 3 (role: counter, kitchen, driver) |
| Observer | |
| Device and browser | |
| Build (git commit or release) | |
| Was the tablet used before in the view? | yes / no (must be no) |
| Phone rang during the test; fingers damp or oiled | yes / no; yes / no (both must be yes, otherwise the run is invalid and does not count) |

---

## 1. Rules for the observer

1. Say one sentence, nothing else: "Please use the view and say out loud what you think." (no mention of a call or an arrival; the tablet stays untouched until the silent watch of section 2a is over) No tour, no hint on what a button does.
2. Do not help. A stumble is data. Note it, wait 30 seconds without progress, then give a hint and write down that you gave one.
3. Stop at 5 minutes, even if tasks are open.
4. No judgement of the tester. A stumble is always the view's fault.

---

## 2. Tasks

Give them one by one, as a sentence a team member would say, never with the button name. Start the clock when task 1 is spoken (after the silent 10-second watch of section 2a). Mark each: **OK** (done alone, first try, within 20 s) · **SLOW** (done alone, but more than 20 s or a second try after a wrong action that was corrected by the tester) · **HINT** (needed a hint, even if done afterwards) · **FAIL** (not done, or the wrong thing stays done). One result per task, highest wins: FAIL, then HINT, then SLOW, then OK.

| # | Task, as spoken | What counts as done | Taps expected | Time (s) | Result | Stumble (what, where) |
|---|---|---|---|---|---|---|
| 1 | "Tell me the pickup code, what is in the new order and when it is ready." | On order A, reads pickup code, items and ready time aloud | 0 | | | |
| 2 | "The order with code [code of order B] is correct, check it off." | Taps "Passt" on order B (not on C or D) | 1 | | | |
| 3 | "On the order with code [code of order C], the customer wants dish [number from section 0] with [choice] instead of the first dish, fix it." | On order C: opens "Korrigieren", taps "Tauschen", taps the number field, types the number, "Übernehmen", picks the required choice, picks the reason "falsches Gericht", then confirms on the corrected card that the note of the line is still there (any other reason is a FAIL: the correction is saved with a false reason) | 6 plus the digits (rule 1 of section 1 does not cover this flow; the count is for the notes, not a fail) | | | |
| 4 | "The slip did not print, send it again." | Finds the red card, taps "Nochmal senden" | 1 | | | |
| 5 | "There is a complaint. Call the person back and mark it done." | Taps "Anrufen"; the dialer or call prompt opens (the observer checks it on the tablet; if the tablet has no working phone function the task is a FAIL and the incompatibility goes on the stumble list as severity A); only then "Erledigt" | 2 | | | |
| 6 | "Today we are out of dish 23." | Opens "Gericht aus", finds 23, switches it off | 2 (opening the panel, the switch for 23); a third tap for the search field is a known overrun of rule 1 of section 1: recorded as a stumble of severity B and not scored | | | |
| 7 | "The kitchen is busy, pickup takes 15 minutes longer. A few minutes later it is calm again: take the 15 minutes back." | Raises the pickup "Wartezeit" by 15 minutes, then lowers it by 15 minutes (settles the open point "Lower the wait time from the tablet"); does not touch the delivery time | 2 | | | |
| 8 | "Is the AI answering the phone right now?" | Reads the dot and its text and answers "on" (green, "KI nimmt an"; the reset of section 0 guarantees it) | 0 | | | |
| 9 | "Stop the AI, we take every call ourselves." | Taps "KI pausieren"; dot turns red, text "KI ist aus" | 1 | | | |
| 10 | "Switch it back on." | Taps the button and confirms | 2 | | | |
| 11 | "Which reservation is next today?" | Finds it in the column "Heute" | 0 | | | |
| 12 | "On the order with code [code of order D], the customer wants [new choice] instead of [old choice]." | On order D: opens "Korrigieren", changes the choice, picks the reason "Sonstiges" (any other reason is a FAIL: false data in the accuracy figures) and thereby saves, then reads the card to confirm the new choice shows (a change not saved with a reason is lost; today: "Tauschen" with the same number re-picks the options); note how many taps and whether the tester looked for an "Ändern" on the option line | open, note it | | | |

### 2a. Live arrival (scored, section 1 rule 5)

Before the tester's first tap, with the tablet untouched since loading, create order A from the second device and watch the tester for 10 seconds without any cue, before task 1 is spoken (the "noticed within 10 s" cell is filled from this silent watch). Later, after the tester has tapped the page, create a second pickup order E (a dish other than 23, known to be available) during task 6 and one new callback during task 7 or 8 (the tester is back in the view, not in a dialer prompt from task 5). Record:

| Event | Sound played by itself (yes/no) | Tester noticed within 10 s (yes/no) | Notes |
|---|---|---|---|
| New order A, before the first tap | | | |
| New order E, after the first tap, during task 6 (a second pickup created from the second device) | | | |
| New callback, after the first tap, during a task | | | |

The first row is an observation, not a pass measure: the browser plays no tone before the first tap today (`app.js`), so "no" is the expected result and means the "Ton an" hint of the status assumption of 18.09.2026 is needed; do not tap the page before this row is recorded. Rows 2 and 3 are scored: after one tap each tone must play. 

Free observation during the tasks (tick if seen):

- [ ] Tester tried to tap something that is not a button
- [ ] Tester looked for a menu, a search or a back button
- [ ] Tester asked "what does ... mean?" (note the word)
- [ ] Tester could not read something from 2 m (code, time, dot)
- [ ] A button was missed with a greasy or wet finger (note which)

---

## 3. Stumbles

One line each, in the tester's words where possible. Severity: **A** blocks the task · **B** slows or confuses · **C** nit.

| # | Task | What happened | Their words | Sev. | Suggested change |
|---|---|---|---|---|---|
| | | | | | |
| | | | | | |
| | | | | | |

---

## 4. Questions after the 5 minutes (ask, do not explain)

1. What was the easiest thing? What was the hardest?
2. Was there anything you did not dare to tap? Why?
3. Would you trust this during a rush? What would you want changed first?
4. Is any word on a button or card one you would not use?

Answers (short):

---

## 5. Verdict

| Measure | Pass when | Result |
|---|---|---|
| Tasks done alone, first try (OK) | at least 10 of 12 | / 12 |
| Tasks FAIL | 0 | |
| Tasks that needed a hint | 0 (a hinted task means the view was not operated without explanation) | |
| Severity A stumbles | 0 | |
| Total time of the tasks | within the 5 minutes | min |
| New order E and callback noticed within 10 s (section 2a, rows 2 and 3); the first arrival is recorded and not scored | yes | |
| New-order tone (row 2) and callback tone (row 3) played by themselves after the first tap | yes, both | |
| Tone before the first tap (section 2a, row 1, observation) | not scored; "no" puts the "Ton an" hint (docs/01_STATUS.md) on the list of changes and the verdict is at best PASS WITH CHANGES until it is built | |

**Verdict:** PASS · PASS WITH CHANGES · FAIL

- **PASS:** all measures met, no A stumble.
- **PASS WITH CHANGES:** all measures met but B stumbles that Maxi wants fixed before G1.
- **FAIL:** a measure missed. Fix the A stumbles, repeat the test with a different team member who has not seen the view.

The thresholds and the two-tester rule (T-3.5 closes after two passing testers) are assumptions of this template (nothing was measured so far); Maxi changes them before the first test if he wants them harder.

Next step (changes to make, who repeats the test):

---

## 6. After the test (the one who runs it)

0. **Restore the stack after the last tester**: dish 23 available again ("Wieder da"), test orders, callbacks and the reservation removed or closed, waiting times back to the real values, the AI switched on, the dispatcher service started again if it was stopped. A test stack that is also the working stack must not keep sold-out dishes or test cards.

1. Put the verdict and the list of changes in `docs/01_STATUS.md` (one row in the changelog, the open points of "Operating view" that the test settled, "Ton an" hint and "Ändern" on an option line included).
2. Each A or B stumble becomes a task or an issue with the label `bug` or `enhancement` as fits; do not keep it only on this sheet.
3. T-3.5 closes only after two different team members have each passed on the same build (PASS or PASS WITH CHANGES after the fixes); a first PASS still needs the second run, and if the UI changed after a tester passed, that pass no longer counts and the count restarts with a new tester on the changed build.
4. Do not keep the filled sheet in the repository; shred it or keep it in the team's own folder.
