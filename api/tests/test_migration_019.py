"""Migration 019: the request log of data subjects (T-11.8, U2), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

INSERT = (
    "INSERT INTO data_subject_requests (tenant_id, reference, kind, channel, "
    "received_at, deadline, verification_note, answered_at, outcome, delete_after) "
    "VALUES (:tenant, :reference, :kind, :channel, now(), current_date, 'Name', "
    ":answered_at, :outcome, :delete_after)"
)


def _row(tenant, **overrides) -> dict:
    return {
        "tenant": tenant,
        "reference": "dsr_2026_001",
        "kind": "erasure",
        "channel": "mail",
        "answered_at": None,
        "outcome": None,
        "delete_after": None,
    } | overrides


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "019")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _tenant(conn, name="One"):
    return conn.execute(
        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
    ).scalar_one()


def test_up_creates_the_table_with_its_tenant(engine):
    columns = {
        c["name"]: c for c in inspect(engine).get_columns("data_subject_requests")
    }
    assert not columns["tenant_id"]["nullable"]
    assert columns["verification_note"]["nullable"]
    assert {"received_at", "deadline", "answered_at", "kind", "channel"} < set(columns)
    assert {"outcome", "delete_after", "reference"} < set(columns)


def test_a_reference_is_unique_per_tenant_only(engine):
    with engine.begin() as conn:
        conn.execute(text(INSERT), _row(_tenant(conn)))
        conn.execute(text(INSERT), _row(_tenant(conn, "Two")))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        tenant = _tenant(conn, "Three")
        conn.execute(text(INSERT), _row(tenant))
        conn.execute(text(INSERT), _row(tenant))


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "rectification"},
        {"channel": "fax"},
        {"reference": "Müller"},
        {"answered_at": "2026-11-02T10:00:00Z"},
        {"outcome": "done"},
        {"delete_after": "2029-12-31"},
        # A free text here would keep a name for three years.
        {
            "answered_at": "2026-11-02T10:00:00Z",
            "outcome": "Frau Müller",
            "delete_after": "2029-12-31",
        },
    ],
)
def test_the_table_refuses_a_broken_row(engine, overrides):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(INSERT), _row(_tenant(conn), **overrides))


def test_an_answer_is_stored_with_its_outcome_and_date(engine):
    with engine.begin() as conn:
        conn.execute(
            text(INSERT),
            _row(
                _tenant(conn),
                answered_at="2026-11-02T10:00:00Z",
                outcome="not_verified",
                delete_after="2029-12-31",
            ),
        )


def test_down_drops_the_table(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "019")
    command.downgrade(_config(scratch_db_url), "018")
    engine = create_engine(scratch_db_url)
    assert "data_subject_requests" not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(_config(scratch_db_url), "head")
