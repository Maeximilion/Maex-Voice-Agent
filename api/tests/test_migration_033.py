"""Migration 033: today's team number and the staff changes' check cards (#570 b), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "032")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def test_up_adds_todays_number_and_the_cards_and_down_drops_them(
    engine, scratch_db_url
):
    with engine.begin() as conn:
        tenant = conn.execute(
            text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO service_config (tenant_id, team_phone)"
                " VALUES (:t, '+4972215551234')"
            ),
            {"t": tenant},
        )
    command.upgrade(_config(scratch_db_url), "033")
    columns = {c["name"] for c in inspect(engine).get_columns("service_config")}
    assert {"team_phone_today", "team_phone_today_until"} <= columns
    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT team_phone, team_phone_today FROM service_config")
        ).one() == ("+4972215551234", None)
    # A number for today always carries its end, and the other way round.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE service_config SET team_phone_today = '+491'"))
    card = (
        "INSERT INTO staff_changes (tenant_id, kind, day, before, after, held)"
        " VALUES (:t, :kind, '2026-11-14', '{}', '{}', true)"
    )
    with engine.begin() as conn:
        conn.execute(text(card), {"t": tenant, "kind": "day_hours"})
    # One open card per kind and day; another kind or a decided one is fine.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(card), {"t": tenant, "kind": "day_hours"})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(card), {"t": tenant, "kind": "call_mode"})
    with engine.begin() as conn:
        conn.execute(text(card), {"t": tenant, "kind": "team_phone"})
        conn.execute(text("UPDATE staff_changes SET status = 'kept'"))
        conn.execute(text(card), {"t": tenant, "kind": "day_hours"})

    command.downgrade(_config(scratch_db_url), "032")
    assert "staff_changes" not in inspect(engine).get_table_names()
    columns = {c["name"] for c in inspect(engine).get_columns("service_config")}
    assert "team_phone_today" not in columns
