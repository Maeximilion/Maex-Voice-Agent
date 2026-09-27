"""Migration 005: Reservierungsbeginn in service_config (T-1.14, D12), up und down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

FIELDS = {"reservation_lead_minutes", "reservation_last_start_minutes"}


def _config_columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("service_config")}
    finally:
        engine.dispose()


def test_upgrade_fuellt_bestehende_betriebe_mit_d12_werten(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "004")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = conn.execute(
                text("INSERT INTO tenants (name) VALUES ('Alt') RETURNING id")
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO service_config (tenant_id, team_phone) "
                    "VALUES (:t, '+4900000000')"
                ),
                {"t": tenant},
            )
        command.upgrade(_config(scratch_db_url), "005")
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT reservation_lead_minutes, reservation_last_start_minutes "
                    "FROM service_config"
                )
            ).one()
        assert tuple(row) == (15, 30)
    finally:
        engine.dispose()


def test_negativer_abstand_wird_abgelehnt(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "005")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = conn.execute(
                text("INSERT INTO tenants (name) VALUES ('Neg') RETURNING id")
            ).scalar_one()
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO service_config (tenant_id, team_phone, "
                    "reservation_lead_minutes) VALUES (:t, '+4900000000', -5)"
                ),
                {"t": tenant},
            )
    finally:
        engine.dispose()


def test_downgrade_nimmt_nur_die_spalten_weg(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "005")
    assert _config_columns(scratch_db_url) >= FIELDS
    command.downgrade(_config(scratch_db_url), "004")
    columns = _config_columns(scratch_db_url)
    assert not FIELDS & columns
    assert {"pickup_wait_minutes", "team_phone"} <= columns
    command.upgrade(_config(scratch_db_url), "head")
    assert _config_columns(scratch_db_url) >= FIELDS
