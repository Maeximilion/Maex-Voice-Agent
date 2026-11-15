# Skeptic pass, area F (review #207 batch 2, main 69ef3678)

Probes re-run in /home/user/rev-l/api/tests/ (copies of probes-f/): 11 red, 6 green, exactly as the notes say.
Every red case fails at the claimed assertion, through the real entry point (`call_routing`,
`normalize_phone` / `guest_details.numbers_said`, `load_hours`).

### [P2] A 00:00 mode or limit row follows the wrong day around a special day
Verdict: DOWNGRADE to P3 (latent). It becomes P2 the day a window row exists, and then effectively P1 under the xhigh rule, because the call mode decides the fate of a call and whether an order waits for "Passt".
Evidence: The probe reproduces through `call_routing` (`routing.py:71`, which calls `mode_at`). Both special-day cases give `CallRouting(ai_answers=False, mode='shadow', call_limit=1)` while `_hours_open` is True; the control gives primary and 3. The mechanism is real: `mode.py:62` and `routing.py:113` key on `weekday_of(local.date())`, while `hours.py` `open_window_at` reads the day before with that day's own special day.
Why it is lower than P2 today:
(1) docs/03:62 and :73 say "no row is written there, and no editor on the tablet exists yet". Windows are hand SQL only, and the seed (`scripts/seed.py:26`, closes at 22:00) has no spill. #686 itself was filed as a design decision for the same reason ("no window rows exist yet").
(2) The behaviour literally matches the contract. docs/03:114 says the windows of that weekday hold "on the date", and a 00:00 row is its own window by docs/03:79 ("The second row begins a window of its own at 00:00"). It is a gap of the two-row model, which `mode.py:66` already flags as a shortcut ("join the rows when the tablet gets a window editor").
(3) In the probe's direction (shadow instead of primary) the team phone rings, so rule 5 holds. The unsafe direction (primary instead of a planned shadow, so orders skip "Passt", `confirm.py:116` and `_waits_for`) needs the mirror setup.
Fix: When the window editor joins the rows (or before it), a row that starts at 00:00 and continues the evening before takes `weekday_of(day - 1)`, or windows become one row past midnight like opening_hours. The fix belongs with the existing shortcut note. · Test: the two probe cases go green and the control stays green, plus the mirror case (config primary, shadow windows across midnight, Tuesday 00:30 after a "wie Sonntag" Monday) gives shadow. · Needs Maxi: no (plain fix). Its timing (now, or with the window editor) is a planning call.

### [P1] The trunk zero stays after every other country code with one (+33, +44, +31)
Verdict: CONFIRMED as a defect. Severity on its own merits P2, effective P1 under the xhigh rule: the number becomes the callback and transfer-fallback number (`transfer.py:144`), "the fate of a call".
Evidence: The probe gives 5 red. The spoken path reaches it, and not only when the ear writes symbols. `numbers_said("Meine Nummer ist null null drei drei null sechs eins zwei drei vier fünf sechs sieben acht")` gives `{'+330612345678'}`, and "+33 06 ...", "0033 06 ..." and "+44 07700 ..." give the same class (my run). The tablet (`manual.py:86`) and `create_callback` (`callbacks/create.py:54`) also feed `normalize_phone`.
- The caller ID cannot produce it. A foreign caller ID arrives as 00/+ CC NSN without a trunk digit (only German mobiles arrive national, docs/20:710; the dialplan passes `CALLERID(num)` through, `deploy/asterisk/conf/extensions.conf:46`). So the defect is limited to a number the guest says or the team types.
- Expected countries: no doc restricts callback numbers to Germany (docs/03:10 "E.164, normalised at entry"; phone.py says "German spellings first"). #683's own "Required" was the general rule: "one leading 0 after a country code that has a trunk prefix (49, not 39)". The PR narrowed it to 49/43/41 (HISTORY:308), so this is a residual of #683, not a new class.
- Frequency is low: it needs a foreign guest, a callback or card, and a said zero.

Is the proposed general rule safe? Not as written, for three reasons.
(a) A keep-list is fragile, and a missing entry corrupts a correct number, which is worse than leaving a guest's slip. National numbers that begin with 0 exist beyond 39/378/379: Côte d'Ivoire +225 since 2021, Benin +229 since Nov 2024, Congo +242 and Gabon +241. The list changes as plans change.
(b) Hungary's trunk prefix is "06", so a single-0 drop turns "+36 06 30 ..." into another wrong number.
(c) Russia, Belarus and Lithuania use 8 as the trunk prefix, so no zero rule covers them.

