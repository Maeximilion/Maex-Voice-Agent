"""Migration 035: a tenant gets the key of its customer (#573), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "034")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _tenant(conn, key):
    conn.execute(
        text("INSERT INTO tenants (name, customer_key) VALUES ('T', :k)"), {"k": key}
    )


def test_up_takes_one_safe_key_per_tenant_and_down_drops_it(engine, scratch_db_url):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO tenants (name) VALUES ('Alt')"))

    command.upgrade(_config(scratch_db_url), "035")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT customer_key FROM tenants")).scalar() is None
        _tenant(conn, "pizzeria-sued")
        # Several older tenants without a key are no clash.
        conn.execute(text("INSERT INTO tenants (name) VALUES ('Alt 2')"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _tenant(conn, "pizzeria-sued")
    for bad in ("Pizzeria", "pizza--sued", "-pizza", "pizza-", "1pizza", "a" * 33):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _tenant(conn, bad)
    with engine.begin() as conn:
        _tenant(conn, "a" * 32)

    command.downgrade(_config(scratch_db_url), "034")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tenants")).scalar() == 4
        columns = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'tenants'"
            )
        ).scalars()
        assert "customer_key" not in set(columns)
