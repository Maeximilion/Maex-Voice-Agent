"""A tenant gets the key of its customer (#573, D32).

Provisioning creates a customer's tenant once and is run again safely: the
key it was created with finds it (`domain/provision.py`). The same key names
the customer's stack (folder, Compose project, Caddy site), so it is short,
lower case and safe in a host or file name. Unique; empty on every existing
row, which provisioning never touches.

The downgrade drops the column and with it the keys: a rerun of provisioning
after it refuses, because the stack already holds a tenant. On a database a
restaurant uses, take a backup first with `bash scripts/backup.sh --label
before_migration_035` (CLAUDE.md §8, docs/13 §4).

Revision: 035
Revises: 034
Created: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "035"
down_revision: str | None = "034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("customer_key", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_tenants_customer_key",
        "tenants",
        r"customer_key ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(customer_key) <= 32",
    )
    op.create_unique_constraint("uq_tenants_customer_key", "tenants", ["customer_key"])


def downgrade() -> None:
    op.drop_constraint("uq_tenants_customer_key", "tenants", type_="unique")
    op.drop_constraint("ck_tenants_customer_key", "tenants", type_="check")
    op.drop_column("tenants", "customer_key")
