# shellcheck shell=bash
# Shared by backup.sh and restore.sh (T-9.3): which database, which Postgres client,
# how a dump is encrypted. Sourced, never run.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { # die <exit code> <message>
    local code="$1"
    shift
    echo "error: $*" >&2
    exit "$code"
}

# Take KEY from the env file when the caller did not set it. The file is read line by
# line, not sourced: TENANT_NAME=<Firmenname> is a redirect to a shell.
env_default() {
    local key="$1" file="${BACKUP_ENV_FILE:-$REPO_ROOT/.env}" line value
    [ -z "${!key+set}" ] || return 0
    [ -f "$file" ] || return 0
    line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
    [ -n "$line" ] || return 0
    value="${line#*=}"
    value="${value%$'\r'}"
    case "$value" in
        \"*\") value="${value#\"}" value="${value%\"}" ;;
        \'*\') value="${value#\'}" value="${value%\'}" ;;
    esac
    printf -v "$key" '%s' "$value"
    export "${key?}"
}

urldecode() {
    local escaped="${1//\\/\\\\}"
    printf '%b' "${escaped//%/\\x}"
}

# Connection parameters behind the "?" of the URL, as the variables libpq reads. The
# application connects with them (TLS mode, certificates), so a dump must not go
# without them: a parameter that cannot be passed on stops the script instead of
# being dropped. Messages name the key only, a value may be a secret.
pg_params_from_query() {
    local pairs pair key var
    IFS='&' read -ra pairs <<<"$1"
    for pair in ${pairs[@]+"${pairs[@]}"}; do
        key="${pair%%=*}"
        case "$key" in
            sslmode) var=PGSSLMODE ;;
            sslrootcert) var=PGSSLROOTCERT ;;
            sslcert) var=PGSSLCERT ;;
            sslkey) var=PGSSLKEY ;;
            sslcrl) var=PGSSLCRL ;;
            connect_timeout) var=PGCONNECT_TIMEOUT ;;
            application_name) var=PGAPPNAME ;;
            options) var=PGOPTIONS ;;
            channel_binding) var=PGCHANNELBINDING ;;
            gssencmode) var=PGGSSENCMODE ;;
            target_session_attrs) var=PGTARGETSESSIONATTRS ;;
            *) die 2 "DATABASE_URL carries the connection parameter '$key', which backup and restore cannot pass on" ;;
        esac
        printf -v "$var" '%s' "$(urldecode "${pair#*=}")"
        export "${var?}"
        case "$key" in
            sslrootcert | sslcert | sslkey | sslcrl) PG_FILE_PARAMS+=("$key") ;;
        esac
    done
}

