"""Datenbankzugang: eine Engine je Prozess, eine Session je Request."""

from collections.abc import Generator

from sqlalchemy import create_engine, event
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

    return sessionmaker(bind=bounded, autoflush=False, expire_on_commit=False)


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
