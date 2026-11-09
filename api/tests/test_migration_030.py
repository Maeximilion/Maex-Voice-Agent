"""Migration 030: the restaurant's login to the operations view (#570), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

TABLES = {
    "gui_accounts",
    "gui_sessions",
    "gui_setup_links",
    "gui_devices",
    "gui_login_failures",
}


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "029")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def test_up_adds_the_five_tables_and_down_removes_them(engine, scratch_db_url):
    with engine.begin() as conn:
        tenant = conn.execute(
            text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
        ).scalar_one()
    command.upgrade(_config(scratch_db_url), "030")
    assert set(inspect(engine).get_table_names()) >= TABLES

    with engine.begin() as conn:
        account = conn.execute(
            text("INSERT INTO gui_accounts (tenant_id) VALUES (:t) RETURNING id"),
            {"t": tenant},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO gui_sessions (account_id, tenant_id, token_hash, created_at,"
                " last_seen_at, last_active_at) VALUES (:a, :t, 'h', now(), now(), now())"
            ),
            {"a": account, "t": tenant},
        )
    # One login per restaurant (D34).
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text("INSERT INTO gui_accounts (tenant_id) VALUES (:t)"), {"t": tenant}
        )

    command.downgrade(_config(scratch_db_url), "029")
    assert not TABLES & set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tenants")).scalar_one() == 1
