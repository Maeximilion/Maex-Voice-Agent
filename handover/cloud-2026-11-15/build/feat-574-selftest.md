# feat/574-selftest

Branch feat/574-selftest, worktree /home/user/wt-574-selftest, commit 315cb42b (not pushed). No AI attribution in it.

## What it does
`docker compose exec api python -m sim.selftest [--print-slip]` runs six checks in order and stops at the first failure: health, database (reachable and at the head migration), configuration (tenant, service_config, opening hours; gaps from `provision check` shown as notes), menu (dish count and how many have no allergen information, which is allowed), reservation, and print (only with --print-slip). It ends with `connected` or `not connected: <check>`. Exit codes: 3 to 8 per check, 0 when connected.

Reservation: a simulated call with the scripted model makes a draft, then a yes, then `confirm`. The test then checks that the tablet's day list, the SSE change token and the outbox `reservation.confirmed` event (with the call's call_id) all show it. Everything runs in one transaction that is rolled back. A second connection checks during the call that nothing is visible from outside, so the dispatcher, n8n and push see nothing. A fresh read afterwards checks that no rows are left.

Why rollback and not a test flag: the schema has no test flag (it would need a migration and a GUI filter), and a rollback makes reaching the team impossible. The trade-off is that a real tablet never shows a card.

Print: a kitchen event marked SELBSTTEST with no order and no call (`api/domain/ordering/selftest_slip.py`). Kitchen events never go to n8n. The row is removed after the bridge answers.

The module sits in sim/, not jobs/, because jobs/ sits below agent/ (docs/11). `new_stack.py` prints the command at the end, or runs it with `--selftest`.

## Files
- New: sim/selftest.py, sim/selftest_call.py, api/domain/ordering/selftest_slip.py, api/tests/test_sim_selftest.py
- Changed: scripts/new_stack.py, api/tests/test_new_stack.py, docs/13 §8, docs/11, docs/vorlagen/go_live_checklist.md, scripts/README.md

## Tests
- 15 new self-test tests and 2 new new_stack tests, all green. They cover: a normal run, a missing menu, a database one migration behind, an empty or unreachable database, a failed health check, a dispatcher pass during the call that sends nothing, no rows left (also when the call fails), and the slip cases.
- Full suite: 9439 passed, 1 failed and 13 errors, the same as the known baseline.
- ruff is clean. mypy shows only the 5 baseline numpy errors.
- I ran the CLI for real against a live uvicorn and a provisioned database: exit 0, and exit 8 with --print-slip and no bridge running.

## Not run
- Evals: the conversation path is unchanged.
- Docker and a real print bridge are not available here.

## Open question
Should the self-test also put a visible card on the real tablet? A: no, rollback only (recommended). B: add an `is_test` column plus a GUI filter (needs a migration).
