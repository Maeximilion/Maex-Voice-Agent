#!/usr/bin/env bash
# Run Alembic in the api container as the owner of the schema (#252, docs/13 §3).
#
#   bash scripts/migrate.sh [alembic arguments, default: upgrade head]
#
# Where the application has a role of its own, that role cannot change the schema.
# Alembic then gets the owner's URL (OWNER_DATABASE_URL, from the environment or
# .env) for this one run; the running application never holds it. Without that
# variable nothing is passed and DATABASE_URL of the container applies, as before.
# Backup first on a database with real data: bash scripts/backup.sh --label before_migration
set -euo pipefail
# shellcheck source=scripts/lib_pg.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib_pg.sh"

[ $# -gt 0 ] || set -- upgrade head
env_default OWNER_DATABASE_URL
options=()
# Without a terminal (cron, a pipe) Compose must not ask for one.
[ -t 0 ] || options+=(-T)
if [ -n "${OWNER_DATABASE_URL:-}" ]; then
    export DATABASE_URL="$OWNER_DATABASE_URL"
    # Named without a value: taken from this environment, so the password never
    # shows up in a process list.
    options+=(-e DATABASE_URL)
fi
cd "$REPO_ROOT"
exec docker compose exec ${options[@]+"${options[@]}"} api alembic -c db/alembic.ini "$@"
