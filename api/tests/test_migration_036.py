"""Migration 036: a call carries a note about its line (#651), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

INSERT = (
    "INSERT INTO calls"
    " (tenant_id, external_session_id, started_at, delete_after, line_note)"
    " VALUES ((SELECT id FROM tenants), :sid, now(), '2028-01-01', :note)"
)


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "035")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def test_up_adds_the_note_with_its_known_values_and_down_drops_it(
    engine, scratch_db_url
):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO tenants (name) VALUES ('T')"))
        conn.execute(
            text(
                "INSERT INTO calls"
                " (tenant_id, external_session_id, started_at, delete_after)"
                " VALUES ((SELECT id FROM tenants), 'old', now(), '2028-01-01')"
            )
        )

    command.upgrade(_config(scratch_db_url), "036")
    with engine.begin() as conn:
        # A call from before the migration has no note.
        assert conn.execute(text("SELECT line_note FROM calls")).scalar() is None
        conn.execute(text(INSERT), {"sid": "quiet", "note": "no_caller_audio"})
        conn.execute(text(INSERT), {"sid": "plain", "note": None})
    with (
        pytest.raises(IntegrityError, match="ck_calls_line_note"),
        engine.begin() as conn,
    ):
        conn.execute(text(INSERT), {"sid": "typo", "note": "no_audio"})

    command.downgrade(_config(scratch_db_url), "035")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM calls")).scalar() == 3
        columns = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'calls'"
            )
        ).scalars()
        assert "line_note" not in set(columns)
