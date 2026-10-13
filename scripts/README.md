# scripts

| Script | Purpose | Task |
|---|---|---|
| `seed.py` | Tenant, hours, capacity, test config | T-1.2 |
| `import_menu.py` | Load CSV per `docs/14_MENU_IMPORT_FORMAT.md`, `--dry-run`, `--deactivate-missing` (refuses an empty file or more than half the active menu unless `--allow-large-deactivation`) | T-4.2, T-4.11 |
| `kasse_to_csv.py` | Register `.dbf` copies in `imports/kasse/` to the CSV files of docs/14, read-only | T-4.11 |
| `menu_diff.py` | Price register vs. agent DB | T-4.9 |
| `backup.sh` / `restore.sh` | Encrypted `pg_dump` of the database of `DATABASE_URL`; restore into a new database with `--check` (rehearsal) or `--replace` (swap by rename). `lib_pg.sh` is their shared part. Usage in `docs/13_DEPLOYMENT.md` §4 | T-9.3 |
| `db_role.sh` | The application's own database role without superuser: creates or updates the user of `DATABASE_URL` as the owner (`OWNER_DATABASE_URL`), then logs in as that role and checks what it holds. Safe to run again, changes no data. Usage in `docs/13_DEPLOYMENT.md` §3 | #252 |
| `migrate.sh` | Alembic as the owner of the schema, in a one-off container of the `api` service; `make migrate` calls it. Arguments go to Alembic, default `upgrade head` | #252 |
| `seed_zones.py` | Delivery zones from zone list (from delivery service project) | T-6.3 |
| `call_log.py` | Call log without recording per `docs/17_ANRUFPROTOKOLL.md`: C1 baseline, `--cases` eval drafts | – (C1) |
| `setup_local_llm.sh` | Local test model on the workbench: installs Ollama, picks the model by GPU memory, checks one invented order line against the answer contract, prints the `.env` lines; `--dry-run`. Invented data only | T-2.4, docs/18 §5 |
| `voice_spike.py` | Entry of the kit of the spike T-10.1 step 1 (the kit is `api/telephony/spike.py`): starts a test PBX, a scripted softphone and the probe as containers, places the test calls and judges them point by point; `--dry-run`, `--keep`, `--down`. Docker only, no sudo, invented data only | T-10.1, docs/20 §10 |
| `speech_spike.py` | Entry of the kit of the spike T-10.3 (the kit is `api/telephony/speech_spike.py`): builds the image with the open speech engines, speaks the number eval and the dish names of the eval menu through a telephone band into every ear, judges with the number rule, prints the pick; `--dry-run`, `--limit`. Docker only, no sudo, invented sentences only, no network while it measures | T-10.3, docs/20 §10 |
| `status_bump.py` | Auto-advance version, date, changelog line in `docs/01_STATUS.md` | – (Meta/Tooling) |
