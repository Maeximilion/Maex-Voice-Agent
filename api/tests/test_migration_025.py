"""Migration 025: the notice per weekday (T-3.13), up and down."""

from datetime import time

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

_ROW = text(
    "INSERT INTO capacity (tenant_id, weekday, slot_start, slot_end, max_guests) "
    "VALUES (:t, 1, :a, :b, 40)"
)


def _tenant_with_seats(conn):
    tenant = conn.execute(
        text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
    ).scalar_one()
    conn.execute(_ROW, {"t": tenant, "a": time(17), "b": time(22)})
    return tenant


def test_up_starts_every_window_without_a_notice(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "024")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            _tenant_with_seats(conn)

        command.upgrade(_config(scratch_db_url), "025")

        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT notice_minutes FROM capacity")).scalar() == 0
            )
    finally:
        engine.dispose()


def test_a_notice_is_never_negative(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "025")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            _tenant_with_seats(conn)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE capacity SET notice_minutes = -1"))
    finally:
        engine.dispose()


def test_down_drops_the_notice_and_keeps_the_seats(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "025")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            _tenant_with_seats(conn)
            conn.execute(text("UPDATE capacity SET notice_minutes = 60"))

        command.downgrade(_config(scratch_db_url), "024")

        columns = {c["name"] for c in inspect(engine).get_columns("capacity")}
        assert "notice_minutes" not in columns
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM capacity")).scalar() == 1
    finally:
        engine.dispose()
