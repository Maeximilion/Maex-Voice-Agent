# fix/764-dialog-batch (worktree /home/user/wt-fix-dialog), not pushed

Commits:
- 3ae30c67 fix(agent): an order's own wish word in front of a complaint keeps the order for the team (#744 follow-up)
- 7699a361 fix(reservations): a draft made after midnight names the day by weekday and date (#750 follow-up)

1. `_WANTS_TO_COMPLAIN` (api/agent/escalation.py): the gap between the wish and "beschwer"/"reklam" holds only the guest, filler, the person complained to, a pronoun ("das reklamieren") or what follows "über"/"wegen"; the four-word bound stays. All four probe sentences go to the team. Trade-off: "Ich möchte die Pizza reklamieren" now goes to the team too (the safe side, written down as a shortcut in the code and in docs/05).
2. `spoken_when` (api/domain/reservations/spoken.py) takes the draft's local creation moment. From 00:00 to 04:59 it says weekday and date, never heute/morgen; this is the docs/04 read-back format. docs/04 synced.

Tests: the probes became tests in test_agent_escalation.py, test_agent_complaint_rules.py and test_domain_create_reservation.py (13 + 5 red on main, all green now, both reviewer probes green). Evals before and after: 103/103, 0 hard misses, wrong escalation 0.0%. Full suite after the Postgres restart: 9449 passed, 1 failed, 13 errors. None of these are in files I touched. The failure is test_scripts_db_role (db_role.sh login check against the local Postgres). The errors are telephony tests that cannot import their speech packages here. mypy: 5 pre-existing numpy errors in telephony files.

Open decision (Maxi): should "Ich möchte die Pizza reklamieren" keep getting the remedy offer? A: no, the team reads it (recommended, as built). B: allow an article plus a noun directly before "reklamieren".
