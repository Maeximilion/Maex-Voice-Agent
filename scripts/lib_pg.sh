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

# Split a SQLAlchemy or libpq URL into the PG* variables every Postgres client reads.
# The URL holds the password, so no message here repeats it.
pg_target_from_url() {
    local url="$1" rest userinfo="" hostport
    case "$url" in
        postgresql://* | postgres://* | postgresql+*://*) ;;
        *) die 2 "DATABASE_URL is not a postgresql:// URL" ;;
    esac
    rest="${url#*://}"
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
    PGHOST="${hostport%%:*}"
    PGPORT=5432
    if [[ "$hostport" == *:* ]]; then PGPORT="${hostport##*:}"; fi
    [ -n "$PGHOST" ] || die 2 "DATABASE_URL names no host"
    unset PGUSER PGPASSWORD
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
        if command -v pg_dump >/dev/null && command -v pg_restore >/dev/null &&
            command -v psql >/dev/null; then
            PG_CLIENT=local
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
        -e PGDATABASE -e PGCONNECT_TIMEOUT "$PG_IMAGE" "$@"
}

pg_sql() { # pg_sql <database> <sql>; prints bare values
    pg psql --no-psqlrc --quiet --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --dbname "$1" --command "$2" </dev/null | tr -d '\r'
}

CONNECT_HINT="check DATABASE_URL; a host like 'db' only resolves inside the Compose network (BACKUP_DOCKER_NETWORK)"

require_passphrase() {
    env_default BACKUP_PASSPHRASE_FILE
    [ -n "${BACKUP_PASSPHRASE_FILE:-}" ] ||
        die 2 "BACKUP_PASSPHRASE_FILE is not set: the dump holds customer data and is only written encrypted"
    [ -s "$BACKUP_PASSPHRASE_FILE" ] || die 2 "BACKUP_PASSPHRASE_FILE is missing or empty"
    command -v gpg >/dev/null || die 2 "gpg is not on PATH"
}

# --no-symkey-cache: without it gpg-agent remembers a passphrase and a restore with
# the wrong one would pass on the machine that wrote the backup.
gpg_quiet() {
    gpg --batch --quiet --no-symkey-cache --pinentry-mode loopback \
        --passphrase-file "$BACKUP_PASSPHRASE_FILE" "$@"
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

# How many tables a dump holds (pg_dump lists empty ones too); 0 also when it cannot
# be read at all.
dump_table_count() {
    read_dump "$1" 2>/dev/null | pg pg_restore --list 2>/dev/null | grep -c 'TABLE DATA' || true
}
