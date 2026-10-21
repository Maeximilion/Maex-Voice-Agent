"""Tests for scripts/status_bump.py."""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "status_bump", Path(__file__).resolve().parents[2] / "scripts" / "status_bump.py"
)
status_bump = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(status_bump)

STATUS = "Status: 01.01.2026 · Status version: 1.2.3\n\n## Changelog\n\n- old entry\n"


def _run(tmp_path, text, level="patch"):
    f = tmp_path / "status.md"
    if text is not None:
        f.write_text(text, encoding="utf-8")
    return f, status_bump.main([level, "did a thing", "--file", str(f)])


@pytest.mark.parametrize(
    ("level", "expected"),
    [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")],
)
def test_levels(tmp_path, capsys, level, expected):
    f, code = _run(tmp_path, STATUS, level)
    assert code == 0
    text = f.read_text(encoding="utf-8")
    assert f"Status version: {expected}" in text
    assert f"- **v{expected} · " in text
    assert text.index("did a thing") < text.index("- old entry")
    assert f"v1.2.3 -> v{expected}" in capsys.readouterr().out


def test_missing_file(tmp_path, capsys):
    _, code = _run(tmp_path, None)
    assert code == 1
    assert "does not exist" in capsys.readouterr().err


def test_missing_header(tmp_path, capsys):
    _, code = _run(tmp_path, "## Changelog\n\n")
    assert code == 1
    assert "no header line" in capsys.readouterr().err


def test_missing_changelog(tmp_path, capsys):
    f, code = _run(tmp_path, "Status: 01.01.2026 · Status version: 1.2.3\n")
    assert code == 1
    assert "## Changelog" in capsys.readouterr().err
    assert "1.2.3" in f.read_text(encoding="utf-8")
