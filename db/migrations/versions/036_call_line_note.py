"""A call carries a note about its line (#651, Maxi 11.11.2026).

`calls.line_note` says what was wrong with the line of a call, for the call
log and the monitor: `no_caller_audio` for a call we answered on which no
audio of the caller arrived (no frame, or only digital zero) by the time a
silence passed. Such a call ends as a silent one, without the team; the
note is what tells a muted telephone from one-way audio on our side once
several calls in a row carry it. Empty on every other call and on every
existing row. A closed list: a new note is a migration that widens the
check.

The downgrade drops the column and the notes with it; no call is lost. On a
database a restaurant uses, take a backup first with `bash scripts/backup.sh
--label before_migration_036` (CLAUDE.md §8, docs/13 §4).

Revision: 036
Revises: 035
Created: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "036"
down_revision: str | None = "035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("line_note", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_calls_line_note", "calls", "line_note IN ('no_caller_audio')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_calls_line_note", "calls", type_="check")
    op.drop_column("calls", "line_note")
