"""Stufe 2: pg_trgm, menu_items, item_options, item_allergens, item_aliases, orders, order_items.

Quelle: docs/03_DATA_MODEL.md §Stufe 2 und §Migrationsreihenfolge. Von Hand
geschrieben, nach den Modellen in api/models/menu.py und orders.py; der Test
test_migration_002 vergleicht beide.

Abweichend von docs/03: `orders.pickup_code` (docs/04 confirm liefert ihn,
docs/06 zeigt ihn gross). `orders.address_id` und der Fremdschlüssel auf
`customer_id` kommen mit 003.

Revision: 002
Vorgänger: 001
Erstellt: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
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


def _id() -> sa.Column:
    return sa.Column(
        "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "menu_items",
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("sold_out_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        _id(),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("price_cents >= 0", name="ck_menu_items_price_cents"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "number", name="uq_menu_items_tenant_number"),
    )
    op.create_index(
        op.f("ix_menu_items_tenant_id"), "menu_items", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_menu_items_name_trgm",
        "menu_items",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )

    op.create_table(
        "item_options",
        sa.Column("menu_item_id", sa.UUID(), nullable=False),
        sa.Column("group_name", sa.Text(), nullable=False),
        sa.Column("option_name", sa.Text(), nullable=False),
        sa.Column(
            "price_delta_cents", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("required", sa.Boolean(), server_default="false", nullable=False),
        _id(),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["menu_item_id"], ["menu_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "menu_item_id",
            "group_name",
            "option_name",
            name="uq_item_options_item_group_option",
        ),
    )
    op.create_index(
        op.f("ix_item_options_menu_item_id"),
        "item_options",
        ["menu_item_id"],
        unique=False,
    )

    op.create_table(
        "item_allergens",
        sa.Column("menu_item_id", sa.UUID(), nullable=False),
        sa.Column("allergen_code", sa.Text(), nullable=False),
        sa.Column("confirmed_by", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["menu_item_id"], ["menu_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("menu_item_id", "allergen_code"),
    )

    op.create_table(
        "item_aliases",
        sa.Column("menu_item_id", sa.UUID(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("hits", sa.Integer(), server_default="0", nullable=False),
        _id(),
        *_timestamps(),
        sa.CheckConstraint(
            "source IN ('manual', 'call', 'import')", name="ck_item_aliases_source"
        ),
        sa.CheckConstraint("hits >= 0", name="ck_item_aliases_hits"),
        sa.ForeignKeyConstraint(
            ["menu_item_id"], ["menu_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("menu_item_id", "alias", name="uq_item_aliases_item_alias"),
    )
    op.create_index(
        op.f("ix_item_aliases_menu_item_id"),
        "item_aliases",
        ["menu_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_item_aliases_alias_trgm",
        "item_aliases",
        ["alias"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"alias": "gin_trgm_ops"},
    )

    op.create_table(
        "orders",
        sa.Column("call_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("customer_name", sa.Text(), nullable=False),
        sa.Column("items_total_cents", sa.Integer(), nullable=False),
        sa.Column(
            "delivery_fee_cents", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("pickup_code", sa.Text(), nullable=True),
        sa.Column("handover_state", sa.Text(), nullable=True),
        _id(),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("type IN ('pickup', 'delivery')", name="ck_orders_type"),
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed', 'approved', 'handed_over', 'cancelled')",
            name="ck_orders_status",
        ),
        sa.CheckConstraint(
            "handover_state IN ('pending', 'sent', 'failed')",
            name="ck_orders_handover_state",
        ),
        sa.CheckConstraint(
            "items_total_cents >= 0 AND delivery_fee_cents >= 0",
            name="ck_orders_amounts",
        ),
        sa.CheckConstraint(
            "total_cents = items_total_cents + delivery_fee_cents",
            name="ck_orders_total",
        ),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(op.f("ix_orders_tenant_id"), "orders", ["tenant_id"], unique=False)
    op.create_index(
        "ix_orders_tenant_created", "orders", ["tenant_id", "created_at"], unique=False
    )

    op.create_table(
        "order_items",
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("menu_item_id", sa.UUID(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        _id(),
        *_timestamps(),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity"),
        sa.CheckConstraint("unit_price_cents >= 0", name="ck_order_items_unit_price"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["menu_item_id"], ["menu_items.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_order_items_order_id"), "order_items", ["order_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_order_items_order_id"), table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_orders_tenant_created", table_name="orders")
    op.drop_index(op.f("ix_orders_tenant_id"), table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_item_aliases_alias_trgm", table_name="item_aliases")
    op.drop_index(op.f("ix_item_aliases_menu_item_id"), table_name="item_aliases")
    op.drop_table("item_aliases")
    op.drop_table("item_allergens")
    op.drop_index(op.f("ix_item_options_menu_item_id"), table_name="item_options")
    op.drop_table("item_options")
    op.drop_index("ix_menu_items_name_trgm", table_name="menu_items")
    op.drop_index(op.f("ix_menu_items_tenant_id"), table_name="menu_items")
    op.drop_table("menu_items")
    # pg_trgm bleibt bewusst: eine Extension ist datenbankweit, und ein Downgrade
    # soll nichts entfernen, was ausserhalb dieser Migration genutzt werden kann.
