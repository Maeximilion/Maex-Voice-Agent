"""Migration 004: Artikelnummer der Kasse `menu_items.pos_code` (T-4.11), up und down."""

from alembic import command
from sqlalchemy import create_engine, inspect, text

from api.tests.conftest import alembic_config as _config


def _item_columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("menu_items")}
    finally:
        engine.dispose()


def test_upgrade_legt_die_spalte_leer_an(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "003")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = conn.execute(
                text("INSERT INTO tenants (name) VALUES ('Alt') RETURNING id")
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO menu_items (tenant_id, number, name, category, price_cents) "
                    "VALUES (:t, '35b', 'Nudeln', 'Haupt', 1350)"
                ),
                {"t": tenant},
            )
        command.upgrade(_config(scratch_db_url), "head")
        assert "pos_code" in _item_columns(scratch_db_url)
        with engine.connect() as conn:
            # Bestehende Gerichte aus dem Chat haben keine Kassennummer.
            assert (
                conn.execute(text("SELECT pos_code FROM menu_items")).scalar() is None
            )
    finally:
        engine.dispose()


def test_downgrade_nimmt_nur_die_spalte_weg(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "head")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE menu_items SET pos_code = '35B'"))
        command.downgrade(_config(scratch_db_url), "003")
        assert "pos_code" not in _item_columns(scratch_db_url)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM menu_items")).scalar_one() == 1
            )
    finally:
        engine.dispose()
    command.upgrade(_config(scratch_db_url), "head")
    assert "pos_code" in _item_columns(scratch_db_url)
