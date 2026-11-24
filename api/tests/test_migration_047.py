"""Migration 047: one open callback per call (#981), a partial unique index.

Up and down on a database that already holds two open cards of one call:
the upgrade must not fail on them, keeps the oldest open and closes the
others as done by the migration; the downgrade opens those again."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

INDEX = "uq_callbacks_open_per_call"
CLOSED_BY = "migration:047"
NOW = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "046")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _call(conn) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = conn.scalar(
        text(
            "insert into tenants (name, timezone) values ('Testbetrieb', "
            "'Europe/Berlin') returning id"
        )
    )
    call = conn.scalar(
        text(
            "insert into calls (tenant_id, external_session_id, started_at, "
            "delete_after) values (:t, :e, :s, :d) returning id"
        ),
        {"t": tenant, "e": uuid.uuid4().hex, "s": NOW, "d": date(2028, 1, 1)},
    )
    return tenant, call


def _card(conn, tenant, call, minutes, status="open", deleted=False) -> uuid.UUID:
    return conn.scalar(
        text(
            "insert into callbacks (tenant_id, call_id, phone, reason, summary, "
            "status, created_at, deleted_at, done_by, done_at) values (:t, :c, "
            "'+4972215551234', 'not_understood', 'Karte', :st, :at, :del, :by, "
            ":done) returning id"
        ),
        {
            "t": tenant,
            "c": call,
            "st": status,
            "at": NOW + timedelta(minutes=minutes),
            "del": NOW if deleted else None,
            "by": "gui:tablet" if status == "done" else None,
            "done": NOW if status == "done" else None,
        },
    )


def _state(conn, card) -> tuple[str, str | None]:
    row = conn.execute(
        text("select status, done_by from callbacks where id = :id"), {"id": card}
    ).one()
    return row[0], row[1]


def test_up_keeps_the_oldest_open_card_and_down_opens_the_others(
    engine, scratch_db_url
):
    with engine.begin() as conn:
        tenant, call = _call(conn)
        oldest = _card(conn, tenant, call, 0)
        later = _card(conn, tenant, call, 5)
        done = _card(conn, tenant, call, 1, status="done")
        gone = _card(conn, tenant, call, 2, deleted=True)
        _, other_call = _call(conn)
        alone = _card(conn, tenant, other_call, 3)

    command.upgrade(_config(scratch_db_url), "047")
    with engine.connect() as conn:
        assert _state(conn, oldest) == ("open", None)
        assert _state(conn, later) == ("done", CLOSED_BY)
        assert _state(conn, done) == ("done", "gui:tablet")
        assert _state(conn, gone) == ("open", None)
        assert _state(conn, alone) == ("open", None)
        assert conn.scalar(
            text("select count(*) from pg_indexes where indexname = :n"),
            {"n": INDEX},
        )

    command.downgrade(_config(scratch_db_url), "046")
    with engine.connect() as conn:
        assert _state(conn, later) == ("open", None)
        assert _state(conn, done) == ("done", "gui:tablet")
        assert not conn.scalar(
            text("select count(*) from pg_indexes where indexname = :n"),
            {"n": INDEX},
        )


def test_the_index_refuses_a_second_open_card_of_a_call(engine, scratch_db_url):
    command.upgrade(_config(scratch_db_url), "047")
    with engine.begin() as conn:
        tenant, call = _call(conn)
        _card(conn, tenant, call, 0)
        # Done or deleted ones are no open card: they may stand beside it.
        _card(conn, tenant, call, 1, status="done")
        _card(conn, tenant, call, 2, deleted=True)

    with pytest.raises(IntegrityError, match=INDEX), engine.begin() as conn:
        _card(conn, tenant, call, 3)


def test_down_and_up_again_gives_the_same_index(engine, scratch_db_url):
    command.upgrade(_config(scratch_db_url), "047")
    command.downgrade(_config(scratch_db_url), "046")
    command.upgrade(_config(scratch_db_url), "047")
    with engine.connect() as conn:
        definition = conn.scalar(
            text("select indexdef from pg_indexes where indexname = :n"),
            {"n": INDEX},
        )
    assert "UNIQUE INDEX" in definition
    assert "(tenant_id, call_id)" in definition
    assert "(status = 'open'::text) AND (deleted_at IS NULL)" in definition
