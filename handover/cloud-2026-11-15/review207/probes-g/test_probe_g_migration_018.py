"""Probe (review #207, area G): migration 018 down and up again on a scratch
database with rows: linked orders (pickup with a customer, delivery with a
customer and an address), a zone; and an objection refusing from head."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from api.tests.conftest import alembic_config as _config
from api.tests.test_migration_018 import _address, _customer, _object, _order, _tenant


def test_down_through_018_with_rows_and_up_to_head(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "018")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn)
            customer = _customer(conn, tenant)
            address = _address(conn, tenant, customer, is_default=True)
            pickup = _order(conn, tenant, kind="pickup", customer=customer)
            delivery = _order(conn, tenant, customer=customer, address=address)
        command.upgrade(_config(scratch_db_url), "head")
        command.downgrade(_config(scratch_db_url), "017")
        with engine.connect() as conn:
            links = conn.execute(
                text("SELECT customer_id FROM orders WHERE id IN (:a, :b)"),
                {"a": pickup, "b": delivery},
            ).scalars().all()
            assert links == [None, None]
        command.upgrade(_config(scratch_db_url), "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM orders")).scalar_one() == 2
    finally:
        engine.dispose()


def test_an_objection_stops_a_downgrade_from_head_and_keeps_head(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "head")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            _object(conn, _tenant(conn))
        with pytest.raises(RuntimeError, match="objection"):
            command.downgrade(_config(scratch_db_url), "017")
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            kept = conn.execute(text("SELECT count(*) FROM recognition_objections")).scalar_one()
        assert kept == 1
        from alembic.script import ScriptDirectory

        assert version == ScriptDirectory.from_config(_config(scratch_db_url)).get_current_head()
    finally:
        engine.dispose()
