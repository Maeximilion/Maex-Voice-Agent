# Skeptic verdicts, area G (review #207 batch 2, main @ 69ef3678, worktree /home/user/rev-m)

### [P2] Replayed TOTP code from another address is audited `repeat`, alarm suppressed
Verdict: CONFIRMED (P2)
Evidence: probe re-run red through the real `access.sign_in` (the same function `app.py:174` calls with
`client_address(request)` and the device cookie): `[('repeat', '203.0.113.9')]`. `_just_signed_in`
(api/dashboard/access.py:334-352) asks only "code == totp_last_step and any session of the account created
in the last 2 min"; `OperatorSession` (models.py:85) stores neither address nor device. docs/09:156 tells the
operator `repeat` is "a double tap, no alarm". #688 asked for "a code refused only because this account just
used it in a sign-in of the same device". Not-a-bug angles checked: the replay itself is refused (TOTP step
replay protection holds), and it still counts toward the waits, so no access is gained; the attacker's win of
the race would show as an `operator.signed_in` from a foreign address. But the loss is the one audit signal
docs/23 §5 and docs/09 name for "password is known" in exactly the shoulder-surf / lost-race case; no other
control covers it yet (new-device push to `ops` and step-up are "still to come", docs/23:143-146). Mitigation
that keeps it at P2, not higher: dashboard is not reachable from outside yet (docs/23:129).
The address IS available at this point: `client_address` = last X-Forwarded-For entry from Caddy (app.py:86-92,
docs/23:119), and the `operator.signed_in` audit row carries it (access.py:156-158). The device cookie is NOT a
usable key: a real double tap from a new browser sends both posts before the first answer sets the cookie, and
the PR's own test `test_one_form_sent_twice_is_audited_as_a_repeat` (test_dashboard_login.py:575) clears
cookies between the posts on purpose. So the reviewer's "device cookie of the session that just signed in"
direction would turn genuine double taps back into `factor`.
Fix: `repeat` only when the refused post comes from the same address as the account's `operator.signed_in`
within REPEAT_WINDOW (read the audit row, or store the address on `operator_sessions`), or the same known
device when the post brings one; otherwise `factor`. Optionally put the address next to `repeat` in the
runbook line ("compare with the `operator.signed_in` address"). · Test: sign in from A, replay the code from B
without cookie -> `factor`; same code from A without cookie -> `repeat` (existing test stays green). · Needs
Maxi: no (plain fix inside #688's stated requirement).

### [P2] Gap hint "TEAM_PHONE in .env" does not clear the stored placeholder number
Verdict: DOWNGRADE to P3 (effective severity P3; the rule 5 escalation "a P2 counts as P1" does not apply,
because no call loses its ring)
Evidence: probe re-run red as claimed: `('+49000000000', ['+4972215559999'])`; `create_tenant` returns on the
key match before touching `service_config` (api/domain/provision.py:67-71), `missing()` keeps the gap
(:184-186). But the claimed rule 5 harm ("transfers ring nowhere") does not hold through the real path: every
dial of `routing.team_phone` goes through `AsteriskCall.transfer` -> "transfer <number>" to the dialplan
(api/telephony/handler.py:436/486/549/674, adapters/asterisk.py:216-219), and the dialplan dials a number only
if it is ENV TEAM_PHONE or on TRANSFER_TARGETS, else ENV TEAM_PHONE (deploy/asterisk/conf/extensions.conf:95-106,
#252 finding 3; drill number 999 in deploy/asterisk/drill/extensions.conf:85-89 tests exactly this). The
asterisk container reads TEAM_PHONE from the same `.env` (docker-compose.yml:321), so after the operator
follows the hint, every transfer to the stale `+49000000000` rings the real team number; the outage fallback
uses `settings.team_phone` (handler.py:201), also real. What remains: the remedy text is wrong (the gap and
`python -m api.jobs.provision check` exit 1 stay forever), the tablet's team-number sheet shows the
placeholder as "standard" (api/gui/router.py:1715), and every transfer logs "transfer to a number not on the
list". Provisioning runs only on Maxi's server, by Maxi, who sees the persistent gap; call mode starts
`shadow`. So an operator-facing wrong hint and a stale displayed value, not a lost call.
Fix: make the named remedy true: the hint names the real path (customer file `team_phone` before the first
run, or for an existing tenant a provisioning subcommand / the owner's standard-number change that sets
`service_config.team_phone` from `dialable_numbers()`), or a rerun updates a placeholder `team_phone` to
the now-dialable TEAM_PHONE (only from the placeholder, audited). · Test: the reviewer's probe
(placeholder tenant, set TEAM_PHONE, follow the remedy -> no team-number gap and
`call_routing().team_phone in dialable_numbers()`). · Needs Maxi: no for the hint text; a rerun that writes
the stored team number changes a transfer target, so that variant goes through the xhigh merge handling
(code that decides a transfer), still a plain fix.

### [P2] New site file goes live in a recreated shared Caddy before `caddy validate`
Verdict: DOWNGRADE to P3
Evidence: probe re-run red as claimed (validate is command 7, the shared Caddy's `up -d` command 6):
scripts/new_stack.py:349-353 runs `up -d`, then `exec -T caddy caddy validate`, then `reload`; the PR's test
`test_a_site_is_checked_before_it_is_loaded` (api/tests/test_new_stack.py:325-335) pins only validate before
reload. A new customer adds a network to networks.json, so `up -d` recreates the container (Compose config
hash; reading, docker not run), and a site Caddy refuses at load (bad `N8N_EDITOR_AUTH_HASH`, a user with a
space; only the half pair is refused earlier, :442) stops the start. Why not P2: #687 rated the *persistent*
form P2 (file left on disk, every later recreate or reboot dark, "calls are not affected"). That form is fixed:
the failing `exec` raises `Refused` (run(), :282-289), the file is removed or put back and networks.json
rewritten (:354-360), and `restart: always` (deploy/caddy/docker-compose.yml) brings the same container up
without the bad file within Docker's restart backoff, a few seconds in this short crash loop. What is left is
a brief, self-healing blip of every stack's tablets, print bridges and pushes (no call path: Asterisk and
`voice` do not pass through Caddy), only for a new customer whose site has an operator-edited malformed value,
during an attended run that only Maxi does on his own server, and the run ends with a clear refusal. A rerun
that keeps networks.json unchanged does not recreate, so validation in the running container already catches it.
Fix: validate before anything is recreated: while the shared Caddy runs, `exec -T caddy caddy validate` first
(the site dir is a bind mount, the new file is already visible), then `up -d`, then reload; with no running
Caddy (first customer) `run --rm --no-deps caddy caddy validate`. · Test: FakeDocker order check that
`caddy validate` comes before the shared Caddy's `up -d` (the reviewer's probe), and a failing validate leaves
no `up -d` at all. · Needs Maxi: no (plain fix).

## Counts
CONFIRMED: 1 (#731 repeat, P2) · DOWNGRADE: 2 (#730 team-number hint P2 -> P3, the rule 5 path is guarded by the
dialplan; #730 validate after recreate P2 -> P3, self-healing blip) · REJECTED: 0 · UNCLEAR: 0
Probes: copied into /home/user/rev-m/api/tests/ (untracked, test_probe_g_dashboard_repeat.py,
test_probe_g_team_placeholder.py, test_probe_g_caddy_order.py), all three red as the reviewer said. No docker run.
