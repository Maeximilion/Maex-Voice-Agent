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
# nslookup -type=SRV <service>.<name>, in the format of busybox.
printf 'Server:\\t\\t127.0.0.11\\nAddress:\\t127.0.0.11:53\\n\\n'
printf 'Non-authoritative answer:\\n'
awk -v n="$2" '$1 == n { print n "\\tservice = 0 0 5060 " $2 }' "$FAKE_DNS/srv"
"""


@pytest.fixture
def dns(tmp_path):
    """Records for the stand-in: `dns["a"]` lines "name address", `dns["srv"]`
    lines "_sip._udp.name target"."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("getent", GETENT), ("nslookup", NSLOOKUP)):
        (bin_dir / name).write_text(script)
        (bin_dir / name).chmod(0o755)
    return {"a": "", "srv": ""}


@pytest.fixture
def render(tmp_path, dns):
    def run(**values: str) -> subprocess.CompletedProcess[str]:
        (tmp_path / "a").write_text(dns["a"])
        (tmp_path / "srv").write_text(dns["srv"])
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


@pytest.mark.parametrize("form", ["+4972215550900", "004972215550900", "072215550900"])
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


def test_the_team_on_our_own_number_is_refused(render):
    done = render(TEAM_PHONE="072215550100")
    assert done.returncode == 1
    assert "TEAM_PHONE is the AI telephone's own number" in done.stderr


def test_an_ipv4_access_is_the_only_address(render):
    done = render(ACCESS_HOST="10.1.2.3")
    assert reached_the_templates(done), done.stderr
    assert "SIP only from 10.1.2.3\n" in done.stdout


def test_a_named_access_counts_with_the_servers_of_its_srv_records(render, dns):
    """Asterisk's registration follows the SRV records of ACCESS_HOST, so the
    calls of a trunk come from the servers they name; an ACL with the A
    record alone would refuse them while the registration stays green
    (ultrareview of PR #340)."""
    dns["a"] = "trunk.example 10.0.0.1\nsip1.example 10.0.0.2\nsip2.example 10.0.0.3\n"
    dns["srv"] = (
        "_sip._udp.trunk.example sip1.example.\n_sip._tcp.trunk.example sip2.example\n"
    )
    done = render(ACCESS_HOST="trunk.example")
    assert reached_the_templates(done), done.stderr
    assert "SIP only from 10.0.0.1,10.0.0.2,10.0.0.3\n" in done.stdout


def test_a_name_with_srv_records_only_is_an_access(render, dns):
    dns["a"] = "sip1.example 10.0.0.2\n"
    dns["srv"] = "_sip._udp.trunk.example sip1.example\n"
    done = render(ACCESS_HOST="trunk.example")
    assert reached_the_templates(done), done.stderr
    assert "SIP only from 10.0.0.2\n" in done.stdout


@pytest.mark.parametrize("host", ["access.invalid", "::1"])
def test_an_access_without_an_ipv4_address_is_refused(render, host):
    """The ACL takes addresses, not names: without one Asterisk would take SIP
    from nobody, or with a broken ACL block it all (#252 finding 2)."""
    done = render(ACCESS_HOST=host)
    assert done.returncode == 1
    assert "ACCESS_HOST has no IPv4 address" in done.stderr