Fix: Extend the explicit drop-list (`TRUNK_ZERO_AFTER`) to the trunk-0 countries whose numbers never begin with 0: the neighbours and tourist origins first (33, 44, 31, 32, 353, 46, 358, 385, 386, 90, 380), plus harmless no-trunk codes if wanted (48, 420, 352, 45, 34, 351). The alternative is `phonenumbers` (libphonenumber), which strips national prefixes per country plan; that is a new dependency (docs/15). Keep the `rest[1:2]` guard and the +39 case.
Test: the reviewer's five inputs give the right numbers; "+39 06 ...", "+225 07 ...", "+229 01 ..." keep their zero; the placeholder +49000000000 is unchanged.
Needs Maxi: no for the drop-list. Yes only if libphonenumber is chosen (new dependency).
Residual, not filed: a French guest giving the national form "06 12 34 56 78" gets +49612345678, a plausible German number. No normaliser can know the country, so only the readback or a question can catch it.

### [P2] The tablet stores a number typed without its zero as a foreign number
Verdict: DOWNGRADE to P3
Evidence: The probe gives 3 red. The path is real: `router.py:1282`, then `manual.py:86`, then `normalize_phone`, with the bare-digit branch at `phone.py:31`. Mitigations:
- The field is optional ("freiwillig", `reservierung.html:46`).
- The team typed it itself, and the summary shows the stored form before confirming (`reservierung.html:17`, `draft.phone`).
- No call is involved: a tablet reservation has no `call_id`, no callback card and no transfer.
- The worst case is the team dialling a foreign number by hand when it wants to reach the guest; no call is lost and no booking is wrong.
- The agent path is guarded: `number_known` and `e164` (`guest_details.py:492`) take only 0 or + at the start.
Side note: the data-subject lookup (`requests.py:465`) has the same bare-digit read. It finds nothing on a mistyped number, so it fails safe.
Fix: A domain helper for typed numbers, `phone.py`, refuses input that starts with neither 0, 00 nor +. `manual.py` uses it, and the caller-ID path keeps the bare-digit branch. · Test: "5551234", "7221 5551234" and "171 2345678" through `manual` raise InvalidInput with the say text; "4972215551234" as a caller ID still gives +4972215551234. · Needs Maxi: no

### [P2] 24:00 in opening_hours (and special_days) still breaks every hours read
Verdict: DOWNGRADE to P3 (hardening). Opening hours are an xhigh trigger, but nothing can write the value, so a P2-to-P1 promotion does not apply.
Evidence: The probe gives 1 red (psycopg DataError, "hour must be in 0..23"), but only via a raw SQL insert. Every input path parses to Python `time`, which cannot hold 24:00:
- Provisioning: `schemas/provision.py:45` `Clock = Annotated[time, ...]`. Pydantic refuses "24:00" and "24:00:00" (my run).
- Tablet special-day hours: `router.py:1856` `_hhmm` uses `time.fromisoformat`, which raises.
- `staff_changes.py:148` takes `DayHours` of `time`. `holidays.py:151` writes only `like_weekday`.
- `scripts/seed.py`, `bedientest_data.py:191` and `demo/stack_setup.py:40` use `time(...)`; the last two deliberately use 23:59.
- No migration or script writes '24:00' (grep over db/ scripts/ api/).
- docs/03:90-99 gives opening_hours no 24:00 rule; the 24:00 text sits only under the two window tables, so an operator would have to carry the habit over by hand.
- The failure is loud and ends with the team (rule 5): `call_routing` does not read hours, and tools fail to the outage path.
Fix: A migration adding `CHECK (closes_at < '24:00')`, or `<= '23:59:59.999999'`, on opening_hours.closes_at, opens_at, special_days.opens_at/closes_at and capacity.slot_start/slot_end. Optionally, docs/03 says "00:00 is midnight here". · Test: the migration test refuses the probe's insert; up and down pass. · Needs Maxi: no (plain migration; xhigh routing applies).

### [P2] The refusal asks the team to move or cancel a booking, which the tablet cannot do
Verdict: DOWNGRADE to P3
Evidence: The dead end is confirmed by reading:
- GUI write routes: only `/reservierung` and `/reservierung/{id}/eintragen` touch reservations (router.py:1358, :1385).
- No writer sets a reservation to cancelled anywhere in api/ (grep).
- "Stornieren" on the tablet is for orders only (docs/06:100: "reservations are not part of it").
- No task in docs/07 or docs/01 plans a reservation cancel or move.
- docs/06:153 documents the same text, so the doc and the code agree, and both name actions that do not exist.
Why P3: the impact is on the safe side. The refusal keeps the table and the booking intact, and the guest's booking is not lost or double-booked. It is an instruction the team cannot follow; the workarounds are to keep the table, rename it, or ask the operator.
Fix: The message names what the tablet can do ("Tisch bleibt bis nach der Reservierung; bitte vorher den Betreiber fragen" or similar), and docs/06:153 follows. A real cancel or move for reservations is a feature proposal for Maxi. · Test: a GUI test that the refusal text names no "verlegen" or "stornieren" while no such route exists. · Needs Maxi: no for the wording. Yes if a reservation cancel or move is to be built (product scope).
