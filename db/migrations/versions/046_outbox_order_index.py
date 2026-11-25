"""An index on the outbox by tenant and order id (#980).

`send_ticket` and `last_reason` (`api/domain/ordering/ticket.py`) and the
newest revision of a ticket (`api/domain/ordering/handover.py`, on every
claim and ack of the print bridge) find the kitchen tickets of one order by
`payload->>'order_id'`. Without an index that walks every outbox row of the
tenant, up to the 90 days `RETENTION_OUTBOX_ROW_DAYS` keeps them. Partial: only
the events of an order carry the key, the rest never meets this lookup. The
upgrade and the downgrade change no row. The plain CREATE INDEX holds writes
to the outbox while it builds, a fraction of a second at pilot volume; on a
database a restaurant signs in to, take a backup first with
`bash scripts/backup.sh --label before_migration_046` (CLAUDE.md §8, docs/13
§4).

Revision: 046
Revises: 045
Created: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "046"
down_revision: str | None = "045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_outbox_tenant_order"
ORDER_ID = sa.text("(payload ->> 'order_id')")


def upgrade() -> None:
    op.create_index(
        INDEX,
        "outbox",
        [sa.text("tenant_id"), ORDER_ID],
        postgresql_where=sa.text("(payload ->> 'order_id') IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="outbox")
