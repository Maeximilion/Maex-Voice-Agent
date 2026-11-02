"""Migration 021: the catalogue of complaint rules (T-10.11, D19), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

INSERT = (
    "INSERT INTO complaint_rules "
    "(tenant_id, name, phrases, remedy_cents, offer_say, granted_say) "
    "VALUES (:tenant, :name, :phrases, :remedy_cents, :offer_say, :granted_say)"
)


def _row(tenant, **overrides) -> dict:
    # Invented for this test, no rule of any restaurant.
    return {
        "tenant": tenant,
        "name": "Testregel",
        "phrases": ["kalt"],
        "remedy_cents": 345,
        "offer_say": "Testangebot?",
        "granted_say": "Testzusage.",
    } | overrides


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "021")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _tenant(conn, name="One"):
    return conn.execute(
        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
    ).scalar_one()


def test_up_creates_the_table_empty_with_its_tenant(engine):
    columns = {c["name"]: c for c in inspect(engine).get_columns("complaint_rules")}
    assert not columns["tenant_id"]["nullable"]
    assert {"name", "phrases", "remedy_cents", "offer_say", "granted_say"} < set(
        columns
    )
    assert {"active", "created_at", "updated_at"} < set(columns)
    with engine.begin() as conn:
        assert conn.scalar(text("SELECT count(*) FROM complaint_rules")) == 0


def test_a_new_row_is_switched_off(engine):
    with engine.begin() as conn:
        conn.execute(text(INSERT), _row(_tenant(conn)))
        assert conn.scalar(text("SELECT active FROM complaint_rules")) is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"phrases": []},
        # Every remedy is money (D19): nothing, or less, is no remedy.
        {"remedy_cents": 0},
        {"remedy_cents": -100},
        {"name": "  "},
        # It has to fit a callback card with the guest's sentence.
        {"name": "x" * 101},
        {"offer_say": ""},
        {"granted_say": "   "},
    ],
)
def test_the_table_refuses_a_broken_row(engine, overrides):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(INSERT), _row(_tenant(conn), **overrides))


def test_a_rule_needs_its_tenant(engine):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(INSERT), _row("00000000-0000-0000-0000-000000000000"))


def test_down_drops_the_table(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "021")
    command.downgrade(_config(scratch_db_url), "-1")
    engine = create_engine(scratch_db_url)
    assert "complaint_rules" not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(_config(scratch_db_url), "head")
