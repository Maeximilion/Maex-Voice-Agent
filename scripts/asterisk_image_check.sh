#!/bin/sh
# Check an image of the AI telephone's Asterisk (#775): it boots, runs the
# modules the product uses, and lives through media connections to `voice`
# that fail. Asterisk 22.11.0 crashed after one or two such failures (Asterisk
# issue #2121, #768); the dialplan's probe of `/media/ready` keeps most calls
# away from that path, this check dials it on purpose.
#
#   sh scripts/asterisk_image_check.sh <image>
#
# Runs the product's own `Dial(WebSocket/...)` line from
# deploy/asterisk/conf/extensions.conf and its websocket_client.conf, against a
# `voice` that refuses the connection, one whose name does not resolve and one
# that takes the connection and answers nothing (Asterisk issue #1979). Each
# failure must end the Dial as CHANUNAVAIL with Asterisk still up, and a call
# after them must run. Own network, no host port, removed at the end. Exit 0
# when all holds, 1 when not, 2 on wrong use. Needs docker and the alpine base
# image of deploy/asterisk/image/Dockerfile.
set -eu

image=${1:-}
if [ -z "$image" ]; then
    echo "usage: sh scripts/asterisk_image_check.sh <image>" >&2
    exit 2
fi
root=$(cd "$(dirname "$0")/.." && pwd)
conf=$root/deploy/asterisk/conf
alpine=$(sed -n 's/^FROM \([^ ]*\) AS build$/\1/p' "$root/deploy/asterisk/image/Dockerfile")
dial=$(grep -m1 -o 'Dial(WebSocket/[^ ]*' "$conf/extensions.conf")
if [ -z "$dial" ] || [ -z "$alpine" ]; then
    echo "asterisk_image_check: no media Dial in extensions.conf or no base image" >&2
    exit 2
