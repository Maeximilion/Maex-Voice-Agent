"""Migration 010: the consent log (T-11.1, legal check L13), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

QUESTION = "Darf ich die Allergie als Hinweis für die Küche notieren?"


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _call(conn) -> tuple:
    tenant = conn.execute(
        text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
    ).scalar_one()
    call = conn.execute(
        text(
            "INSERT INTO calls (tenant_id, external_session_id, started_at, "
            "delete_after) VALUES (:t, 's', now(), current_date) RETURNING id"
        ),
        {"t": tenant},
    ).scalar_one()
    return tenant, call


_INSERT = text(
    "INSERT INTO consent_log (tenant_id, call_id, question, answer, answered_at) "
    "VALUES (:t, :c, :q, :a, now())"
)


def test_upgrade_logs_each_of_the_three_answers(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "010")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant, call = _call(conn)
            for answer in ("yes", "no", "unclear"):
                conn.execute(
                    _INSERT, {"t": tenant, "c": call, "q": QUESTION, "a": answer}
                )
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM consent_log")).scalar() == 3
    finally:
        engine.dispose()


def test_an_answer_outside_the_three_is_refused(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "010")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant, call = _call(conn)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(_INSERT, {"t": tenant, "c": call, "q": QUESTION, "a": "maybe"})
    finally:
        engine.dispose()


def test_a_row_needs_a_call_that_exists(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "010")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant, _ = _call(conn)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                _INSERT,
                {
                    "t": tenant,
                    "c": "00000000-0000-0000-0000-000000000000",
                    "q": QUESTION,
                    "a": "yes",
                },
            )
    finally:
        engine.dispose()


def test_downgrade_removes_only_this_table(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "010")
    assert "consent_log" in _tables(scratch_db_url)
    command.downgrade(_config(scratch_db_url), "009")
    tables = _tables(scratch_db_url)
    assert "consent_log" not in tables
    assert {"calls", "orders", "reservations"} <= tables
    command.upgrade(_config(scratch_db_url), "head")
    assert "consent_log" in _tables(scratch_db_url)
