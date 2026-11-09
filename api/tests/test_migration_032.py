"""Migration 032: the owner's admin PIN and its 60 minutes per device (#570 b), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "031")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_up_adds_the_admin_pin_apart_from_the_team_pin_and_down_drops_it(
    engine, scratch_db_url
):
    with engine.begin() as conn:
        tenant = conn.execute(
            text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO gui_accounts (tenant_id, password_hash, team_pin)"
                " VALUES (:t, 'scrypt$x', '4711')"
            ),
            {"t": tenant},
        )
    command.upgrade(_config(scratch_db_url), "032")
    assert {
        "admin_pin",
        "admin_failures",
        "admin_last_failed_at",
        "admin_blocked_until",
    } <= _columns(engine, "gui_accounts")
    assert "admin_until" in _columns(engine, "gui_sessions")
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT team_pin, admin_pin, admin_failures FROM gui_accounts")
        ).one()
        assert row == ("4711", None, 0)
        conn.execute(text("UPDATE gui_accounts SET admin_pin = '080815'"))
    # Digits only, and never the team PIN: the staff know that one.
    for bad in ("abc", "123", "1234567", "4711"):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE gui_accounts SET admin_pin = :p"), {"p": bad})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE gui_accounts SET team_pin = '080815'"))

    command.downgrade(_config(scratch_db_url), "031")
    assert "admin_pin" not in _columns(engine, "gui_accounts")
    assert "admin_until" not in _columns(engine, "gui_sessions")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT team_pin FROM gui_accounts")).scalar() == (
            "4711"
        )
