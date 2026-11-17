"""Migration 042: `reservations.off_hours`, a table booked outside the opening
hours on purpose (#819), up and down."""

import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "041")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _mark(engine) -> dict | None:
    columns = {c["name"]: c for c in inspect(engine).get_columns("reservations")}
    return columns.get("off_hours")


def _a_tablet_table(engine) -> None:
    """A row from before the migration: nobody can tell any more whether it was
    entered outside the hours on purpose, so it comes out unmarked."""
    tenant = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name, timezone) VALUES (:id, 'T', 'UTC')"),
            {"id": tenant},
        )
        connection.execute(
            text(
                "INSERT INTO reservations (id, tenant_id, source, guest_name, phone,"
                " party_size, reserved_for, idempotency_key)"
                " VALUES (:id, :t, 'tablet', 'G', '', 2, :at, 'k')"
            ),
            {"id": uuid.uuid4(), "t": tenant, "at": datetime.now(UTC)},
        )


def test_up_adds_the_mark_false_for_old_rows_and_down_drops_it(engine, scratch_db_url):
    _a_tablet_table(engine)
    assert _mark(engine) is None

    command.upgrade(_config(scratch_db_url), "042")

    mark = _mark(engine)
    assert mark is not None and mark["nullable"] is False
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT bool_or(off_hours) FROM reservations"))
            is False
        )

    command.downgrade(_config(scratch_db_url), "041")
    assert _mark(engine) is None


def test_up_keeps_the_mark_off_the_agents_tables(engine, scratch_db_url):
    command.upgrade(_config(scratch_db_url), "042")
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

    with (
        pytest.raises(IntegrityError, match="ck_reservations_off_hours_tablet"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO reservations (id, tenant_id, call_id, guest_name,"
                " phone, party_size, reserved_for, idempotency_key, off_hours)"
                " VALUES (:id, :t, :c, 'G', '+4972215551234', 2, :at, 'k', true)"
            ),
            {"id": uuid.uuid4(), "t": tenant, "c": call, "at": datetime.now(UTC)},
        )
