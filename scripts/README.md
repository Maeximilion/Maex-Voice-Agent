# scripts

| Script | Purpose | Task |
|---|---|---|
| `seed.py` | Tenant, hours, capacity, test config | T-1.2 |
| `import_menu.py` | Load CSV per `docs/14_MENU_IMPORT_FORMAT.md`, `--dry-run`, `--deactivate-missing` (refuses an empty file or more than half the active menu unless `--allow-large-deactivation`) | T-4.2, T-4.11 |
| `kasse_to_csv.py` | Register `.dbf` copies in `imports/kasse/` to the CSV files of docs/14, read-only | T-4.11 |
| `menu_diff.py` | Price register vs. agent DB | T-4.9 |
| `backup.sh` / `restore.sh` | `pg_dump` encrypted, restore | T-8.6 |
| `seed_zones.py` | Delivery zones from zone list (from delivery service project) | T-6.3 |
| `call_log.py` | Call log without recording per `docs/17_ANRUFPROTOKOLL.md`: C1 baseline, `--cases` eval drafts | – (C1) |
| `status_bump.py` | Auto-advance version, date, changelog line in `docs/01_STATUS.md` | – (Meta/Tooling) |
