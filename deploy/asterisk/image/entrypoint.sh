#!/bin/sh
# Entry of the own Asterisk image (deploy/asterisk/image/Dockerfile), called by
# deploy/asterisk/render.sh. Does what the vendor image's entrypoint did for
# us: as root, hand the runtime folders to the asterisk user (render.sh writes
# /etc/asterisk as root), and swap the -W of the command for
# ASTERISK_TERMINAL_OPTS when that is set ("-n": no colours in the logs).
# Asterisk drops to its user itself (-U asterisk).
set -eu

if [ "$(id -u)" = "0" ]; then
    for path in /etc/asterisk /var/lib/asterisk /var/log/asterisk \
        /var/spool/asterisk /var/run/asterisk; do
        [ -d "$path" ] && chown -R asterisk:asterisk "$path"
    done
fi

if [ -n "${ASTERISK_TERMINAL_OPTS+x}" ]; then
    count=$#
    while [ "$count" -gt 0 ]; do
        arg=$1
        shift
        if [ "$arg" = "-W" ]; then
            # Split on purpose: the variable holds options, one per word.
            # shellcheck disable=SC2086
            set -- "$@" $ASTERISK_TERMINAL_OPTS
        else
            set -- "$@" "$arg"
        fi
        count=$((count - 1))
    done
fi

exec "$@"
