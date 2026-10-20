"""deploy/asterisk/render.sh: the checks before the AI telephone starts.

Rendering and Asterisk itself run in the outage drills on the workbench
(`python3 -m scripts.voice_drill`); here the refusals and the lookup of the
access's addresses, which need only a shell. A value the dialplan or the ACL
cannot use stops the start: then the access rings the team telephones as
before (docs/20 §3, §5).

Name lookups go to a stand-in on PATH: `getent ahostsv4` and busybox
`nslookup -type=SRV`, as the Asterisk image has them, answering from the
records a test writes. No test depends on the network.
"""

import os
import subprocess
from pathlib import Path

import pytest

RENDER = Path(__file__).resolve().parents[2] / "deploy" / "asterisk" / "render.sh"
VALID = {
    "VOICE_NUMBER": "+4972215550100",
    "VOICE_MEDIA_TOKEN": "t" * 32,
    "TEAM_PHONE": "+4972215550900",
    "ACCESS_HOST": "127.0.0.1",
    "ACCESS_USER": "ai",
    "ACCESS_PASSWORD": "pw",
}

GETENT = """#!/bin/sh
# getent ahostsv4 <name>: an IPv4 literal is itself, a name its A records.
case "$2" in
    *[!0-9.]*) ;;
    *) printf '%s\\tSTREAM %s\\n%s\\tDGRAM  %s\\n' "$2" "$2" "$2" "$2"; exit 0 ;;
esac
awk -v n="$2" '$1 == n { print $2 "\\tSTREAM " n; print $2 "\\tDGRAM  " n; hit = 1 }
    END { exit !hit }' "$FAKE_DNS/a"
"""
NSLOOKUP = """#!/bin/sh
# nslookup -type=SRV <service>.<name> as busybox in the Asterisk image answers
# (measured 20.10.2026): exit 0 with the records, or with none for a name that
# has other records only (NODATA); exit 1 with NXDOMAIN for a name without any,
# and exit 1 for a timeout or a SERVFAIL.
kind=$(awk -v n="$2" '$1 == n { print $2 }' "$FAKE_DNS/other")
case "$kind" in
    timeout) echo ';; connection timed out; no servers could be reached'; exit 1 ;;
esac
printf 'Server:\\t\\t127.0.0.11\\nAddress:\\t127.0.0.11:53\\n\\n'
case "$kind" in
    servfail) printf "** server can't find %s: SERVFAIL\\n" "$2"; exit 1 ;;
    nodata) printf 'Non-authoritative answer:\\n'; exit 0 ;;
esac
records=$(awk -v n="$2" '$1 == n { print n "\\tservice = 0 0 5060 " $2 }' "$FAKE_DNS/srv")
if [ -z "$records" ]; then
    printf "** server can't find %s: NXDOMAIN\\n" "$2"
    exit 1
fi
printf 'Non-authoritative answer:\\n%s\\n' "$records"
"""


