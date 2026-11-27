"""Image pins: Postgres one exact pin everywhere, n8n a release checked against the workflow.

A tag like `16-alpine` moves under the stack with every pull; an exact minor
changes only through a pull request (Dependabot's docker-compose updates, which
keep the major, .github/dependabot.yml). Dependabot bumps the compose files
only, so the CI services, the backup client and the docs must follow by hand:
this test fails on its pull request until they do.
"""

import re

from api.tests.test_commands import REPO_ROOT

PIN = re.compile(r"postgres:16\.\d+-alpine")
ANY = re.compile(r"postgres:[0-9][\w.-]*")
NAMED_IN = [
    "docker-compose.yml",
    "deploy/docker-compose.operator.yml",
    "deploy/asterisk/docker-compose.drill.yml",
    ".github/workflows/ci.yml",
    "scripts/lib_pg.sh",
    "docs/13_DEPLOYMENT.md",
]


def _named() -> dict[str, set[str]]:
    return {path: set(ANY.findall((REPO_ROOT / path).read_text())) for path in NAMED_IN}


def test_every_file_names_the_same_exact_postgres_16_pin():
    named = _named()
    assert all(named.values()), {path: tags for path, tags in named.items() if not tags}
    tags = set().union(*named.values())
    assert len(tags) == 1, named
    (tag,) = tags
    assert PIN.fullmatch(tag), tag


def test_no_other_compose_file_names_postgres_unpinned():
    # A compose file added later that brings its own database is held too.
    for path in REPO_ROOT.glob("**/docker-compose*.yml"):
        if ".git" in path.parts or "node_modules" in path.parts:
            continue
        for tag in ANY.findall(path.read_text()):
            assert PIN.fullmatch(tag), (path, tag)


# n8n: an exact release that was run against the workflow export (#1060).
# Dependabot #1027 proposed 2.49.3, a pre-release with a plain tag: no pattern
# tells it from a stable release. So every pin must also be named in a
# "Checked ... against n8n X.Y.Z" line of n8n/README.md, written only after the
# release was confirmed stable on GitHub and the export imported and published
# against it; a bump goes red until that is done.
N8N_ANY = re.compile(r"n8nio/n8n:(\S+)")
N8N_PIN = re.compile(r"\d+\.\d+\.\d+")
N8N_CHECKED = re.compile(r"[Cc]hecked [^\n]*?against n8n (\d+\.\d+\.\d+)")


def test_n8n_is_one_exact_release_checked_against_the_workflow():
    tags = set(N8N_ANY.findall((REPO_ROOT / "docker-compose.yml").read_text()))
    assert len(tags) == 1, tags
    (tag,) = tags
    assert N8N_PIN.fullmatch(tag), tag
    checked = set(N8N_CHECKED.findall((REPO_ROOT / "n8n" / "README.md").read_text()))
    assert tag in checked, (tag, checked)


def test_the_n8n_pin_pattern_refuses_moving_and_pre_release_tags():
    for tag in (
        "latest",
        "stable",
        "next",
        "beta",
        "2",
        "2.49",
        "2.49.7-rc.1",
        "2.49.7-debian",
    ):
        assert not N8N_PIN.fullmatch(tag), tag
    assert N8N_PIN.fullmatch("2.49.7")
