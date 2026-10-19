"""Migration 006: bounds on capacity and party size (#252), up and down.

A grid of zero minutes never ends: `CapacityWindow.grid` adds the step until it
passes the end, inside the day lock of a reservation, so one bad row would stop
every booking of that restaurant and day. The values come from the database
(CLAUDE.md §2 rule 1), so the database is where they are held.
"""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

CHECKS = {
    "capacity": {"ck_capacity_slot_minutes", "ck_capacity_max_guests"},
    "reservations": {"ck_reservations_party_size"},
}
CAPACITY = (
    "INSERT INTO capacity (tenant_id, weekday, slot_start, slot_end, max_guests, "
    "slot_minutes) VALUES (:t, 1, '17:00', '22:00', :guests, :minutes)"
)


def _checks(url: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        constraints = inspect(engine).get_check_constraints(table)
        # A constraint without a name is none of the ones looked for here.
        return {name for c in constraints if (name := c["name"]) is not None}
    finally:
        engine.dispose()


def _tenant(conn) -> str:
    return conn.execute(
        text("INSERT INTO tenants (name) VALUES ('Betrieb') RETURNING id")
    ).scalar_one()


@pytest.mark.parametrize(
    ("guests", "minutes"),
    [(40, 0), (40, -30), (-1, 30)],
    ids=["grid of zero minutes", "negative grid", "negative seats"],
)
def test_a_capacity_row_outside_the_bounds_is_refused(scratch_db_url, guests, minutes):
    command.upgrade(_config(scratch_db_url), "006")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(CAPACITY), {"t": tenant, "guests": guests, "minutes": minutes}
            )
    finally:
        engine.dispose()


def test_a_window_without_seats_is_a_valid_row(scratch_db_url):
    """Zero guests closes a window for reservations; that is a setting."""
    command.upgrade(_config(scratch_db_url), "006")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(CAPACITY), {"t": _tenant(conn), "guests": 0, "minutes": 30}
            )
    finally:
        engine.dispose()


def test_a_party_of_nobody_is_refused(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "006")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn)
            call = conn.execute(
                text(
                    "INSERT INTO calls (tenant_id, external_session_id, started_at, "
                    "delete_after) VALUES (:t, 'ext', now(), current_date) RETURNING id"
                ),
                {"t": tenant},
            ).scalar_one()
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO reservations (tenant_id, call_id, guest_name, phone, "
                    "party_size, reserved_for, idempotency_key) VALUES (:t, :c, "
                    "'Müller', '+4972215551234', 0, now(), 'k')"
                ),
                {"t": tenant, "c": call},
            )
    finally:
        engine.dispose()


def test_a_row_that_is_already_wrong_stops_the_upgrade(scratch_db_url):
    """Nothing is changed silently: what the grid should have been is the
    restaurant's decision. The upgrade fails and names the constraint."""
    command.upgrade(_config(scratch_db_url), "005")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(CAPACITY), {"t": _tenant(conn), "guests": 40, "minutes": 0}
            )
        with pytest.raises(IntegrityError, match="ck_capacity_slot_minutes"):
            command.upgrade(_config(scratch_db_url), "006")
    finally:
        engine.dispose()


def test_downgrade_takes_only_the_bounds_away(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "006")
    for table, names in CHECKS.items():
        assert names <= _checks(scratch_db_url, table)

    command.downgrade(_config(scratch_db_url), "005")
    for table, names in CHECKS.items():
        assert not names & _checks(scratch_db_url, table)
    # What was there before stays.
    assert "ck_capacity_weekday" in _checks(scratch_db_url, "capacity")
    assert "ck_reservations_status" in _checks(scratch_db_url, "reservations")

    command.upgrade(_config(scratch_db_url), "head")
    for table, names in CHECKS.items():
        assert names <= _checks(scratch_db_url, table)
