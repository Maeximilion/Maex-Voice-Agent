# Area G review notes (main @ 69ef3678)

PRs: #712 #713 #731 #732 #730 #734 #728

## #712 (idle lock keeps the owner's click, signed-out device's stream ends)

No finding. See "Checked and clean".

## #713 (device and cookies slide with use, no try without a team PIN)

No finding. See "Checked and clean".

## #731 (dashboard sign-in wait tells no names, double tap raises no takeover alarm)

### [P2] Replayed TOTP code from another address is audited `repeat`, alarm suppressed
PR: #731 · File: api/dashboard/access.py:331 (`_just_signed_in`) · Verified: probe
Trigger: operator signs in from 198.51.100.7 with code C; within 2 minutes and the same 30 s step, a second
`sign_in` with the right password and the same C from 203.0.113.9 without the operator's device cookie.
Wrong: `_just_signed_in` only asks "does this account have any session created in the last 2 minutes and is C
the last used step"; it does not compare the device cookie or the address. The replay is audited
`operator.sign_in_failed` with `reason` `repeat`, which docs/09:156 (changed in this PR) tells the operator to
read as "a double tap, no alarm". That is exactly the real-time phishing / shoulder-surf case where the
password is known and the attacker races the operator's code: the one audit signal that the password is
known (`factor`) is suppressed. Issue #688 asked for "a code refused only because this account just used it
in a sign-in of the same device".
Required: `repeat` only when the refused post comes from the same device (device cookie of the session that
just signed in) or at least the same address as that `operator.signed_in`; otherwise `factor`. Verification
case: sign in from A, replay the code from B without device cookie -> reason `factor`; same post from A with
the device cookie the first answer set -> `repeat`.
Probe: probes-g/test_probe_g_dashboard_repeat.py (red: `[('repeat', '203.0.113.9')]`)

## #732 (no LAN bind reopened by reset, board maintenance in its own group)

No finding. See "Checked and clean".

## #730 (provisioning: no broken shared Caddy, placeholder number or shared token)

### [P2] Gap hint "TEAM_PHONE in .env" does not clear the stored placeholder number
PR: #730 · File: api/domain/provision.py:184 (with `create_tenant` rerun at :70) · Verified: probe
Trigger: customer file without `team_phone`, `.env` still `+49000000000`; `new_stack.py` provisions, the
report says `still missing: team number, still the placeholder (TEAM_PHONE in .env)`. The operator sets
TEAM_PHONE=+4972215559999 in .env, restarts, reruns.
Wrong: the rerun finds the tenant by key and changes nothing, so `service_config.team_phone` stays
`+49000000000`; `call_routing().team_phone` (what `transfer_to_team` and the outage fallback dial) is now a
number that is not in `dialable_numbers()` any more (the dialplan no longer dials it), and the gap keeps
showing. The remedy the operator is told does not fix the state; the real fix (a staff card for the number
plus the owner's "keep" behind the admin PIN) is named nowhere. Rule 5 path (transfers ring nowhere) if the
tenant is switched live with the gap still listed; mitigated by `shadow` and the gap staying visible.
Required: the remedy that `missing()` names actually clears the gap (e.g. a provisioning step or
`python -m api.jobs.provision` subcommand that sets `service_config.team_phone` from a dialable number, or the
gap text names the GUI path); verification case: placeholder tenant, set TEAM_PHONE, follow the named
remedy -> `missing()` has no team-number gap and `call_routing().team_phone in dialable_numbers()`.
Probe: probes-g/test_probe_g_team_placeholder.py (red: `('+49000000000', ['+4972215559999'])`)

### [P2] New site file goes live in a recreated shared Caddy before `caddy validate`
PR: #730 · File: scripts/new_stack.py:349-351 (`go_live`) · Verified: probe (order) + reading (Docker behaviour)
Trigger: a new customer whose site file the shared Caddy refuses (e.g. an operator-edited, malformed
`N8N_EDITOR_AUTH_HASH`; the half-pair case is now refused earlier, a bad hash is not).
Wrong: `go_live` writes the file and a networks.json with one more network, then runs `caddy up -d` first.
The changed Compose config makes `up -d` recreate the shared container, which loads every site file at start
and fails on the bad one: every customer's tablets, print bridges and pushes go dark (the outage #687
describes). Only afterwards `exec caddy validate` fails (the container is restarting) and the file is removed;
recovery then depends on `restart: always` backoff. #687 required "validate the new config before it goes
live"; the PR's test `test_a_site_is_checked_before_it_is_loaded` only checks validate-before-reload.
Required: validate the candidate before any recreate (e.g. `docker compose ... run --rm --no-deps caddy caddy
validate --config /etc/caddy/Caddyfile` with the new file mounted, or `exec` validate in the running container
before `up -d`), and `up -d` again after a rollback; verification case: FakeDocker order check that
`caddy validate` precedes the shared Caddy's `up -d`.
Probe: probes-g/test_probe_g_caddy_order.py (red: validate is command 7, `up -d` command 6)

