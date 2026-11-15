# fix/764-outage-path (worktree /home/user/wt-fix-outage), ledger #764 area C

Commits:
- 2bfeb96a fix(telephony): the outage sentence plays before the team number is read again (#754 follow-up)
- 04bd7924 fix(callbacks): a card without the core's summary names the call's standing booking (#743 follow-up)

1. New port method `TelephonyPort.speak_now`. On `Lines`/`CallSession` it carries out what the turn has asked for so far, in order. On the fake it does nothing. `_fail` calls it after the outage sentence, so the team-number read runs while the sentence plays. The transfer still waits for the end of the sentence, so a slow database can delay the ring by no more than the part of the read that outlasts the sentence (today the whole read comes first). If the mouth fails on that sentence, that failure's own path takes the call to the team: no second read, no second transfer.
2. `team_missed`: when the core wrote no card, the card names the call's standing bookings from the DB by call_id ("Bereits gebucht: Reservierung, 4 Personen, 20.09.2026 19:30 Uhr, Name Mueller"; orders: type, name, code; never a note). This replaces "Anliegen nicht bekannt.". The core's `_BOOKED` now uses the same domain constant.

Files: api/telephony/{port,session,handler}.py, api/telephony/adapters/fake.py, api/domain/callbacks/transfer.py, api/agent/loop.py, tests in test_telephony.py, test_telephony_session.py, test_domain_team_missed.py; docs/04, docs/11, docs/20 (§4, §5 #683 paragraph, database-cut row marked for a new measurement, T-10.8 card text).

Tests: the three probes ran red on main. They are now 8 wire and session tests (red without the fix) plus 5 domain tests. Full suite: 9436 passed, 2 skipped, 1 failed, 13 errors. The 13 errors are the missing `audioop` on Python 3.13 in this sandbox. The 1 failure is test_scripts_db_role (the login check in scripts/db_role.sh against local Postgres auth); it does not touch this change. ruff clean. mypy shows 5 errors, all in files this branch does not touch (speech_bench, stt_whisper, test_telephony_speech_bench).

Drill rows for Maxi on the workbench (`python3 -m scripts.voice_drill`, needs Docker):
- `voice` cut off from the database in the middle of a call (docs/20 §5 T-9.8 table, 12.4 s before #754): write the new time into the row.
- Database stopped (8.3 s row).
- The T-10.2 wire rows "model down" and the outage transfer whose team leg nobody answers: check that the missed card names a booking of the call.

Not done: evals (need `docker compose exec api`). The only change on the conversation path is that `_BOOKED` now comes from a domain constant with the same value.

Open item, not built: a draft that waits for its yes (no booking yet) is still not named on such a card. Proposal: a follow-up that appends the open draft (A, recommended), or leave it as it is (B).
outage evals: 103/103 bestanden (scripted, run by coordinator)
