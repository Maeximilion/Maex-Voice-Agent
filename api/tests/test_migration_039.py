"""Migration 039: `is_test` on `calls` and `reservations` (#574), up and down."""

import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from api.tests.conftest import alembic_config as _config

TABLES = ("calls", "reservations")


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "038")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _flag(engine, table: str) -> dict | None:
    columns = {c["name"]: c for c in inspect(engine).get_columns(table)}
    return columns.get("is_test")


def _a_call_and_its_table(engine) -> None:
    """Rows from before the migration: they must come out as real ones."""
    tenant, call = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name, timezone) VALUES (:id, 'T', 'UTC')"),
            {"id": tenant},
        )
        connection.execute(
            text(
                "INSERT INTO calls (id, tenant_id, external_session_id, started_at,"
                " delete_after) VALUES (:id, :t, 'ext', :at, :day)"
            ),
            {
                "id": call,
                "t": tenant,
                "at": datetime.now(UTC),
                "day": datetime.now(UTC).date(),
            },
        )
        connection.execute(
            text(
                "INSERT INTO reservations (id, tenant_id, call_id, guest_name, phone,"
                " party_size, reserved_for, idempotency_key)"
                " VALUES (:id, :t, :c, 'G', '+4972215551234', 2, :at, 'k')"
            ),
            {"id": uuid.uuid4(), "t": tenant, "c": call, "at": datetime.now(UTC)},
        )


def test_up_adds_the_flag_false_for_old_rows_and_down_drops_it(engine, scratch_db_url):
    _a_call_and_its_table(engine)
    assert all(_flag(engine, table) is None for table in TABLES)

    command.upgrade(_config(scratch_db_url), "039")

    for table in TABLES:
        flag = _flag(engine, table)
        assert flag is not None and flag["nullable"] is False
        with engine.connect() as connection:
            assert (
                connection.scalar(text(f"SELECT bool_or(is_test) FROM {table}"))
                is False
            )

    command.downgrade(_config(scratch_db_url), "038")
    assert all(_flag(engine, table) is None for table in TABLES)


def test_down_refuses_while_a_test_call_is_left(engine, scratch_db_url):
    _a_call_and_its_table(engine)
    command.upgrade(_config(scratch_db_url), "039")
    with engine.begin() as connection:
        connection.execute(text("UPDATE calls SET is_test = true"))

    with pytest.raises(RuntimeError, match="1 self-test row"):
        command.downgrade(_config(scratch_db_url), "038")

    assert _flag(engine, "calls") is not None


def test_down_refuses_while_a_test_table_is_left(engine, scratch_db_url):
    # A test table whose call is no test row any more still passes for a real
    # one once the flag is gone (Graphify on PR #794).
    _a_call_and_its_table(engine)
    command.upgrade(_config(scratch_db_url), "039")
    with engine.begin() as connection:
        connection.execute(text("UPDATE reservations SET is_test = true"))

    with pytest.raises(RuntimeError, match="1 self-test row"):
        command.downgrade(_config(scratch_db_url), "038")

    assert _flag(engine, "reservations") is not None
