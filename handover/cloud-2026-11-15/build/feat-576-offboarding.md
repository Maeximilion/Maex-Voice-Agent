# feat/576-offboarding (#576)

Branch feat/576-offboarding, commit 3642e5b6 (no attribution lines), worktree /home/user/wt-576-offboarding.

Files: api/jobs/offboarding.py (new), scripts/remove_stack.py (new), api/tests/test_jobs_offboarding.py (new, 6),
api/tests/test_remove_stack.py (new, 13), docs/13_DEPLOYMENT.md §8 "Offboarding a customer", docs/03 Löschkonzept row,
docs/11, docs/23 §7, scripts/README.md.

Built: export job (menu in docs/14 format, round trip through scripts/import_menu.py proven by byte-equal re-export;
orders, order_items, reservations as CSV in cents and tenant local time; one READ ONLY REPEATABLE READ transaction;
manifest with SHA-256 written last; rerun unchanged). remove_stack.py: --export, dry run default, --confirm <key>;
refuses without a whole export, with another tenant in the DB, BACKUP_DIR outside the stack, or a foreign .env;
stops, re-exports, takes the Caddy site out (restored on refusal), compose down --volumes, label leftovers, rm -rf folder;
appends a line without personal data to <root>/offboarding/removed.jsonl.

Tests: full suite 9441 passed, 2 skipped, 1 failed + 13 errors = the known baseline (db_role, voice deps); the run
finished with no database errors. After the Postgres restart the affected files were rerun: 81 passed. ruff clean,
mypy clean on the new files (remaining mypy errors are the baseline speech_bench/stt_whisper ones).

Not executed: no real Docker, compose, caddy or rm calls (fakes only, no Docker here).

Open decisions for Maxi:
1. How long a removed customer stays in off-site backups (#321)? (A, recommended) the rolling 30 days run out and the
   confirmation to the customer names the date; (B) the removal also deletes the stack's off-site prefix (needs a
   delete credential on the host, irreversible); (C) a longer period for claims (needs its own legal basis).
2. A stack with a second tenant (a test tenant beside the real one, docs/13 §7): the removal refuses. (A, recommended)
   remove the test tenant by hand first, or remove the whole stack after checking the export; (B) build a per-tenant
   deletion by tenant_id, needed only for a later shared stack.
