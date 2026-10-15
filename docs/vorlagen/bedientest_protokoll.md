# Operating test protocol (T-3.5)

> One page per test. Print it or fill it in on a second device. Goal from `docs/06_GUI.md` section 1: a team member uses the operations view for 5 minutes, without any explanation, with greasy fingers, on the tablet, while the phone rings.
> Filled sheets stay out of the repository (they hold the team member's impressions). Only the verdict and the list of changes it caused go to `docs/01_STATUS.md`. No names: write "team member 1", "team member 2".

---

## 0. Before the test (the one who runs it, not the tester)

- [ ] Tablet is the real device, held the way it hangs in the kitchen or at the counter, logged in, sound on, brightness as in operation
- [ ] Operations view reachable from the tablet through Caddy (docs/13); the header shows the green dot "KI nimmt an"
- [ ] Test data only, invented customers: at least two new orders (one pickup, one delivery), one with an option, one with a note; one red card ("Zettel nicht gedruckt"); one complaint callback; one reservation today
- [ ] A second device or a text phone (`sim.cli`) ready to create a new order and a new callback while the test runs; these are the live arrivals of section 2a
- [ ] A clock or the phone's stopwatch; this sheet; a pen

| Field | Entry |
|---|---|
| Date and time | |
| Tester | team member 1 / 2 / 3 (role: counter, kitchen, driver) |
| Observer | |
| Device and browser | |
| Build (git commit or release) | |
| Was the tablet used before in the view? | yes / no (must be no) |

---

## 1. Rules for the observer

1. Say one sentence, nothing else: "Please run the view as if a call just came in; say out loud what you think." No tour, no hint on what a button does.
2. Do not help. A stumble is data. Note it, wait 20 seconds, then give a hint and write down that you gave one.
3. Stop at 5 minutes, even if tasks are open.
4. No judgement of the tester. A stumble is always the view's fault.

---

## 2. Tasks

Give them one by one, as a sentence a team member would say, never with the button name. Start the clock at the first task. Mark each: **OK** (done alone, first try) · **SLOW** (more than 20 s or two tries) · **HINT** (needed a hint) · **FAIL** (not done, or wrong thing done).

| # | Task, as spoken | What counts as done | Taps expected | Time (s) | Result | Stumble (what, where) |
|---|---|---|---|---|---|---|
| 1 | "A new order came in. Tell me what it is and when it is ready." | Reads pickup code, items and ready time aloud | 0 | | | |
| 2 | "That order is fine, send it on." | Taps "Passt" on the right card | 1 | | | |
| 3 | "The customer wanted a different dish, fix it." | Opens "Korrigieren", taps "Tauschen", taps the number field, types the number, "Übernehmen", picks the reason | 5 plus the digits (rule 1 of section 1 does not cover this flow; the count is for the notes, not a fail) | | | |
| 4 | "The slip did not print, send it again." | Finds the red card, taps "Nochmal senden" | 1 | | | |
| 5 | "There is a complaint. Call the person back and mark it done." | Taps "Anrufen", then "Erledigt" | 2 | | | |
| 6 | "Today we are out of dish 23." | Opens "Gericht aus", finds 23, switches it off | 2 to 3 | | | |
| 7 | "The kitchen is busy, pickup takes longer." | Raises the pickup "Wartezeit" by one step; does not touch the delivery time | 1 | | | |
| 8 | "Is the AI answering the phone right now?" | Reads the dot and its text; says on, off or fills in | 0 | | | |
| 9 | "Stop the AI, we take every call ourselves." | Taps "KI pausieren"; dot turns red, text "KI ist aus" | 1 | | | |
| 10 | "Switch it back on." | Taps the button and confirms | 2 | | | |
| 11 | "Which reservation is next today?" | Finds it in the column "Heute" | 0 | | | |

### 2a. Live arrival (scored, section 1 rule 5)

Before the tester's first tap, with the tablet untouched since loading, create one new order from the second device; later, while the tester is busy with task 6 or 7, create one new callback. Record:

| Event | Sound played by itself (yes/no) | Tester noticed within 10 s (yes/no) | Notes |
|---|---|---|---|
| New order, before the first tap | | | |
| New callback, during a task | | | |

The first row answers the "Ton an" question of the status assumption of 18.09.2026: no sound before the first tap means the hint is needed.

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
| Tasks done alone, first try (OK) | at least 9 of 11 | / 11 |
| Tasks FAIL | 0 | |
| Tasks that needed a hint | at most 1 | |
| Severity A stumbles | 0 | |
| Total time of the tasks | within the 5 minutes | min |
| Live arrivals noticed within 10 s (section 2a) | both | / 2 |

**Verdict:** PASS · PASS WITH CHANGES · FAIL

- **PASS:** all measures met, no A stumble.
- **PASS WITH CHANGES:** all measures met but B stumbles that Maxi wants fixed before G1.
- **FAIL:** a measure missed. Fix the A stumbles, repeat the test with a different team member who has not seen the view.

The thresholds and the two-tester rule (T-3.5 closes after two passing testers) are assumptions of this template (nothing was measured so far); Maxi changes them before the first test if he wants them harder.

Next step (changes to make, who repeats the test):

---

## 6. After the test (the one who runs it)

1. Put the verdict and the list of changes in `docs/01_STATUS.md` (one row in the changelog, the open points of "Operating view" that the test settled, "Ton an" hint and "Ändern" on an option line included).
2. Each A or B stumble becomes a task or an issue with the label `bug` or `enhancement` as fits; do not keep it only on this sheet.
3. T-3.5 closes only after two different team members have each passed (PASS or PASS WITH CHANGES after the fixes); a first PASS still needs the second run.
4. Do not keep the filled sheet in the repository; shred it or keep it in the team's own folder.
