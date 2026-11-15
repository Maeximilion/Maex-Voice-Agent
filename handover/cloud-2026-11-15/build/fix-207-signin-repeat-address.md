# fix/207-signin-repeat-address

Branch fix/207-signin-repeat-address (worktree /home/user/wt-fix-signin), one commit 8e6d1e14
"fix(dashboard): a replayed sign-in code from another address stays a factor alarm". No AI attribution.

Change: `api/dashboard/access.py` `_just_signed_in` takes the address `sign_in` already receives
(`client_address`, last X-Forwarded-For entry set by Caddy, trusted only outside dev/test). `repeat` now
also needs the address of the account's last TOTP `operator.signed_in` audit row to equal the refused
post's address; otherwise `factor`. Recovery-code sign-ins are skipped (they leave the TOTP step unchanged).
No migration, no device cookie as key.

Tests (`api/tests/test_dashboard_login.py`): three new, red on main first (reviewer's probe as
`test_the_code_just_used_sent_from_another_address_is_a_factor_alarm`, plus recovery sign-in elsewhere and
a later sign-in from another address). File: 36 passed; the PR's double-tap test stays green; probe green.
Docs: docs/09 runbook line 156 and docs/23 section 5 describe `repeat` with the address.

Lint: ruff format and check clean; mypy has 5 errors in api/telephony speech files, identical on main
(local numpy).

Full suite: 9425 passed, 2 skipped, 1 failed, 13 errors. None are in the change: 13 telephony errors
(`audioop` missing on local Python), 1 `test_scripts_db_role` failure (local Postgres login rule), both
from the environment; dashboard and db_role re-run after the Postgres restart: 70 passed, same db_role
failure.

Open decisions for Maxi: none.
