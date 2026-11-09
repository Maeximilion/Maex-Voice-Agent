"""Migration 034: the outbox takes the pushes of staff and call-mode changes (#588), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

NEW_TYPES = ("team_phone.changed", "day_hours.changed", "call_mode.changed")


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "033")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _insert(conn, tenant, event_type: str) -> None:
    conn.execute(
        text(
            "INSERT INTO outbox (tenant_id, event_type, payload)"
            " VALUES (:t, :e, '{}'::jsonb)"
        ),
        {"t": tenant, "e": event_type},
    )


def test_up_admits_the_three_change_events_and_down_drops_only_them(
    engine, scratch_db_url
):
    with engine.begin() as conn:
        tenant = conn.execute(
            text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
        ).scalar_one()
        _insert(conn, tenant, "callback.created")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert(conn, tenant, "call_mode.changed")

    command.upgrade(_config(scratch_db_url), "034")
    with engine.begin() as conn:
        for event_type in NEW_TYPES:
            _insert(conn, tenant, event_type)
    # Still closed to a type nobody named.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert(conn, tenant, "staff.whatever")

    command.downgrade(_config(scratch_db_url), "033")
    with engine.connect() as conn:
        left = conn.execute(text("SELECT event_type FROM outbox")).scalars().all()
    assert left == ["callback.created"]
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert(conn, tenant, "team_phone.changed")
