#!/usr/bin/env bash
# Run Alembic as the owner of the schema (#252, docs/13 §3).
#
#   bash scripts/migrate.sh [alembic arguments, default: upgrade head]
#
# Where the application has a role of its own, that role cannot change the schema.
# Alembic then gets the owner's URL (OWNER_DATABASE_URL, from the environment or
# .env) for this one run. Without that variable nothing is passed and DATABASE_URL
# of the service applies, as before.
#
# Alembic runs in a container of its own, made from the api service, not inside the
# running one: there the application could read the owner's URL from the process
# list while a migration runs (/proc/<pid>/environ, same user). It also means a
# migration does not need the API to be up, only the database.
#
# Compose picks its files as for every other command. On the server:
#   COMPOSE_FILE=docker-compose.yml:deploy/docker-compose.prod.yml bash scripts/migrate.sh
# Backup first on a database with real data: bash scripts/backup.sh --label before_migration
set -euo pipefail
# shellcheck source=scripts/lib_pg.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib_pg.sh"

[ $# -gt 0 ] || set -- upgrade head
env_default OWNER_DATABASE_URL
options=(--rm)
# Without a terminal (cron, a pipe) Compose must not ask for one.
[ -t 0 ] || options+=(-T)
if [ -n "${OWNER_DATABASE_URL:-}" ]; then
    export DATABASE_URL="$OWNER_DATABASE_URL"
    # Named without a value: taken from this environment, so the password never
    # shows up in a process list of the host.
    options+=(-e DATABASE_URL)
fi
cd "$REPO_ROOT"
exec docker compose run "${options[@]}" api alembic -c db/alembic.ini "$@"
