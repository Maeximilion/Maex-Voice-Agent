# Area C: voice layer, transfer and outage fallback (ledger #764, main @ 69ef3678)

Reviewer: area C subagent (Opus 5.5, high). Worktree `/home/user/rev-c`, detached at 69ef3678.
PRs: #743 (390a4e49), #774 (a2415213), #783 (9f46bbbf), #754 only "outage team number re-read" (7dc6eb7e).
Docker not available: no drill run; dialplan reasoned over, Python side probed.
Baseline: `api/tests/test_telephony.py`, `test_telephony_drill.py`, `test_telephony_voice.py` 286 passed;
11 errors in `test_telephony_voice.py` are `ModuleNotFoundError: audioop` (Python 3.13 in this sandbox,
`tts_piper.py`), unrelated to these PRs.

## Findings

### [P2] Outage path reads the team number before the outage sentence plays
PR: #754 · File: api/telephony/handler.py:674-677 (`_fail`), docs/20_VOICE_LAYER.md:227 · Verified: probe
Trigger: any failed turn on the own voice layer (`CallSession` + `Lines`), e.g. the model or a DB
statement raises inside `on_user_turn`; worst when the database is the outage (network cut to `db`).
Wrong: the comment ("After the sentence has gone out, so a read the line's bound cuts off (2 s) delays
only the transfer, never the words") and docs/20 §5 ("after the outage sentence has gone out") are
false. `_fail` runs inside the session's `_turn`, where `Lines.say` is only queued
(`session._ask` appends to `_asked` while `_turn_thread` is the caller) and carried out when the
handler returns. So `current_team_phone` runs first, then the outage sentence is synthesized and
played. Probe action order: `answer, play(greeting), team_number_read, play(SAY_OUTAGE)`. With the
database cut, the read is also not bounded by 2 s: `live.session.rollback()` released the
connection, so `begin_nested` checks out again: pool pre-ping on a dead socket (tcp_user_timeout
2 s) then a reconnect (connect_timeout 2 s), about 4 s (by reading of `db.bounded_sessions`). The
caller hears that much more silence before "technische Störung" on the slowest row of the outage
map (docs/20 line 371: 12.4 s to the team ring, measured before #754). Rule 5 still holds (the
transfer goes out, the dialplan still rings), hence P2 and not P1.
Required: the outage sentence reaches the line before any database read on the outage path (e.g.
read the number in `_fail` only for the transfer after the turn's queued say is flushed, or keep the
call's number on the `_fail` path and re-read only on the healthy handovers); or, if the order stays,
correct the comment and docs/20 and re-measure the "cut from the database mid-call" drill.
Verification case: `CallSession` + real `CallHandler` with `BrokenLLM`, `current_team_phone`
instrumented to append to `FakeLine.actions`; assert `play(SAY_OUTAGE)` precedes the read.
Probe: probes-c/test_probe_c_order.py (red on main).

### [P2] Hang-up card of a failed call drops the booking that stands
PR: #743 (root cause predates it in `_fail`) · File: api/telephony/handler.py:396-407 (`on_call_ended`), 659-661 (`_fail` → `_keep_named_phone`) · Verified: probe
Trigger: a booking of the call is confirmed (`state.stage == "confirmed"`, `state.booking_say` set),
a later turn raises (DB statement timeout, any non-LLM exception) → `_fail` → outage sentence; the
caller hangs up on it (`caller_hung_up=True`).
Wrong: #743 now writes a card, but its text is "Aufgelegt, bevor das Team klingelte. Anliegen nicht
bekannt." with reason `out_of_scope`: no word of the booking. `_fail` keeps only
`state.handoff_summary`, which only the core's own handover writes, so nothing carries
`booking_say` (or an open read-back / cart) to the card. The core's own handovers append
" Bereits gebucht: ..." precisely so the team does not book a second time (loop.py `_card_tail`,
Codex PR #347, #655). Same text already on the missed-leg card of an outage transfer
(`team_missed` with `SUMMARY_BY_REASON[None]`); #743 adds a second path to it. The same card is
also written when the ear or the playback report fails during the farewell of a finished booking
(`on_voice_failure` → `_fail`) and the guest, who heard "Auf Wiederhören", hangs up.
Required: the `_fail` path keeps a card text with what stands (booked sentence, open draft) like
`_card` does, e.g. set `handoff_summary` from `booking_say` / the open order in `_fail` before
`_keep_named_phone`. Verification case: probe below plus the same with `readback_pending`.
Probe: probes-c/test_probe_c_card.py (red on main: summary lacks "gebucht" / booking sentence).

## Checked and clean

#743 (hang-up on the outage sentence leaves a card)
- `caller_hung_up` is only true for session outcome `closed` with the line answered
  (`_closed_unanswered` false): an outage transfer that reached the line ends the session as
  `transfer`, a later close is ignored, so no card on top of the missed-leg card; no double card.
- Outcome stays `error` (not `abandoned`): `outcome is None` guard; `team_missed` keeps `error`.
- Withheld number: `team_missed` returns None, no card, outcome stays `error` (test exists).
- Transfer refused in `_fail`: the call is claimed and finished there (`caller_waits=True`), the
  later `on_call_ended` finds nothing; dialplan rings the team. No card path confused.
- Mode shadow with a failed recording step: one `_card` call even when both conditions hold.
- Leg closed before our first sentence (SAY_OUTAGE_FIRST not yet played): no card (#652 rule holds).
- Named number of the guest is kept by `_keep_named_phone` in `_fail` and used for the card.

#754, outage team-number re-read only
- `_team_phone` never raises (catches Exception), so the transfer in `_fail` always follows; on a
  read failure the call's own number is used (probe of the existing test confirmed).
- Savepoint after an aborted outer transaction: SAVEPOINT fails, caught, old number used.
- `now` passed as `self._now` (None in production → `utcnow()`), `team_phone_at` honours today's
  number window; a number not on TRANSFER_TARGETS/TEAM_PHONE is replaced by the dialplan.
- `_fail` on a call whose end another thread handled (`still_open` False) would read on that
  thread's session; not reachable in production (one worker per `CallSession`, `CallHandler` built
  only in `telephony/app.py`), so not reported.

#774 (drill fails when Asterisk dies; model outage logged as outage)
- `_TRANSFER_REASONS` excludes `outage`: a model naming it hands over as `not_understood` (both
  model paths use `_transfer_reason`).
- `outage` reaching `create_callback`: mapped to `not_understood` in `_handoff` (also on the
  `_callback_answered` re-entry, which passes `state.callback_reason` back through `_handoff`) and
  in `team_missed._card_reason`; no CHECK constraint on `calls.transfer_reason` (migration 001,
  plain TEXT); GUI and n8n read only callback reasons; `evals/judge.py` only tests truthiness.
- `run_scenarios`: crash attributed to the scenario during which `asterisk_life` changed; the
  only scenario that restarts Asterisk (`drill_voice_down_calls`) does so only after it died;
  `restart("voice")`/`other_media_token` do not recreate Asterisk (it depends on voice, not the
  reverse); a scenario that raises still appends the Asterisk check (finally) before main's
  DrillError handler.

#783 (dialplan filters)
- Caller number: `FROM=${FILTER(+0-9,...)}` before the loop compare; empty (withheld) FROM never
  equals OWN/OWN_INTL/OWN_NATIONAL; no other `$[...]` reads CALLERID; `__VOICE_CALLER` raw only in
  a Set (no expression parse) and bounded in the handler (`CALLER_ID_MAX_LENGTH`).
- ENDING filtered to `a-z +0-9`: no quote, comma, paren, pipe or `&` can reach `$[...]`, `ExecIf`,
  `Log` or `FIELDNUM`; voice's answers ("hangup", "transfer +49...", "record +49...", "") are
  PlainTextResponse in that alphabet; error bodies (`{"detail":"Unauthorized"}`,
  "Internal Server Error") filter to strings matching nothing → `toteam` → TEAM_PHONE.
- Empty or failed CURL: ENDING "" → not hangup, not transfer → TEAM_PHONE.
- CARD filtered to `a-z`: only "noted" plays missed_noted; anything else missed_later.
- Every branch ends at the team or by design: loop → Hangup(21) for our own number only,
  ready-probe failure and CHANUNAVAIL → unreached → TEAM_PHONE, listed target → second leg at
  TEAM_PHONE → missed card; only an explicit "hangup" reaches `done`.
- The unfiltered `/media/ready` compare uses `!=`, so a broken expression (true) goes to the team.
