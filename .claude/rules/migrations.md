---
paths:
  - "db/migrations/**"
  - "db/dashboard/migrations/**"
---

# Migrations

Conventions of `CLAUDE.md` sections 7 and 8 for a schema change (moved here by #695 Loop D PR 4). They load when a migration file is read or edited.

- Every schema change is an Alembic migration, tested up **and** down (Definition of Done).
- Before any production migration: backup (`scripts/backup.sh`). A production database waits for Maxi (`CLAUDE.md` §10).
- The workbench database Claude migrates itself, one step at a time, and hands Maxi only a step that failed: the backup and migration of the workbench database (`bash scripts/backup.sh --label before_migration_<NNN>`, `make migrate`, `docker compose restart api dispatcher`, the check of `alembic_version`; recipe `docs/13_DEPLOYMENT.md` §4).
