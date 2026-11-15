# fix/764-allergen-guard (worktree /home/user/wt-fix-allergen), not pushed

Commits (one per finding, separate guards):
- 39cf950f #747 follow-up: `escalation.asks_about_an_allergen` (question whether an allergen is in a dish forces the flag and the unread guard); `guards.states_allergens` + `guards.unread_allergens` (model sentence naming a dish's allergens without a lookup goes back once, then team); after an unflagged lookup the core sentence (`details.say_allergens`) or the team for unknown values.
- c3ce6486 #748 absence forms in `allergen_claim` (ist/kommen + kein/nix/nichts, kein..drin/rein, enthält/hat ... nicht, weder..noch, "Nüsse? ..."), notes too.
- f4a278e2 short guest ingredients (Kiwi, Mais, Zimt, Mohn, Lamm, Reis) kept off callback cards (`loop._ingredients`), consistent with #788's Soja rule.

Files: api/agent/{escalation,guards,loop}.py, api/domain/menu/details.py, tests in test_agent_allergens_beyond.py, test_agent_guards.py, test_agent_loop.py, docs/04, docs/05.

Tests: new tests red first (15 + 17 + 7), then green. Two existing cases moved deliberately (a positive allergen sentence without lookup now goes back; "Nein, da ist kein Sellerie." now goes to the team at once). Evals scripted: before 103/103, refused 2; after 103/103, refused 2, all 21 allergie_* pass, hard metrics 0. Full suite: 9493 passed, 2 skipped, 1 failed + 13 errors, all environmental and in untouched files (test_scripts_db_role login check, also red after the Postgres restart; telephony tests need the missing `audioop` module). ruff clean; mypy shows the same 5 numpy errors as main.

Not built (out of scope): #751, #760, Zöliakie mapping. Open decisions: none.
