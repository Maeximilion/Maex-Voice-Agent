"""Migration 008: minutes until a waiting order's card turns red (#252), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

FIELD = "order_approval_overdue_minutes"


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


def test_upgrade_gives_an_existing_tenant_the_decided_five_minutes(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "007")
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
        command.upgrade(_config(scratch_db_url), "008")
        with engine.connect() as conn:
            assert conn.execute(text(f"SELECT {FIELD} FROM service_config")).one() == (
                5,
            )
    finally:
        engine.dispose()


def test_less_than_a_minute_is_refused_and_one_minute_is_a_setting(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "008")
    engine = create_engine(scratch_db_url)
    insert = text(
        f"INSERT INTO service_config (tenant_id, team_phone, {FIELD}) "
        "VALUES (:t, '+4900000000', :v)"
    )
    try:
        with engine.begin() as conn:
            below, at = _tenant(conn, "Below"), _tenant(conn, "At the bound")
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"t": below, "v": 0})
        with engine.begin() as conn:
            conn.execute(insert, {"t": at, "v": 1})
    finally:
        engine.dispose()


def test_downgrade_removes_only_this_column(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "008")
    assert FIELD in _config_columns(scratch_db_url)
    command.downgrade(_config(scratch_db_url), "007")
    columns = _config_columns(scratch_db_url)
    assert FIELD not in columns
    assert {"order_approval_above_cents", "turn_deadline_seconds"} <= columns
    command.upgrade(_config(scratch_db_url), "head")
    assert FIELD in _config_columns(scratch_db_url)
