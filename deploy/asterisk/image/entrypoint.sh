#!/bin/sh
# Entry of the own Asterisk image (deploy/asterisk/image/Dockerfile), called by
# deploy/asterisk/render.sh. Does what the vendor image's entrypoint did for
# us: as root, hand the runtime folders to the asterisk user (render.sh writes
# /etc/asterisk as root). Asterisk drops to its user itself (-U asterisk).
set -eu

if [ "$(id -u)" = "0" ]; then
    for path in /etc/asterisk /var/lib/asterisk /var/log/asterisk \
        /var/spool/asterisk /var/run/asterisk; do
        [ -d "$path" ] && chown -R asterisk:asterisk "$path"
    done
fi

exec "$@"