@pytest.fixture
def dns(tmp_path):
    """Records for the stand-in: `dns["a"]` lines "name address", `dns["srv"]`
    lines "_sip._udp.name target", `dns["other"]` lines "_sip._udp.name kind"
    for an SRV query answered otherwise (nodata, timeout, servfail)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("getent", GETENT), ("nslookup", NSLOOKUP)):
        (bin_dir / name).write_text(script)
        (bin_dir / name).chmod(0o755)
    return {"a": "", "srv": "", "other": ""}


@pytest.fixture
def render(tmp_path, dns):
    def run(**values: str) -> subprocess.CompletedProcess[str]:
        for name in ("a", "srv", "other"):
            (tmp_path / name).write_text(dns[name])
        return subprocess.run(
            ["sh", str(RENDER)],
            env={
                "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
                "FAKE_DNS": str(tmp_path),
                **VALID,
                **values,
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    return run


def reached_the_templates(done: subprocess.CompletedProcess[str]) -> bool:
    """Every check passed: on the host the script stops at the templates, which
    are only in the container."""
    return "does not start" not in done.stderr and "/templates/conf" in done.stderr


@pytest.mark.parametrize(
    ("targets", "more"),
    [
        ("", 0),
        ("+4972215550901", 1),
        ("+4972215550901,072215550902", 2),
        # Written loosely: blanks and empty fields go, the list stays.
        (" +4972215550901 , ,072215550902,", 2),
        (",+4972215550901", 1),
    ],
)
def test_valid_values_pass_every_check(render, targets, more):
    done = render(TRANSFER_TARGETS=targets)
    assert reached_the_templates(done), done.stderr
    assert f"{more} more numbers for a transfer" in done.stdout


@pytest.mark.parametrize(
    "form", ["+4972215550900", "004972215550900", "072215550900", "4972215550900"]
)
def test_team_phone_on_the_list_in_any_form_is_left_out(render, form):
    """TEAM_PHONE is always on the list; in another form the dialplan would
    ring the same telephone a second time after an unanswered leg at it."""
    done = render(TEAM_PHONE="072215550900", TRANSFER_TARGETS=f"{form},+4972215550901")
    assert reached_the_templates(done), done.stderr
    assert "1 more numbers for a transfer" in done.stdout


@pytest.mark.parametrize(
    "targets",
    [
        "+4972215550100",
        "004972215550100",
        "072215550100",
        # International digits without a prefix: an access may dial them.
        "4972215550100",
        "+4972215550901,072215550100",
    ],
)
def test_our_own_number_as_a_transfer_target_is_refused(render, targets):
    """The loop guard would drop every call handed there (rule 5)."""
    done = render(TRANSFER_TARGETS=targets)
    assert done.returncode == 1
    assert "TRANSFER_TARGETS holds the AI telephone's own number" in done.stderr


@pytest.mark.parametrize(
    "targets",
    [
        "+49-721-5550901",
        "sip:team@example.com",
        # grep reads line by line: each line a number, the value is none.
        "+4972215550901\n+4972215550902",
    ],
)
def test_a_transfer_target_that_is_no_number_is_refused(render, targets):
    done = render(TRANSFER_TARGETS=targets)
    assert done.returncode == 1
    assert "TRANSFER_TARGETS holds something that is not a number" in done.stderr


@pytest.mark.parametrize("team", ["072215550100", "4972215550100"])
def test_the_team_on_our_own_number_is_refused(render, team):
    done = render(TEAM_PHONE=team)
    assert done.returncode == 1
    assert "TEAM_PHONE is the AI telephone's own number" in done.stderr


def test_an_ipv4_access_is_the_only_address(render):
    done = render(ACCESS_HOST="10.1.2.3")
    assert reached_the_templates(done), done.stderr
    assert "SIP only from 10.1.2.3\n" in done.stdout
    assert "team legs to ACCESS_HOST, port 5060" in done.stdout


def test_a_named_access_without_srv_records_is_dialled_on_port_5060(render, dns):
    """The port spares lookups of records that do not exist (docs/20 §11)."""
    dns["a"] = "router 10.0.0.9\n"
    done = render(ACCESS_HOST="router")
    assert reached_the_templates(done), done.stderr
    assert "SIP only from 10.0.0.9\n" in done.stdout
    assert "team legs to ACCESS_HOST, port 5060" in done.stdout


def test_a_named_access_counts_with_the_servers_of_its_udp_srv_records(render, dns):
    """Asterisk's registration follows the SRV records of ACCESS_HOST, so the
    calls of a trunk come from the servers they name; an ACL with the A
    record alone would refuse them while the registration stays green
    (ultrareview of PR #340). Only `_sip._udp`: our only transport is UDP, and
    a TCP server has no business sending it, and the A record of the name is
    used for nothing then, so it stays out too (Codex on PR #340). The team
    legs go by the SRV records too."""
    dns["a"] = "trunk.example 10.0.0.1\nsip1.example 10.0.0.2\nsip2.example 10.0.0.3\n"
    dns["srv"] = (
        "_sip._udp.trunk.example sip1.example.\n_sip._tcp.trunk.example sip2.example\n"
    )
    done = render(ACCESS_HOST="trunk.example")
    assert reached_the_templates(done), done.stderr
    assert "SIP only from 10.0.0.2\n" in done.stdout
    assert "team legs by the SRV records of ACCESS_HOST" in done.stdout


def test_a_name_with_srv_records_only_is_an_access(render, dns):
    dns["a"] = "sip1.example 10.0.0.2\n"
    dns["srv"] = "_sip._udp.trunk.example sip1.example\n"
    done = render(ACCESS_HOST="trunk.example")
    assert reached_the_templates(done), done.stderr
    assert "SIP only from 10.0.0.2\n" in done.stdout
    # No A record: with port 5060 the team leg would find no server at all.
    assert "team legs by the SRV records of ACCESS_HOST" in done.stdout


def test_a_name_with_other_records_only_counts_with_its_a_records(render, dns):
    """NODATA: the name exists, busybox answers without records and exits 0."""
    dns["a"] = "router 10.0.0.9\n"
    dns["other"] = "_sip._udp.router nodata\n"
    done = render(ACCESS_HOST="router")
    assert reached_the_templates(done), done.stderr
    assert "SIP only from 10.0.0.9\n" in done.stdout
    assert "team legs to ACCESS_HOST, port 5060" in done.stdout


@pytest.mark.parametrize("kind", ["timeout", "servfail"])
def test_a_failed_srv_lookup_stops_the_start(render, dns, kind):
    """A lookup that fails is no answer. Taken as "no records", the ACL would
    hold the name's A record only, and once DNS is back the registration would
    follow the SRV records to servers whose calls the ACL refuses until a
    restart (Codex on PR #340). Stopped, the container's restart policy tries
    again, and the router rings the team telephones meanwhile (docs/20 §3)."""
    dns["a"] = "trunk.example 10.0.0.1\n"
    dns["other"] = f"_sip._udp.trunk.example {kind}\n"
    done = render(ACCESS_HOST="trunk.example")
    assert done.returncode == 1
    assert "the SRV lookup of ACCESS_HOST failed" in done.stderr


@pytest.mark.parametrize("host", ["access.invalid", "::1"])
def test_an_access_without_an_ipv4_address_is_refused(render, host):
    """The ACL takes addresses, not names: without one Asterisk would take SIP
    from nobody, or with a broken ACL block it all (#252 finding 2)."""
    done = render(ACCESS_HOST=host)
    assert done.returncode == 1
    assert "ACCESS_HOST has no IPv4 address" in done.stderr
