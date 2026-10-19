"""deploy/asterisk/render.sh: the checks before the AI telephone starts.

Rendering and Asterisk itself run in the outage drills on the workbench
(`python3 -m scripts.voice_drill`); here the refusals, which need only a shell.
A value the dialplan or the ACL cannot use stops the start: then the access
rings the team telephones as before (docs/20 §3, §5).
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


def render(**values: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(RENDER)],
        env={"PATH": os.environ["PATH"], **VALID, **values},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    "targets", ["", "+4972215550901", "+4972215550901,072215550902"]
)
def test_valid_values_pass_every_check(targets):
    """On the host the script gets as far as the templates, which are only in
    the container."""
    done = render(TRANSFER_TARGETS=targets)
    assert "does not start" not in done.stderr


@pytest.mark.parametrize(
    "targets",
    [
        "+4972215550100",
        "004972215550100",
        "072215550100",
        "+4972215550901,072215550100",
    ],
)
def test_our_own_number_as_a_transfer_target_is_refused(targets):
    """The loop guard would drop every call handed there (rule 5)."""
    done = render(TRANSFER_TARGETS=targets)
    assert done.returncode == 1
    assert "TRANSFER_TARGETS holds the AI telephone's own number" in done.stderr


@pytest.mark.parametrize(
    "targets",
    [
        "+49 721 5550901",
        "+4972215550901,,+4972215550902",
        "sip:team@example.com",
        # grep reads line by line: each line a number, the value is none.
        "+4972215550901\n+4972215550902",
    ],
)
def test_a_transfer_target_that_is_no_number_is_refused(targets):
    done = render(TRANSFER_TARGETS=targets)
    assert done.returncode == 1
    assert "TRANSFER_TARGETS holds something that is not a number" in done.stderr


def test_the_team_on_our_own_number_is_refused():
    done = render(TEAM_PHONE="072215550100")
    assert done.returncode == 1
    assert "TEAM_PHONE is the AI telephone's own number" in done.stderr


@pytest.mark.parametrize("host", ["access.invalid", "::1"])
def test_an_access_without_an_ipv4_address_is_refused(host):
    """The ACL takes addresses, not names: without one Asterisk would take SIP
    from nobody, or with a broken ACL block it all (#252 finding 2)."""
    done = render(ACCESS_HOST=host)
    assert done.returncode == 1
    assert "ACCESS_HOST has no IPv4 address" in done.stderr
