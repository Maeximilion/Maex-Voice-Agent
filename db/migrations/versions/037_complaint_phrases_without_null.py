"""A complaint rule holds no NULL among its phrases (#692).

`ck_complaint_rules_phrases` asked only for at least one phrase. A row written
by hand with a NULL among them (`'{kalt,null}'`, or `string_to_array('kalt,
lauwarm,', ',', '')`) passed it, and the matcher raised on it for every
complaint: the guest heard the outage sentence instead of the rule or the team.
The check now refuses a NULL element as well.

The upgrade first drops the NULLs of an existing row; its other phrases stay.
A row that held nothing but NULLs matched nothing before either: the upgrade
then fails on the check and names it, and such a row is fixed or deleted by
hand, never by the migration (a remedy is money). The downgrade restores the
old check and changes no row. On a database a restaurant uses, take a backup
first with `bash scripts/backup.sh --label before_migration_037` (CLAUDE.md §8,
docs/13 §4).

Revision: 037
Revises: 036
Created: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "037"
down_revision: str | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "cardinality(phrases) > 0"
_NEW = "cardinality(phrases) > 0 AND array_position(phrases, NULL) IS NULL"


def upgrade() -> None:
    op.execute(
        "UPDATE complaint_rules SET phrases = array_remove(phrases, NULL)"
        " WHERE array_position(phrases, NULL) IS NOT NULL"
    )
    op.drop_constraint("ck_complaint_rules_phrases", "complaint_rules", type_="check")
    op.create_check_constraint("ck_complaint_rules_phrases", "complaint_rules", _NEW)


def downgrade() -> None:
    op.drop_constraint("ck_complaint_rules_phrases", "complaint_rules", type_="check")
    op.create_check_constraint("ck_complaint_rules_phrases", "complaint_rules", _OLD)
