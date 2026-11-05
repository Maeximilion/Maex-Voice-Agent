"""Migration 029: reservations without a call from the tablet, seated on the
floor plan (T-3.12), up and down."""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

_TENANT = text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
_CALL = text(
    "INSERT INTO calls (tenant_id, external_session_id, started_at, delete_after)"
    " VALUES (:t, :e, now(), current_date) RETURNING id"
)
_AREA = text(
    "INSERT INTO floor_areas (tenant_id, name, width_cm, depth_cm)"
    " VALUES (:t, 'Gastraum', 1000, 700) RETURNING id"
)
_TABLE = text(
    "INSERT INTO dining_tables (tenant_id, area_id, name, seats, width_cm, depth_cm,"
    " x_cm, y_cm) VALUES (:t, :a, 'T1', 4, 120, 80, 200, 200) RETURNING id"
)


def _reservation(conn, tenant, call, source=None):
    columns = "tenant_id, call_id, guest_name, phone, party_size, reserved_for"
    values = ":t, :c, 'Müller', '', 2, now()"
    if source is not None:
        columns, values = f"{columns}, source", f"{values}, :s"
    return conn.execute(
        text(
            f"INSERT INTO reservations ({columns}, idempotency_key)"
            f" VALUES ({values}, :k) RETURNING id"
        ),
        {"t": tenant, "c": call, "s": source, "k": uuid.uuid4().hex},
    ).scalar_one()


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "028")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _seed(engine):
    with engine.begin() as conn:
        tenant = conn.execute(_TENANT).scalar_one()
        call = conn.execute(_CALL, {"t": tenant, "e": uuid.uuid4().hex}).scalar_one()
        agent = _reservation(conn, tenant, call)
    return tenant, call, agent


def test_up_keeps_the_agents_tables_with_their_calls(engine, scratch_db_url):
    tenant, call, agent = _seed(engine)

    command.upgrade(_config(scratch_db_url), "029")

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT source, call_id FROM reservations WHERE id = :id"),
            {"id": agent},
        ).one()
        assert (row.source, row.call_id) == ("agent", call)
        tablet = _reservation(conn, tenant, None, "tablet")
        area = conn.execute(_AREA, {"t": tenant}).scalar_one()
        table = conn.execute(_TABLE, {"t": tenant, "a": area}).scalar_one()
        conn.execute(
            text(
                "INSERT INTO reservation_tables (reservation_id, table_id, tenant_id)"
                " VALUES (:r, :d, :t)"
            ),
            {"r": tablet, "d": table, "t": tenant},
        )
        # A reservation that goes takes its seating; the table stays.
        conn.execute(text("DELETE FROM reservations WHERE id = :id"), {"id": tablet})
        assert (
            conn.execute(text("SELECT count(*) FROM reservation_tables")).scalar() == 0
        )
        assert conn.execute(text("SELECT count(*) FROM dining_tables")).scalar() == 1


@pytest.mark.parametrize(
    "with_call, source",
    [(True, "tablet"), (False, "agent"), (False, None), (True, "whatsapp")],
)
def test_the_checks_tie_the_call_to_the_source(
    engine, scratch_db_url, with_call, source
):
    tenant, call, _ = _seed(engine)
    command.upgrade(_config(scratch_db_url), "029")

    with pytest.raises(IntegrityError), engine.begin() as conn:
        _reservation(conn, tenant, call if with_call else None, source)


def test_down_removes_the_tablets_tables_and_keeps_the_agents(engine, scratch_db_url):
    tenant, _, agent = _seed(engine)
    command.upgrade(_config(scratch_db_url), "029")
    with engine.begin() as conn:
        _reservation(conn, tenant, None, "tablet")

    command.downgrade(_config(scratch_db_url), "028")

    columns = {c["name"]: c for c in inspect(engine).get_columns("reservations")}
    assert "source" not in columns
    assert columns["call_id"]["nullable"] is False
    assert "reservation_tables" not in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM reservations")).scalars().all() == [
            agent
        ]
