"""Bestellungen (docs/03 Stufe 2). confirm ist der einzige Weg von draft nach confirmed.

Beträge in Cent, von Code berechnet, nie vom Modell (CLAUDE.md §2 Regel 1). Die
Datenbank hält zusätzlich fest, dass die Summe stimmt - ein Rechenfehler im
Code soll an der Tabelle scheitern, nicht auf dem Bon landen.

`customer_id` ist bis Migration 003 ein Feld ohne Fremdschlüssel: `customers`
gibt es erst in Stufe 3. `address_id` kommt mit 003 (docs/03
§Migrationsreihenfolge).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, SoftDelete, TenantScoped, Timestamps, UUIDPrimaryKey
from api.models.tenants import _in

ORDER_TYPES = ("pickup", "delivery")
ORDER_STATUSES = ("draft", "confirmed", "approved", "handed_over", "cancelled")
HANDOVER_STATES = ("pending", "sent", "failed")


class Order(UUIDPrimaryKey, TenantScoped, Timestamps, SoftDelete, Base):
    __tablename__ = "orders"
    __table_args__ = (
        _in("type", ORDER_TYPES, "ck_orders_type"),
        _in("status", ORDER_STATUSES, "ck_orders_status"),
        _in("handover_state", HANDOVER_STATES, "ck_orders_handover_state"),
        CheckConstraint(
            "items_total_cents >= 0 AND delivery_fee_cents >= 0",
            name="ck_orders_amounts",
        ),
        CheckConstraint(
            "total_cents = items_total_cents + delivery_fee_cents",
            name="ck_orders_total",
        ),
        # Spalte "Neue Bestellungen" und Tagesbericht lesen je Mandant nach Zeit.
        Index("ix_orders_tenant_created", "tenant_id", "created_at"),
        # Ziel des zusammengesetzten Fremdschluessels aus order_items.
        UniqueConstraint("id", "tenant_id", name="uq_orders_id_tenant"),
    )

    call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calls.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    customer_name: Mapped[str] = mapped_column(Text, nullable=False)
    items_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_fee_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # Zugesagte Zeit. Im Entwurf kann sie noch fehlen.
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Abholcode ("A17"), den confirm liefert (docs/04 confirm) und den das Team
    # am Tresen liest (docs/06 §3). Erst ab confirm gesetzt.
    pickup_code: Mapped[str | None] = mapped_column(Text)
    # Übergabe an Küche/Kasse. Ein Entwurf wird nicht übergeben, deshalb leer.
    handover_state: Mapped[str | None] = mapped_column(Text)


class OrderItem(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    """Eine Position. Ohne menu_item_id keine Position (CLAUDE.md §2 Regel 2).

    Zwei Eltern, ein Mandant: Bestellung und Gericht werden je über (id,
    tenant_id) referenziert, sonst prüft die Datenbank beide Schlüssel einzeln
    und nimmt ein Gericht von Mandant B in eine Bestellung von Mandant A
    (Befund Codex PR #114).
    """

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity"),
        CheckConstraint("unit_price_cents >= 0", name="ck_order_items_unit_price"),
        ForeignKeyConstraint(
            ["order_id", "tenant_id"],
            ["orders.id", "orders.tenant_id"],
            ondelete="CASCADE",
            name="fk_order_items_order_tenant",
        ),
        ForeignKeyConstraint(
            ["menu_item_id", "tenant_id"],
            ["menu_items.id", "menu_items.tenant_id"],
            ondelete="RESTRICT",
            name="fk_order_items_menu_item_tenant",
        ),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    menu_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Preis zum Bestellzeitpunkt, eingefroren: eine spätere Preisänderung auf der
    # Karte ändert keine bestätigte Bestellung.
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # Gewählte Optionen mit Preisdifferenz, z. B. [{"group": "Fleisch",
    # "option": "Huhn", "price_delta_cents": 0}].
    options: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    note: Mapped[str | None] = mapped_column(Text)
