"""Mandant, Live-Schalter, Öffnungszeiten, Sondertage, Kapazität (docs/03 Stufe 1)."""

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    Time,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey

CALL_MODES = ("shadow", "overflow", "primary", "paused")
# What a window of `call_mode_windows` may hold: the emergency stop is a button
# with its own way back, never a plan (T-8.1). The order matters: from the mode
# in which the agent does the least to the one in which it does the most, which
# `domain/status/mode.py` reads as the caution order of overlapping windows.
WINDOW_MODES = ("shadow", "overflow", "primary")
SERVICES = ("dinein", "pickup", "delivery")
# A row of `special_days` counts only when `set`. The holiday job writes
# `suggested`; the team's answer on the tablet turns it into `set`, or into
# `dismissed`, which keeps the job from suggesting that date again (T-9.5).
SPECIAL_DAY_STATUSES = ("set", "suggested", "dismissed")
# German states by their ISO 3166-2 code without "DE-" (T-9.5, migration 015).
HOLIDAY_REGIONS = (
    *("BB", "BE", "BW", "BY", "HB", "HE", "HH", "MV"),
    *("NI", "NW", "RP", "SH", "SL", "SN", "ST", "TH"),
)


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
    __table_args__ = (
        _in("call_mode", CALL_MODES, "ck_service_config_call_mode"),
        CheckConstraint(
            "reservation_lead_minutes >= 0",
            name="ck_service_config_reservation_lead_minutes",
        ),
        CheckConstraint(
            "reservation_last_start_minutes >= 0",
            name="ck_service_config_reservation_last_start_minutes",
        ),
        CheckConstraint(
            "reservation_max_days_ahead >= 0",
            name="ck_service_config_reservation_max_days_ahead",
        ),
        CheckConstraint(
            "reservation_max_party_size >= 0",
            name="ck_service_config_reservation_max_party_size",
        ),
        CheckConstraint(
            "reservation_max_party_size_same_day >= 0",
            name="ck_service_config_reservation_max_party_size_same_day",
        ),
        CheckConstraint(
            "order_approval_above_cents >= 0",
            name="ck_service_config_order_approval_above_cents",
        ),
        CheckConstraint(
            "turn_deadline_seconds >= 1",
            name="ck_service_config_turn_deadline_seconds",
        ),
        CheckConstraint(
            "order_approval_overdue_minutes >= 1",
            name="ck_service_config_order_approval_overdue_minutes",
        ),
        CheckConstraint(
            "max_concurrent_calls >= 1",
            name="ck_service_config_max_concurrent_calls",
        ),
        CheckConstraint(
            r"privacy_notice_address ~ '\S'",
            name="ck_service_config_privacy_notice_address",
        ),
        _in("holiday_region", HOLIDAY_REGIONS, "ck_service_config_holiday_region"),
    )

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
    # Reservierungsbeginn je Service-Fenster: frühestens so viele Minuten nach
    # Öffnung, spätestens so viele vor Schluss (T-1.14, D12). Nur Reservierungen.
    reservation_lead_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="15"
    )
    reservation_last_start_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="30"
    )
    # Limits for a reservation by phone (#252, migration 007): how many days
    # ahead, and up to which party size the agent books itself. Above it, and
    # on the same day above the smaller limit, the team calls back.
    reservation_max_days_ahead: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="90"
    )
    reservation_max_party_size: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="8"
    )
    reservation_max_party_size_same_day: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="5"
    )
    # The two other limits of #252, in the same migration. An order above this
    # amount waits for the team's approval before a slip prints
    # (domain/ordering/confirm.py). A guest waits this long in silence for the
    # answer to one sentence before the call goes to the team (the phone line:
    # domain/calls/routing.py, agent/loop.py).
    order_approval_above_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="15000"
    )
    turn_deadline_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="10"
    )
    # An order that waits for "Passt" on the tablet longer than this is overdue:
    # its card turns red (#252, migration 008, domain/ordering/board.py).
    order_approval_overdue_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="5"
    )
    # Whether an order of the agent in `overflow` waits for "Passt" before a slip
    # prints (T-8.2, migration 014). The restaurant switches it off after G5;
    # the amount above still holds an order in every mode.
    overflow_needs_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    # Where the restaurant's privacy notice is, as the caller should hear it;
    # the phone greeting names it (docs/09 L4, T-11.5, migration 009). Empty
    # until the notice is online: the greeting then leaves the sentence out.
    privacy_notice_address: Mapped[str | None] = mapped_column(Text)
    # How many calls the agent takes at once outside a window of
    # `call_limit_windows` (D16, T-10.9, migration 011); a call above the limit
    # rings the team. 1 in the test phase: the router path allows 2 and the team
    # needs its lines (docs/20 §3, §8).
    max_concurrent_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    # When the team last chose a mode on the tablet's switch (T-8.1, migration
    # 013). A window of `call_mode_windows` that began after it holds; the tap
    # holds against one that began before. Pause and resume leave it as it is.
    call_mode_chosen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # The state whose statutory holidays the job suggests (T-9.5, migration
    # 015); none until the restaurant names it, and then nothing is suggested.
    holiday_region: Mapped[str | None] = mapped_column(Text)


