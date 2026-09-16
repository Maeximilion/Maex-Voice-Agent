"""Mandant, Live-Schalter, Öffnungszeiten, Sondertage, Kapazität (docs/03 Stufe 1)."""

import uuid
from datetime import date, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    SmallInteger,
    Text,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey

CALL_MODES = ("shadow", "overflow", "primary", "paused")
SERVICES = ("dinein", "pickup", "delivery")


def _in(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


class Tenant(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="Europe/Berlin"
    )


class ServiceConfig(Timestamps, Base):
    __tablename__ = "service_config"
    __table_args__ = (_in("call_mode", CALL_MODES, "ck_service_config_call_mode"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    call_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="shadow"
    )
    delivery_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    pickup_wait_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="20"
    )
    delivery_wait_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="45"
    )
    team_phone: Mapped[str] = mapped_column(Text, nullable=False)
    max_call_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="420"
    )


class OpeningHours(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    __tablename__ = "opening_hours"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_opening_hours_weekday"),
        _in("service", SERVICES, "ck_opening_hours_service"),
    )

    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    opens_at: Mapped[time] = mapped_column(Time, nullable=False)
    closes_at: Mapped[time] = mapped_column(Time, nullable=False)
    service: Mapped[str] = mapped_column(Text, nullable=False)


class SpecialDay(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    __tablename__ = "special_days"

    date: Mapped[date] = mapped_column(Date, nullable=False)
    closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    opens_at: Mapped[time | None] = mapped_column(Time)
    closes_at: Mapped[time | None] = mapped_column(Time)
    note: Mapped[str | None] = mapped_column(Text)


class Capacity(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    __tablename__ = "capacity"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_capacity_weekday"),
    )

    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    slot_start: Mapped[time] = mapped_column(Time, nullable=False)
    slot_end: Mapped[time] = mapped_column(Time, nullable=False)
    max_guests: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="30"
    )