# Certificate files of the URL exist on this machine, not in the client container.
pg_refuse_files_in_container() {
    [ "$PG_CLIENT" = docker ] || return 0
    [ ${#PG_FILE_PARAMS[@]} -eq 0 ] ||
        die 2 "DATABASE_URL names certificate files (${PG_FILE_PARAMS[*]}); they need a local Postgres client of the server's version, not the client container"
}

# Split a SQLAlchemy or libpq URL into the PG* variables every Postgres client reads.
# The URL holds the password, so no message here repeats it.
pg_target_from_url() {
    local url="$1" rest userinfo="" hostport
    case "$url" in
        postgresql://* | postgres://* | postgresql+*://*) ;;
        *) die 2 "DATABASE_URL is not a postgresql:// URL" ;;
    esac
    rest="${url#*://}"
    PG_FILE_PARAMS=()
    if [[ "$rest" == *\?* ]]; then pg_params_from_query "${rest#*\?}"; fi
    rest="${rest%%\?*}"
    case "$rest" in
        */?*) ;;
        *) die 2 "DATABASE_URL names no database" ;;
    esac
    PGDATABASE="$(urldecode "${rest#*/}")"
    rest="${rest%%/*}"
    hostport="$rest"
    if [[ "$rest" == *@* ]]; then
        userinfo="${rest%@*}"
        hostport="${rest##*@}"
    fi
    PGPORT=5432
    if [[ "$hostport" == \[*\]* ]]; then
        # IPv6 literal: [::1]:5432
        PGHOST="${hostport#\[}"
        PGHOST="${PGHOST%%\]*}"
        hostport="${hostport##*\]}"
    else
        PGHOST="${hostport%%:*}"
    fi
    if [[ "$hostport" == *:* ]]; then PGPORT="${hostport##*:}"; fi
    [ -n "$PGHOST" ] || die 2 "DATABASE_URL names no host"
    # User and password of the URL win; where the URL names none, what the caller
    # exported (PGUSER, PGPASSWORD) stays.
    if [ -n "${userinfo%%:*}" ]; then
        PGUSER="$(urldecode "${userinfo%%:*}")"
        export PGUSER
    fi
    if [[ "$userinfo" == *:* ]]; then
        PGPASSWORD="$(urldecode "${userinfo#*:}")"
        export PGPASSWORD
    fi
    export PGHOST PGPORT PGDATABASE
}

# The same variable the application and the import read decides what is dumped and
# what is replaced (lesson from PR #169: a backup of some other stack is no backup).
pg_resolve_target() {
    env_default BACKUP_DATABASE_URL
    env_default DATABASE_URL
    local url="${BACKUP_DATABASE_URL:-${DATABASE_URL:-}}"
    [ -n "$url" ] || die 2 "DATABASE_URL is not set, in the environment or in .env"
    pg_target_from_url "$url"
    # Database names are quoted into SQL below; anything else would need escaping.
    [[ "$PGDATABASE" =~ ^[A-Za-z0-9_]+$ ]] ||
        die 2 "database name may only hold letters, digits and underscores"
    export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
    TARGET_LABEL="${PGUSER:-default user}@${PGHOST}:${PGPORT}/${PGDATABASE}"
}

# Client tools from PATH, or from the Postgres image when the host has none. A dump
# needs a client at least as new as the server, and the image is the server's own.
pg_resolve_client() {
    env_default BACKUP_PG_CLIENT
    env_default BACKUP_PG_IMAGE
    env_default BACKUP_DOCKER_NETWORK
    PG_CLIENT="${BACKUP_PG_CLIENT:-auto}"
    PG_IMAGE="${BACKUP_PG_IMAGE:-postgres:16-alpine}"
    if [ "$PG_CLIENT" = auto ]; then
        PG_CLIENT=docker
        # A client on the host cannot resolve names of the Compose network.
        if [ -z "${BACKUP_DOCKER_NETWORK:-}" ] && command -v pg_dump >/dev/null &&
            command -v pg_restore >/dev/null && command -v psql >/dev/null; then
            PG_CLIENT=local
            PG_CLIENT_MAY_SWITCH=1
        fi
    fi
    case "$PG_CLIENT" in
        local)
            command -v pg_dump >/dev/null || die 2 "BACKUP_PG_CLIENT=local, but pg_dump is not on PATH"
            ;;
        docker)
            command -v docker >/dev/null || die 2 "neither a Postgres client nor docker on PATH"
            ;;
        *) die 2 "BACKUP_PG_CLIENT must be auto, local or docker" ;;
    esac
    DOCKER_ARGS=()
    CONTAINER_PGHOST="$PGHOST"
    if [ -n "${BACKUP_DOCKER_NETWORK:-}" ]; then
        # Production: Postgres has no published port, only the Compose network reaches it.
        DOCKER_ARGS=(--network "$BACKUP_DOCKER_NETWORK")
    elif [ "$PGHOST" = localhost ] || [ "$PGHOST" = 127.0.0.1 ]; then
        # The host's localhost, seen from inside the client container.
        CONTAINER_PGHOST=host.docker.internal
        DOCKER_ARGS=(--add-host host.docker.internal:host-gateway)
    fi
    pg_refuse_files_in_container
}

pg_major() { # first number of a version line: "pg_dump (PostgreSQL) 16.4" -> 16
    sed -E 's/^[^0-9]*([0-9]+).*/\1/'
}

# pg_dump refuses a newer server, and a newer pg_dump writes what an older server
# cannot restore. A local client of another major version is replaced by the image
# when the choice was automatic, and refused when it was asked for.
pg_match_server_version() { # <database to ask>
    [ "$PG_CLIENT" = local ] || return 0
    local server client
    server="$(pg_sql "$1" "show server_version_num")"
    server=$((server / 10000))
    client="$(pg_dump --version | pg_major)"
    [ "$server" != "$client" ] || return 0
    if [ "${PG_CLIENT_MAY_SWITCH:-0}" = 1 ] && command -v docker >/dev/null; then
        echo "local Postgres client is version $client, the server $server: using $PG_IMAGE"
        PG_CLIENT=docker
        pg_refuse_files_in_container
        return 0
    fi
    die 2 "local Postgres client is version $client, the server $server: set BACKUP_PG_CLIENT=docker"
}

