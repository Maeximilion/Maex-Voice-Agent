"""The outbox takes the push of wrong admin PINs (#827 F4, docs/23 §6).

Every third wrong admin PIN in a row, and a locked device the wrong PINs
signed out, go to the owner and to the operator's `ops` topic through the
outbox and n8n (`api/events/types.py`). The check on `outbox.event_type` names
the new type; the upgrade changes no row.

The downgrade deletes the outbox rows of this type, sent or not, since the
older check refuses them: a push still waiting is not sent. On a database a
restaurant uses, take a backup first with `bash scripts/backup.sh --label
before_migration_040` (CLAUDE.md §8, docs/13 §4).

Revision: 040
Revises: 039
Created: 2026-11-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "040"
down_revision: str | None = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = (
    "order.confirmed",
    "reservation.confirmed",
    "callback.created",
    "order.handover_failed",
    "daily.report",
    "team_phone.changed",
    "day_hours.changed",
    "call_mode.changed",
)
NEW = "admin_pin.wrong"


def _check(types: tuple[str, ...]) -> None:
    op.drop_constraint("ck_outbox_event_type", "outbox", type_="check")
    listed = ", ".join(f"'{t}'" for t in types)
    op.create_check_constraint(
        "ck_outbox_event_type", "outbox", f"event_type IN ({listed})"
    )


def upgrade() -> None:
    _check((*OLD, NEW))


def downgrade() -> None:
    op.execute(f"DELETE FROM outbox WHERE event_type = '{NEW}'")
    _check(OLD)
