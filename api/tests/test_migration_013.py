"""Migration 013: weekly windows of the call mode and the time of the team's
last tap on the mode switch (T-8.1), up and down."""

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
        text(
            "INSERT INTO service_config (tenant_id, team_phone, call_mode) "
            "VALUES (:t, '+49', 'overflow')"
        ),
        {"t": tenant},
    )
    return tenant


_WINDOW = text(
    "INSERT INTO call_mode_windows (tenant_id, weekday, starts_at, ends_at, "
    "call_mode) VALUES (:t, :d, :s, :e, :m)"
)


def test_upgrade_keeps_the_mode_and_records_no_tap(scratch_db_url):
    """No backfill: a tap nobody made would beat every window."""
    command.upgrade(_config(scratch_db_url), "012")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            _tenant(conn)
        command.upgrade(_config(scratch_db_url), "013")
        with engine.connect() as conn:
            mode, chosen = conn.execute(
                text("SELECT call_mode, call_mode_chosen_at FROM service_config")
            ).one()
        assert (mode, chosen) == ("overflow", None)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "values",
    [
        {"d": 7, "s": "17:00", "e": "22:00", "m": "primary"},
        {"d": 4, "s": "22:00", "e": "17:00", "m": "primary"},
        # The emergency stop is a button, never a plan.
        {"d": 4, "s": "17:00", "e": "22:00", "m": "paused"},
        {"d": 4, "s": "17:00", "e": "22:00", "m": "on"},
    ],
)
def test_a_window_out_of_range_is_refused(scratch_db_url, values):
    command.upgrade(_config(scratch_db_url), "013")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(_WINDOW, {"t": tenant, **values})
    finally:
        engine.dispose()


def test_downgrade_removes_only_the_windows_and_the_tap(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "013")
    tables, columns = _schema(scratch_db_url)
    assert "call_mode_windows" in tables and "call_mode_chosen_at" in columns
    command.downgrade(_config(scratch_db_url), "012")
    tables, columns = _schema(scratch_db_url)
    assert "call_mode_windows" not in tables and "call_mode_chosen_at" not in columns
    assert {"call_limit_windows", "service_config"} <= tables
    assert "call_mode" in columns
    command.upgrade(_config(scratch_db_url), "head")
    assert "call_mode_windows" in _schema(scratch_db_url)[0]
