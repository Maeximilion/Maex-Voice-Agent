"""Customers, addresses, delivery zones, the objection to recognition (T-6.1).

`customers` holds one row per guest, not per number: the phone is no key, a name
that does not match means a new record (legal check L12, docs/03). An objection
to recognition hangs on the number in `recognition_objections` and outlives the
customer. `orders.customer_id`, a plain column until now that nothing has
written, gets its foreign key, and `orders.address_id` comes with its own. Both
keep the tenant of the row, and a customer deleted after 24 months clears only
the links: the order stays as a tax row (L18).

Nothing writes these tables yet: T-6.2 and T-6.5 do, and the deletion job for
them is T-6.7.

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_018` (CLAUDE.md §8, docs/13
§4). The downgrade drops the four tables with every row in them and the links
on the orders; `orders.customer_id` stays as the plain column it was.

Revision: 018
Revises: 017
Created: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "018"
down_revision: str | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

E164 = r"phone ~ '^\+[1-9][0-9]{1,14}$'"


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _tenant_key() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT")


def upgrade() -> None:
    op.create_table(
        "delivery_zones",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("match_type", sa.Text(), nullable=False),
        sa.Column("postal_codes", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("polygon", postgresql.JSONB(), nullable=True),
        sa.Column("fee_cents", sa.Integer(), nullable=False),
        sa.Column("min_order_cents", sa.Integer(), nullable=False),
        sa.Column("eta_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        *_base_columns(),
        sa.CheckConstraint(
            "match_type IN ('postal_code', 'polygon')",
            name="ck_delivery_zones_match_type",
        ),
        sa.CheckConstraint(
            "(match_type = 'postal_code' AND coalesce(cardinality(postal_codes), 0) > 0)"
            " OR (match_type = 'polygon' AND polygon IS NOT NULL)",
            name="ck_delivery_zones_match_value",
        ),
        sa.CheckConstraint(
            "fee_cents >= 0 AND min_order_cents >= 0 AND eta_minutes >= 0",
            name="ck_delivery_zones_amounts",
        ),
        _tenant_key(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_delivery_zones_id_tenant"),
    )
    op.create_index(
        "ix_delivery_zones_tenant_id", "delivery_zones", ["tenant_id"], unique=False
    )

    op.create_table(
        "customers",
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("last_order_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_contact_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("order_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "blocked", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        *_base_columns(),
        sa.CheckConstraint(E164, name="ck_customers_phone"),
        sa.CheckConstraint("order_count >= 0", name="ck_customers_order_count"),
        _tenant_key(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_customers_id_tenant"),
    )
    op.create_index("ix_customers_tenant_id", "customers", ["tenant_id"], unique=False)
    op.create_index(
        "ix_customers_tenant_phone", "customers", ["tenant_id", "phone"], unique=False
    )

    op.create_table(
        "addresses",
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("street", sa.Text(), nullable=False),
        sa.Column("house_number", sa.Text(), nullable=False),
        sa.Column("postal_code", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("floor_note", sa.Text(), nullable=True),
        sa.Column("zone_id", sa.UUID(), nullable=True),
        sa.Column("lat", sa.Double(), nullable=True),
        sa.Column("lon", sa.Double(), nullable=True),
        sa.Column(
            "is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        *_base_columns(),
        sa.ForeignKeyConstraint(
            ["customer_id", "tenant_id"],
            ["customers.id", "customers.tenant_id"],
            name="fk_addresses_customer_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id", "tenant_id"],
            ["delivery_zones.id", "delivery_zones.tenant_id"],
            name="fk_addresses_zone_tenant",
            ondelete="SET NULL (zone_id)",
        ),
        _tenant_key(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_addresses_id_tenant"),
    )
    op.create_index("ix_addresses_tenant_id", "addresses", ["tenant_id"], unique=False)
    op.create_index("ix_addresses_customer", "addresses", ["customer_id"], unique=False)
    op.create_index(
        "uq_addresses_one_default",
        "addresses",
        ["customer_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "recognition_objections",
        sa.Column("phone", sa.Text(), nullable=False),
        *_base_columns(),
        sa.CheckConstraint(E164, name="ck_recognition_objections_phone"),
        _tenant_key(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "phone", name="uq_recognition_objections_phone"
        ),
    )
    op.create_index(
        "ix_recognition_objections_tenant_id",
        "recognition_objections",
        ["tenant_id"],
        unique=False,
    )

    op.add_column("orders", sa.Column("address_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_orders_customer_tenant",
        "orders",
        "customers",
        ["customer_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="SET NULL (customer_id)",
    )
    op.create_foreign_key(
        "fk_orders_address_tenant",
        "orders",
        "addresses",
        ["address_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="SET NULL (address_id)",
    )
    op.create_check_constraint(
        "ck_orders_address_on_delivery",
        "orders",
        "address_id IS NULL OR type = 'delivery'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_orders_address_on_delivery", "orders", type_="check")
    op.drop_constraint("fk_orders_address_tenant", "orders", type_="foreignkey")
    op.drop_constraint("fk_orders_customer_tenant", "orders", type_="foreignkey")
    op.drop_column("orders", "address_id")
    op.drop_table("recognition_objections")
    op.drop_table("addresses")
    op.drop_table("customers")
    op.drop_table("delivery_zones")
