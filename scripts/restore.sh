#!/usr/bin/env bash
# Restore a dump written by backup.sh (T-9.3, docs/13 §4).
#
#   bash scripts/restore.sh FILE --check
#   bash scripts/restore.sh FILE --replace DATABASE
#
#   --check              rehearsal: restore into a scratch database, report, drop it.
#                        The database of DATABASE_URL is not touched
#   --replace DATABASE   restore and put the result in place of the database of
#                        DATABASE_URL; DATABASE must be its name, as a confirmation
#
# The dump always goes into a new database first. --replace then renames the current
# database to <name>_before_restore_<time> and the new one into its place: nothing is
# dropped, the way back is a rename. Stop api and dispatcher before, a database with
# open sessions cannot be renamed.
#
# Exit codes: 0 done, 1 failed and nothing changed, 2 wrong call or configuration.
set -euo pipefail
# shellcheck source=scripts/lib_pg.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib_pg.sh"

file=""
mode=""
confirm=""
while [ $# -gt 0 ]; do
    case "$1" in
        --check)
            mode=check
            shift
            ;;
        --replace)
            [ $# -ge 2 ] || die 2 "--replace needs the name of the database"
            mode=replace
            confirm="$2"
            shift 2
            ;;
        -*) die 2 "unknown argument: $1 (usage in the head of scripts/restore.sh)" ;;
        *)
            [ -z "$file" ] || die 2 "one dump file at a time"
            file="$1"
            shift
            ;;
    esac
done
[ -n "$file" ] || die 2 "name the dump file (usage in the head of scripts/restore.sh)"
[ -f "$file" ] || die 2 "no such file: $file"
[ -n "$mode" ] || die 2 "say --check or --replace DATABASE"

pg_resolve_target
pg_resolve_client
if [ "$mode" = replace ] && [ "$confirm" != "$PGDATABASE" ]; then
    die 2 "--replace names '$confirm', but DATABASE_URL points at '$PGDATABASE'"
fi
case "$file" in
    *.gpg) require_passphrase ;;
esac

env_default BACKUP_MAINTENANCE_DB
maintenance="${BACKUP_MAINTENANCE_DB:-postgres}"
stamp="$(date -u +%Y%m%d_%H%M%S)"
scratch="${PGDATABASE}_restore_${stamp}_$$"
previous="${PGDATABASE}_before_restore_${stamp}"
if [ ${#scratch} -gt 63 ] || [ ${#previous} -gt 63 ]; then
    die 2 "database name too long for the names restore needs next to it"
fi

echo "restore of $(basename "$file") on $TARGET_LABEL ($mode)"
tables="$(dump_table_count "$file")"
[ "$tables" -gt 0 ] ||
    die 1 "cannot read the dump (wrong passphrase, damaged file or no table data); nothing changed"
pg_sql "$maintenance" "select 1" >/dev/null ||
    die 1 "cannot connect to ${PGHOST}:${PGPORT}/${maintenance}: $CONNECT_HINT; nothing changed"

scratch_exists=0
drop_scratch() {
    [ "$scratch_exists" = 1 ] || return 0
    scratch_exists=0
    pg_sql "$maintenance" "drop database if exists \"$scratch\"" ||
        echo "warning: scratch database $scratch is left behind, drop it by hand" >&2
}
trap drop_scratch EXIT

# template0: the dump brings its own extensions and must not meet leftovers.
pg_sql "$maintenance" "create database \"$scratch\" template template0" ||
    die 1 "cannot create the scratch database; nothing changed"
scratch_exists=1
read_dump "$file" | pg pg_restore --no-owner --no-privileges --exit-on-error --dbname "$scratch" ||
    die 1 "pg_restore failed; nothing changed"

revision="$(pg_sql "$scratch" "select version_num from alembic_version" 2>/dev/null || true)"
restored="$(pg_sql "$scratch" "select count(*) from information_schema.tables where table_schema = 'public'")"
echo "restored: $restored of $tables tables, schema revision ${revision:-unknown}"

if [ "$mode" = check ]; then
    drop_scratch
    echo "restore check passed, $PGDATABASE untouched"
    exit 0
fi

swap="alter database \"$scratch\" rename to \"$PGDATABASE\""
exists="$(pg_sql "$maintenance" "select count(*) from pg_database where datname = '$PGDATABASE'")"
if [ "$exists" = 1 ]; then
    sessions="$(pg_sql "$maintenance" "select count(*) from pg_stat_activity where datname = '$PGDATABASE' and backend_type = 'client backend'")"
    [ "$sessions" = 0 ] ||
        die 1 "$sessions session(s) still connected to $PGDATABASE, stop api and dispatcher first (docker compose stop api dispatcher); nothing changed"
    # One statement string is one transaction: both renames or none.
    swap="alter database \"$PGDATABASE\" rename to \"$previous\"; $swap"
fi
pg_sql "$maintenance" "$swap" || die 1 "could not swap the databases; nothing changed"
scratch_exists=0

echo "$PGDATABASE now holds the restored state, schema revision ${revision:-unknown}"
if [ "$exists" = 1 ]; then
    echo "previous state kept as $previous; once the restored state is checked: drop database \"$previous\""
fi
echo "if the code is newer than that revision: alembic -c db/alembic.ini upgrade head"
