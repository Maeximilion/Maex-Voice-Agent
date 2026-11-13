"""Every migration that deletes data names the backup it relies on (AGENTS.md
"Migrations", CLAUDE.md §8).

The review findings on #280, #500 and #586 were the same: a migration dropped a
column or table without naming `scripts/backup.sh` and its label. From 006 on,
when the first restaurant data could exist, a migration whose upgrade or
downgrade drops a column or table, or deletes rows, says in its docstring
`bash scripts/backup.sh --label before_migration_<revision>`. 001 to 005 came
before the rule and before any data.

It covers deletions only (review of #586, #598): a data change by `UPDATE ...
SET` or a `NOT NULL` `alter_column` passes unflagged, so such a migration names
its backup by the same rule without this guard noticing.
"""

import ast
import re
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[2] / "db" / "migrations" / "versions"
DELETES = re.compile(r"op\.drop_(column|table)\(|\bDELETE\s+FROM\b", re.IGNORECASE)
FIRST_WITH_DATA = 6


def _migrations():
    for path in sorted(VERSIONS.glob("[0-9][0-9][0-9]_*.py")):
        revision = int(path.name[:3])
        if revision >= FIRST_WITH_DATA:
            yield pytest.param(path, revision, id=path.name)


@pytest.mark.parametrize(("path", "revision"), _migrations())
def test_a_migration_that_deletes_data_names_its_backup(path, revision):
    source = path.read_text(encoding="utf-8")
    if not DELETES.search(source):
        return
    docstring = ast.get_docstring(ast.parse(source)) or ""
    label = f"backup.sh --label before_migration_{revision:03d}"
    assert label in " ".join(docstring.split()), (
        f"{path.name} deletes data: name `bash scripts/{label}` in its docstring"
    )
