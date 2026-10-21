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
BASE, HEAD, MERGE = "b" * 40, "h" * 40, "m" * 40
LOCK, VOICE = "api/requirements.txt", "api/requirements-voice.txt"
COMPARE = f"{R}/dependency-graph/compare/{BASE}...{HEAD}"
BLOCKS = "BLOCKS TASKS, no open PR into main fixes it"


def _gh(answers):
    """gh stand-in: a path it was not given fails the test."""

    def gh(path):
        value = answers[path]
        if isinstance(value, Exception):
            raise value
        return value

    return gh


def _dependabot(number=2, severity="critical", name="certifi", path=LOCK):
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
    return {
        "number": number,
        "base": {"sha": BASE},
        "head": {"sha": HEAD},
        "merge_commit_sha": MERGE,
    }


def _change(kind="removed", name="certifi", ghsa=GHSA, manifest=LOCK):
    vulns = [{"advisory_ghsa_id": ghsa}] if ghsa else []
    return {
        "change_type": kind,
        "name": name,
        "manifest": manifest,
        "vulnerabilities": vulns,
    }


def _answers(dependabot=(), code_scanning=(), prs=(), **more):
    answers = {
        f"{R}/dependabot/alerts?state=open&per_page=100": list(dependabot),
        f"{R}/code-scanning/alerts?state=open&per_page=100": list(code_scanning),
        f"{R}/pulls?state=open&base=main&per_page=100": list(prs),
    }
    answers.update(more)
    return answers


def _files(n):
    return f"{R}/pulls/{n}/files?per_page=100"


def _instances(n, ref, number=12):
    return f"{R}/code-scanning/alerts/{number}/instances?ref=refs/pull/{n}/{ref}&per_page=100"


def _line(answers):
    lines, errors = alert_fixes.report(_gh(answers))
    assert errors == []
    assert len(lines) == 1
    return lines[0]


def test_a_pr_that_removes_the_advisory_fixes_the_alert(capsys):
    answers = _answers(
        [_dependabot()],
        prs=[_pr(350)],
        **{COMPARE: [_change(), _change("added", ghsa=None)]},
    )
    assert alert_fixes.main(_gh(answers)) == 0
    assert capsys.readouterr().out == (
        "dependabot #2 critical certifi api/requirements.txt: fixed by #350\n"
    )


def test_an_upgrade_that_keeps_the_advisory_is_no_fix():
    answers = _answers(
        [_dependabot()],
        prs=[_pr(9)],
        **{COMPARE: [_change(), _change("added")], _files(9): []},
    )
    # Still removed from the manifest, so a candidate, never the fix.
    assert _line(answers).endswith(
        f"{BLOCKS}; candidates, not confirmed as the fix: #9"
    )


def test_a_removal_from_another_manifest_is_a_candidate_not_the_fix():
    # requirements-voice.txt takes certifi from requirements.txt (-c): #350
    # changes only the lock file, so the voice alert names it but stays open.
    answers = _answers(
        [_dependabot(1, path=VOICE)],
        prs=[_pr(350)],
        **{COMPARE: [_change(manifest=LOCK)], _files(350): [{"filename": LOCK}]},
    )
    assert _line(answers) == (
        f"dependabot #1 critical certifi {VOICE}: {BLOCKS}; "
        "candidates, not confirmed as the fix: #350"
    )


def test_another_package_or_advisory_only_counts_by_the_file():
    answers = _answers(
        [_dependabot()],
        prs=[_pr(7), _pr(8)],
        **{
            COMPARE: [_change(name="idna"), _change(ghsa="GHSA-other")],
            _files(7): [{"filename": LOCK}],
            _files(8): [{"filename": "README.md"}],
        },
    )
    assert _line(answers).endswith(
        f"{BLOCKS}; candidates, not confirmed as the fix: #7"
    )


