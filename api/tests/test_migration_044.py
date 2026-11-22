"""Migration 044: the reservations' index by call and creation time (#908), up
and down, and twice up does no harm."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect

from api.tests.conftest import alembic_config as _config

INDEX = "ix_reservations_call_created_at"


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "043")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _indexes(engine) -> dict[str, list[str]]:
    found = inspect(engine).get_indexes("reservations")
    return {index["name"]: index["column_names"] for index in found}


def test_up_adds_the_index_and_down_drops_it(engine, scratch_db_url):
    assert INDEX not in _indexes(engine)

    command.upgrade(_config(scratch_db_url), "044")
    assert _indexes(engine)[INDEX] == ["call_id", "created_at"]

    command.downgrade(_config(scratch_db_url), "043")
    indexes = _indexes(engine)
    assert INDEX not in indexes
    assert "ix_reservations_tenant_reserved_for" in indexes


def test_down_and_up_again_gives_the_same_index(engine, scratch_db_url):
    command.upgrade(_config(scratch_db_url), "044")
    command.downgrade(_config(scratch_db_url), "043")
    command.upgrade(_config(scratch_db_url), "044")
    assert _indexes(engine)[INDEX] == ["call_id", "created_at"]
