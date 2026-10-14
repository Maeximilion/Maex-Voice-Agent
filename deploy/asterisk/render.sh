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

for name in VOICE_NUMBER VOICE_MEDIA_TOKEN TEAM_PHONE ACCESS_HOST ACCESS_USER ACCESS_PASSWORD; do
    # eval on a fixed list of names, never on input.
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
        echo "asterisk does not start: $name is not set (.env, docs/20 §7)" >&2
        exit 1
    fi
done

for file in /templates/conf/*.conf; do
    envsubst '${VOICE_MEDIA_TOKEN} ${ACCESS_HOST} ${ACCESS_USER} ${ACCESS_PASSWORD}' \
        < "$file" > "/etc/asterisk/$(basename "$file")"
done

exec /usr/local/bin/entrypoint.sh "$@"
