# scripts

| Script | Purpose | Task |
|---|---|---|
| `seed.py` | Tenant, hours, capacity, test config | T-1.2 |
| `import_menu.py` | Load CSV per `docs/14_MENU_IMPORT_FORMAT.md`, `--dry-run` | T-4.2 |
| `menu_diff.py` | Price register vs. agent DB | T-4.9 |
| `backup.sh` / `restore.sh` | `pg_dump` encrypted, restore | T-8.6 |
| `seed_zones.py` | Delivery zones from zone list (from delivery service project) | T-6.3 |
| `status_bump.py` | Auto-advance version, date, changelog line in `docs/01_STATUS.md` | – (Meta/Tooling) |
