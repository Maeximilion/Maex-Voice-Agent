"""Tests for scripts/alert_fixes.py, against a stand-in for `gh api`."""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "alert_fixes", Path(__file__).resolve().parents[2] / "scripts" / "alert_fixes.py"
)
assert _SPEC is not None and _SPEC.loader is not None
alert_fixes = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(alert_fixes)

R = "repos/{owner}/{repo}"
GHSA = "GHSA-6q48-6c7w-9gw2"
BASE, HEAD = "b" * 40, "h" * 40


def _gh(answers):
    """gh stand-in: a path it was not given fails the test."""

    def gh(path):
        value = answers[path]
        if isinstance(value, Exception):
            raise value
        return value

    return gh


def _dependabot(
    number=2, severity="critical", name="certifi", path="api/requirements.txt"
):
    return {
        "number": number,
        "security_advisory": {"severity": severity, "ghsa_id": GHSA},
        "dependency": {"package": {"name": name}, "manifest_path": path},
    }


def _code_scanning(number=12, level="high", path="api/domain/menu/wishes.py"):
    rule = {"id": "py/polynomial-redos", "severity": "error"}
    if level:
        rule["security_severity_level"] = level
    return {
        "number": number,
        "rule": rule,
        "most_recent_instance": {"location": {"path": path}},
    }


def _pr(number):
    return {"number": number, "base": {"sha": BASE}, "head": {"sha": HEAD}}


def _removed(name="certifi", ghsa=GHSA):
    return {
        "change_type": "removed",
        "name": name,
        "vulnerabilities": [{"advisory_ghsa_id": ghsa}],
    }


def _answers(dependabot=(), code_scanning=(), prs=(), **more):
    answers = {
        f"{R}/dependabot/alerts?state=open&per_page=100": list(dependabot),
        f"{R}/code-scanning/alerts?state=open&per_page=100": list(code_scanning),
        f"{R}/pulls?state=open&base=main&per_page=100": list(prs),
    }
    answers.update(more)
    return answers


COMPARE = f"{R}/dependency-graph/compare/{BASE}...{HEAD}"


def _files(n):
    return f"{R}/pulls/{n}/files?per_page=100"


def test_a_pr_that_removes_the_advisory_fixes_the_alert(capsys):
    answers = _answers([_dependabot()], prs=[_pr(350)], **{COMPARE: [_removed()]})
    assert alert_fixes.main(_gh(answers)) == 0
    assert capsys.readouterr().out == (
        "dependabot #2 critical certifi api/requirements.txt: fixed by #350\n"
    )


def test_the_voice_manifest_is_fixed_through_the_main_lock_file():
    # requirements-voice.txt takes certifi from requirements.txt (-c), so the
    # removal shows up in the other manifest only.
    answers = _answers(
        [_dependabot(1, path="api/requirements-voice.txt")],
        prs=[_pr(350)],
        **{COMPARE: [_removed() | {"manifest": "api/requirements.txt"}]},
    )
    lines, errors = alert_fixes.report(_gh(answers))
    assert lines == [
        "dependabot #1 critical certifi api/requirements-voice.txt: fixed by #350"
    ]
    assert errors == []


def test_a_pr_that_only_touches_the_lock_file_is_a_candidate_not_the_fix():
    # Another package moved, or the same package without this advisory.
    answers = _answers(
        [_dependabot()],
        prs=[_pr(7), _pr(8)],
        **{
            COMPARE: [_removed(name="idna"), _removed(ghsa="GHSA-other")],
            _files(7): [{"filename": "api/requirements.txt"}],
            _files(8): [{"filename": "README.md"}],
        },
    )
    lines, _ = alert_fixes.report(_gh(answers))
    assert lines == [
        "dependabot #2 critical certifi api/requirements.txt: BLOCKS TASKS, "
        "no open PR into main fixes it; candidates that change the file: #7"
    ]


