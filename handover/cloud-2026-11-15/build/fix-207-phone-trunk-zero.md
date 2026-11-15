# fix/207-phone-trunk-zero

Branch fix/207-phone-trunk-zero, commit bb80bd18 (one commit, no attribution lines, not pushed).

Files: api/domain/customers/phone.py, api/tests/test_domain_phone.py, api/tests/test_agent_guest_details.py, docs/04_API_TOOLS.md.

Fix: TRUNK_ZERO_AFTER now holds 26 European codes whose trunk prefix is a single 0, plus Poland (its former trunk 0). The said zero after those codes is dropped. All other codes keep today's behaviour: IT, CI, BJ, NO, HU (06), RU (8), and the countries without a trunk prefix. The double zero stays, for the placeholder and Bulgaria's 00800. Sources: libphonenumber 9.0.43 metadata, read as text only (not executed, not a dependency); for Lithuania, RRT and LRT. Wikipedia was blocked by the network.

Tests: 9 new cases were red on main and are green now (5 probe inputs and the number-word path); 488 targeted pass. Full suite: 9441 passed, 1 failed, 13 errors. The errors come from the missing piper module. The one failure was not identified because the run overlapped the Postgres stop. Ruff is clean. Mypy shows 5 errors, all in telephony speech files I did not touch.

Open decision for Maxi:
(A) Keep the explicit, sourced list (recommended for now).
(B) Adopt libphonenumber. This is a new dependency and changes docs/15 and the requirements.