## #734 (a recording file without a row is deleted after the period too)

No finding. Green probe probes-g/test_probe_g_retention_links.py (symlink out of the folder, subfolder,
dangling link). See "Checked and clean".

## #728 (downgrade of 018 empties order links and keeps objections)

No finding. Green probe probes-g/test_probe_g_migration_018.py on scratch DBs (fixture `scratch_db_url`,
unique name, dropped after): rows at 018 (customer, default address, linked pickup, linked delivery with
address), up to head, down to 017 (links NULL), up to head again; and an objection refusing a downgrade from
head, leaving the DB at head with the objection (alembic runs the whole downgrade in one transaction,
db/migrations/env.py). Note: 41 `maex_test_*`/`maex_ops_*` databases exist on the local server, apparently
from parallel sessions; not mine, not dropped.

## Counts

P0: 0 · P1: 0 · P2: 3 (#731 replay audited as `repeat`; #730 placeholder-number remedy; #730 validate after
recreate)

## Checked and clean

- #712: every way a GUI session ends deletes its row (`_end` via owner sign-out, operator CLI, `use_link`
  password_set, fifth wrong team PIN, retention purge, expiry in `resolve`), so `_signed_in` ends every such
  stream on the next tick; a second tab of the same device shares the session row and its stream ends too.
- #712: the reconnect after the stream ends meets `require_gui_session` (401/redirect), no data in between;
  `/gui/events` stays read-only, no CSRF needed; the stream of a locked device stays live by design (D34).
- #712: `_needs_admin` walks route-level and parameter dependencies (covers `/gui/geraete/*` and the
  router with `dependencies=[..., Depends(require_admin)]`); a locked owner write lands on `/gui/inhaber`,
  whose POST resolves without the lock check and `unlock_admin` clears the idle lock; staff writes still go
  to `/gui/entsperren`.
- #712: `unlock_admin` on a row deleted meanwhile: `populate_existing` + FOR UPDATE returns None ->
  `ended`, no failure counted; `_back` keeps the way back on `/gui/...` (no open redirect, no `..`, no owner
  page loop), a broken HX-Current-URL falls back to the board.
- #712/#713: device sign-out is POST behind `_require_htmx` (CSRF header token) and same-origin; the
  admin PIN and lock forms keep their CSRF field check.
- #713: sliding is at most once a minute, only after `resolve` accepted the token for this stack's tenant;
  the middleware re-sets only the cookies the request brought (no new token minted, empty values skipped,
  same HttpOnly/SameSite=Lax/Secure/`__Host-` attributes); there is no absolute GUI session lifetime by
  design (docs/23 §6: 30 days without use), so no absolute expiry is undercut.
- #713: re-setting the cookie of a session deleted in the same request (own device signed out) only
  refreshes a dead token; `resolve` refuses it. Device `last_used_at` moves only for a device of the same
  account.
- #713: without a team PIN, `unlock` rolls back before counting, so no try counts and nothing ends.
- #731: unknown and known names now wait alike from a second address (name counter keyed on the SHA-256
  of the normalized name, forgotten after a day with the address rows); oversized names fit the index;
  the audit of unknown names stays empty. Timing: unknown names do two extra small queries before the
  scrypt, known names one audit insert after it; not measured as distinguishable, not filed.
- #732: `reset` without `--allow-lan` rewrites a LAN override to loopback; `down` removes the override;
  the printed link and note use the bound address; `up` (build) always takes the arguments, which the
  `--allow-lan` guard covers. Workflow groups: sync and maintenance write the same board values; a waiting
  maintenance run can no longer be replaced by a sync.
- #730: rerun keeps `.env` byte for byte, prints no secret value (all FRESH_VARIABLES, DATABASE_URL,
  MONITOR_PUSH_TOKEN checked against stdout and stderr of both runs; green probe
  probes-g/test_probe_g_rerun.py); push passwords are printed only on the run that made them; the
  provisioning rerun returns the existing tenant; a lost setup link is named by `missing()` with its
  command; the `fcntl` lock serialises runs per root; `networks.json` comes from the files on disk, also
  after a rollback; a refused rerun puts the working site back; key `caddy` refused, no other Compose
  project or root entry collides; all four tokens pairwise distinct in prod settings.
- #734: the orphan pass lists only the folder's top level, unlinks a symlink itself and never its target,
  skips directories and dangling links, never deletes a file earlier than its row would go (same period,
  file time is the end of the call), a file being written has a row (committed first) and a fresh mtime;
  a failed unlink is logged and does not stop the pass; no folder -> 0, as for rows.
- #728: the downgrade refuses before any change while an objection exists, inside the single alembic
  transaction, also from head; the order links are emptied before the FK drops, the next upgrade passes
  with rows; no later migration references customers, addresses or objections.