pg() { # pg <tool> [arguments]; stdin goes through to the tool
    if [ "$PG_CLIENT" = local ]; then
        "$@"
        return
    fi
    # Variables named without a value are taken from this environment, so the
    # password never shows up in a process list.
    MSYS_NO_PATHCONV=1 docker run --rm -i ${DOCKER_ARGS[@]+"${DOCKER_ARGS[@]}"} \
        -e PGHOST="$CONTAINER_PGHOST" -e PGPORT -e PGUSER -e PGPASSWORD \
        -e PGDATABASE -e PGCONNECT_TIMEOUT -e PGSSLMODE -e PGAPPNAME -e PGOPTIONS \
        -e PGCHANNELBINDING -e PGGSSENCMODE -e PGTARGETSESSIONATTRS "$PG_IMAGE" "$@"
}

pg_sql() { # pg_sql <database> <sql>; prints bare values
    pg psql --no-psqlrc --quiet --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --dbname "$1" --command "$2" </dev/null | tr -d '\r'
}

CONNECT_HINT="check DATABASE_URL; a host like 'db' only resolves inside the Compose network, and on Linux a client container does not reach the loopback port of the dev stack (BACKUP_DOCKER_NETWORK for both)"

require_passphrase() {
    env_default BACKUP_PASSPHRASE_FILE
    [ -n "${BACKUP_PASSPHRASE_FILE:-}" ] ||
        die 2 "BACKUP_PASSPHRASE_FILE is not set: the dump holds customer data and is only written encrypted"
    [ -s "$BACKUP_PASSPHRASE_FILE" ] || die 2 "BACKUP_PASSPHRASE_FILE is missing or empty"
    command -v gpg >/dev/null || die 2 "gpg is not on PATH"
}

# The passphrase is the first line of the file without its line ending: gpg itself
# would take a carriage return as part of it, and a file saved on Windows would
# write dumps nobody can open with the passphrase from the password manager.
# --no-symkey-cache: without it gpg-agent remembers a passphrase and a restore with
# the wrong one would pass on the machine that wrote the backup.
gpg_quiet() {
    gpg --batch --quiet --no-symkey-cache --pinentry-mode loopback \
        --passphrase-fd 3 "$@" 3< <(head -n 1 "$BACKUP_PASSPHRASE_FILE" | tr -d '\r\n')
}

encrypt_stream() { # stdin -> stdout; the custom format is compressed already
    gpg_quiet --symmetric --cipher-algo AES256 --compress-algo none --output -
}

read_dump() { # plain dump bytes of <file> on stdout
    case "$1" in
        *.gpg) gpg_quiet --decrypt "$1" ;;
        *) cat "$1" ;;
    esac
}

# pg_restore <arguments> fed with the plain bytes of <file>. pg_restore stops at the end
# marker without reading what follows (padding, a gpg trailer), so the writer of the
# pipe is cut off by SIGPIPE and `pipefail` calls a good dump unreadable, depending on
# timing. The rest is drained before the status is read; gpg still checks to the end.
pg_restore_from() { # pg_restore_from <file> [pg_restore arguments]
    local file="$1"
    shift
    read_dump "$file" | {
        status=0
        pg pg_restore "$@" || status=$?
        cat >/dev/null
        exit "$status"
    }
}

# Read the whole dump, not only its table of contents: decrypts to the last byte
# (gpg checks integrity there) and lets pg_restore unpack every table. Errors show.
verify_dump() {
    pg_restore_from "$1" --file=/dev/null
}

# How many tables a dump holds (pg_dump lists empty ones too). Only the table of
# contents is read, so the writer of the pipe is cut off: its status says nothing.
dump_table_count() {
    read_dump "$1" 2>/dev/null | pg pg_restore --list 2>/dev/null | grep -c 'TABLE DATA' || true
}
