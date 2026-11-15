# Cross-cutting wave notes (ledger #764), main @ 69ef3678

Angles: races and idempotency, time, data (call_id, audit_log, personal data in logs), injection and CSRF.
Skipped and named: DST and the night after midnight (area B, #750); migration 038 up/down and owner idle
clock under concurrency (area D). area-c.md did not exist when this wave started.

area-c.md appeared during the wave: it covers the telephony side of #743, #774, #783 and the #754 team-number
re-read (including the say-before-read order and the hang-up card text). Not repeated here.

Counts: P0 0, P1 0, P2 1.

## Findings

### [P2] Guest list to call lives only on an open held card: owner's keep erases it
PR: #757 (with #754) · File: api/gui/router.py:1612-1622 (`_check_rows`), api/domain/status/staff_changes.py:389-403 (`open_cards`), :332-386 (`decide`) · Verified: probe
Trigger: (a) staff close a date at the tablet (held at once, the card lists "Familie Bauer"); the owner
taps "Als Standard übernehmen" on another device before the staff phoned. (b) a "wie Sonntag" date
(#754: its times wait for the owner even when shorter); staff set 17:00-22:00, a 12:00 table is
outside; the owner keeps the waiting card.
Wrong: the list is computed only for an open card whose holding differs from `before`. (a) `decide`
closes the card, `cards_change_token` moves, every tablet re-renders and the guest list is gone, though
nobody called the guests yet; the owner's keep has nothing to do with whether they were called.
(b) a waiting card holds `before`, so no list; the keep writes the shorter hours and closes the card in
the same step, so the 12:00 guest is never listed anywhere. #754 made (b) reachable for today too
(today's times on a like-weekday date wait now). Nothing else in the GUI names the tables a closure or
shortening stranded (`bookings_outside_hours` has one caller), so the guest comes to a closed
restaurant (the case #710 exists for).
Required: the list of stranded tables stays visible until the team has dealt with it, independent of
the owner's decision (e.g. computed for every date from today whose holding leaves confirmed tables
without a window, or a list kept with the card after it closes), and a waiting card shows the tables
its change would strand so the owner's keep is informed. Verification case: the two probes below
(both red on main: "list gone once the owner kept the closure", "12:00 guest never listed").
Probe: probes-cross/test_probe_cross_757.py (imports the fixtures of api/tests/test_gui_staff_changes.py)

## Checked and clean

Races and idempotency
- #754/#757 two tablets at once: every staff change and owner decision takes `_locked` (FOR UPDATE on
  `service_config`), so they run one after the other; `confirm` takes the same row FOR SHARE plus the
  day's capacity advisory lock, so a closure and a caller's yes cannot interleave; a table confirmed just
  before the closure is in the list (computed at render, `cards_change_token` counts confirmed tables).
  The one interleaving left (closure, then times on a like-weekday date) is area D's #754 finding.
- #757 adds no write: `bookings_outside_hours`, `_check_rows` and the token only read; the owner's
  `gesehen` check is unaffected by the list.
- #743 card written twice: `caller_hung_up` is true only for a session that ended `closed` before our
  transfer, so `on_team_missed` never follows for the same leg; a second writer would still find the open
  card (`create_callback`: per-call advisory lock plus one open card per call, `team_missed` row lock on
  the call). `_fail` and the hang-up meet in `_claim` under the handler lock: one `_finish`, one card.
  Events of one call run on one `CallSession` worker, so `_team_phone` on `live.session` cannot meet
  `_finish` on another thread in production (area C names the same).
- No PR of the batch touches `api/tools/` or a write tool's idempotency key path; `transfer_to_team`
  with the new `outage` reason (#774) still sets `calls.transfer_reason` and its audit once per call under
  the call row lock.
- No new SELECT-then-INSERT: the new writes are `mark_admin_active` (conditional UPDATE, area D),
  `change_day_hours`/`change_team_phone` (under `_locked`, row lock on the special day) and the #743
  card (existing locked path).

Time
- New code stores and compares aware UTC: `bookings_outside_hours` (TIMESTAMPTZ, `astimezone(tz)` only
  for display), `current_team_phone` (`self._now or utcnow()`, `team_phone_at` vs the UTC
  `team_phone_today_until`), `mark_admin_active` (`clock.utcnow()`), `change_team_phone` end of the
  business day from `business_day_bounds_utc(today, zone)`. `_hhmm` returns a naive time of day, which is
  what the TIME columns hold; #754 refuses one with an offset.
- `cards_change_token` bounds tables by `business_day_bounds_utc(today)` without the tenant zone
  (settings default): same zone for the pilot; tables before the cutoff of the card's date are only
  missed once they are past.
- No decision in the batch reads `now` twice where it matters (#755's two reads are area D's).

Data
- call_id: the #743 card goes through `team_missed` with the call's own id; #774's outage handover
  writes `transfer_reason` on the call row; #754/#757 staff writes are tablet config changes, not call
  transactions, and carry `audit_log` (`staff_change.day_hours`, `.team_phone`, `.kept/.undone`).
- audit_log on new write paths: #743 card (`callback.created`, actor `telephony`), #774 transfer
  (`call.transferred`), #754 staff changes; `admin_active_at` like `last_active_at` is an activity mark,
  not audited by design.
- Personal data in logs: the only new log line (#754 `_team_phone`, `logger.exception`) carries the
  session id and a DB or NotFound error, no number; `_card` logs only the error type; no new raise
  message carries a number, name or guest sentence; #783 logs `ENDING:0:8` and `CARD:0:5` after the new
  filters, no digit of a number fits; `FROM` is set, never logged. #774's Asterisk check prints container
  state only.

Injection and CSRF
- No PR adds a GUI route: all 27 POST routes keep `_require_htmx` (header CSRF token) and the session
  guard's `same_origin` for writes. Starlette's `Jinja2Templates` autoescapes; no `|safe` or `Markup`
  anywhere in `api/gui`. #757's guest name and phone are escaped; `tel:` gets only a non-costly stored
  number, `hx-vals` gets `updated_at.isoformat()`.
- Considered, not reported: #755 moves `admin_active_at` on owner GET pages, which have no same-origin
  check (GETs never had one); a cross-site top-level GET could refresh the owner clock, but only on a
  device whose staff keep it unlocked by writes and only up to `admin_until`; no concrete attacker path
  on the tablet.
- #783 dialplan: covered by area C.
