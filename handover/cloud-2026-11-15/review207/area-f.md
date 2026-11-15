# Area F: opening hours, call mode, bookings, phone numbers (review #207, main 69ef3678)

PRs: #724 (floor plan, #680), #725 (24:00 window, bridge, #682), #729 (special day weekday, #686), #726 (trunk zero, #683).
Probes: /tmp/claude-0/-home-user-Maex-Voice-Agent/57c61f4a-774e-5270-a000-171ac1882a8d/scratchpad/review207/probes-f/

## #729 special day like Sunday takes Sunday's call mode and call limit

### [P2] A 00:00 mode or limit row follows the wrong day around a special day
PR: #729 · File: api/domain/status/mode.py:62, api/domain/calls/routing.py:113 (weekday_of, api/domain/status/hours.py:60) · Verified: probe
Trigger: Mode windows Sunday 18:00-24:00 primary plus Monday 00:00-01:00 primary (one window across midnight is two rows, docs/03), same for call_limit_windows (max 3), default mode shadow, Sunday pickup hours 18:00-01:00. Monday 23.11.2026 set "wie Sonntag". (a) Monday 00:30, still the plain Sunday's evening: `weekday_of(Monday)` = 6 picks Sunday's 00:00 rows (Saturday night's spill), none, so mode shadow and limit 1. (b) Tuesday 00:30, the spill of the Sunday-like Monday evening: Tuesday's own 00:00 rows are read (the spill of a plain Monday), none, so shadow again. A plain Monday 00:30 (control) gives primary and 3.
Wrong: the hours already follow the evening's day across midnight (`open_window_at` looks at the day before with its own special day), but the mode and the limit pick the 00:00 row by the calendar date's special day. After midnight hours and mode diverge again, the exact #686 class: around a holiday the agent stays off (or answers, or skips the "Passt" step in `confirm`, which reads the same `mode_at`) against the plan, and the wrong call limit applies.
Required: a row starting at 00:00 that continues the evening before (or simply every window before the day's first opening) takes the weekday of the day before (`weekday_of(day - 1)` when the previous day's mapping differs), or the windows are stored as one row past midnight like opening_hours; verification case: the two probe cases above go green, the control stays green.
Probe: probes-f/test_probe_f_weekday.py (2 red, control green)

## #726 trunk zero after the country code

### [P1] The trunk zero stays after every other country code with one (+33, +44, +31)
PR: #726 · File: api/domain/customers/phone.py:11 (TRUNK_ZERO_AFTER = 49, 43, 41) · Verified: probe
Trigger: `normalize_phone("+33 06 12 34 56 78")` = +330612345678, `"0033 06 12 34 56 78"` the same, `"+44 07700 900123"` = +4407700900123, `"+31 06 12345678"` = +310612345678, `"+48 0 600 123 456"` = +480600123456. Same through `guest_details.e164` (the spoken path), all pass E.164.
Wrong: #683's class is "a trunk zero said after a country code", not three countries. The fix lists the countries whose zero goes instead of the few whose numbers do start with 0 (Italy +39, San Marino +378, Vatican +379; Côte d'Ivoire +225, Congo +242 and a few others). A French, British or Dutch guest (the pilot's area code 07221 is about 60 km from Strasbourg) who gives the number with the zero gets a callback number that does not exist. The number they said beats the caller ID (`transfer.py:141`) and `own_line` compares unequal, so the team calls nobody back. #683 rated this a P1 (the fate of a call); for the countries left out the impact is the same.
Required: one 0 right after any country code is dropped unless the country's own numbers begin with 0 (a short keep-list: 39, 378, 379, and the African codes that use a leading 0), the placeholder +49000000000 still stays as it is; verification case: the five inputs above give +33612345678, +447700900123, +31612345678, +48600123456, and the Italian cases keep their zero.
Probe: probes-f/test_probe_f_phone.py::test_trunk_zero_of_other_countries (5 red)

### [P2] The tablet stores a number typed without its zero as a foreign number
PR: #726 (same class, older code) · File: api/domain/customers/phone.py:31, reached via api/gui/router.py:1282 -> api/domain/reservations/manual.py:86 · Verified: probe (normalize_phone) and reading (path)
Trigger: The team enters a tablet reservation with "telefon" = "5551234" (local number, no area code), "7221 5551234" (area code without its 0) or "171 2345678" (mobile without its 0).
Wrong: the branch for caller IDs without a plus (`elif digits and not digits.startswith("+")`) makes them +5551234 (Brazil), +72215551234 (Kazakhstan) and +1712345678 (USA). All pass E.164 and are stored on the reservation, so the team's callback dials abroad. The spoken path refuses these (`guest_details.e164` takes only 0 or + at the start); the tablet path does not.
Required: a number typed on the tablet that begins neither with 0, 00 nor + is refused with the say text (or read as national with the tenant's country code only where it has an area code); the caller-ID path keeps its bare-digit form; verification case: the three inputs through `manual` raise InvalidInput, "4972215551234" as a caller ID still gives +4972215551234.
Probe: probes-f/test_probe_f_phone.py::test_a_number_without_zero_or_plus_is_no_foreign_number (3 red)

## #725 24:00 mode window, bridge alarm and order switches

### [P2] 24:00 in opening_hours (and special_days) still breaks every hours read
PR: #725 · File: api/domain/status/hours.py:29 (regular_hours), api/models/tenants.py (opening_hours has no check) · Verified: probe
Trigger: `opening_hours` row Friday pickup 18:00 to `24:00`, written by SQL as the window tables are (docs/03 now teaches 24:00 as the end at midnight for the two window tables; no editor exists for the weekly hours either).
Wrong: Postgres takes it, `regular_hours` loads every row of the tenant through the ORM and psycopg raises DataError ("hour must be in 0..23"). The read is not per day: every `load_hours` fails all week, so slots, `service` status, `_team_reachable` in `transfer_to_team` (`transfer.py:224`), staff changes and the bridge alarm all fail. The calls then go down the outage path to the team (rule 5 holds), but the agent cannot book or hand over normally for any time. #725 fixed the read for one table of the class and left the others with the same column type and no check.
Required: one rule for all hours TIME columns: either a check refusing 24:00 on opening_hours.closes_at, special_days.closes_at and capacity.slot_end (00:00 already means "to midnight" there), or the same text read; verification case: the probe row is refused by the database or read as open to midnight.
Probe: probes-f/test_probe_f_midnight.py (1 red, DataError)

## #724 booked table never removed silently

### [P2] The refusal asks the team to move or cancel a booking, which the tablet cannot do
PR: #724 · File: api/domain/reservations/floor_plan.py:451-456, docs/06_GUI.md · Verified: reading
Trigger: T5 holds a confirmed tablet reservation in three weeks; T5 breaks and the team removes it in the floor plan editor.
Wrong: the save is refused with "Bitte die Reservierung zuerst verlegen oder stornieren." There is no tablet route, domain function or tool that moves a reservation to another table or cancels one (`api/gui/router.py` write routes: only `/reservierung` and `/reservierung/{id}/eintragen`; no `status = "cancelled"` writer for reservations anywhere in `api/`). The team cannot follow the instruction; the table can leave the plan only once its last booking is six hours past, or by renaming it, which the message does not say. Not a booking error, a dead end the fix opened.
Required: either a way to move or cancel the booking from the refusal, or a message that names what the tablet can do (keep the table, rename it, or ask the operator), and docs/06 says the same; verification case: GUI test that the refusal's text names only actions that exist.

## Checked and clean

#726 (phone):
- "+49 (0) 7221 5551234", "0049 0 7221 5551234", "+4907221 5551234", "+49 0 171 ...", "0049 (0) 171 ...", "+49 (0) 0171 ..." all give the right +49 number (probe run).
- "0 7221 5551234" gives +4972215551234; empty input raises InvalidInput (number_or_none gives None); "+490" alone is refused.
- +43 0664 and +41 079 are dropped correctly, +39 06 keeps its zero, the placeholder +49000000000 and "+49 00 ..." are left alone (rest[1:2] "0" guard).
- Short service numbers 110, 112, 116117, 19222 are refused (E.164 needs 7 digits); the spoken path (`guest_details.e164`) refuses numbers without 0 or + at the start.
- `change_team_phone` only takes numbers from the sheet list, so a mis-normalized team number cannot be set.
- The handler comment (team number read once for the outage and shadow legs) matches `current_team_phone` and docs/20; no code change to check.

#725 (24:00, bridge):
- `mode_at` with a 24:00 row: WHERE compares in SQL ('24:00' > 23:30 holds), the end becomes the next midnight; DST nights 25.10.2026 (02:30 both passes, 23:30) and 29.03.2026 (03:30 after the gap, 23:30) give primary and planned_until = next local midnight (probe, 5 green).
- `call_limit_windows` reads only `max_calls`, so its 24:00 never reaches Python; no other reader of either window table (`grep` over api, scripts).
- `bridge.silence` with config None keeps both services; one service on keeps the alarm; the monitor opens a fresh session per pass (`cold_path.watch_forever`), so the switch is not read stale.
- Noted, not filed: switching a service back on mid-window with the register PC still off alarms at once (the "opening starts the clock" grace is not restarted on re-enable); noise only, safe side.

#729 (special day weekday):
- `weekday_of` filters status "set" and like_weekday not null, as `load_hours` does; a suggestion does not count.
- A closed or own-hours special day has like_weekday NULL by `ck_special_days_like_weekday_alone`, so it keeps its own weekday's mode and limit; closing a "wie Sonntag" day (staff_changes:267, holidays:240) drops the Sunday mapping for mode and limit too. Consistent with the hours (closed beats all); the reopen side is #754's known finding, not refiled.
- `capacity_windows` refactor is behaviour-preserving (same query); capacity windows wrap midnight themselves and check_slot reads the day before with its own weekday_of, so seats have no spill gap. `notice.py` follows capacity, `confirm.py:116` reads `mode_at`, so both follow the fix.
- A special day "like Sunday" on a weekday that is itself closed takes Sunday's hours, mode and limit (the PR's tests plus reading).

#724 (floor plan):
- Removal of a table with a confirmed booking ahead or begun within 6 h is refused; past, cancelled and draft bookings do not block (PR tests, reading).
- Race with `confirm_tablet`: the save locks the removed tables FOR UPDATE before it reads bookings; `seating` locks FOR SHARE with `deleted_at IS NULL`, re-evaluated after the wait in READ COMMITTED, so a confirm after the save finds the table gone; a removed pair is re-read after the wait too.
- Only the floor plan writes `dining_tables.deleted_at`; rooms cannot be deleted, so no second removal path. Only the tablet writes `reservation_tables`, so agent bookings hold no table.
- A renamed or moved table keeps its id and its booking; undo of a removed table whose name a new table took is refused by the name check.
