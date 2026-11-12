"""Migration 037: a complaint rule holds no NULL among its phrases (#692), up
and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

INSERT = (
    "INSERT INTO complaint_rules "
    "(tenant_id, name, phrases, remedy_cents, offer_say, granted_say) "
    "VALUES ((SELECT id FROM tenants), :name, :phrases, 345, 'Angebot?', 'Zusage.')"
)


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "036")
    engine = create_engine(scratch_db_url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO tenants (name) VALUES ('T')"))
    yield engine
    engine.dispose()


def _phrases(conn) -> dict[str, list[str | None]]:
    rows = conn.execute(text("SELECT name, phrases FROM complaint_rules"))
    return {row.name: row.phrases for row in rows}


@pytest.mark.parametrize("phrases", [["kalt", None], [None]])
def test_up_refuses_a_null_phrase(engine, scratch_db_url, phrases):
    command.upgrade(_config(scratch_db_url), "037")
    with (
        pytest.raises(IntegrityError, match="ck_complaint_rules_phrases"),
        engine.begin() as conn,
    ):
        conn.execute(text(INSERT), {"name": "Neu", "phrases": phrases})


def test_up_drops_the_nulls_of_an_existing_row_and_down_keeps_the_rest(
    engine, scratch_db_url
):
    with engine.begin() as conn:
        # Written by hand, as `string_to_array('kalt,lauwarm,', ',', '')` does.
        conn.execute(text(INSERT), {"name": "Alt", "phrases": ["kalt", None, "lau"]})
        conn.execute(text(INSERT), {"name": "Gut", "phrases": ["spät"]})

    command.upgrade(_config(scratch_db_url), "037")
    with engine.begin() as conn:
        assert _phrases(conn) == {"Alt": ["kalt", "lau"], "Gut": ["spät"]}
        conn.execute(text(INSERT), {"name": "Neu", "phrases": ["zu salzig"]})

    command.downgrade(_config(scratch_db_url), "036")
    with engine.begin() as conn:
        assert len(_phrases(conn)) == 3
        # The old check again: a NULL among phrases passes, an empty list not.
        conn.execute(text(INSERT), {"name": "Null", "phrases": ["kalt", None]})
    with (
        pytest.raises(IntegrityError, match="ck_complaint_rules_phrases"),
        engine.begin() as conn,
    ):
        conn.execute(text(INSERT), {"name": "Leer", "phrases": []})


def test_up_fails_on_a_row_of_nulls_only_and_deletes_nothing(engine, scratch_db_url):
    """Such a row is fixed by hand: the migration never deletes a remedy."""
    with engine.begin() as conn:
        conn.execute(text(INSERT), {"name": "Nur Null", "phrases": [None]})

    with pytest.raises(IntegrityError, match="ck_complaint_rules_phrases"):
        command.upgrade(_config(scratch_db_url), "037")

    with engine.begin() as conn:
        assert _phrases(conn) == {"Nur Null": [None]}
