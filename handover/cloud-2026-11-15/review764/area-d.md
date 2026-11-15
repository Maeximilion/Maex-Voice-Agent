# Area D review notes: access, staff, migrations (#755, #757, #754 staff parts)

Main @ 69ef3678, worktree /home/user/rev-d. Probe file (all three tests red on main):
`probes-d/test_probe_d_staff.py` (copy of /home/user/rev-d/api/tests/test_probe_d_staff.py; it imports
the fixtures of `api/tests/test_domain_staff_changes.py`, run it from the worktree with testenv.sh).

Counts: P0 0, P1 0, P2 3.

## #755 owner idle clock, migration 038

No finding. Migration 038 run up and down on a scratch database (`revd_mig038_*`, dropped after) with
a populated `gui_sessions` row (admin_until 50 minutes ahead): up leaves `admin_active_at` NULL, so
`resolve` gives `admin=False` at once (documented in the migration and docs/03); down drops the column
and keeps the row and its `admin_until`; `upgrade head` again clean. See "Checked and clean".

## #757 held closure lists the confirmed tables to call

### [P2] Today's closure lists tables whose time has already passed
PR: #757 · File: api/domain/status/staff_changes.py:406 (`bookings_outside_hours`), api/gui/router.py:1612 · Verified: probe
Trigger: weekly Tuesday hours; a confirmed table at 12:00 and one at 19:00 today. At 15:00 the staff
close today (held at once, the kitchen-down case the feature is for).
Wrong: the card lists both under "Diese Gäste bitte selbst anrufen", the 12:00 lunch party too. It
has eaten and left: reservations have no status after `confirmed` (only draft/confirmed/cancelled),
and the function takes no `now`, so every table of the business day is listed, also those seated
right now. The team calls guests who already came, and on a busy day the useful rows get lost in it.
Required: on today's card list only tables with `reserved_for` at or after now (or a stated grace,
for example still-arriving tables), the card list fed with the request's time. Verification case:
today closed at 15:00 with tables at 12:00 and 19:00 lists only 19:00; the SSE token must then also
follow time passing, or the list is computed on render only.
Probe: probes-d/test_probe_d_staff.py::test_757_today_closure_lists_tables_already_past (red:
`['LunchDone', 'Evening'] != ['Evening']`)

