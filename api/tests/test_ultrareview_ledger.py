"""Tests for scripts/ultrareview_ledger.py: candidates are added once, ticked with
their review's level and the issues of their unfixed findings."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ultrareview_ledger",
    Path(__file__).resolve().parents[2] / "scripts" / "ultrareview_ledger.py",
)
assert _SPEC is not None and _SPEC.loader is not None
ledger = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ledger)

DAY = "2026-11-05"


def test_a_candidate_is_added_once_as_an_open_line():
    body = ledger.add(ledger.fresh_body(), 544, "migration 029,\n a booking", DAY)
    again = ledger.add(body, 544, "other words", "2026-11-06")

    assert again == body
    assert ledger.open_items(body) == [
        "- [ ] #544 migration 029, a booking · named 2026-11-05"
    ]
    assert ledger.MARKER in body


def test_a_review_ticks_its_line_with_level_and_open_findings():
    body = ledger.add(ledger.fresh_body(), 544, "a booking", DAY)
    body = ledger.add(body, 545, "a migration", DAY)

    body = ledger.tick(body, 544, "max", "2026-11-06", [601, 602])

    assert ledger.open_items(body) == ["- [ ] #545 a migration · named 2026-11-05"]
    assert (
        "- [x] #544 a booking · named 2026-11-05 · reviewed 2026-11-06 (max)"
        " · findings: #601, #602" in body
    )


def test_a_second_review_adds_to_the_line_and_a_new_pr_gets_a_ticked_one():
    body = ledger.tick(
        ledger.add(ledger.fresh_body(), 544, "x", DAY), 544, "max", DAY, []
    )
    body = ledger.tick(body, 544, "ultra", "2026-11-07", [])
    body = ledger.tick(body, 371, "ultra", DAY, [])

    line = next(x for x in body.splitlines() if x.startswith("- [x] #544"))
    assert line.endswith(
        "(max) · findings: none left open · reviewed 2026-11-07 (ultra)"
        " · findings: none left open"
    )
    assert "- [x] #371 reviewed without a candidate line" in body
    assert ledger.open_items(body) == []


def test_an_unknown_level_is_refused():
    with pytest.raises(ValueError):
        ledger.tick(ledger.fresh_body(), 544, "medium", DAY, [])


def test_a_hand_written_line_of_another_shape_is_kept(monkeypatch):
    body = ledger.fresh_body() + "Note by Maxi: #371 was reviewed in October.\n"

    assert "Note by Maxi" in ledger.add(body, 544, "x", DAY)


def test_add_creates_the_issue_on_the_first_run_and_edits_it_after(monkeypatch):
    calls = []
    issues: list[dict] = []

    def fake_run(cmd, input=None, **_):
        calls.append(cmd)
        if cmd[1:3] == ["issue", "list"]:
            out = json.dumps(issues)
        elif cmd[1:3] == ["issue", "create"]:
            issues.append({"number": 900, "title": ledger.TITLE, "body": input})
            out = "https://github.com/o/r/issues/900\n"
        else:
            issues[0]["body"] = next(a for a in cmd if a.startswith("body="))[5:]
            out = "{}"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(ledger.subprocess, "run", fake_run)

    assert ledger.main(["add", "544", "a booking"]) == 0
    assert ledger.main(["add", "544", "a booking"]) == 0
    assert ledger.main(["done", "544", "--level", "ultra"]) == 0

    assert [c[1:3] for c in calls if c[1:3] != ["issue", "list"]] == [
        ["issue", "create"],
        ["api", "-X"],
    ]
    assert ledger.open_items(issues[0]["body"]) == []


def test_a_failing_gh_is_reported_not_raised(monkeypatch, capsys):
    def fail(cmd, **_):
        raise subprocess.CalledProcessError(1, cmd, "", "HTTP 401")

    monkeypatch.setattr(ledger.subprocess, "run", fail)

    assert ledger.main(["list"]) == 1
    assert "not readable" in capsys.readouterr().err


def test_gh_talks_utf8_whatever_the_locale(monkeypatch):
    """The ledger body goes to gh and back as UTF-8; on Windows text mode would
    use cp1252 and fail on an emoji (UTF-8 byte 0x9d)."""
    seen = {}

    def run(args, **kwargs):
        seen.update(kwargs)
        encoding = kwargs.get("encoding") or "cp1252"
        kwargs["input"].encode(encoding)
        out = "\U0001f4dd body\n".encode().decode(encoding)
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(ledger.subprocess, "run", run)
    assert (
        ledger._gh("issue", "edit", stdin="- [ ] #566 \U0001f4dd")
        == "\U0001f4dd body\n"
    )
    assert seen["encoding"] == "utf-8"


def test_a_closed_ledger_stays_closed_and_the_next_add_opens_a_new_one(monkeypatch):
    issues = [
        {
            "number": 546,
            "state": "CLOSED",
            "title": ledger.TITLE,
            "body": ledger.fresh_body(),
        }
    ]
    patched: list[list[str]] = []

    def fake_run(cmd, input=None, **_):
        if cmd[1:3] == ["issue", "list"]:
            state = cmd[cmd.index("--state") + 1]
            out = json.dumps(
                [i for i in issues if state == "all" or i["state"] == state.upper()]
            )
        elif cmd[1:3] == ["issue", "create"]:
            issues.append({"number": 901, "state": "OPEN", "title": "", "body": input})
            out = "https://github.com/o/r/issues/901\n"
        else:
            patched.append(cmd)
            issues[-1]["body"] = next(a for a in cmd if a.startswith("body="))[5:]
            out = "{}"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(ledger.subprocess, "run", fake_run)

    assert ledger.main(["add", "770", "a booking"]) == 0
    assert ledger.main(["add", "771", "an allergen"]) == 0

    assert [i["number"] for i in issues if i["state"] == "OPEN"] == [901]
    assert issues[0]["body"] == ledger.fresh_body()
    assert all("state=open" not in a for c in patched for a in c)
    assert len(ledger.open_items(issues[1]["body"])) == 2