def test_package_names_compare_by_pep_503():
    answers = _answers(
        [_dependabot(name="Typing_Extensions")],
        prs=[_pr(9)],
        **{COMPARE: [_removed(name="typing-extensions")]},
    )
    assert alert_fixes.report(_gh(answers))[0][0].endswith("fixed by #9")


def test_code_scanning_counts_only_a_fixed_instance_on_the_pr_head():
    instances = (
        f"{R}/code-scanning/alerts/12/instances?ref=refs/pull/5/head&per_page=100"
    )
    fixed_now = [{"state": "fixed", "commit_sha": HEAD}]
    fixed_on_an_old_push = [{"state": "fixed", "commit_sha": "o" * 40}]
    for found, expected in (
        (fixed_now, "fixed by #5"),
        (fixed_on_an_old_push, "BLOCKS TASKS"),
    ):
        answers = _answers(
            code_scanning=[_code_scanning()],
            prs=[_pr(5)],
            **{instances: found, _files(5): []},
        )
        line = alert_fixes.report(_gh(answers))[0][0]
        assert expected in line, line


def test_a_path_with_a_space_and_more_than_100_files_match_exactly():
    path = "src/generated files/query.py"
    many = [{"filename": f"f{i}.py"} for i in range(150)] + [{"filename": path}]
    answers = _answers(
        code_scanning=[_code_scanning(path=path)],
        prs=[_pr(4), _pr(6)],
        **{
            f"{R}/code-scanning/alerts/12/instances?ref=refs/pull/4/head&per_page=100": [],
            f"{R}/code-scanning/alerts/12/instances?ref=refs/pull/6/head&per_page=100": [],
            _files(4): many,
            _files(6): [{"filename": "src/generated"}, {"filename": "files/query.py"}],
        },
    )
    line = alert_fixes.report(_gh(answers))[0][0]
    assert line.endswith("candidates that change the file: #4"), line


def test_lower_alerts_ask_no_pull_request_and_come_last():
    answers = _answers(
        [_dependabot(3, severity="medium"), _dependabot(2)],
        [_code_scanning(13, level=None)],
        prs=[],
    )
    lines, errors = alert_fixes.report(_gh(answers))
    assert lines == [
        "dependabot #2 critical certifi api/requirements.txt: BLOCKS TASKS, "
        "no open PR into main fixes it",
        "code-scanning #13 error py/polynomial-redos api/domain/menu/wishes.py: "
        "below high: a suggestion next to the tasks",
        "dependabot #3 medium certifi api/requirements.txt: "
        "below high: the pin rides in the next feature PR",
    ]
    assert errors == []
    # Without a blocking alert no pull request is looked up at all.
    quiet = _answers([_dependabot(3, severity="low")])
    del quiet[f"{R}/pulls?state=open&base=main&per_page=100"]
    assert len(alert_fixes.report(_gh(quiet))[0]) == 1


def test_a_failed_source_is_an_error_never_zero(capsys):
    answers = _answers(
        [_dependabot(severity="high")], prs=[_pr(350)], **{COMPARE: [_removed()]}
    )
    answers[f"{R}/code-scanning/alerts?state=open&per_page=100"] = alert_fixes.GhError(
        "HTTP 403: Advanced Security must be enabled"
    )
    assert alert_fixes.main(_gh(answers)) == 1
    out = capsys.readouterr()
    assert out.out == "dependabot #2 high certifi api/requirements.txt: fixed by #350\n"
    assert (
        out.err
        == "error: code-scanning alerts: HTTP 403: Advanced Security must be enabled\n"
    )


def test_a_pr_lookup_that_fails_does_not_count_as_a_fix(capsys):
    answers = _answers(
        [_dependabot()],
        prs=[_pr(11)],
        **{COMPARE: alert_fixes.GhError("HTTP 404"), _files(11): []},
    )
    assert alert_fixes.main(_gh(answers)) == 1
    out = capsys.readouterr()
    assert "BLOCKS TASKS" in out.out
    assert out.err == "error: pull request #11: HTTP 404\n"
