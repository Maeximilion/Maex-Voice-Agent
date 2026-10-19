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
# In every form the dialplan's loop guard knows: +49..., 0049... and 0....
e164() {
    case "$1" in
        +*) printf '%s' "$1" ;;
        00*) printf '+%s' "${1#00}" ;;
        0*) printf '+49%s' "${1#0}" ;;
        *) printf '%s' "$1" ;;
    esac
}
same() { [ "$(e164 "$1")" = "$(e164 "$2")" ]; }
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
# to another address needs a restart. A name counts with its own addresses and
# those of the servers its SIP SRV records name: Asterisk's registration follows
# them, and a trunk's calls come from them (ultrareview of PR #340). IPv4 only,
# as the transport.
ipv4() { getent ahostsv4 "$1" | awk '{ print $1 }'; }
srv_targets() {
    case "$1" in *[!0-9.]*) ;; *) return 0 ;; esac
    for service in _sip._udp _sip._tcp _sips._tcp; do
        nslookup -type=SRV "$service.$1" 2>/dev/null |
            awk '/service = / { sub(/\.$/, "", $NF); print $NF }'
    done
}
ACCESS_ADDRESSES=$(
    {
        ipv4 "$ACCESS_HOST"
        srv_targets "$ACCESS_HOST" | while read -r server; do ipv4 "$server"; done
    } | awk '!seen[$0]++ { printf "%s%s", sep, $0; sep = "," }'
)
if [ -z "$ACCESS_ADDRESSES" ]; then
    echo "asterisk does not start: ACCESS_HOST has no IPv4 address" >&2
    exit 1
fi
echo "asterisk: SIP only from $ACCESS_ADDRESSES"

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
