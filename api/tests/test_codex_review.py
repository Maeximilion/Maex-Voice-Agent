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


def _gh(summary, reviews):
    answers = {
        f"{R}/pulls/371": [{"head": {"sha": HEAD}}],
        f"{R}/issues/371/comments?per_page=100": [
            {
                "user": {"login": "Maeximilion"},
                "body": "@codex review",
                "created_at": "2026-10-26T10:01:14Z",
                "updated_at": "2026-10-26T10:01:14Z",
            },
            {
                "user": {"login": BOT},
                "body": summary,
                "created_at": "2026-10-26T08:56:24Z",
                "updated_at": "2026-10-26T10:05:13Z",
            },
        ],
        f"{R}/pulls/371/reviews?per_page=100": reviews,
    }
    return lambda path: answers[path]


def _thread(comment_id, review_id, path="api/jobs/retention.py", line=97, title="Fix"):
    return {
        "databaseId": comment_id,
        "path": path,
        "line": line,
        "originalLine": line,
        "body": f"**<sub><sub>![P2 Badge](x)</sub></sub>  {title}**\n\nWhy",
        "author": {"login": "chatgpt-codex-connector"},
        "pullRequestReview": {"databaseId": review_id},
    }


def _no_threads(pr):
    raise AssertionError("threads read although Codex reported no finding")


def test_main_lists_new_and_older_open_threads(capsys):
    threads = [
        _thread(4376012463, 7, title="Invalidate the callback stream token"),
        _thread(4375519158, 3, line=164, title="Remove allergy notes from copies"),
    ]
    gh = _gh(_summary(HEAD, "completed", findings=True), [_review(HEAD)])

    assert codex_review.main(["371"], gh=gh, gq=lambda pr: threads) == 1

    out = capsys.readouterr().out
    assert f"codex {HEAD[:7]}: findings" in out
    assert (
        "4376012463 api/jobs/retention.py:97 P2 Invalidate the callback stream token\n"
        in out
    )
    assert (
        "4375519158 api/jobs/retention.py:164 P2 Remove allergy notes from copies (earlier head"
        in out
    )


def test_only_older_threads_open_reads_open_threads(capsys):
    """PR #371, 5748bd2: Codex found nothing new but reported findings, the
    answered threads of earlier heads that were never resolved."""
    gh = _gh(_summary(HEAD, "completed", findings=True), [])
    old = [_thread(4375519158, 3)]

    assert codex_review.main(["371"], gh=gh, gq=lambda pr: old) == 1
    assert f"codex {HEAD[:7]}: open threads" in capsys.readouterr().out

    assert codex_review.main(["371"], gh=gh, gq=lambda pr: []) == 0
    out = capsys.readouterr().out
    assert f"codex {HEAD[:7]}: clean, threads resolved" in out
    assert f"`Merge gate: {HEAD}`" in out


def test_main_says_clean_and_exits_0(capsys):
    gh = _gh(_summary(HEAD, "completed", findings=False), [])
    assert codex_review.main(["371"], gh=gh, gq=_no_threads) == 0
    assert capsys.readouterr().out.strip() == f"codex {HEAD[:7]}: clean"


def test_main_names_the_single_retrigger_only_when_stalled(capsys):
    stalled = _summary(HEAD, "running", since=datetime.now(UTC) - timedelta(minutes=20))
    assert codex_review.main(["371"], gh=_gh(stalled, []), gq=_no_threads) == 3
    assert "one comment `@codex review`" in capsys.readouterr().out

    running = _summary(HEAD, "running", since=datetime.now(UTC))
    assert codex_review.main(["371"], gh=_gh(running, []), gq=_no_threads) == 2
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

    assert (
        codex_review.main(["371", "--wait", "--interval", "0"], gh=gh, gq=_no_threads)
        == 0
    )
    assert "clean" in capsys.readouterr().out


def test_an_api_error_exits_4(capsys):
    def gh(path):
        raise codex_review.GhError("HTTP 502")

    assert codex_review.main(["371"], gh=gh) == 4
    assert "HTTP 502" in capsys.readouterr().err


def test_codex_answering_instead_of_reviewing_is_declined(capsys):
    """PR #373: the description named `@codex review`; Codex answered with
    the environment message and reviewed nothing, not even later pushes."""
    answers = {
        f"{R}/pulls/373": [{"head": {"sha": HEAD}}],
        f"{R}/issues/373/comments?per_page=100": [
            {
                "user": {"login": BOT},
                "body": "To use Codex here, [create an environment for this repo](x).",
                "created_at": "2026-10-26T10:16:33Z",
                "updated_at": "2026-10-26T10:16:33Z",
            }
        ],
    }

    assert (
        codex_review.main(["373"], gh=lambda path: answers[path], gq=_no_threads) == 3
    )

    out = capsys.readouterr().out
    assert f"codex {HEAD[:7]}: declined" in out
    assert "Codex answered: To use Codex here" in out
    assert "remove the mention" in out


def test_a_review_request_after_the_answer_is_waited_for(capsys):
    """PR #373: after the mention was removed, one `@codex review`; Codex
    reacted with eyes, so the wait must go on instead of reading declined."""
    answers = {
        f"{R}/pulls/373": [{"head": {"sha": HEAD}}],
        f"{R}/issues/373/comments?per_page=100": [
            {
                "user": {"login": BOT},
                "body": "To use Codex here, create an environment.",
                "created_at": "2026-10-26T10:16:33Z",
                "updated_at": "2026-10-26T10:16:33Z",
            },
            {
                "user": {"login": "Maeximilion"},
                "body": "@codex review",
                "created_at": "2026-10-26T12:10:18Z",
                "updated_at": "2026-10-26T12:10:18Z",
            },
        ],
    }

    assert (
        codex_review.main(["373"], gh=lambda path: answers[path], gq=_no_threads) == 2
    )
    assert f"codex {HEAD[:7]}: not started" in capsys.readouterr().out
