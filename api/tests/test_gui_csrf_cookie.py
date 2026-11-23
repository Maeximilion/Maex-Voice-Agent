"""The CSRF cookie the operations view's scripts send, in node (#888 S2)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSRF = ROOT / "api" / "gui" / "static" / "csrf.js"


def test_the_scripts_read_the_csrf_cookie_by_its_exact_name():
    node = shutil.which("node")
    if not node:
        # The api image carries no node; CI's runner does, and must run this.
        if os.environ.get("CI"):
            pytest.fail("the csrf cookie check needs node")
        pytest.skip("the csrf cookie check needs node")
    out = subprocess.run(
        [node, str(Path(__file__).with_name("csrf_cookie.mjs")), str(CSRF)],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_both_writers_use_it_and_no_script_reads_the_old_name():
    static = ROOT / "api" / "gui" / "static"
    for name in ("base.js", "raumplan.js"):
        assert 'from "./csrf.js"' in (static / name).read_text(encoding="utf-8")
    for script in (ROOT / "api" / "gui" / "ts").glob("*.ts"):
        assert "csrf_token=" not in script.read_text(encoding="utf-8"), script.name
