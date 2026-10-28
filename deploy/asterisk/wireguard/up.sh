#!/bin/sh
# The AI telephone's own WireGuard tunnel into the pilot's router (T-10.1 step 2,
# docs/20 §3). Asterisk shares this container's network: the router reaches it
# at its tunnel address, with no NAT in between. Behind Docker's and WSL's
# address translation the router's INVITE never arrived (408 in the router's
# event log, 27.10.2026), only the answers to our own requests did.
#
# Reads the file the router hands out for a WireGuard device (ACCESS_WG_CONF,
# mounted read-only) and takes from it what the kernel needs: the keys, the
# endpoint, the device's IPv4 address and the networks behind the router. Never
# its DNS, which would take Docker's name lookup and with it `voice`, and never
# a default route: only the networks behind the router and the router's public
# address go through the tunnel.

set -eu

conf="${ACCESS_WG_CONF:-/run/access-wg.conf}"
work="${WG_WORK:-/tmp}"
# The copies hold the private key: gone however the script ends.
trap 'rm -f "$work/access.conf" "$work/wg.conf"' EXIT
# A file saved on Windows may end its lines with CR.
tr -d '\r' < "$conf" > "$work/access.conf"

values() {
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//Ip" "$work/access.conf" |
        tr ',' '\n' | tr -d ' \t'
}

address=$(values Address | grep -m1 -E '^[0-9]+(\.[0-9]+){3}(/[0-9]+)?$' || true)
if [ -z "$address" ]; then
    echo "wireguard does not start: the configuration has no IPv4 Address" >&2
    exit 1
fi
case "$address" in */*) ;; *) address="$address/32" ;; esac
networks=$(values AllowedIPs | grep -E '^[0-9]+(\.[0-9]+){3}/[0-9]+$' | grep -v '^0\.0\.0\.0/0$' || true)
if [ -z "$networks" ]; then
    echo "wireguard does not start: no IPv4 network behind the router in AllowedIPs" >&2
    exit 1
fi

# What `wg` takes: the file without the wg-quick lines, and a keepalive of our
# own, so the address translation in front of the tunnel stays open for the
# router's calls.
grep -Eiv '^[[:space:]]*(Address|DNS|MTU|Table|PreUp|PostUp|PreDown|PostDown|SaveConfig|PersistentKeepalive)[[:space:]]*=' \
    "$work/access.conf" | sed '/^[[:space:]]*\[Peer\]/a PersistentKeepalive = 25' > "$work/wg.conf"

ip link add wg0 type wireguard
wg setconf wg0 "$work/wg.conf"
# Now, not at the exit: `exec` below ends the shell without its trap.
rm -f "$work/wg.conf" "$work/access.conf"
ip address add "$address" dev wg0
ip link set wg0 up
for network in $networks; do
    ip route replace "$network" dev wg0
done
# The router names its public address in Via and Contact of a call, also to a
# device in its tunnel, and that address is the tunnel's endpoint: answers to
# it go through the tunnel, the tunnel's own packets (marked) around it, as
# wg-quick does it (28.10.2026, 200 OK sent past the router 68 times).
peer=$(wg show wg0 endpoints | head -n1)
endpoint=$(echo "$peer" | cut -f2 | sed 's/:[0-9]*$//')
case "$endpoint" in
    *.*.*.*)
        # WireGuard takes from a peer only what its AllowedIPs name: a file that
        # sends only the router's network through the tunnel would drop the calls.
        allowed=$(wg show wg0 allowed-ips | head -n1 | cut -f2 | tr ' ' ',')
        wg set wg0 peer "$(echo "$peer" | cut -f1)" allowed-ips "$allowed,$endpoint/32"
        wg set wg0 fwmark 51820
        ip route replace "$endpoint/32" dev wg0 table 51820
        ip rule add not fwmark 51820 table 51820
        ;;
    *) echo "wireguard: no IPv4 endpoint, the router's public address stays outside the tunnel" >&2 ;;
esac
echo "wireguard: tunnel up, $(echo "$networks" | wc -l) network(s) behind the router"
exec sleep infinity
