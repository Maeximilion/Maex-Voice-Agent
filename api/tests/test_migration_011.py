"""Migration 011: the limit of concurrent AI calls (D16, T-10.9), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config


def _schema(url: str) -> tuple[set[str], set[str]]:
    engine = create_engine(url)
    try:
        found = inspect(engine)
        columns = {c["name"] for c in found.get_columns("service_config")}
        return set(found.get_table_names()), columns
    finally:
        engine.dispose()


def _tenant(conn) -> str:
    tenant = conn.execute(
        text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
    ).scalar_one()
    conn.execute(
        text("INSERT INTO service_config (tenant_id, team_phone) VALUES (:t, '+49')"),
        {"t": tenant},
    )
    return tenant


_WINDOW = text(
    "INSERT INTO call_limit_windows (tenant_id, weekday, starts_at, ends_at, "
    "max_calls) VALUES (:t, :d, :s, :e, :m)"
)


def test_upgrade_gives_every_restaurant_one_call(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "010")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            _tenant(conn)
        command.upgrade(_config(scratch_db_url), "011")
        with engine.connect() as conn:
            limit = conn.execute(
                text("SELECT max_concurrent_calls FROM service_config")
            ).scalar_one()
        assert limit == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("statement", "values"),
    [
        (text("UPDATE service_config SET max_concurrent_calls = 0"), {}),
        (_WINDOW, {"d": 7, "s": "17:00", "e": "22:00", "m": 3}),
        (_WINDOW, {"d": 4, "s": "22:00", "e": "17:00", "m": 3}),
        (_WINDOW, {"d": 4, "s": "17:00", "e": "22:00", "m": 0}),
    ],
)
def test_a_limit_or_window_out_of_range_is_refused(scratch_db_url, statement, values):
    command.upgrade(_config(scratch_db_url), "011")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(statement, {"t": tenant, **values})
    finally:
        engine.dispose()


def test_downgrade_removes_only_the_limit(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "011")
    tables, columns = _schema(scratch_db_url)
    assert "call_limit_windows" in tables and "max_concurrent_calls" in columns
    command.downgrade(_config(scratch_db_url), "010")
    tables, columns = _schema(scratch_db_url)
    assert "call_limit_windows" not in tables and "max_concurrent_calls" not in columns
    assert {"calls", "consent_log", "service_config"} <= tables
    command.upgrade(_config(scratch_db_url), "head")
    assert "call_limit_windows" in _schema(scratch_db_url)[0]
