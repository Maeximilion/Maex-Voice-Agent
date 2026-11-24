"""Migration 046: the outbox's index by tenant and order id (#980), up and
down, twice up does no harm, and the lookups of an order's tickets use it."""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects import postgresql

from api.models import OutboxEvent
from api.tests.conftest import alembic_config as _config

INDEX = "ix_outbox_tenant_order"


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "045")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _indexes(engine) -> set[str]:
    return {index["name"] for index in inspect(engine).get_indexes("outbox")}


def _definition(engine) -> str:
    with engine.connect() as conn:
        return conn.scalar(
            text("select indexdef from pg_indexes where indexname = :name"),
            {"name": INDEX},
        )


def test_up_adds_the_index_and_down_drops_it(engine, scratch_db_url):
    assert INDEX not in _indexes(engine)

    command.upgrade(_config(scratch_db_url), "046")
    definition = _definition(engine)
    assert "(tenant_id, ((payload ->> 'order_id'::text)))" in definition
    # Only the events of an order: the others never meet this lookup.
    assert "WHERE ((payload ->> 'order_id'::text) IS NOT NULL)" in definition

    command.downgrade(_config(scratch_db_url), "045")
    indexes = _indexes(engine)
    assert INDEX not in indexes
    assert "ix_outbox_status_next_attempt" in indexes


def test_down_and_up_again_gives_the_same_index(engine, scratch_db_url):
    command.upgrade(_config(scratch_db_url), "046")
    first = _definition(engine)
    command.downgrade(_config(scratch_db_url), "045")
    command.upgrade(_config(scratch_db_url), "046")
    assert _definition(engine) == first


def _plan(conn, statement) -> str:
    # With bound parameters, as the application sends the lookup (the key
    # 'order_id' too): the custom plan sees them as constants and matches the
    # index expression. A generic plan cannot (`payload ->> $1`); in `auto`
    # mode Postgres keeps the custom plan while it is the cheaper one.
    compiled = statement.compile(dialect=postgresql.psycopg.dialect())
    rows = conn.exec_driver_sql(f"explain {compiled}", compiled.params)
    return "\n".join(row[0] for row in rows)


def test_the_lookups_of_an_orders_tickets_use_the_index(engine, scratch_db_url):
    """The filter `send_ticket`, `last_reason` and the newest revision of a
    ticket (`domain/ordering/ticket.py`, `handover.py`) put on the outbox. With
    the sequential scan off the planner shows whether the index can serve it;
    at pilot volume it may rightly prefer the scan."""
    command.upgrade(_config(scratch_db_url), "046")
    tenant, order = uuid.uuid4(), uuid.uuid4()
    lookup = select(OutboxEvent.id).where(
        OutboxEvent.tenant_id == tenant,
        OutboxEvent.event_type == "order.confirmed",
        OutboxEvent.payload["order_id"].astext == str(order),
    )
    with engine.connect() as conn:
        conn.execute(text("set enable_seqscan = off"))
        assert INDEX in _plan(conn, lookup)
