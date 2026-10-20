#!/bin/sh
# Entry of the service `asterisk` (docs/20 §2): render the configuration from
# deploy/asterisk/conf/ and the environment into /etc/asterisk (a tmpfs, so the
# rendered secrets never reach a disk), then start Asterisk as the image does.
#
# Only the names below are replaced. The dialplan's own ${...} stay as they are,
# and so does every other text. A value that is missing stops the start: an AI
# telephone that cannot register, cannot ring the team or lets anyone onto the
# media channel is worse than none, because then the router or the trunk rings
# the team telephones as before (docs/20 §3).
set -eu

newline='
'
for name in VOICE_NUMBER VOICE_MEDIA_TOKEN TEAM_PHONE ACCESS_HOST ACCESS_USER ACCESS_PASSWORD; do
    # eval on a fixed list of names, never on input.
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
        echo "asterisk does not start: $name is not set (.env, docs/20 §7)" >&2
        exit 1
    fi
    case "$value" in
        *"$newline"*)
            echo "asterisk does not start: $name holds a line break" >&2
            exit 1
            ;;
    esac
done

# Numbers the dialplan can use. A TEAM_PHONE it cannot dial would let every
# outage end in a dead line (rule 5); the rule is the one `voice` holds a
# transfer target to (`adapters/asterisk.py`). VOICE_NUMBER is E.164 in
# Germany: the loop guard derives 0049... and 0... from its "+49".
# grep reads line by line, so a value with a line break is never a number.
number() {
    case "$1" in *"$newline"*) return 1 ;; esac
    printf '%s' "$1" | grep -Eqx "$2"
}
dialable='\+?[0-9]{3,20}'
if ! number "$TEAM_PHONE" "$dialable"; then
    echo "asterisk does not start: TEAM_PHONE is not a number to dial" >&2
    exit 1
fi
if ! number "$VOICE_NUMBER" '\+49[1-9][0-9]{4,12}'; then
    echo "asterisk does not start: VOICE_NUMBER is not a German number in E.164" >&2
    exit 1
fi

# The team on our own number: the dialplan would ring us, the loop guard would
# drop the call, and every caller would be lost (rule 5). `voice` refuses the
# same; this is for the calls that reach the team without it.
# In every form the dialplan's loop guard knows: +49..., 0049... and 0..., and
# as international digits without a prefix, which an access may dial too
# (Codex on PR #340): the forms are compared without their "+".
e164() {
    case "$1" in
        +*) printf '%s' "$1" ;;
        00*) printf '+%s' "${1#00}" ;;
        0*) printf '+49%s' "${1#0}" ;;
        *) printf '%s' "$1" ;;
    esac
}
same() {
    a=$(e164 "$1")
    b=$(e164 "$2")
    [ "${a#+}" = "${b#+}" ]
}
if same "$TEAM_PHONE" "$VOICE_NUMBER"; then
    echo "asterisk does not start: TEAM_PHONE is the AI telephone's own number" >&2
    exit 1
fi

# The numbers a transfer may go to besides TEAM_PHONE, comma-separated; may be
# empty. The dialplan dials no other (#252 finding 3), so each has to be one it
# can dial, and none ours. Blanks and empty fields are dropped, and so is
# TEAM_PHONE in any form: it is always on the list, and in another form the
# dialplan would ring it a second time after an unanswered leg at it. The
# dialplan reads the cleaned list from the environment this script hands on.
targets=
set -f
old_ifs=$IFS
IFS=,
for target in $(printf '%s' "${TRANSFER_TARGETS:-}" | tr -d ' \t'); do
    [ -n "$target" ] || continue
    if ! number "$target" "$dialable"; then
        echo "asterisk does not start: TRANSFER_TARGETS holds something that is not a number" >&2
        exit 1
    fi
    if same "$target" "$VOICE_NUMBER"; then
        echo "asterisk does not start: TRANSFER_TARGETS holds the AI telephone's own number" >&2
        exit 1
    fi
    same "$target" "$TEAM_PHONE" || targets="$targets${targets:+,}$target"
done
IFS=$old_ifs
set +f
export TRANSFER_TARGETS="$targets"
# The count, not the numbers: a team number can be somebody's own mobile.
echo "asterisk: $(printf '%s\n' "$targets" | awk -F, '{ print NF }') more numbers for a transfer besides TEAM_PHONE"

