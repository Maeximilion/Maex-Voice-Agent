"""Migration 007: the restaurant's limits in service_config (#252), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

# Field -> (default, lowest value the table takes).
FIELDS = {
    "reservation_max_days_ahead": (90, 0),
    "reservation_max_party_size": (8, 0),
    "reservation_max_party_size_same_day": (5, 0),
    "order_approval_above_cents": (15000, 0),
    "turn_deadline_seconds": (10, 1),
}


def _config_columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("service_config")}
    finally:
        engine.dispose()


def _tenant(conn, name: str):
    return conn.execute(
        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
    ).scalar_one()


def test_upgrade_gives_existing_tenants_the_decided_values(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "006")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO service_config (tenant_id, team_phone) "
                    "VALUES (:t, '+4900000000')"
                ),
                {"t": _tenant(conn, "Old")},
            )
        command.upgrade(_config(scratch_db_url), "007")
        with engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {', '.join(FIELDS)} FROM service_config")
            ).one()
        assert tuple(row) == tuple(default for default, _ in FIELDS.values())
    finally:
        engine.dispose()


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_a_value_below_the_bound_is_refused_and_the_bound_is_a_setting(
    scratch_db_url, field
):
    lowest = FIELDS[field][1]
    command.upgrade(_config(scratch_db_url), "007")
    engine = create_engine(scratch_db_url)
    insert = text(
        f"INSERT INTO service_config (tenant_id, team_phone, {field}) "
        "VALUES (:t, '+4900000000', :v)"
    )
    try:
        with engine.begin() as conn:
            below, at = _tenant(conn, "Below"), _tenant(conn, "At the bound")
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"t": below, "v": lowest - 1})
        with engine.begin() as conn:
            conn.execute(insert, {"t": at, "v": lowest})
    finally:
        engine.dispose()


def test_downgrade_removes_only_these_columns(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "007")
    assert _config_columns(scratch_db_url) >= set(FIELDS)
    command.downgrade(_config(scratch_db_url), "006")
    columns = _config_columns(scratch_db_url)
    assert not set(FIELDS) & columns
    assert {"reservation_lead_minutes", "team_phone"} <= columns
    command.upgrade(_config(scratch_db_url), "head")
    assert _config_columns(scratch_db_url) >= set(FIELDS)
