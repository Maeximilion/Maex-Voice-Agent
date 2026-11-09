"""Migration 031: the team PIN readable for the operator (Maxi 09.11.2026), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "030")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _columns(engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("gui_accounts")}


def test_up_empties_the_hashed_pin_and_takes_digits_only(engine, scratch_db_url):
    with engine.begin() as conn:
        tenant = conn.execute(
            text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO gui_accounts (tenant_id, password_hash, team_pin_hash)"
                " VALUES (:t, 'scrypt$x', 'scrypt$y')"
            ),
            {"t": tenant},
        )
    command.upgrade(_config(scratch_db_url), "031")
    assert "team_pin" in _columns(engine) and "team_pin_hash" not in _columns(engine)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT password_hash, team_pin FROM gui_accounts")
        ).one()
        assert row == ("scrypt$x", None)
        conn.execute(text("UPDATE gui_accounts SET team_pin = '471100'"))
    for bad in ("abc", "123", "1234567"):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE gui_accounts SET team_pin = :p"), {"p": bad})

    command.downgrade(_config(scratch_db_url), "030")
    assert "team_pin_hash" in _columns(engine) and "team_pin" not in _columns(engine)
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT password_hash FROM gui_accounts")).scalar()
            == "scrypt$x"
        )