### [P2] A shortening lists override tables that were outside the hours before too
PR: #757 · File: api/domain/status/staff_changes.py:428-454 · Verified: probe
Trigger: Saturday holds special hours 17:00-22:00; the team booked a table at 15:30 on its own word
(tablet `override`, `domain/reservations/manual.py`: "a time outside every window is booked only on
the team's word"), and one at 21:30. Staff shorten to 17:00-21:00.
Wrong: both are listed. The candidate span is the whole business day plus the old windows, so a
table that no old window held passes the span filter, and the new hours do not hold it either. The
change never touched the 15:30 table, yet the card asks the team to call that guest about the
shortened hours, inviting a cancellation of a booking the team accepted deliberately. The docstring
says "the tables ... that `hours` would leave without a dine-in window"; this one had none before.
Required: on a shortening (not a closure) list only tables that an old window held
(`any(a <= t < b for a, b in dinein(before))`, including neighbour-day windows) and no new window
holds. Verification case: the probe above lists only "Late".
Probe: probes-d/test_probe_d_staff.py::test_757_shortening_lists_an_override_table_it_never_touched
(red: `['Override', 'Late'] != ['Late']`)

## #754 staff parts (open items of #596, batch 9 of #546)

### [P2] Close then reopen today drops "wie Sonntag" without the owner
PR: #754 · File: api/domain/status/staff_changes.py:276 · Verified: probe
Trigger: today (Tuesday) is a special day `like_weekday=6` ("wie Sonntag"). Staff close today (held at
once, closing drops nothing), then send times 12:00-13:00 for today on the same card.
Wrong: the second change holds at once. `held` checks `_drops_weekday(hours, holding)` against the
row that holds now, which after the closure is `closed` with `like_weekday=None`, not against the
card's `origin` (`card.before`, still "wie Sonntag"). The times are written, the date loses Sunday's
seats, call mode and call limit for today without the owner, the exact case the #710 fix
(`test_shorter_times_today_on_a_day_like_another_weekday_wait_too`) closes for the direct path. The
owner's undo restores it later, but today runs on the plain weekday's capacity meanwhile. The same
holds for close, then "normale Zeiten" today.
Required: the weekday check also against the card's origin, `_drops_weekday(hours, holding) or
_drops_weekday(hours, origin)` (the `_within` path for later dates is safe: closed old windows make
any times wait). Verification case: the probe; expected `not card.held` and the row still closed.
Probe: probes-d/test_probe_d_staff.py::test_754_close_then_reopen_today_keeps_like_weekday (red:
`card.held is True`)

## Checked and clean

### #755
- Migration 038 up and down with a live owner row: up gives NULL, rights end at once (documented); down keeps rows; head again clean. Existing `test_migration_038` covers the column.
- `resolve` needs `admin_until > now` and `admin_active_at` set and `now - admin_active_at < 15 min` and not locked; boundary at exactly 15 minutes is not admin, consistent with `IDLE`.
- A clock that never expires: `admin_active_at` is only ever set to `now`; a future value would still be capped by `admin_until` (60 minutes). No path sets `admin_until` without `admin_active_at` (unlock_admin is the only writer of both).
- Every path that ends owner rights clears both columns: `close_admin`, team-PIN `unlock`, `set_admin_pin` (all devices); `use_link` ends the sessions; `set_team_pin` leaves both, which is fine (it ends nothing by design).
- `mark_admin_active` cannot reopen rights: it runs only after `access.admin` held in `require_admin`, and its UPDATE matches no row when the clock is NULL (`NULL <= x`), so a concurrent `close_admin`, team-PIN unlock or `set_admin_pin` committed first wins under READ COMMITTED; if it lands after, those writes clear it again; `admin_until` NULL closes rights anyway.
- Throttle `TOUCH_EVERY` (1 minute) can only shorten the owner's 15 minutes by under a minute, never lengthen them.
- Staff pages and writes never move it: only `require_admin` calls `mark_admin_active`; `_checked_access` calls `mark_active` only. Owner routes: `/gui/geraete` GET, `/gui/geraete/{id}/abmelden`, `/gui/pruefen/{card}/{choice}`; no owner page polls (no hx-trigger/SSE on them), the check-card fragment GET is a staff route, so an open owner page does not keep the clock alive.
- Same request DB session: `require_gui_session` and `require_admin` share the cached `get_db`; the rollback in `require_gui_session` happens before `mark_admin_active`; its commit before the route body breaks nothing.
- UTC: `clock.utcnow()` is aware UTC, columns are TIMESTAMPTZ; no local time involved.
- Step-up and fixation: the admin PIN does not rotate the session token; pre-existing since #587, the token is HttpOnly, `__Host-`, SameSite=Lax outside dev, and shared by design on the device; no regression in #755.
- Locked device on an owner write goes to the owner PIN page (`_needs_admin`), and the admin PIN sets both clocks; no loop.

### #757
- Status filter: only `confirmed`, `deleted_at IS NULL`; drafts, cancelled and soft-deleted stay off (tests and reading). `source = tablet` tables are included (no source filter), as they should be.
- Date boundaries in Europe/Berlin: span from `business_day_bounds_utc(day, zone)` plus the old windows; DST handled by zone-aware combine.
- After midnight: a 00:30 table held by the date's 18:00-01:00 window is listed when that window closes or is cut before it; a table held by the next date's own window (02:00-04:00) or the previous date's window stays off (`open_window_at` checks the day before; `load_hours` loads day-1..day+1).
- A table before 05:00 of the date that belongs to the previous business day is not in the span unless an old window of the date covers it.
- The card list only computes for held cards whose holding differs from `before`; a waiting card holds `before`, so no list.
- Last-start rule: a table that starts inside the new window but after `closes - reservation_last_start_minutes` is kept; the tests assert it on purpose ("Inside" 13:00 with close 13:30), so not reported.
- Template: guest name autoescaped by Jinja; costly numbers get no `tel:` link.
- SSE token: empty when no open card; otherwise count and max `updated_at` of confirmed tables from today's business day, so a cancel, a soft delete, a move or a new table refreshes the card.
- Transient `SpecialDay` rows from `DayHours.row` carry only `tenant_id`, no relationship, so autoflush never inserts them.

### #754 staff parts
- `_hhmm`: 5-character inputs with a zone (`1000Z`, `10+02`, `10-02`) are refused; `T1000` parses to a naive 10:00, harmless; no 5-character input yields seconds.
- `sheet_numbers`: only E.164 entries offered and accepted; `change_team_phone` normalises first, so a typed national form of a listed E.164 number is accepted; `dialable_numbers` unchanged for `provision`.
- `_drops_weekday` in `_within`: later dates, including close-then-times on a like-weekday date, wait (closed old windows hold no times). Closing still holds at once. "Normale Zeiten" on a like-weekday date waits on the direct path.
- The telephony "team number re-read" part was not reviewed (other reviewer).
