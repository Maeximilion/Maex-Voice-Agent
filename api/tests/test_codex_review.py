"""Tests for scripts/codex_review.py, against a stand-in for `gh api`.

The summary bodies are trimmed copies of what Codex wrote on PR #371."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "codex_review", Path(__file__).resolve().parents[2] / "scripts" / "codex_review.py"
)
assert _SPEC is not None and _SPEC.loader is not None
codex_review = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(codex_review)

R = "repos/{owner}/{repo}"
BOT = "chatgpt-codex-connector[bot]"
HEAD, OLD = "f" * 40, "a" * 40
NOW = datetime(2026, 10, 26, 10, 5, tzinfo=UTC)


def _summary(sha, status, findings=None, since=NOW - timedelta(minutes=3)):
    sha_json = f'"{sha}"' if sha else "null"
    found = "null" if findings is None else str(findings).lower()
    row = (
        f'| 📝 **Code Review** | 🔄 **Running** since <relative-time datetime="'
        f'{since.isoformat()}">x</relative-time> | `{(sha or "")[:7]}` | New commits |'
        if status == "running"
        else "| 📝 **Code Review** | ✅ **Completed** | x | New commits |"
    )
    return (
        "<!-- codex-pull-request-review-summary -->\n"
        '<!-- codex-review-state:v1 {"code_review":{"commit_sha":'
        f'{sha_json},"has_any_findings":{found},"has_unresolved_findings":{found},'
        f'"status":"{status}"}},"security_review":{{"commit_sha":null,'
        '"has_any_findings":null,"has_unresolved_findings":null,"status":null}} -->\n'
        f"## Codex Review Summary\n{row}\n"
    )


def _review(commit, review_id=7, login=BOT):
    return {"id": review_id, "user": {"login": login}, "commit_id": commit}


def test_running_on_the_head_is_not_done():
    assert codex_review.state(HEAD, _summary(HEAD, "running"), [], NOW) == "running"


def test_clean_review_of_the_head():
    body = _summary(HEAD, "completed", findings=False)
    assert codex_review.state(HEAD, body, [], NOW) == "clean"


def test_findings_with_a_null_sha_are_found_through_the_review():
    """PR #371, 09:03: the completed state named no commit; the review did."""
    body = _summary(None, "completed", findings=True)
    assert codex_review.state(HEAD, body, [_review(HEAD)], NOW) == "findings"


def test_a_completed_state_of_an_older_head_is_not_this_one():
    """Right after a push the summary still shows the previous head's result."""
    body = _summary(OLD, "completed", findings=False)
    assert codex_review.state(HEAD, body, [_review(OLD)], NOW) == "not started"
    nulled = _summary(None, "completed", findings=True)
    assert codex_review.state(HEAD, nulled, [_review(OLD)], NOW) == "not started"


def test_a_review_running_for_more_than_15_minutes_is_stalled():
    """PR #371, 62ed2a5: running from 09:27 to 10:01, never done."""
    body = _summary(HEAD, "running", since=NOW - timedelta(minutes=16))
    assert codex_review.state(HEAD, body, [], NOW) == "stalled"


def test_a_review_by_someone_else_on_the_head_is_no_codex_result():
    body = _summary(HEAD, "running")
    reviews = [_review(HEAD, login="Maeximilion")]
    assert codex_review.state(HEAD, body, reviews, NOW) == "running"


def _gh(summary, reviews, comments=()):
    answers = {
        f"{R}/pulls/371": [{"head": {"sha": HEAD}}],
        f"{R}/issues/371/comments?per_page=100": [
            {
                "user": {"login": "Maeximilion"},
                "body": "@codex review",
                "updated_at": "z",
            },
            {
                "user": {"login": BOT},
                "body": summary,
                "updated_at": "2026-10-26T10:05:13Z",
            },
        ],
        f"{R}/pulls/371/reviews?per_page=100": reviews,
        f"{R}/pulls/371/comments?per_page=100": list(comments),
    }
    return lambda path: answers[path]


def test_main_lists_the_findings_of_the_head_review(capsys):
    comments = [
        {
            "id": 4376012463,
            "pull_request_review_id": 7,
            "path": "api/jobs/retention.py",
            "line": 97,
            "body": "**<sub>![P2 Badge](x)</sub>  Invalidate the callback stream token**\n\nWhy",
        },
        {
            "id": 1,
            "pull_request_review_id": 3,
            "path": "old.py",
            "line": 1,
            "body": "**Old**",
        },
    ]
    gh = _gh(_summary(HEAD, "completed", findings=True), [_review(HEAD)], comments)

    assert codex_review.main(["371"], gh=gh) == 1

    out = capsys.readouterr().out
    assert f"codex {HEAD[:7]}: findings" in out
    assert (
        "4376012463 api/jobs/retention.py:97 P2 Invalidate the callback stream token"
        in out
    )
    assert "old.py" not in out


def test_main_says_clean_and_exits_0(capsys):
    gh = _gh(_summary(HEAD, "completed", findings=False), [])
    assert codex_review.main(["371"], gh=gh) == 0
    assert capsys.readouterr().out.strip() == f"codex {HEAD[:7]}: clean"


def test_main_names_the_single_retrigger_only_when_stalled(capsys):
    stalled = _summary(HEAD, "running", since=datetime.now(UTC) - timedelta(minutes=20))
    assert codex_review.main(["371"], gh=_gh(stalled, [])) == 3
    assert "one comment `@codex review`" in capsys.readouterr().out

    running = _summary(HEAD, "running", since=datetime.now(UTC))
    assert codex_review.main(["371"], gh=_gh(running, [])) == 2
    assert "@codex" not in capsys.readouterr().out


def test_main_waits_until_the_review_is_done(capsys):
    """--wait polls; the first look sees the run, the second its result."""
    looks = iter(
        [
            _summary(HEAD, "running", since=datetime.now(UTC)),
            _summary(HEAD, "completed", findings=False),
        ]
    )
    current = {"body": next(looks)}

    def gh(path):
        if path.endswith("/reviews?per_page=100"):
            current["body"] = next(looks, current["body"])
            return []
        return _gh(current["body"], [])(path)

    assert codex_review.main(["371", "--wait", "--interval", "0"], gh=gh) == 0
    assert "clean" in capsys.readouterr().out


def test_an_api_error_exits_4(capsys):
    def gh(path):
        raise codex_review.GhError("HTTP 502")

    assert codex_review.main(["371"], gh=gh) == 4
    assert "HTTP 502" in capsys.readouterr().err