class BridgeHeartbeat(Base):
    """When the print bridge last polled (T-8.3, domain/status/bridge.py). A
    table of its own: the bridge polls every 2 s, and a write to `service_config`
    would redraw every tablet, whose header token reads `updated_at`, and wait
    on the lock `confirm` holds there."""

    __tablename__ = "bridge_heartbeats"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CallLimitWindow(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    """A weekly window with a limit of its own for concurrent AI calls (D16:
    weekdays 1, Friday to Sunday evenings up to 3). Where windows overlap, the
    lowest limit counts. A window ends before midnight; one across it is two
    rows (T-10.9, migration 011)."""

    __tablename__ = "call_limit_windows"
    __table_args__ = (
        CheckConstraint(
            "weekday BETWEEN 0 AND 6", name="ck_call_limit_windows_weekday"
        ),
        CheckConstraint("starts_at < ends_at", name="ck_call_limit_windows_order"),
        CheckConstraint("max_calls >= 1", name="ck_call_limit_windows_max_calls"),
    )

    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    starts_at: Mapped[time] = mapped_column(Time, nullable=False)
    ends_at: Mapped[time] = mapped_column(Time, nullable=False)
    max_calls: Mapped[int] = mapped_column(Integer, nullable=False)


class CallModeWindow(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    """A weekly window with a call mode of its own, say the agent answers on
    Friday evenings and only listens in by day (T-8.1, docs/02 §4). Where
    windows overlap, the most cautious mode counts. A window ends before
    midnight; one across it is two rows (as `CallLimitWindow`)."""

    __tablename__ = "call_mode_windows"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_call_mode_windows_weekday"),
        CheckConstraint("starts_at < ends_at", name="ck_call_mode_windows_order"),
        _in("call_mode", WINDOW_MODES, "ck_call_mode_windows_call_mode"),
    )

    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    starts_at: Mapped[time] = mapped_column(Time, nullable=False)
    ends_at: Mapped[time] = mapped_column(Time, nullable=False)
    call_mode: Mapped[str] = mapped_column(Text, nullable=False)


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
    __table_args__ = (
        _in("status", SPECIAL_DAY_STATUSES, "ck_special_days_status"),
        CheckConstraint(
            "like_weekday BETWEEN 0 AND 6", name="ck_special_days_like_weekday"
        ),
        # A day like another weekday is neither closed nor open at hours of
        # its own: one meaning per row.
        CheckConstraint(
            "like_weekday IS NULL OR "
            "(NOT closed AND opens_at IS NULL AND closes_at IS NULL)",
            name="ck_special_days_like_weekday_alone",
        ),
        # One suggestion or dismissal per date: two passes of the holiday job
        # at once write one row (`domain/status/holidays.py`).
        Index(
            "uq_special_days_suggestion",
            "tenant_id",
            "date",
            unique=True,
            postgresql_where=text("status <> 'set'"),
        ),
    )

    date: Mapped[date] = mapped_column(Date, nullable=False)
    closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    opens_at: Mapped[time | None] = mapped_column(Time)
    closes_at: Mapped[time | None] = mapped_column(Time)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="set")
    # The hours and seats of this weekday hold on the date ("wie Sonntag"),
    # Monday is 0 (T-9.5, migration 015).
    like_weekday: Mapped[int | None] = mapped_column(SmallInteger)


class Capacity(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    __tablename__ = "capacity"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_capacity_weekday"),
        # A grid of zero minutes never ends (`CapacityWindow.grid`); zero guests
        # closes a window for reservations and is a setting (migration 006).
        CheckConstraint("slot_minutes > 0", name="ck_capacity_slot_minutes"),
        CheckConstraint("max_guests >= 0", name="ck_capacity_max_guests"),
    )

    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    slot_start: Mapped[time] = mapped_column(Time, nullable=False)
    slot_end: Mapped[time] = mapped_column(Time, nullable=False)
    max_guests: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="30"
    )
