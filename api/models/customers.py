"""Customers, their addresses and the delivery zones (docs/03 Stufe 3, T-6.1).

A phone number is no key: several customers can share one, and a name that does
not match means a new record (legal check L12). The objection to recognition
therefore hangs on the number and outlives every customer behind it. Personal
rows here are deleted for real, after 24 months without contact (docs/03
Löschkonzept), so nothing here is soft-deleted.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey
from api.models.tenants import _in

ZONE_MATCH_TYPES = ("postal_code", "polygon")
E164 = r"phone ~ '^\+[1-9][0-9]{1,14}$'"


class DeliveryZone(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    __tablename__ = "delivery_zones"
    __table_args__ = (
        _in("match_type", ZONE_MATCH_TYPES, "ck_delivery_zones_match_type"),
        CheckConstraint(
            "(match_type = 'postal_code' AND coalesce(cardinality(postal_codes), 0) > 0)"
            " OR (match_type = 'polygon' AND polygon IS NOT NULL)",
            name="ck_delivery_zones_match_value",
        ),
        CheckConstraint(
            "fee_cents >= 0 AND min_order_cents >= 0 AND eta_minutes >= 0",
            name="ck_delivery_zones_amounts",
        ),
        # Target of the composite foreign key from addresses.
        UniqueConstraint("id", "tenant_id", name="uq_delivery_zones_id_tenant"),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    match_type: Mapped[str] = mapped_column(Text, nullable=False)
    postal_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    polygon: Mapped[dict | None] = mapped_column(JSONB)
    fee_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    min_order_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    eta_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )


class Customer(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint(E164, name="ck_customers_phone"),
        CheckConstraint("order_count >= 0", name="ck_customers_order_count"),
        Index("ix_customers_tenant_phone", "tenant_id", "phone"),
        # Target of the composite foreign keys from addresses and orders.
        UniqueConstraint("id", "tenant_id", name="uq_customers_id_tenant"),
    )

    phone: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    last_order_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The 24-month period runs from it: set on an order, a reservation, a
    # callback and a complaint (Maxi, 28.10.2026, #344).
    last_contact_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    order_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class Address(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    __tablename__ = "addresses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "tenant_id"],
            ["customers.id", "customers.tenant_id"],
            ondelete="CASCADE",
            name="fk_addresses_customer_tenant",
        ),
        # Only the zone goes: the tenant of the row is not null.
        ForeignKeyConstraint(
            ["zone_id", "tenant_id"],
            ["delivery_zones.id", "delivery_zones.tenant_id"],
            ondelete="SET NULL (zone_id)",
            name="fk_addresses_zone_tenant",
        ),
        Index("ix_addresses_customer", "customer_id"),
        Index(
            "uq_addresses_one_default",
            "customer_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        # Target of the composite foreign key from orders.
        UniqueConstraint("id", "tenant_id", name="uq_addresses_id_tenant"),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    street: Mapped[str] = mapped_column(Text, nullable=False)
    house_number: Mapped[str] = mapped_column(Text, nullable=False)
    postal_code: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    floor_note: Mapped[str | None] = mapped_column(Text)
    zone_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lat: Mapped[float | None] = mapped_column(Double)
    lon: Mapped[float | None] = mapped_column(Double)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class RecognitionObjection(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    """Art. 21: recognition by this number stays off (L12). In E.164, because
    a number stored in another spelling would switch nothing off."""

    __tablename__ = "recognition_objections"
    __table_args__ = (
        CheckConstraint(E164, name="ck_recognition_objections_phone"),
        UniqueConstraint("tenant_id", "phone", name="uq_recognition_objections_phone"),
    )

    phone: Mapped[str] = mapped_column(Text, nullable=False)
