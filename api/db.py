"""Datenbankzugang: eine Engine je Prozess, eine Session je Request."""

import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from api.config import settings

# pool_pre_ping: eine vom Server gekappte Verbindung (DB-Neustart, Timeout) wird
# erkannt und ersetzt, statt als Fehler bis zum Agenten durchzuschlagen.
#
# isolation_level: READ COMMITTED gehoert zum Sperr-Protokoll der schreibenden
# Vorgaenge (domain/callbacks, domain/reservations). Wer eine Advisory-Sperre
# bekommt, muss anschliessend sehen, was der Vorgaenger committet hat. Unter
# REPEATABLE READ liest die Transaktion den Stand ihres Beginns weiter, also den
# Zustand vor dem fremden Commit, und legt denselben Vorgang ein zweites Mal an.
# Postgres hat diesen Wert als Default; hier steht er ausdruecklich, damit eine
# andere Servereinstellung die Fachlogik nicht still aushebelt.
#
# hide_parameters: the error SQLAlchemy raises carries the statement's SQL,
# never its bound values. Those are guest names, phone numbers and notes, and a
# log line is outside every deletion period of docs/03. It does not reach the
# server's own message: a constraint error says "DETAIL: Failing row contains
# (...)" with the row's values. So the log formatter and `error_text`
# (api/core/logging.py) write only the type and the constraint of a database
# error, and Postgres runs with log_error_verbosity=terse (docker-compose.yml)
# for its own log.
_ENGINE_ARGS = {
    "pool_pre_ping": True,
    "isolation_level": "READ COMMITTED",
    "hide_parameters": True,
}
engine = create_engine(settings.database_url, **_ENGINE_ARGS)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def bounded_sessions(
    timeout_ms: int, *, url: str = settings.database_url
) -> sessionmaker[Session]:
    """Sessions on connections of their own on which every statement fails
    after `timeout_ms`, a wait for a lock inside it included.

    For the phone line (`telephony/app.py`), where a guest holds a receiver: a
    tool that waits on a row lock would keep them waiting past the turn's
    deadline and could still commit after it (Codex on PR #287). The statement
    fails instead, its transaction rolls back, and the call goes to the team
    (CLAUDE.md §2 rule 5). Set when a connection is made, so a rollback of the
    pool does not undo it; the application's other sessions stay unbounded.

    A database that stops answering at the network level never reaches its
    `statement_timeout`: the client gives up then too, on a connection that
    does not open, on data sent and never acknowledged, and on a peer that
    answers no keepalive while a reply is awaited (own review on PR #310).
    """
    ms = int(timeout_ms)
    # Whole seconds, and libpq takes no connect timeout under 2.
    seconds = max(2, -(-ms // 1000))
    bounded = create_engine(
        url,
        **_ENGINE_ARGS,
        connect_args={
            "connect_timeout": seconds,
            "tcp_user_timeout": ms,
            "keepalives": 1,
            "keepalives_idle": 1,
            "keepalives_interval": 1,
            "keepalives_count": seconds,
        },
    )

    @event.listens_for(bounded, "connect")
    def _bound(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"set statement_timeout = {ms}")
        dbapi_connection.commit()

    sessions = sessionmaker(
        bind=bounded, autoflush=False, expire_on_commit=False, info={_BOUND: ms}
    )

    @event.listens_for(sessions, "after_begin")
    def _within(session: Session, _transaction, connection) -> None:  # type: ignore[no-untyped-def]
        # A tool that committed goes on in a new transaction: still bounded.
        if _DEADLINE in session.info:
            _set_local(connection, session.info)

    return sessions


_BOUND = "statement_timeout_ms"
_DEADLINE = "statement_deadline"
_LOWERED = "statement_timeout_lowered"


def _set_local(connection: Connection, info: dict[str, Any]) -> None:
    left = int((info[_DEADLINE] - time.monotonic()) * 1000)
    # Never 0, which Postgres reads as no bound at all.
    ms = max(1, min(info[_BOUND], left))
    if ms < info[_BOUND]:
        connection.exec_driver_sql(f"set local statement_timeout = {ms}")
        info[_LOWERED] = True


@contextmanager
def bounded_by(session: Session, seconds_left: float | None) -> Iterator[None]:
    """The statements inside wait at most `seconds_left`, a wait on a lock
    included, and never longer than the session's own bound
    (`bounded_sessions`): a guest whose turn has 0.3 s left is not kept on the
    line for 2 s (Codex on PR #298). Past it the statement fails like at the
    bound, and the caller's rollback (`telephony/handler.py`) undoes the bound
    with the transaction.

    Transaction-local: a commit or rollback inside gives the next transaction
    the time still left, and the bound is lifted again on the way out. A
    session without a bound of its own (the HTTP tools, the text phone, the
    evals) and a turn without a deadline stay as they are; so does a turn with
    more time left than the bound, which costs no statement then."""
    if seconds_left is None or _BOUND not in session.info:
        yield
        return
    session.info[_DEADLINE] = time.monotonic() + seconds_left
    try:
        # Without a transaction yet, its begin sets the bound (`_within`).
        if session.in_transaction():
            _set_local(session.connection(), session.info)
        yield
    finally:
        del session.info[_DEADLINE]
        lowered = session.info.pop(_LOWERED, False)
    # Only after a clean exit: a failed transaction takes no statement, and
    # the rollback that follows it undoes the bound anyway.
    if lowered and session.in_transaction():
        session.connection().exec_driver_sql(
            f"set local statement_timeout = {session.info[_BOUND]}"
        )


def get_db() -> Generator[Session]:
    """FastAPI-Dependency. Commit entscheidet die Fachlogik, Rollback und Close passieren hier."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
