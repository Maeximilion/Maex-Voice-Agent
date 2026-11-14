"""Migration 038: the owner's idle clock on `gui_sessions` (#600), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect

from api.tests.conftest import alembic_config as _config


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "037")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _columns(engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("gui_sessions")}


def test_up_adds_the_owner_idle_clock_and_down_drops_it(engine, scratch_db_url):
    assert "admin_active_at" not in _columns(engine)
    command.upgrade(_config(scratch_db_url), "038")
    assert "admin_active_at" in _columns(engine)
    command.downgrade(_config(scratch_db_url), "037")
    assert "admin_active_at" not in _columns(engine)
