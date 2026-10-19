"""Migration 003: Begruendung fuer den Aufpreis einer Option (T-4.10), up und down."""

from alembic import command
from sqlalchemy import create_engine, inspect, text

from api.tests.conftest import alembic_config as _config


def _option_columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("item_options")}
    finally:
        engine.dispose()


def test_upgrade_legt_die_spalte_an(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "head")
    assert "price_reason" in _option_columns(scratch_db_url)


def test_downgrade_nimmt_nur_die_spalte_weg(scratch_db_url):
    """Zurueck auf 002: die Spalte faellt weg, die Optionen bleiben."""
    command.upgrade(_config(scratch_db_url), "head")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = conn.execute(
                text("INSERT INTO tenants (name) VALUES ('Bleibt') RETURNING id")
            ).scalar_one()
            item = conn.execute(
                text(
                    "INSERT INTO menu_items (tenant_id, number, name, category, price_cents) "
                    "VALUES (:t, '47', 'Ente knusprig', 'Haupt', 1550) RETURNING id"
                ),
                {"t": tenant},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO item_options (menu_item_id, group_name, option_name, "
                    "price_delta_cents, price_reason) "
                    "VALUES (:i, 'Beilage', 'Nudeln', 300, 'zweite Station')"
                ),
                {"i": item},
            )
        command.downgrade(_config(scratch_db_url), "002")
        assert "price_reason" not in _option_columns(scratch_db_url)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM item_options")).scalar_one()
                == 1
            )
    finally:
        engine.dispose()
    command.upgrade(_config(scratch_db_url), "head")
    assert "price_reason" in _option_columns(scratch_db_url)
