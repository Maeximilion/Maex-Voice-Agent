"""Migration 020: the register price of the price check on the dish (T-4.9), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

COLUMNS = {"register_price_cents", "register_price_rejected"}


def _columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("menu_items")}
    finally:
        engine.dispose()


def _dish(conn) -> object:
    tenant = conn.execute(
        text("INSERT INTO tenants (name) VALUES ('One') RETURNING id")
    ).scalar_one()
    return conn.execute(
        text(
            "INSERT INTO menu_items (tenant_id, number, name, category, price_cents) "
            "VALUES (:t, '23', 'Rollen', 'Vorspeisen', 690) RETURNING id"
        ),
        {"t": tenant},
    ).scalar_one()


def test_an_existing_dish_has_no_difference_after_the_upgrade(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "019")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            _dish(conn)
        command.upgrade(_config(scratch_db_url), "020")
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT price_cents, register_price_cents, register_price_rejected "
                    "FROM menu_items"
                )
            ).one()
        assert tuple(row) == (690, None, False)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "values",
    ["register_price_cents = -1", "register_price_rejected = true"],
)
def test_the_database_refuses_a_wrong_difference(scratch_db_url, values):
    command.upgrade(_config(scratch_db_url), "020")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            dish = _dish(conn)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(f"UPDATE menu_items SET {values} WHERE id = :d"), {"d": dish}
            )
    finally:
        engine.dispose()


def test_a_rejected_difference_keeps_its_price(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "020")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            dish = _dish(conn)
            conn.execute(
                text(
                    "UPDATE menu_items SET register_price_cents = 720, "
                    "register_price_rejected = true WHERE id = :d"
                ),
                {"d": dish},
            )
    finally:
        engine.dispose()


def test_downgrade_drops_the_columns_and_keeps_the_price(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "020")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            dish = _dish(conn)
            conn.execute(
                text("UPDATE menu_items SET register_price_cents = 720 WHERE id = :d"),
                {"d": dish},
            )
        command.downgrade(_config(scratch_db_url), "019")
        assert not COLUMNS & _columns(scratch_db_url)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT price_cents FROM menu_items")).one() == (
                690,
            )
        command.upgrade(_config(scratch_db_url), "head")
        assert COLUMNS <= _columns(scratch_db_url)
    finally:
        engine.dispose()
