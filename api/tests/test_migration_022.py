"""Migration 022: the recordings of calls (T-11.3, legal check L2), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _call(conn) -> tuple:
    tenant = conn.execute(
        text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
    ).scalar_one()
    call = conn.execute(
        text(
            "INSERT INTO calls (tenant_id, external_session_id, started_at, "
            "delete_after) VALUES (:t, 's', now(), current_date) RETURNING id"
        ),
        {"t": tenant},
    ).scalar_one()
    return tenant, call


_INSERT = text(
    "INSERT INTO recordings (tenant_id, call_id, file_name, started_at) "
    "VALUES (:t, :c, 's.wav', now())"
)


def test_upgrade_makes_a_table_of_one_recording_per_call(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "022")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant, call = _call(conn)
            conn.execute(_INSERT, {"t": tenant, "c": call})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(_INSERT, {"t": tenant, "c": call})
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT file_name, withdrawn_at FROM recordings")
            ).one()
        assert tuple(row) == ("s.wav", None)
    finally:
        engine.dispose()


def test_a_recording_keeps_its_call(scratch_db_url):
    """The row finds the file: the call it belongs to cannot go first."""
    command.upgrade(_config(scratch_db_url), "022")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant, call = _call(conn)
            conn.execute(_INSERT, {"t": tenant, "c": call})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("DELETE FROM calls WHERE id = :c"), {"c": call})
    finally:
        engine.dispose()


def test_downgrade_drops_the_table_and_leaves_the_rest(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "022")
    assert "recordings" in _tables(scratch_db_url)

    command.downgrade(_config(scratch_db_url), "020")

    tables = _tables(scratch_db_url)
    assert "recordings" not in tables
    assert {"calls", "consent_log"} <= tables

    command.upgrade(_config(scratch_db_url), "022")
    assert "recordings" in _tables(scratch_db_url)
