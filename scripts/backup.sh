#!/usr/bin/env bash
# Dump the database of DATABASE_URL, encrypted, into BACKUP_DIR (T-9.3, docs/13 §4).
#
#   bash scripts/backup.sh [--label NAME] [--no-encrypt]
#
#   --label NAME   goes into the file name (before_menu_import); a labelled dump is
#                  never removed by the retention
#   --no-encrypt   plain dump, only for a database without customer data
#
# Configuration from the environment or .env: DATABASE_URL, BACKUP_PASSPHRASE_FILE,
# BACKUP_DIR (default backups/), BACKUP_KEEP_DAYS (default 14, 0 keeps everything),
# BACKUP_DOCKER_NETWORK, BACKUP_PG_CLIENT (auto, local, docker).
#
# Exit codes: 0 dump written and read back, 1 dump failed and nothing was kept,
# 2 wrong call or configuration.
set -euo pipefail
# shellcheck source=scripts/lib_pg.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib_pg.sh"

label=""
encrypt=1
while [ $# -gt 0 ]; do
    case "$1" in
        --label)
            [ $# -ge 2 ] || die 2 "--label needs a name"
            label="$2"
            shift 2
            ;;
        --no-encrypt)
            encrypt=0
            shift
            ;;
        *) die 2 "unknown argument: $1 (usage in the head of scripts/backup.sh)" ;;
    esac
done
[[ -z "$label" || "$label" =~ ^[a-z0-9_]+$ ]] ||
    die 2 "--label may only hold lower-case letters, digits and underscores"

pg_resolve_target
pg_resolve_client
env_default BACKUP_DIR
env_default BACKUP_KEEP_DAYS
backup_dir="${BACKUP_DIR:-$REPO_ROOT/backups}"
keep_days="${BACKUP_KEEP_DAYS:-14}"
[[ "$keep_days" =~ ^[0-9]+$ ]] || die 2 "BACKUP_KEEP_DAYS must be a number of days"
suffix=dump
if [ "$encrypt" = 1 ]; then
    require_passphrase
    suffix=dump.gpg
fi

umask 077
mkdir -p "$backup_dir"
name="maex_${PGDATABASE}_$(date -u +%Y%m%dT%H%M%SZ)${label:+_$label}.$suffix"
final="$backup_dir/$name"
# Keeps the suffix, so read_dump knows whether to decrypt it.
partial="$backup_dir/.partial_$name"
[ ! -e "$final" ] || die 1 "$final exists already"
trap 'rm -f "$partial"' EXIT

echo "backup of $TARGET_LABEL"
pg_sql "$PGDATABASE" "select 1" >/dev/null || die 1 "cannot connect to $TARGET_LABEL: $CONNECT_HINT"

if [ "$encrypt" = 1 ]; then
    pg pg_dump --format=custom </dev/null | encrypt_stream >"$partial" ||
        die 1 "pg_dump failed, nothing kept"
else
    pg pg_dump --format=custom </dev/null >"$partial" || die 1 "pg_dump failed, nothing kept"
fi

# A dump only counts once it was read back with the same passphrase.
tables="$(dump_table_count "$partial")"
[ "$tables" -gt 0 ] || die 1 "the dump cannot be read back or holds no table data, nothing kept"

mv "$partial" "$final"
echo "written: $final ($(wc -c <"$final" | tr -d ' ') bytes, $tables tables)"

# Only after a good run, and only scheduled dumps of this database: the time stamp
# is directly followed by the suffix, a label sits in between.
if [ "$keep_days" -gt 0 ]; then
    find "$backup_dir" -maxdepth 1 -type f \
        -name "maex_${PGDATABASE}_????????T??????Z.dump*" -mtime "+$keep_days" \
        -print -delete | sed "s/^/removed, older than $keep_days days: /"
fi
