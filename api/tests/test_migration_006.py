"""Migration 006: `menu_items.lunch_only` and `lunch_hours` (T-4.13), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

INSERT_HOURS = text(
    "INSERT INTO lunch_hours (tenant_id, weekday, starts_at, ends_at) "
    "VALUES (:t, :d, :s, :e)"
)


def _item_columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("menu_items")}
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _tenant(conn, name: str):
    return conn.execute(
        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
    ).scalar_one()


def test_upgrade_keeps_existing_dishes_as_regular_dishes(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "005")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO menu_items (tenant_id, number, name, category, "
                    "price_cents) VALUES (:t, '47', 'Ente', 'Haupt', 1550)"
                ),
                {"t": _tenant(conn, "Alt")},
            )
        command.upgrade(_config(scratch_db_url), "006")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT lunch_only FROM menu_items")).all() == [
                (False,)
            ]
            # No window after the migration: no lunch menu is sold until
            # someone enters one.
            assert conn.execute(text("SELECT count(*) FROM lunch_hours")).scalar() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("weekday", "starts", "ends"),
    [
        (7, "11:30", "14:00"),
        (-1, "11:30", "14:00"),
        # A window must not be empty or cross midnight.
        (1, "14:00", "11:30"),
        (1, "12:00", "12:00"),
    ],
)
def test_window_outside_the_rules_is_rejected(scratch_db_url, weekday, starts, ends):
    command.upgrade(_config(scratch_db_url), "006")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn, "Regeln")
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                INSERT_HOURS, {"t": tenant, "d": weekday, "s": starts, "e": ends}
            )
    finally:
        engine.dispose()


def test_one_window_per_weekday_and_tenant(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "006")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            first, second = _tenant(conn, "Eins"), _tenant(conn, "Zwei")
            for tenant in (first, second):
                conn.execute(
                    INSERT_HOURS, {"t": tenant, "d": 1, "s": "11:30", "e": "14:00"}
                )
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(INSERT_HOURS, {"t": first, "d": 1, "s": "12:00", "e": "15:00"})
    finally:
        engine.dispose()


def test_downgrade_removes_only_column_and_table(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "006")
    assert "lunch_only" in _item_columns(scratch_db_url)
    assert "lunch_hours" in _tables(scratch_db_url)

    command.downgrade(_config(scratch_db_url), "005")
    assert "lunch_only" not in _item_columns(scratch_db_url)
    assert "lunch_hours" not in _tables(scratch_db_url)
    assert {"pos_code", "sold_out_until"} <= _item_columns(scratch_db_url)

    command.upgrade(_config(scratch_db_url), "head")
    assert "lunch_only" in _item_columns(scratch_db_url)
    assert "lunch_hours" in _tables(scratch_db_url)
