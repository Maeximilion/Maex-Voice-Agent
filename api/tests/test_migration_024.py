"""Migration 024: the standard wait and the wait for today (T-3.14), up and down."""

from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

_TENANT = text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
_OLD_ROW = text(
    "INSERT INTO service_config (tenant_id, team_phone, pickup_wait_minutes, "
    "delivery_wait_minutes) VALUES (:t, '+4972215551234', 15, 90)"
)
_NEW_ROW = text(
    "INSERT INTO service_config (tenant_id, team_phone) VALUES (:t, '+4972215551234')"
)


def _columns(engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("service_config")}


def test_up_keeps_what_a_restaurant_set_as_its_standard(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "023")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = conn.execute(_TENANT).scalar_one()
            conn.execute(_OLD_ROW, {"t": tenant})

        command.upgrade(_config(scratch_db_url), "024")

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT pickup_wait_standard_minutes, "
                    "delivery_wait_standard_minutes, pickup_wait_today_minutes, "
                    "pickup_wait_today_until, delivery_wait_today_minutes, "
                    "delivery_wait_today_until FROM service_config"
                )
            ).one()
        assert tuple(row) == (15, 90, None, None, None, None)
        assert "pickup_wait_minutes" not in _columns(engine)
    finally:
        engine.dispose()


def test_a_new_tenant_starts_on_the_defaults(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "024")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = conn.execute(_TENANT).scalar_one()
            conn.execute(_NEW_ROW, {"t": tenant})
            row = conn.execute(
                text(
                    "SELECT pickup_wait_standard_minutes, "
                    "delivery_wait_standard_minutes FROM service_config"
                )
            ).one()
        assert tuple(row) == (30, 60)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "assignment",
    [
        "pickup_wait_today_minutes = 45",
        "pickup_wait_today_until = now()",
        "delivery_wait_today_minutes = 45",
        "delivery_wait_today_until = now()",
    ],
)
def test_a_wait_for_today_has_a_value_and_an_end_or_neither(scratch_db_url, assignment):
    command.upgrade(_config(scratch_db_url), "024")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = conn.execute(_TENANT).scalar_one()
            conn.execute(_NEW_ROW, {"t": tenant})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(f"UPDATE service_config SET {assignment}"))
    finally:
        engine.dispose()


def test_down_leaves_the_standard_and_drops_the_wait_for_today(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "024")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = conn.execute(_TENANT).scalar_one()
            conn.execute(_NEW_ROW, {"t": tenant})
            conn.execute(
                text(
                    "UPDATE service_config SET pickup_wait_standard_minutes = 15, "
                    "pickup_wait_today_minutes = 75, pickup_wait_today_until = :u"
                ),
                {"u": datetime(2026, 11, 3, 4, 0, tzinfo=UTC)},
            )

        command.downgrade(_config(scratch_db_url), "023")

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT pickup_wait_minutes, delivery_wait_minutes "
                    "FROM service_config"
                )
            ).one()
        assert tuple(row) == (15, 60)
        assert not {c for c in _columns(engine) if "today" in c or "standard" in c}
    finally:
        engine.dispose()
