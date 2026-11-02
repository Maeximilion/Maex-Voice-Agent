"""The print bridge as one program file (T-9.6): the smoke run of the built file.

The workflow runs these checks on the real program on Windows. Here they run against
`python -m printbridge`, to prove that each check passes on a sound program and fails on
the fault it exists for.
"""

import http.server
import os
import sys
import threading
from pathlib import Path

import pytest

from scripts.printbridge_smoke import checks

ROOT = Path(__file__).resolve().parents[2]
PROGRAM = [sys.executable, "-m", "printbridge"]
FAKE_WIN32PRINT = "def OpenPrinter(name):\n    raise OSError('no such queue')\n"


def _with_path(monkeypatch: pytest.MonkeyPatch, *folders: Path) -> None:
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(map(str, (*folders, ROOT))))


@pytest.fixture
def win32print(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "win32print.py").write_text(FAKE_WIN32PRINT, encoding="utf-8")
    _with_path(monkeypatch, tmp_path)


@pytest.mark.usefixtures("win32print")
def test_a_sound_program_passes():
    assert checks(PROGRAM) == []


def test_a_program_without_win32print_fails(monkeypatch: pytest.MonkeyPatch):
    # The fault a build without --hidden-import win32print would ship: the register's
    # own printer path gone, while everything over TCP still works.
    _with_path(monkeypatch)
    failures = checks(PROGRAM)
    assert len(failures) == 1
    assert failures[0].startswith("Windows queue path")
    assert "pywin32 fehlt" in failures[0]


class _Refuses(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_error(404)

    def log_message(self, format, *args):
        pass


@pytest.mark.usefixtures("win32print")
def test_an_answering_address_counts_as_reached():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Refuses)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert checks(PROGRAM, f"http://127.0.0.1:{server.server_port}") == []
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.usefixtures("win32print")
def test_an_address_out_of_reach_fails():
    failures = checks(PROGRAM, "https://127.0.0.1:9")
    assert len(failures) == 1
    assert failures[0].startswith("https")


@pytest.mark.parametrize(
    "path",
    [
        "printbridge/install.ps1",
        "printbridge/run.ps1",
        "scripts/printbridge_install_check.ps1",
    ],
)
def test_powershell_files_are_ascii(path):
    # Windows PowerShell 5.1 reads a file without a byte order mark as the system's code
    # page, so one non-ASCII character can break a script only on the register PC.
    assert (ROOT / path).read_bytes().isascii()
