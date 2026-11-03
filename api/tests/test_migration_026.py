"""Migration 026: the switches for pickup and reservations (T-3.10), up and down."""

from alembic import command
from sqlalchemy import create_engine, inspect, text

from api.tests.conftest import alembic_config as _config

_TENANT = text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
_ROW = text(
    "INSERT INTO service_config (tenant_id, team_phone) VALUES (:t, '+4972215551234')"
)


def test_up_starts_both_on(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "025")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(_ROW, {"t": conn.execute(_TENANT).scalar_one()})

        command.upgrade(_config(scratch_db_url), "026")

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT pickup_enabled, reservations_enabled FROM service_config")
            ).one()
        assert tuple(row) == (True, True)
    finally:
        engine.dispose()


def test_down_drops_both(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "026")
    engine = create_engine(scratch_db_url)
    try:
        command.downgrade(_config(scratch_db_url), "025")

        columns = {c["name"] for c in inspect(engine).get_columns("service_config")}
        assert not {"pickup_enabled", "reservations_enabled"} & columns
    finally:
        engine.dispose()
