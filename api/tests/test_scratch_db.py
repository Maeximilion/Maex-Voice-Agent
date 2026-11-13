"""The reused test database (`evals/scratch_db.empty_scratch_db`): a test after
another sees a fresh copy of the template, or the database is not reused."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from evals.scratch_db import (
    create_scratch_db,
    drop_scratch_db,
    empty_scratch_db,
    scratch_db_state,
)


@pytest.fixture
def copy(migrated_template_url):
    """An own copy of the template, not `migrated_db_url`, whose reset is under test."""
    url = create_scratch_db(template=migrated_template_url)
    yield url, scratch_db_state(url)
    drop_scratch_db(url)


def _run(url: str, *statements: str):
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            for statement in statements:
                result = conn.execute(text(statement))
            return result.all() if result.returns_rows else []
    finally:
        engine.dispose()


def _audit_row(url: str) -> int:
    """Writes a tenant and an audit row, returns the audit row's id."""
    return _run(
        url,
        "INSERT INTO tenants (name) VALUES ('Probe')",
        "INSERT INTO audit_log (tenant_id, actor, action, entity)"
        " SELECT id, 'test', 'probe', 'tenant' FROM tenants RETURNING id",
    )[0][0]


def test_rows_go_and_the_sequence_starts_again(copy):
    url, state = copy
    first = _audit_row(url)
    assert _audit_row(url) == first + 1
    assert empty_scratch_db(state)
    assert _run(url, "SELECT count(*) FROM tenants") == [(0,)]
    assert _audit_row(url) == first


def test_a_new_table_is_not_reused(copy):
    url, state = copy
    _run(url, "CREATE TABLE probe (id int)")
    assert not empty_scratch_db(state)


@pytest.mark.parametrize(
    "change",
    [
        "GRANT SELECT ON tenants TO PUBLIC",
        "ALTER TABLE tenants DISABLE TRIGGER ALL",
        "ALTER TABLE tenants ALTER COLUMN name DROP NOT NULL",
        "COMMENT ON TABLE tenants IS 'probe'",
        "CREATE SCHEMA probe",
        "UPDATE alembic_version SET version_num = 'probe'",
        "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO PUBLIC",
    ],
)
def test_anything_but_rows_is_not_reused(copy, change):
    url, state = copy
    _run(url, change)
    assert not empty_scratch_db(state)


def test_a_dropped_and_recreated_index_is_not_reused(copy):
    """Same name and definition, new object: its oid tells them apart."""
    url, state = copy
    name, definition = _run(
        url,
        "SELECT i.relname, pg_get_indexdef(i.oid) FROM pg_index x"
        " JOIN pg_class i ON i.oid = x.indexrelid"
        " WHERE i.relnamespace = 'public'::regnamespace AND NOT x.indisunique LIMIT 1",
    )[0]
    _run(url, f'DROP INDEX "{name}"', definition)
    assert not empty_scratch_db(state)


def test_a_connection_left_in_a_transaction_does_not_survive(copy):
    url, state = copy
    engine = create_engine(url, poolclass=NullPool)
    left = engine.connect()
    left.execute(text("INSERT INTO tenants (name) VALUES ('Probe')"))
    try:
        assert empty_scratch_db(state)
        assert _run(url, "SELECT count(*) FROM tenants") == [(0,)]
        # The next test's reset also works on the same database.
        _audit_row(url)
        assert empty_scratch_db(state)
    finally:
        left.invalidate()
        engine.dispose()
