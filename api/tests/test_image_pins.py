"""The Postgres image is one exact pin everywhere it is named (#888 D3, docs/23 T6).

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
