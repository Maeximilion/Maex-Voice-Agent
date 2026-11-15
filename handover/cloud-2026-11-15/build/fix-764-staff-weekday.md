# fix/764-staff-weekday

Branch fix/764-staff-weekday, commit e9263586 (one commit, no attribution, not pushed).

Fix: in `change_day_hours`, today's clause also checks the card's origin with `_drops_weekday`. After a held closure on a "wie Sonntag" date, times or the weekly hours sent later now wait for the owner. Before, they held at once. That bypassed #710.

Files: api/domain/status/staff_changes.py, api/tests/test_domain_staff_changes.py (3 new tests: own times and the weekly hours after a closure wait, and the owner's undo restores "wie Sonntag"; on a plain day the times still hold at once. Both waiting cases were red before the fix), docs/06_GUI.md.

Tests: staff files 81 passed. The reviewer's probe is green. Full suite: 9425 passed, 2 skipped, 1 failed, 13 errors, all from the environment: `audioop` is missing for the voice tests, and test_scripts_db_role fails because local Postgres accepts the login. ruff is clean. mypy has 5 errors, already on main, in telephony.

Open decisions: none.