fi
tag=aic$$
work=$(mktemp -d)
cleanup() {
    docker rm -f "$tag-ast" "$tag-voice" >/dev/null 2>&1 || true
    docker network rm "$tag" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

sed 's/${VOICE_MEDIA_TOKEN}/check-token/' "$conf/websocket_client.conf" > "$work/websocket_client.conf"
printf '[options]\n' > "$work/asterisk.conf"
printf '[modules]\nautoload=yes\n' > "$work/modules.conf"
printf '[logfiles]\nconsole => notice,warning,error\n' > "$work/logger.conf"
# How long each Dial took, in ms: a frozen `voice` has to hold it for the
# connection timeout, or the check never met a frozen one.
timeout_ms=$(sed -n 's/^connection_timeout=\([0-9][0-9]*\).*/\1/p' "$conf/websocket_client.conf")
if [ -z "$timeout_ms" ]; then
    echo "asterisk_image_check: no connection_timeout in websocket_client.conf" >&2
    exit 2
fi
cat > "$work/extensions.conf" <<EOF
[check]
exten => fail,1,Set(T0=\${STRFTIME(,,%s%3q)})
 same => n,$dial
 same => n,Log(NOTICE,check ended \${DIALSTATUS} after \$[\${STRFTIME(,,%s%3q)} - \${T0}] ms)
 same => n,Hangup()
exten => next,1,Log(NOTICE,check next call runs)
 same => n,Hangup()
EOF
chmod -R a+rX "$work"

failed=0
fail() {
    echo "FAIL  $1"
    failed=1
}
cli() { docker exec "$tag-ast" asterisk -rx "$1" 2>/dev/null; }
alive() { [ "$(docker inspect -f '{{.State.Running}}' "$tag-ast" 2>/dev/null)" = true ]; }
lines() { docker logs "$tag-ast" 2>&1 | grep -c "$1" || true; }

docker network create "$tag" >/dev/null
docker run -d --name "$tag-ast" --network "$tag" -v "$work:/etc/asterisk:ro" \
    --entrypoint /usr/sbin/asterisk "$image" -f -n >/dev/null
booted=no
for _ in $(seq 1 60); do
    if cli "core waitfullybooted" >/dev/null; then booted=yes; break; fi
    sleep 1
done
if [ "$booted" = no ]; then
    docker logs "$tag-ast" 2>&1 | tail -20
    fail "Asterisk did not boot within 60 s"
    exit 1
fi
echo "      $(cli 'core show version' | head -1)"

# Every module the product's dialplan, features.conf and pjsip.conf use
# (api/tests/test_asterisk_image_check.py keeps the list complete): with
# autoload=yes a missing one stops neither the boot nor the healthcheck. A
# module that loaded and declined is listed as "Not Running".
for module in chan_websocket res_websocket_client res_http_websocket chan_pjsip \
    res_pjsip_outbound_registration res_pjsip_acl res_pjsip_endpoint_identifier_ip \
    res_resolver_unbound res_rtp_asterisk pbx_config bridge_simple bridge_native_rtp \
    app_dial app_exec app_stack app_verbose app_mixmonitor app_playback app_system \
    func_callerid func_curl func_env func_logic func_strings \
    codec_ulaw codec_alaw codec_resample format_wav format_sln format_pcm; do
    if cli "module show like $module" | grep -Eq '[0-9][[:space:]]+Running[[:space:]]'; then
        echo "PASS  $module runs"
    else
        fail "$module does not run"
    fi
done

calls=0
for voice in refused unresolved frozen; do
    docker rm -f "$tag-voice" >/dev/null 2>&1 || true
    case $voice in
        refused)
            docker run -d --name "$tag-voice" --network "$tag" --network-alias voice \
                "$alpine" sleep 600 >/dev/null ;;
        # `-e sleep` holds each connection silent, `-k` takes the next: a plain
        # `nc -l` has /dev/null as stdin here and closes at once.
        frozen)
            docker run -d --name "$tag-voice" --network "$tag" --network-alias voice \
                "$alpine" nc -lk -p 8001 -e sleep 600 >/dev/null ;;
    esac
    sleep 1
    for _ in 1 2 3; do
        calls=$((calls + 1))
        cli "channel originate Local/fail@check application Wait 1" >/dev/null || true
        for _ in $(seq 1 20); do
            [ "$(lines 'check ended')" -ge "$calls" ] && break
            alive || break
            sleep 1
        done
    done
    if ! alive; then
        fail "voice $voice: Asterisk died ($(docker inspect -f 'exit {{.State.ExitCode}}' "$tag-ast"))"
        break
    fi
    ended=$(lines 'check ended CHANUNAVAIL')
    if [ "$ended" -lt "$calls" ]; then
        fail "voice $voice: $ended of $calls Dials ended CHANUNAVAIL"
        continue
    fi
    if [ "$voice" = frozen ]; then
        short=$(docker logs "$tag-ast" 2>&1 | sed -n 's/.*check ended CHANUNAVAIL after \([0-9]*\) ms.*/\1/p' |
            tail -n 3 | awk -v min="$timeout_ms" '$1 < min { print; exit }')
        if [ -n "$short" ]; then
            fail "voice frozen: a Dial ended after $short ms, under the connection timeout of $timeout_ms ms"
            continue
        fi
    fi
    echo "PASS  voice $voice: 3 failed media connections, each CHANUNAVAIL, Asterisk up"
done

if alive; then
    cli "channel originate Local/next@check application Wait 1" >/dev/null || true
    sleep 2
    if [ "$(lines 'check next call runs')" -ge 1 ]; then
        echo "PASS  the call after the failures runs"
    else
        fail "the call after the failures did not run"
    fi
fi
# Issue #2121 crashed Asterisk when a call's thread ended, after the call's
# last log line: a death no phase saw still fails the check.
if ! alive && [ "$failed" = 0 ]; then
    fail "Asterisk died after the failures ($(docker inspect -f 'exit {{.State.ExitCode}}' "$tag-ast"))"
fi
exit "$failed"
