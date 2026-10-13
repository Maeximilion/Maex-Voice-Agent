#!/usr/bin/env bash
# Give the application its own database role without superuser (#252, docs/13 §3).
#
#   bash scripts/db_role.sh
#
# The application connects as the user of DATABASE_URL. This script connects as the
# owner of the schema (OWNER_DATABASE_URL) and makes that user a role that logs in
# with the password of DATABASE_URL, reads and writes rows, and nothing else: no
# superuser, no new tables, no dropped ones. Migrations, backup and restore stay
# with the owner (scripts/migrate.sh, backup.sh, restore.sh).
#
# Safe to run again, and the way to change the password: set the new one in
# DATABASE_URL, run this, restart api and dispatcher. No data is touched.
#
# Configuration from the environment or .env: DATABASE_URL, OWNER_DATABASE_URL,
# BACKUP_DOCKER_NETWORK, BACKUP_PG_CLIENT (as for backup.sh, docs/13 §4).
#
# Exit codes: 0 role set up and its own login checked, 1 failed, 2 wrong call or
# configuration.
set -euo pipefail
# shellcheck source=scripts/lib_pg.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib_pg.sh"

[ $# -eq 0 ] || die 2 "unknown argument: $1 (usage in the head of scripts/db_role.sh)"

pg_resolve_target
pg_resolve_client
owner="$(pg_sql "$PGDATABASE" "select current_user")" ||
    die 1 "cannot connect to $TARGET_LABEL: $CONNECT_HINT"
pg_resolve_app_role "$owner"
[ -n "$APP_ROLE" ] ||
    die 2 "DATABASE_URL connects as $owner, the owner itself: give the application its own user and password in DATABASE_URL and name the owner in OWNER_DATABASE_URL (docs/13 §3)"
[ -n "$APP_ROLE_PASSWORD" ] || die 2 "DATABASE_URL names no password for $APP_ROLE"
[ "$(url_part "$DATABASE_URL" PGDATABASE)" = "$PGDATABASE" ] ||
    die 2 "DATABASE_URL and the owner's URL name different databases"
# A superuser of that name is somebody's administrator, not a role this script
# made: it would lose its rights and get a new password here.
[ "$(pg_sql "$PGDATABASE" "select rolsuper from pg_roles where rolname = '$APP_ROLE'")" != t ] ||
    die 2 "the role $APP_ROLE exists and is a superuser; this script does not take that away. Choose another user for DATABASE_URL, or as the owner: alter role \"$APP_ROLE\" nosuperuser"

echo "role $APP_ROLE on $TARGET_LABEL"
quote="'"
password_sql="${APP_ROLE_PASSWORD//$quote/$quote$quote}"
# The password is part of a statement: keep that statement out of the server log,
# also when it fails.
{
    cat <<SQL
set standard_conforming_strings = on;
set log_statement = 'none';
set log_min_error_statement = 'panic';
do \$\$ begin
    if not exists (select from pg_roles where rolname = '$APP_ROLE') then
        create role "$APP_ROLE";
    end if;
end \$\$;
alter role "$APP_ROLE" login nosuperuser nocreatedb nocreaterole noreplication nobypassrls
    password '$password_sql';
SQL
    app_grants_sql "$PGDATABASE" "$APP_ROLE"
} | pg_script "$PGDATABASE" || die 1 "the role could not be set up; nothing changed"

# The proof from the application's side: its own user and password log in, and the
# role holds what it needs and nothing above it. One row, read as the role:
# an attribute above a plain login | may create in the schema | tables it cannot
# read and write | tables it owns | tables.
tables="from pg_class c where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'"
usable="has_table_privilege(c.oid, 'select') and has_table_privilege(c.oid, 'insert') and has_table_privilege(c.oid, 'update') and has_table_privilege(c.oid, 'delete')"
check="select r.rolsuper or r.rolcreatedb or r.rolcreaterole or r.rolreplication or r.rolbypassrls, has_schema_privilege('public', 'create'), (select count(*) $tables and not ($usable)), (select count(*) $tables and c.relowner = r.oid), (select count(*) $tables) from pg_roles r where r.rolname = current_user"
seen="$(PGUSER="$APP_ROLE" PGPASSWORD="$APP_ROLE_PASSWORD" pg_sql "$PGDATABASE" "$check")" ||
    die 1 "the role is set up, but $APP_ROLE cannot log in with the password of DATABASE_URL (pg_hba.conf of the server?)"
IFS='|' read -r above creates locked owned count <<<"$seen"
[ "$above" = f ] || die 1 "$APP_ROLE still holds an attribute above a plain login"
[ "$creates" = f ] || die 1 "$APP_ROLE may create objects in the schema"
[ "$locked" = 0 ] || die 1 "$APP_ROLE cannot read and write $locked of $count tables"
# A table's owner can alter and drop it whatever is granted. That happens when a
# migration ran as the application's user: run them with scripts/migrate.sh.
[ "$owned" = 0 ] ||
    die 1 "$APP_ROLE owns $owned of $count tables and could drop them; as the owner: reassign owned by \"$APP_ROLE\" to \"$owner\""
echo "done: $APP_ROLE logs in, is no superuser, reads and writes $count tables and cannot change the schema"
echo "restart api and dispatcher if DATABASE_URL changed"
