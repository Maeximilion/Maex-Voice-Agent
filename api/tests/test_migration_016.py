"""Migration 016: the print bridge's sign of life (T-8.3), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

TABLE = "bridge_heartbeats"


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_makes_one_row_per_tenant(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "016")
    engine = create_engine(scratch_db_url)
    insert = text(f"INSERT INTO {TABLE} (tenant_id, seen_at) VALUES (:t, now())")
    try:
        with engine.begin() as conn:
            tenant = conn.execute(
                text("INSERT INTO tenants (name) VALUES ('One') RETURNING id")
            ).scalar_one()
            conn.execute(insert, {"t": tenant})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"t": tenant})
    finally:
        engine.dispose()


def test_a_row_needs_a_known_tenant(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "016")
    engine = create_engine(scratch_db_url)
    try:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {TABLE} (tenant_id, seen_at) "
                    "VALUES (gen_random_uuid(), now())"
                )
            )
    finally:
        engine.dispose()


def test_downgrade_removes_only_this_table(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "016")
    assert TABLE in _tables(scratch_db_url)
    command.downgrade(_config(scratch_db_url), "015")
    tables = _tables(scratch_db_url)
    assert TABLE not in tables
    assert "call_mode_windows" in tables
    command.upgrade(_config(scratch_db_url), "head")
    assert TABLE in _tables(scratch_db_url)
