"""The outbox takes the push of an operator's step on the access (#811).

When the operator makes a setup link, sets the team or admin PIN, or signs
devices out from the dashboard, the owner and the operator's `ops` topic hear
of it through the outbox and n8n, with the operator's account named
(`api/events/types.py`, docs/23 T11). The check on `outbox.event_type` names
the new type; the upgrade changes no row.

The downgrade deletes the outbox rows of this type, sent or not, since the
older check refuses them: a push still waiting is not sent. On a database a
restaurant uses, take a backup first with `bash scripts/backup.sh --label
before_migration_041` (CLAUDE.md §8, docs/13 §4).

Revision: 041
Revises: 040
Created: 2026-11-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "041"
down_revision: str | None = "040"
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
    "admin_pin.wrong",
)
NEW = "operator_access.changed"


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
