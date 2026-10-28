"""deploy/asterisk/wireguard/up.sh: the AI telephone's tunnel into the router.

The script runs against stand-ins for `ip`, `wg` and `sleep` on PATH that write
down how they were called; `wg setconf` also keeps the file it was given. The
real tunnel runs on the workbench (docs/20 §10, T-10.1 step 2).
"""

import os
import subprocess
from pathlib import Path

import pytest

UP = Path(__file__).resolve().parents[2] / "deploy" / "asterisk" / "wireguard" / "up.sh"
STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$CALLS"
if [ "$(basename "$0")" = wg ] && [ "$1" = setconf ]; then cp "$3" "$CALLS.wg"; fi
"""
# As the router hands it out for a WireGuard device: the PC's own address, the
# router's DNS, everything through the tunnel. Invented keys and names.
ROUTER_FILE = """[Interface]\r
PrivateKey = cHJpdmF0ZWtleXByaXZhdGVrZXlwcml2YXRla2V5MDA=\r
Address = 192.168.178.201/24,fd00::201/64\r
DNS = 192.168.178.1,fd00::1,fritz.box\r
\r
[Peer]\r
PublicKey = cHVibGlja2V5cHVibGlja2V5cHVibGlja2V5cHViMDA=\r
PresharedKey = c2hhcmVka2V5c2hhcmVka2V5c2hhcmVka2V5c2gwMDA=\r
AllowedIPs = 192.168.178.0/24,0.0.0.0/0,fd00::/64,::/0\r
Endpoint = example.myfritz.example.com:51820\r
PersistentKeepalive = 25\r
"""


@pytest.fixture
def up(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ip", "wg", "sleep"):
        (bin_dir / name).write_text(STUB)
        (bin_dir / name).chmod(0o755)
    calls = tmp_path / "calls"

    def run(text: str) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
        conf = tmp_path / "access.conf"
        conf.write_text(text)
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        done = subprocess.run(
            ["sh", str(UP)],
            env={
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "CALLS": str(calls),
                "ACCESS_WG_CONF": str(conf),
                "WG_WORK": str(work),
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        lines = calls.read_text().splitlines() if calls.exists() else []
        given = Path(f"{calls}.wg")
        assert not list(work.iterdir()), "the key stays nowhere but in the kernel"
        return done, lines, given.read_text() if given.exists() else ""

    return run


def test_the_routers_file_becomes_a_tunnel_to_the_router_only(up):
    done, calls, given = up(ROUTER_FILE)

    assert done.returncode == 0, done.stderr
    assert calls[0] == "ip link add wg0 type wireguard"
    assert calls[1].startswith("wg setconf wg0 ")
    assert calls[2:] == [
        "ip address add 192.168.178.201/24 dev wg0",
        "ip link set wg0 up",
        "ip route replace 192.168.178.0/24 dev wg0",
        "sleep infinity",
    ]
    # Only what `wg` understands, our keepalive once, no CR, nothing of DNS.
    assert "Address" not in given and "DNS" not in given and "\r" not in given
    assert given.count("PersistentKeepalive = 25") == 1
    assert "PrivateKey = " in given and "Endpoint = " in given


def test_an_address_without_a_prefix_is_one_host(up):
    text = ROUTER_FILE.replace("192.168.178.201/24,fd00::201/64", "192.168.178.201")
    text = text.replace("PersistentKeepalive = 25\r\n", "")
    done, calls, given = up(text)

    assert done.returncode == 0, done.stderr
    assert "ip address add 192.168.178.201/32 dev wg0" in calls
    assert given.count("PersistentKeepalive = 25") == 1


def test_a_file_without_an_ipv4_address_starts_nothing(up):
    done, calls, _ = up(ROUTER_FILE.replace("192.168.178.201/24,", ""))

    assert done.returncode == 1
    assert "no IPv4 Address" in done.stderr
    assert calls == []


def test_a_file_that_only_routes_everything_starts_nothing(up):
    done, calls, _ = up(
        ROUTER_FILE.replace("192.168.178.0/24,0.0.0.0/0,fd00::/64,::/0", "0.0.0.0/0")
    )

    assert done.returncode == 1
    assert "no IPv4 network behind the router" in done.stderr
    assert calls == []
