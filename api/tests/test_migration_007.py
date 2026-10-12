"""Migration 007: limits for a reservation by phone in service_config (#252), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

FIELDS = {
    "reservation_max_days_ahead",
    "reservation_max_party_size",
    "reservation_max_party_size_same_day",
}


def _config_columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("service_config")}
    finally:
        engine.dispose()


def _tenant(conn, name: str):
    return conn.execute(
        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
    ).scalar_one()


def test_upgrade_gives_existing_tenants_the_decided_values(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "006")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO service_config (tenant_id, team_phone) "
                    "VALUES (:t, '+4900000000')"
                ),
                {"t": _tenant(conn, "Old")},
            )
        command.upgrade(_config(scratch_db_url), "007")
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT reservation_max_days_ahead, reservation_max_party_size, "
                    "reservation_max_party_size_same_day FROM service_config"
                )
            ).one()
        assert tuple(row) == (90, 8, 5)
    finally:
        engine.dispose()


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_a_negative_limit_is_refused_and_zero_is_a_setting(scratch_db_url, field):
    command.upgrade(_config(scratch_db_url), "007")
    engine = create_engine(scratch_db_url)
    insert = text(
        f"INSERT INTO service_config (tenant_id, team_phone, {field}) "
        "VALUES (:t, '+4900000000', :v)"
    )
    try:
        with engine.begin() as conn:
            negative, zero = _tenant(conn, "Negative"), _tenant(conn, "Zero")
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"t": negative, "v": -1})
        with engine.begin() as conn:
            conn.execute(insert, {"t": zero, "v": 0})
    finally:
        engine.dispose()


def test_downgrade_removes_only_these_columns(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "007")
    assert _config_columns(scratch_db_url) >= FIELDS
    command.downgrade(_config(scratch_db_url), "006")
    columns = _config_columns(scratch_db_url)
    assert not FIELDS & columns
    assert {"reservation_lead_minutes", "team_phone"} <= columns
    command.upgrade(_config(scratch_db_url), "head")
    assert _config_columns(scratch_db_url) >= FIELDS
