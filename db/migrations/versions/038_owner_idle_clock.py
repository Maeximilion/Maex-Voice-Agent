"""The owner's idle clock (#600, Maxi 13.11.2026, docs/23 §6).

`gui_sessions.admin_active_at` is when the owner last used the admin PIN, an
owner page or an owner write on that device. Owner rights end 15 minutes after
it, however busy the staff keep the tablet (the idle lock follows staff
writes, so it never came there).

New column only; the upgrade changes no row, so owner rights open at that
moment (NULL clock) end at once and the owner enters the admin PIN again. The
downgrade drops the column; owner rights then run to `admin_until` as before.
On a database a restaurant signs in to, take a backup first with `bash
scripts/backup.sh --label before_migration_038` (CLAUDE.md §8, docs/13 §4).

Revision: 038
Revises: 037
Created: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "038"
down_revision: str | None = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gui_sessions",
        sa.Column("admin_active_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("gui_sessions", "admin_active_at")
