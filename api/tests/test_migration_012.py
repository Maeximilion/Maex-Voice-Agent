"""Migration 012: the register mark on orders (T-11.6, L18), up and down."""

from alembic import command
from sqlalchemy import create_engine, inspect

from api.tests.conftest import alembic_config as _config


def _columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("orders")}
    finally:
        engine.dispose()


def test_upgrade_adds_an_empty_mark_and_downgrade_removes_only_it(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "011")
    before = _columns(scratch_db_url)
    assert "register_entered_at" not in before

    command.upgrade(_config(scratch_db_url), "012")
    engine = create_engine(scratch_db_url)
    try:
        [column] = [
            c
            for c in inspect(engine).get_columns("orders")
            if c["name"] == "register_entered_at"
        ]
    finally:
        engine.dispose()
    assert column["nullable"] is True and column["default"] is None

    command.downgrade(_config(scratch_db_url), "011")
    assert _columns(scratch_db_url) == before
