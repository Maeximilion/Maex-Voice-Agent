"""Migration 027: orders and reservations note whether they were booked on their
own line (#523), filled from the caller IDs still there, up and down."""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from api.tests.conftest import alembic_config as _config

PHONE = "+4972215551234"

# caller_id of the call, phone of the booking, the flag after the upgrade.
ROWS = [
    ("+4972215551234", PHONE, True),
    ("07221 5551234", PHONE, True),
    ("+4972215559999", PHONE, False),
    ("anonymous", PHONE, False),
    (None, PHONE, False),
    ("+4972215551234", "", False),
]


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "026")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _call(conn, tenant, caller_id):
    return conn.execute(
        text(
            "INSERT INTO calls (tenant_id, external_session_id, caller_id, "
            "started_at, delete_after) "
            "VALUES (:t, :e, :c, now(), current_date) RETURNING id"
        ),
        {"t": tenant, "e": uuid.uuid4().hex, "c": caller_id},
    ).scalar_one()


def _book(conn, tenant, call, phone):
    order = conn.execute(
        text(
            "INSERT INTO orders (tenant_id, call_id, type, phone, customer_name, "
            "items_total_cents, total_cents, idempotency_key) "
            "VALUES (:t, :c, 'pickup', :p, 'Müller', 0, 0, :k) RETURNING id"
        ),
        {"t": tenant, "c": call, "p": phone, "k": uuid.uuid4().hex},
    ).scalar_one()
    table = conn.execute(
        text(
            "INSERT INTO reservations (tenant_id, call_id, guest_name, phone, "
            "party_size, reserved_for, idempotency_key) "
            "VALUES (:t, :c, 'Müller', :p, 2, now(), :k) RETURNING id"
        ),
        {"t": tenant, "c": call, "p": phone, "k": uuid.uuid4().hex},
    ).scalar_one()
    return order, table


def _flags(conn, table):
    return dict(conn.execute(text(f"SELECT id, booked_on_own_line FROM {table}")).all())


def test_up_reads_the_caller_ids_that_are_still_there(engine, scratch_db_url):
    with engine.begin() as conn:
        tenant = conn.execute(
            text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
        ).scalar_one()
        booked = [
            (_book(conn, tenant, _call(conn, tenant, caller), phone), own)
            for caller, phone, own in ROWS
        ]

    command.upgrade(_config(scratch_db_url), "027")

    with engine.connect() as conn:
        orders, tables = _flags(conn, "orders"), _flags(conn, "reservations")
    assert [orders[order] for (order, _), _ in booked] == [own for *_, own in ROWS]
    assert [tables[table] for (_, table), _ in booked] == [own for *_, own in ROWS]


def test_a_call_of_another_tenant_proves_nothing(engine, scratch_db_url):
    with engine.begin() as conn:
        one, two = (
            conn.execute(
                text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": n}
            ).scalar_one()
            for n in ("One", "Two")
        )
        order, table = _book(conn, one, _call(conn, two, PHONE), PHONE)

    command.upgrade(_config(scratch_db_url), "027")

    with engine.connect() as conn:
        assert _flags(conn, "orders")[order] is False
        assert _flags(conn, "reservations")[table] is False


def test_a_new_row_starts_false(engine, scratch_db_url):
    command.upgrade(_config(scratch_db_url), "027")
    with engine.begin() as conn:
        tenant = conn.execute(
            text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
        ).scalar_one()
        order, table = _book(conn, tenant, _call(conn, tenant, PHONE), PHONE)
        assert _flags(conn, "orders")[order] is False
        assert _flags(conn, "reservations")[table] is False


def test_down_drops_the_column(engine, scratch_db_url):
    command.upgrade(_config(scratch_db_url), "027")
    command.downgrade(_config(scratch_db_url), "026")
    for table in ("orders", "reservations"):
        columns = {c["name"] for c in inspect(engine).get_columns(table)}
        assert "booked_on_own_line" not in columns
