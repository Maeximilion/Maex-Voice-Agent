"""Migration 023: the default waits of 30 and 60 minutes, up and down."""

from alembic import command
from sqlalchemy import create_engine, text

from api.tests.conftest import alembic_config as _config

_ROW = text(
    "INSERT INTO service_config (tenant_id, team_phone, pickup_wait_minutes, "
    "delivery_wait_minutes) VALUES (:t, '+4972215551234', :p, :d)"
)
_TENANT = text("INSERT INTO tenants (name) VALUES (:n) RETURNING id")
_WAITS = text(
    "SELECT pickup_wait_minutes, delivery_wait_minutes FROM service_config "
    "WHERE tenant_id = :t"
)


def _tenant(conn, name: str, pickup: int, delivery: int):
    tenant = conn.execute(_TENANT, {"n": name}).scalar_one()
    conn.execute(_ROW, {"t": tenant, "p": pickup, "d": delivery})
    return tenant


def _waits(engine, tenant) -> tuple:
    with engine.connect() as conn:
        return tuple(conn.execute(_WAITS, {"t": tenant}).one())


def test_up_moves_the_old_pair_and_keeps_a_changed_wait(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "022")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            old = _tenant(conn, "old", 20, 45)
            changed = _tenant(conn, "changed", 15, 90)

        command.upgrade(_config(scratch_db_url), "023")

        assert _waits(engine, old) == (30, 60)
        assert _waits(engine, changed) == (15, 90)
    finally:
        engine.dispose()


def test_a_new_tenant_starts_on_the_new_defaults(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "023")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = conn.execute(_TENANT, {"n": "new"}).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO service_config (tenant_id, team_phone) "
                    "VALUES (:t, '+4972215551234')"
                ),
                {"t": tenant},
            )
        assert _waits(engine, tenant) == (30, 60)
    finally:
        engine.dispose()


def test_down_restores_the_old_defaults(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "023")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn, "t", 30, 60)
            changed = _tenant(conn, "changed", 15, 90)

        command.downgrade(_config(scratch_db_url), "022")

        assert _waits(engine, tenant) == (20, 45)
        assert _waits(engine, changed) == (15, 90)
        with engine.begin() as conn:
            fresh = conn.execute(_TENANT, {"n": "fresh"}).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO service_config (tenant_id, team_phone) "
                    "VALUES (:t, '+4972215551234')"
                ),
                {"t": fresh},
            )
        assert _waits(engine, fresh) == (20, 45)
    finally:
        engine.dispose()
