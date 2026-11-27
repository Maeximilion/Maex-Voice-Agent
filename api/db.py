"""Datenbankzugang: eine Engine je Prozess, eine Session je Request."""

import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from psycopg import pq
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from api.config import settings
from api.core.errors import SAY_ON_FAILURE, OutOfTime

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
        # A pool with no free connection: the line's bound, not the pool's
        # 30 s; the phone line hands the call to the team on it.
        pool_timeout=seconds,
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

    @event.listens_for(bounded, "before_cursor_execute")
    def _within_turn(_conn, cursor, *_args) -> None:  # type: ignore[no-untyped-def]
        # Before every statement, so waits that add up stop at the deadline
        # too, and a statement after a commit inside the tool is bounded alike.
        turn = _turn.get()
        # An aborted transaction waits on nothing and takes only the rollback
        # to its savepoint, which a SET in front of it would refuse.
        if turn is None or _aborted(cursor.connection):
            return
        left = int((turn.deadline - time.monotonic()) * 1000)
        if left < ms:
            # Never 0, which Postgres reads as no bound at all.
            cursor.execute(f"set local statement_timeout = {max(1, left)}")
            turn.lowered = True

    return sessionmaker(
        bind=bounded, autoflush=False, expire_on_commit=False, info={_BOUND: ms}
    )


_BOUND = "statement_timeout_ms"
# Postgres' code for a cancelled statement; a timeout says so in its message,
# a cancel by an operator does not.
_QUERY_CANCELED = "57014"
_BY_TIMEOUT = "statement timeout"


def _aborted(dbapi_connection: Any) -> bool:
    return bool(
        dbapi_connection.info.transaction_status == pq.TransactionStatus.INERROR
    )


@dataclass
class _Turn:
    deadline: float
    lowered: bool = False


# Per thread: a call's statements on its own thread, nothing of another call.
_turn: ContextVar[_Turn | None] = ContextVar("turn_deadline", default=None)


@contextmanager
def bounded_by(session: Session, seconds_left: float | None) -> Iterator[None]:
    """The statements inside wait, all together and a wait on a lock
    included, no longer than `seconds_left`, and each no longer than the
    session's own bound (`bounded_sessions`): a guest whose turn has 0.3 s
    left is not kept on the line for 2 s (Codex on PR #298). A statement cut
    by that raises `OutOfTime`, the structured error of a tool, and the
    caller rolls back (`agent/dispatch.py`), which lifts the lowered bound
    with the transaction; on a clean exit the bound is lifted here.

    A session without a bound of its own (the HTTP tools, the text phone, the
    evals) and a turn without a deadline stay as they are; so does a turn with
    more time left than the bound, which costs no statement then."""
    if seconds_left is None or _BOUND not in session.info:
        yield
        return
    turn = _Turn(time.monotonic() + seconds_left)
    token = _turn.set(turn)
    try:
        yield
    except OperationalError as exc:
        _out_of_time(turn, exc)
        raise
    finally:
        _turn.reset(token)
    # A read leaves its transaction open with the lowered bound: the next
    # statements of the call get the line's bound again. This statement runs
    # under the lowered bound itself, and may be cut by it like the others.
    if (
        turn.lowered
        and session.in_transaction()
        and not _aborted(session.connection().connection.dbapi_connection)
    ):
        try:
            session.connection().exec_driver_sql(
                f"set local statement_timeout = {session.info[_BOUND]}"
            )
        except OperationalError as exc:
            _out_of_time(turn, exc)
            raise


def _out_of_time(turn: _Turn, exc: OperationalError) -> None:
    """A statement the lowered bound cancelled raises the tool's `OutOfTime`;
    any other failure stays what it was, for the handler's outage path."""
    cause = exc.orig
    if (
        turn.lowered
        and getattr(cause, "sqlstate", None) == _QUERY_CANCELED
        and _BY_TIMEOUT in str(cause)
    ):
        raise OutOfTime(
            "a statement ran into the turn's deadline", say=SAY_ON_FAILURE
        ) from exc


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