def test_package_names_compare_by_pep_503():
    answers = _answers(
        [_dependabot(name="Typing_Extensions")],
        prs=[_pr(9)],
        **{COMPARE: [_change(name="typing-extensions")]},
    )
    assert _line(answers).endswith("fixed by #9")


def test_code_scanning_counts_a_fixed_instance_on_the_current_head_or_merge():
    old = "o" * 40
    cases = (
        ([{"state": "fixed", "commit_sha": HEAD}], [], "fixed by #5"),
        ([], [{"state": "fixed", "commit_sha": MERGE}], "fixed by #5"),
        (
            [{"state": "fixed", "commit_sha": old}],
            [{"state": "open", "commit_sha": MERGE}],
            BLOCKS,
        ),
        ([], [{"state": "fixed", "commit_sha": old}], BLOCKS),
    )
    for on_head, on_merge, expected in cases:
        answers = _answers(
            code_scanning=[_code_scanning()],
            prs=[_pr(5)],
            **{
                _instances(5, "head"): on_head,
                _instances(5, "merge"): on_merge,
                _files(5): [],
            },
        )
        assert expected in _line(answers), (on_head, on_merge)


def test_files_match_exactly_with_spaces_renames_and_over_100_files():
    path = "src/generated files/query.py"
    many = [{"filename": f"f{i}.py"} for i in range(150)] + [{"filename": path}]
    answers = _answers(
        code_scanning=[_code_scanning(path=path)],
        prs=[_pr(4), _pr(6), _pr(8)],
        **{
            **{_instances(n, ref): [] for n in (4, 6, 8) for ref in ("head", "merge")},
            _files(4): many,
            _files(6): [{"filename": "src/generated"}, {"filename": "files/query.py"}],
            _files(8): [{"filename": "src/query.py", "previous_filename": path}],
        },
    )
    assert _line(answers).endswith("candidates, not confirmed as the fix: #4, #8")


def test_lower_alerts_ask_no_pull_request_and_come_last():
    answers = _answers(
        [_dependabot(3, severity="medium"), _dependabot(2)],
        [_code_scanning(13, level=None)],
        prs=[],
    )
    lines, errors = alert_fixes.report(_gh(answers))
    assert lines == [
        f"dependabot #2 critical certifi api/requirements.txt: {BLOCKS}",
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
        [_dependabot(severity="high")], prs=[_pr(350)], **{COMPARE: [_change()]}
    )
    answers[f"{R}/code-scanning/alerts?state=open&per_page=100"] = alert_fixes.GhError(
        "HTTP 403: Advanced Security must be enabled"
    )
    assert alert_fixes.main(_gh(answers)) == 1
    out = capsys.readouterr()
    assert out.out == "dependabot #2 high certifi api/requirements.txt: fixed by #350\n"
    assert out.err == (
        "error: code-scanning alerts: HTTP 403: Advanced Security must be enabled\n"
    )


def test_a_pr_lookup_that_fails_does_not_count_as_a_fix(capsys):
    answers = _answers(
        [_dependabot()],
        prs=[_pr(11)],
        **{COMPARE: alert_fixes.GhError("HTTP 404"), _files(11): []},
    )
    assert alert_fixes.main(_gh(answers)) == 1
    out = capsys.readouterr()
    assert BLOCKS in out.out
    assert out.err == "error: pull request #11: HTTP 404\n"


def test_each_pull_request_is_fetched_once():
    calls = []
    answers = _answers(
        [_dependabot(1, path=VOICE), _dependabot(2)],
        prs=[_pr(350)],
        **{COMPARE: [_change()], _files(350): [{"filename": LOCK}]},
    )

    def gh(path):
        calls.append(path)
        return answers[path]

    lines, _ = alert_fixes.report(gh)
    assert lines[0].endswith("candidates, not confirmed as the fix: #350")
    assert lines[1].endswith("fixed by #350")
    assert len(calls) == len(set(calls))
