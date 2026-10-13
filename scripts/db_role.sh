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

# BACKUP_DATABASE_URL may name the database under another address for backup and
# restore. The role belongs to the address the application itself uses, so that
# override does not count here: pg_resolve_target then takes OWNER_DATABASE_URL and
# refuses one that names another host, port or database than DATABASE_URL.
BACKUP_DATABASE_URL=""
pg_resolve_target
pg_resolve_client
[ -n "${DATABASE_URL:-}" ] || die 2 "DATABASE_URL is not set: it names the application's role"
owner="$(pg_sql "$PGDATABASE" "select current_user")" ||
    die 1 "cannot connect to $TARGET_LABEL: $CONNECT_HINT"
pg_resolve_app_role "$owner"
[ -n "$APP_ROLE" ] ||
    die 2 "DATABASE_URL connects as $owner, the owner itself: give the application its own user and password in DATABASE_URL and name the owner in OWNER_DATABASE_URL (docs/13 §3)"
[ -n "$APP_ROLE_PASSWORD" ] || die 2 "DATABASE_URL names no password for $APP_ROLE"
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
# role holds what it needs and nothing above it. The statements above took away
# what the role itself was granted; a right that reaches it another way (through
# PUBLIC, through another grantor) is still there and shows here. One row, read as
# the role:
# an attribute above a plain login | roles it is a member of | may create a schema
# in the database | schemas it may create in | tables and sequences it cannot use |
# tables it may truncate, reference or put a trigger on, sequences it may set |
# objects it owns | tables.
tables="from pg_class c where c.relnamespace = 'public'::regnamespace and c.relkind in ('r', 'p')"
sequences="from pg_class c where c.relnamespace = 'public'::regnamespace and c.relkind = 'S'"
usable="has_table_privilege(c.oid, 'select') and has_table_privilege(c.oid, 'insert') and has_table_privilege(c.oid, 'update') and has_table_privilege(c.oid, 'delete')"
beyond="has_table_privilege(c.oid, 'truncate') or has_table_privilege(c.oid, 'references') or has_table_privilege(c.oid, 'trigger')"
# has_sequence_privilege raises on anything that is no sequence, and the planner may
# ask it before it has filtered pg_class by kind: the case decides the order.
counted="case when c.relkind = 'S' then has_sequence_privilege(c.oid, 'usage') and has_sequence_privilege(c.oid, 'select') end"
settable="case when c.relkind = 'S' then has_sequence_privilege(c.oid, 'update') end"
schemas="from pg_namespace n where n.nspname !~ '^pg_' and n.nspname <> 'information_schema'"
check="select r.rolsuper or r.rolcreatedb or r.rolcreaterole or r.rolreplication or r.rolbypassrls, (select count(*) from pg_auth_members m where m.member = r.oid), has_database_privilege(current_database(), 'create'), (select count(*) $schemas and has_schema_privilege(n.oid, 'create')), (select count(*) $tables and not ($usable)) + (select count(*) $sequences and not ($counted)), (select count(*) $tables and ($beyond)) + (select count(*) $sequences and $settable), (select count(*) from pg_class c where c.relowner = r.oid) + (select count(*) from pg_namespace n where n.nspowner = r.oid), (select count(*) $tables) from pg_roles r where r.rolname = current_user"
# Through DATABASE_URL itself, with every parameter the application will use
# (sslmode and the like): a login that only works the owner's way proves nothing.
seen="$(
    unset PGSSLMODE PGSSLROOTCERT PGSSLCERT PGSSLKEY PGSSLCRL PGAPPNAME PGOPTIONS \
        PGCHANNELBINDING PGGSSENCMODE PGTARGETSESSIONATTRS
    pg_target_from_url "$DATABASE_URL"
    pg_refuse_files_in_container
    pg_sql "$PGDATABASE" "$check"
)" ||
    die 1 "the role is set up, but $APP_ROLE could not log in and be checked the way DATABASE_URL says, see the message above (its password, its connection parameters, pg_hba.conf of the server)"
IFS='|' read -r above member new_schema creates locked beyond_rows owned count <<<"$seen"
[ "$above" = f ] || die 1 "$APP_ROLE still holds an attribute above a plain login"
# A member holds what the other role holds, the owner's rights included.
[ "$member" = 0 ] ||
    die 1 "$APP_ROLE is a member of $member other role(s) and holds their rights; as the owner: revoke each of them from \"$APP_ROLE\""
# An owner alters and drops what it owns whatever is granted. That happens when a
# migration ran as the application's user: run them with scripts/migrate.sh. Asked
# before the rights on tables: an owner holds all of them, and the cause is this.
[ "$owned" = 0 ] ||
    die 1 "$APP_ROLE owns $owned object(s) of $PGDATABASE (tables, indexes, sequences, schemas) and could alter or drop them; as the owner: reassign owned by \"$APP_ROLE\" to \"$owner\""
[ "$new_schema" = f ] ||
    die 1 "$APP_ROLE may create a schema in $PGDATABASE, and tables of its own in it (granted to PUBLIC?)"
[ "$creates" = 0 ] || die 1 "$APP_ROLE may create objects in $creates schema(s) of $PGDATABASE"
[ "$locked" = 0 ] || die 1 "$APP_ROLE cannot read and write $locked table(s) or sequence(s)"
[ "$beyond_rows" = 0 ] ||
    die 1 "$APP_ROLE holds a right above reading and writing rows on $beyond_rows table(s) or sequence(s): truncate, references or trigger on a table, update on a sequence (granted to PUBLIC?)"
echo "done: $APP_ROLE logs in, is no superuser, reads and writes $count tables and cannot change the schema"
echo "restart api and dispatcher if DATABASE_URL changed"