# Asterisk takes SIP only from the access's addresses (#252 finding 2). Its ACL
# takes addresses, not names, so ACCESS_HOST is looked up here, once, as
# Asterisk looks up a name in `match` once at the start; an access that moves
# to another address needs a restart. A name with `_sip._udp` SRV records
# counts with the addresses of the servers they name: the registration over
# our only transport, UDP, follows them, and a trunk's calls come from them
# (ultrareview of PR #340). Its own A record is then used for nothing and stays
# out, as do servers for TCP and TLS (Codex on PR #340). A name without such
# records counts with its A records. IPv4 only, as the transport.
# busybox's nslookup exits 0 with the records, or with none (NODATA), and 1
# with NXDOMAIN for a name without any. Anything else, a timeout or SERVFAIL,
# is no answer and stops the start (Codex on PR #340): taken as "no records",
# the ACL would hold the A record alone while the registration, once DNS is
# back, follows the SRV records. The restart policy tries again.
ipv4() { getent ahostsv4 "$1" | awk '{ print $1 }'; }
SRV_SERVERS=
case "$ACCESS_HOST" in
    *[!0-9.]*)
        if srv=$(nslookup -type=SRV "_sip._udp.$ACCESS_HOST" 2>&1); then
            SRV_SERVERS=$(printf '%s\n' "$srv" |
                awk '/service = / { sub(/\.$/, "", $NF); print $NF }')
        else
            case "$srv" in
                *": NXDOMAIN") ;;
                *)
                    echo "asterisk does not start: the SRV lookup of ACCESS_HOST failed" >&2
                    exit 1
                    ;;
            esac
        fi
        ;;
esac
# Every server the SRV records name has to have an address: one left out would
# have its calls refused once DNS answers for it again (Codex on PR #340).
# musl's getent exits 2 for a failed lookup and a missing A record alike, so
# both stop the start; ponytail: a trunk with an IPv6-only server never starts,
# check per record type with nslookup if one does (T-10.12).
if [ -n "$SRV_SERVERS" ]; then
    addresses=
    set -f
    for server in $SRV_SERVERS; do
        found=$(ipv4 "$server")
        if [ -z "$found" ]; then
            echo "asterisk does not start: SRV server $server of ACCESS_HOST has no IPv4 address" >&2
            exit 1
        fi
        addresses="$addresses$found$newline"
    done
    set +f
else
    addresses=$(ipv4 "$ACCESS_HOST")
fi
ACCESS_ADDRESSES=$(printf '%s\n' "$addresses" |
    awk 'NF && !seen[$0]++ { printf "%s%s", sep, $0; sep = "," }')
if [ -z "$ACCESS_ADDRESSES" ]; then
    echo "asterisk does not start: ACCESS_HOST has no IPv4 address" >&2
    exit 1
fi
echo "asterisk: SIP only from $ACCESS_ADDRESSES"

# Where the dialplan sends a team leg. With SRV records: the name alone, and
# Asterisk takes the server and port from them, as the registration does; with
# a port it would skip them and dial the A record, which such an access may not
# have (Codex on PR #340). Without: port 5060 given, which spares the lookups
# of records that do not exist (2 to 7 s on the workbench, docs/20 §11).
if [ -n "$SRV_SERVERS" ]; then
    export ACCESS_DIAL="$ACCESS_HOST"
    echo "asterisk: team legs by the SRV records of ACCESS_HOST"
else
    export ACCESS_DIAL="$ACCESS_HOST:5060"
    echo "asterisk: team legs to ACCESS_HOST, port 5060"
fi

# ";" starts a comment in Asterisk's configuration: a password with one would be
# cut short without a word. Escaped for the files only; the environment the
# dialplan reads keeps the values as they are.
esc() { printf '%s' "$1" | sed 's/;/\\;/g'; }
for file in /templates/conf/*.conf; do
    VOICE_MEDIA_TOKEN="$(esc "$VOICE_MEDIA_TOKEN")" ACCESS_HOST="$(esc "$ACCESS_HOST")" \
        ACCESS_USER="$(esc "$ACCESS_USER")" ACCESS_PASSWORD="$(esc "$ACCESS_PASSWORD")" \
        ACCESS_ADDRESSES="$ACCESS_ADDRESSES" \
        envsubst '${VOICE_MEDIA_TOKEN} ${ACCESS_HOST} ${ACCESS_USER} ${ACCESS_PASSWORD} ${ACCESS_ADDRESSES}' \
        < "$file" > "/etc/asterisk/$(basename "$file")"
done

exec /usr/local/bin/entrypoint.sh "$@"
