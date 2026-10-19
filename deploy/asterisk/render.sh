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
own() {
    [ "$1" = "$VOICE_NUMBER" ] || [ "$1" = "00${VOICE_NUMBER#+}" ] ||
        [ "$1" = "0${VOICE_NUMBER#+49}" ]
}
if own "$TEAM_PHONE"; then
    echo "asterisk does not start: TEAM_PHONE is the AI telephone's own number" >&2
    exit 1
fi

# The numbers a transfer may go to besides TEAM_PHONE, comma-separated; may be
# empty. The dialplan dials no other (#252 finding 3), so each has to be one it
# can dial, and none ours.
TRANSFER_TARGETS="${TRANSFER_TARGETS:-}"
set -f
old_ifs=$IFS
IFS=,
for target in $TRANSFER_TARGETS; do
    if ! number "$target" "$dialable"; then
        echo "asterisk does not start: TRANSFER_TARGETS holds something that is not a number" >&2
        exit 1
    fi
    if own "$target"; then
        echo "asterisk does not start: TRANSFER_TARGETS holds the AI telephone's own number" >&2
        exit 1
    fi
done
IFS=$old_ifs
set +f

# Asterisk takes SIP only from the access's addresses (#252 finding 2). Its ACL
# takes addresses, not names, so ACCESS_HOST is looked up here, once, as
# Asterisk looks up a name in `match` once at the start; an access that moves
# to another address needs a restart. IPv4 only, as the transport.
ACCESS_ADDRESSES=$(getent ahostsv4 "$ACCESS_HOST" |
    awk '!seen[$1]++ { printf "%s%s", sep, $1; sep = "," }')
if [ -z "$ACCESS_ADDRESSES" ]; then
    echo "asterisk does not start: ACCESS_HOST has no IPv4 address" >&2
    exit 1
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
