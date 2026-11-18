"""Migration 043: the audit log's index by tenant, action and id (review of
#837), up and down, and twice up does no harm."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect

from api.tests.conftest import alembic_config as _config

INDEX = "ix_audit_log_tenant_action"


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "042")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _indexes(engine) -> dict[str, list[str]]:
    found = inspect(engine).get_indexes("audit_log")
    return {index["name"]: index["column_names"] for index in found}


def test_up_adds_the_index_and_down_drops_it(engine, scratch_db_url):
    assert INDEX not in _indexes(engine)

    command.upgrade(_config(scratch_db_url), "043")
    assert _indexes(engine)[INDEX] == ["tenant_id", "action", "id"]

    command.downgrade(_config(scratch_db_url), "042")
    indexes = _indexes(engine)
    assert INDEX not in indexes
    assert "ix_audit_log_tenant_at" in indexes


def test_down_and_up_again_gives_the_same_index(engine, scratch_db_url):
    command.upgrade(_config(scratch_db_url), "043")
    command.downgrade(_config(scratch_db_url), "042")
    command.upgrade(_config(scratch_db_url), "043")
    assert _indexes(engine)[INDEX] == ["tenant_id", "action", "id"]
