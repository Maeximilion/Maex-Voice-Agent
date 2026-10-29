"""The approval step of overflow can be switched off (T-8.2).

In `overflow` every order of the agent waits for "Passt" on the tablet before a
slip prints (docs/02 §4, T-4.7). That step is the safety net of stage 5, and
gate G5 decides when it can come off (docs/00: two weeks and 100 AI orders on
target). The switch is the restaurant's rule, so it is a field of
`service_config` and no constant in the code (CLAUDE.md §2 rule 1):
`overflow_needs_approval`, true until the restaurant sets it false.

Read by `confirm` (`domain/ordering/confirm.py`). Only the mode's reason to wait
goes: an order above `order_approval_above_cents` still waits, and `shadow` and
`paused` still hold every order.

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_014` (CLAUDE.md §8, docs/13
§4). The upgrade writes true into every existing `service_config` row, so
nothing changes until someone sets it; the downgrade drops the column, and
with it a false the restaurant set there, which brings the approval step back.

Revision: 014
Revises: 013
Created: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "overflow_needs_approval"


def upgrade() -> None:
    op.add_column(
        "service_config",
        sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default="true"),
    )


def downgrade() -> None:
    op.drop_column("service_config", COLUMN)
