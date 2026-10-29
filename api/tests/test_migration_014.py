"""Migration 014: the approval step of overflow can be switched off (T-8.2), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

FIELD = "overflow_needs_approval"


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


def test_upgrade_keeps_the_approval_step_for_an_existing_tenant(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "013")
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
        command.upgrade(_config(scratch_db_url), "014")
        with engine.connect() as conn:
            assert conn.execute(text(f"SELECT {FIELD} FROM service_config")).one() == (
                True,
            )
    finally:
        engine.dispose()


def test_no_value_is_refused(scratch_db_url):
    """NULL would leave confirm to guess whether the order waits."""
    command.upgrade(_config(scratch_db_url), "014")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn, "Null")
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO service_config (tenant_id, team_phone, {FIELD}) "
                    "VALUES (:t, '+4900000000', NULL)"
                ),
                {"t": tenant},
            )
    finally:
        engine.dispose()


def test_downgrade_removes_only_this_column(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "014")
    assert FIELD in _config_columns(scratch_db_url)
    command.downgrade(_config(scratch_db_url), "013")
    columns = _config_columns(scratch_db_url)
    assert FIELD not in columns
    assert {"call_mode_chosen_at", "order_approval_above_cents"} <= columns
    command.upgrade(_config(scratch_db_url), "head")
    assert FIELD in _config_columns(scratch_db_url)
