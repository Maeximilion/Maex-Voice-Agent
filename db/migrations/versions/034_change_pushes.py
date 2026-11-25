"""The outbox takes the pushes of staff and call-mode changes (#588, docs/23 T12).

A team number for today, a date's hours set by the staff, and every change of
the call mode go to the owner and to the operator's `ops` topic through the
outbox and n8n (`api/events/types.py`). The check on `outbox.event_type` names
the three new types; the upgrade changes no row.

The downgrade deletes the outbox rows of these types, sent or not, since the
older check refuses them: a push still waiting is not sent. On a database a
restaurant uses, take a backup first with `bash scripts/backup.sh --label
before_migration_034` (CLAUDE.md §8, docs/13 §4).

Revision: 034
Revises: 033
Created: 2026-11-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "034"
down_revision: str | None = "033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = (
    "order.confirmed",
    "reservation.confirmed",
    "callback.created",
    "order.handover_failed",
    "daily.report",
)
NEW = ("team_phone.changed", "day_hours.changed", "call_mode.changed")


def _check(types: tuple[str, ...]) -> None:
    op.drop_constraint("ck_outbox_event_type", "outbox", type_="check")
    listed = ", ".join(f"'{t}'" for t in types)
    op.create_check_constraint(
        "ck_outbox_event_type", "outbox", f"event_type IN ({listed})"
    )


def upgrade() -> None:
    _check(OLD + NEW)


def downgrade() -> None:
    listed = ", ".join(f"'{t}'" for t in NEW)
    op.execute(f"DELETE FROM outbox WHERE event_type IN ({listed})")
    _check(OLD)
