""".github/scripts/ai_pr_review.py: the review is pinned to the head it read,
discarded when the pull request moved on, and keeps every finding on the fallback.

The script runs on a self-hosted runner outside the API package; it is loaded by
path, and the GitHub API is replaced by a recorder, so nothing leaves the test.
The api container does not mount `.github/`, so under `make test` these tests are
skipped; on a full checkout they run, and in CI a missing `.github/` is a failure.
"""

import importlib.util
import io
import json
import os
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "ai_pr_review.py"
HEAD = "a" * 40

if not SCRIPT.parent.is_dir():
    if os.environ.get("CI"):
        pytest.fail(".github/ is missing from the checkout", pytrace=False)
    pytest.skip(".github/ is not mounted in the api container", allow_module_level=True)
OTHER = "b" * 40


@pytest.fixture
def review(monkeypatch):
    spec = importlib.util.spec_from_file_location("ai_pr_review", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.example.com")
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setenv("PR_NUMBER", "7")
    monkeypatch.setenv("HEAD_SHA", HEAD)
    return module


class FakeGitHub:
    """Answers GET pulls/7 with a head sha; POSTs are recorded, some fail."""

    def __init__(self, head: str = HEAD, failing: dict[str, int] | None = None):
        self.head = head
        self.failing = failing or {}
        self.posts: list[tuple[str, dict]] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        if req.get_method() == "GET":
            return io.BytesIO(json.dumps({"head": {"sha": self.head}}).encode())
        for part, code in self.failing.items():
            if url.endswith(part):
                raise urllib.error.HTTPError(url, code, "fail", {}, io.BytesIO(b"{}"))
        self.posts.append((url, json.loads(req.data)))
        return io.BytesIO(b"{}")


INLINE = [{"path": "api/x.py", "line": 12, "body": "**[P1]** off by one in the loop"}]


def test_review_is_pinned_to_the_head_it_read(review, monkeypatch):
    github = FakeGitHub()
    monkeypatch.setattr(review.urllib.request, "urlopen", github)

    assert review.post_review("summary", INLINE) is True

    ((url, body),) = github.posts
    assert url.endswith("/pulls/7/reviews")
    assert body["commit_id"] == HEAD
    assert body["comments"] == INLINE


def test_review_of_an_old_head_is_discarded(review, monkeypatch):
    """A push during the long model request: the result belongs to the old
    head and must not appear as the review of the new one."""
    github = FakeGitHub(head=OTHER)
    monkeypatch.setattr(review.urllib.request, "urlopen", github)

    assert review.post_review("summary", INLINE) is False

    assert github.posts == []


def test_fallback_comment_keeps_every_inline_finding(review, monkeypatch):
    """GitHub rejects the inline review (422, a bad line anchor): the comment
    that replaces it names each finding with file and line."""
    github = FakeGitHub(failing={"/pulls/7/reviews": 422})
    monkeypatch.setattr(review.urllib.request, "urlopen", github)

    assert review.post_review("summary", INLINE) is True

    ((url, body),) = github.posts
    assert url.endswith("/issues/7/comments")
    assert "summary" in body["body"]
    assert "`api/x.py:12`" in body["body"]
    assert "off by one in the loop" in body["body"]
    assert HEAD[:12] in body["body"]


def test_failing_fallback_fails_the_job(review, monkeypatch):
    github = FakeGitHub(failing={"/pulls/7/reviews": 422, "/issues/7/comments": 403})
    monkeypatch.setattr(review.urllib.request, "urlopen", github)

    with pytest.raises(RuntimeError):
        review.post_review("summary", INLINE)


def test_missing_head_sha_fails_instead_of_reviewing_the_latest_head(
    review, monkeypatch
):
    monkeypatch.delenv("HEAD_SHA")
    github = FakeGitHub()
    monkeypatch.setattr(review.urllib.request, "urlopen", github)

    with pytest.raises(RuntimeError):
        review.post_review("summary", INLINE)
    assert github.posts == []


# -- the conditions under which the self-hosted runner is accepted (AGENTS.md) --

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _workflow(name: str) -> dict:
    import yaml

    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _uses_self_hosted(job: dict) -> bool:
    runs_on = job.get("runs-on", [])
    labels = [runs_on] if isinstance(runs_on, str) else list(runs_on)
    return "self-hosted" in labels


def test_only_the_review_job_runs_on_the_self_hosted_runner():
    users = [
        f"{path.name}:{name}"
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        for name, job in (_workflow(path.name).get("jobs") or {}).items()
        if _uses_self_hosted(job)
    ]
    assert users == ["ai-pr-review.yml:review"]


def test_review_job_never_runs_for_forks_and_never_checks_out_pr_code():
    workflow = _workflow("ai-pr-review.yml")
    # yaml reads the bare key `on` as True
    assert set(workflow[True]) == {"pull_request"}
    job = workflow["jobs"]["review"]
    assert (
        "github.event.pull_request.head.repo.full_name == github.repository"
        in job["if"]
    )

    checkouts = [
        s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
    ]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert checkouts[0]["with"]["persist-credentials"] is False
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "write"}
