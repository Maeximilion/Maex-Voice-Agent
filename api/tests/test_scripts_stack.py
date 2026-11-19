"""`scripts/stack.cmd` switches the workbench stack in Ubuntu WSL on, off and to pause.

CI runs on Linux, so the file is read there; the one run without Docker or WSL,
an unknown action, happens only on Windows.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "stack.cmd"


def test_each_action_maps_to_its_compose_verb():
    text = SCRIPT.read_text()
    pairs = dict(re.findall(r'if /i "%ACTION%"=="(\w+)" set "VERB=(\w+)"', text))
    assert pairs == {
        "start": "start",
        "pause": "pause",
        "resume": "unpause",
        "stop": "stop",
        "status": "status",
    }
    # status only lists; every other verb runs before the listing.
    assert 'if "%VERB%"=="status" set "STEP="' in text


def test_file_is_ascii_with_crlf():
    raw = SCRIPT.read_bytes()
    assert raw.isascii()
    # cmd.exe misreads labels and goto in a file with bare LF line ends.
    assert raw.count(b"\n") == raw.count(b"\r\n")


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe exists only on Windows")
def test_unknown_action_exits_2_without_touching_docker():
    result = subprocess.run(
        ["cmd", "/c", str(SCRIPT), "bogus"], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "Unknown action" in result.stdout
