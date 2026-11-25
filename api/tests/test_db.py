"""Tests für api/db.py gegen eine echte Postgres (DATABASE_URL)."""

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from api import db as db_module
from api.db import SessionLocal, engine, get_db
from api.main import app


def test_get_db_liefert_arbeitsfaehige_session():
    gen = get_db()
    session = next(gen)
    assert isinstance(session, Session)
    assert session.execute(text("SELECT 1")).scalar_one() == 1
    gen.close()


def test_get_db_rollt_bei_fehler_zurueck_und_schliesst():
    gen = get_db()
    session = next(gen)
    session.execute(text("SELECT 1"))
    assert session.in_transaction()
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("Fachfehler"))
    assert not session.in_transaction()
    # Nach close() holt sich die Session bei Bedarf eine frische Verbindung.
    assert session.execute(text("SELECT 2")).scalar_one() == 2
    session.close()


def test_db_nicht_erreichbar_liefert_huelle_statt_stacktrace(monkeypatch):
    dead_engine = create_engine(
        "postgresql+psycopg://maex:maex@127.0.0.1:1/maex_agent",
        connect_args={"connect_timeout": 1},
    )
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=dead_engine))

    @app.get("/_test/db-down")
    def db_down(session: Session = Depends(get_db)) -> dict:
        session.execute(text("SELECT 1"))
        return {"unreachable": True}

    try:
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/_test/db-down")
        body = r.json()
        assert r.status_code == 200
        assert body["ok"] is False
        assert body["error"]["code"] == "service_unavailable"
        assert "Traceback" not in r.text
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", "") != "/_test/db-down"
        ]


def test_engine_pingt_verbindungen():
    assert engine.pool._pre_ping is True
    assert SessionLocal.kw["bind"] is engine


def test_engine_faehrt_read_committed():
    """Voraussetzung des Sperr-Protokolls, nicht dem Serverdefault ueberlassen.

    Unter REPEATABLE READ sieht eine Transaktion nach dem Warten auf eine
    Advisory-Sperre den fremden Commit nicht und legt denselben Vorgang erneut an.
    """
    with engine.connect() as conn:
        stufe = conn.execute(text("SHOW transaction_isolation")).scalar_one()
    assert stufe == "read committed", f"erwartet read committed, bekam {stufe}"


# -- sessions of the phone line: bounded waits (T-1.11 part 2) ----------------------


@pytest.fixture
def bounded(bounded_db):
    return bounded_db(200)


def test_a_bounded_session_gives_up_on_a_row_lock(migrated_db_url, bounded):
    """A tool of the phone line that waits on a row lock fails after the
    bound instead of keeping the guest on the line, and commits nothing
    (Codex on PR #287)."""
    from sqlalchemy.exc import OperationalError

    holder = create_engine(migrated_db_url)
    with holder.connect() as conn:
        conn.execute(text("create table lock_probe (id int primary key)"))
        conn.execute(text("insert into lock_probe values (1)"))
        conn.commit()
        conn.execute(text("select id from lock_probe where id = 1 for update"))
        with (
            bounded() as db,
            pytest.raises(OperationalError, match="statement timeout"),
        ):
            db.execute(text("select id from lock_probe where id = 1 for update"))
    holder.dispose()


def test_a_bounded_session_ends_a_slow_statement(bounded):
    from sqlalchemy.exc import OperationalError

    with (
        bounded() as db,
        pytest.raises(OperationalError, match="statement timeout"),
    ):
        db.execute(text("select pg_sleep(2)"))


def test_the_bound_holds_on_every_connection_after_a_rollback(bounded):
    """Set when the connection is made, so it survives the pool's rollback
    and a commit, and the application's own sessions stay unbounded."""
    for _ in range(3):
        with bounded() as db:
            db.execute(text("select 1"))
            db.rollback()
            assert db.scalar(text("show statement_timeout")) == "200ms"
            db.commit()
    with SessionLocal() as db:
        assert db.scalar(text("show statement_timeout")) == "0"


def _timeout(db: Session) -> str:
    return db.scalar(text("show statement_timeout"))


@pytest.mark.parametrize("open_first", [True, False])
def test_a_turn_with_little_time_left_lowers_the_bound_until_the_tool_is_done(
    bounded, open_first
):
    """The tool's statements wait no longer than the turn has left, its own
    transaction begun inside or not; the next statements of the call have
    the line's bound again (Codex on PR #298)."""
    with bounded() as db:
        if open_first:
            db.execute(text("select 1"))
        with db_module.bounded_by(db, 0.1):
            assert 1 <= int(_timeout(db).removesuffix("ms")) <= 100
        assert _timeout(db) == "200ms"


@pytest.mark.parametrize("left", [0.0, -3.0])
def test_a_turn_past_its_deadline_gives_the_tool_no_time_at_all(bounded, left):
    """Never 0, which Postgres reads as no bound."""
    from sqlalchemy.exc import OperationalError

    with bounded() as db:
        with (
            pytest.raises(OperationalError, match="statement timeout"),
            db_module.bounded_by(db, left),
        ):
            db.execute(text("select pg_sleep(0.1)"))
        db.rollback()
        assert _timeout(db) == "200ms"


def test_a_tool_that_commits_keeps_the_turns_bound_in_its_next_transaction(bounded):
    with bounded() as db:
        with db_module.bounded_by(db, 0.1):
            db.execute(text("select 1"))
            db.commit()
            assert int(_timeout(db).removesuffix("ms")) <= 100
        assert _timeout(db) == "200ms"


def test_the_rollback_after_a_tool_that_ran_out_of_time_lifts_its_bound(bounded):
    """The statement fails like at the line's bound, and the rollback of the
    failure path leaves the session as it was: the call's end is still
    written (`telephony/handler.py`)."""
    from sqlalchemy.exc import OperationalError

    with bounded() as db:
        with (
            pytest.raises(OperationalError, match="statement timeout"),
            db_module.bounded_by(db, 0.05),
        ):
            db.execute(text("select pg_sleep(1)"))
        db.rollback()
        assert _timeout(db) == "200ms"


def test_more_time_left_than_the_bound_costs_no_statement(bounded):
    with bounded() as db:
        db.execute(text("select 1"))
        with db_module.bounded_by(db, 5.0):
            assert _timeout(db) == "200ms"
            assert db_module._LOWERED not in db.info


@pytest.mark.parametrize("left", [0.0, None])
def test_sessions_without_a_bound_and_turns_without_a_deadline_stay_as_they_are(
    bounded, left
):
    """The HTTP tools, the text phone and the evals have no bound to lower,
    and a turn with no deadline leaves the line's bound alone."""
    with SessionLocal() as db, db_module.bounded_by(db, left):
        assert _timeout(db) == "0"
    with bounded() as db, db_module.bounded_by(db, None):
        assert _timeout(db) == "200ms"


@pytest.mark.parametrize(("ms", "seconds"), [(200, "2"), (2000, "2"), (2500, "3")])
def test_a_bounded_session_gives_up_on_a_silent_network(bounded_db, ms, seconds):
    """A database that stops answering at the network level never reaches its
    statement timeout: the client's connection gives up then too, so the
    guest on the line still reaches the team (own review on PR #310)."""
    with bounded_db(ms)() as db:
        dbapi = db.connection().connection.dbapi_connection
        params = dbapi.info.get_parameters()  # type: ignore[union-attr]
    assert params["tcp_user_timeout"] == str(ms)
    assert params["connect_timeout"] == seconds
    assert params["keepalives"] == "1"
    assert params["keepalives_count"] == seconds
