"""Datenbankzugang: eine Engine je Prozess, eine Session je Request."""

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from api.config import LOCAL_ENVS, settings
from api.core.logging import get_logger

logger = get_logger("api.db")

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
# hide_parameters: a failed statement is logged with its SQL, never with its
# values. Those are guest names, phone numbers and notes, and a log line is
# outside every deletion period of docs/03.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    isolation_level="READ COMMITTED",
    hide_parameters=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def refuse_a_superuser_role(env: str, db: Engine) -> None:
    """Outside dev and test the application does not start as a Postgres superuser.

    The application reads and writes rows. A superuser can also drop every
    table, read files of the server and run programs there, so a flaw in the
    application would reach all of that (#252). On the server DATABASE_URL names
    a role of its own, set up by scripts/db_role.sh, and the owner of the schema
    stays with migrations, backup and restore (docs/13 §3).

    A database that cannot be asked does not stop the start: it could not be
    used either, and a process that stays up connects again once it answers.
    """
    if env in LOCAL_ENVS:
        return
    try:
        with db.connect() as conn:
            # session_user, the role that logged in: a URL with
            # `options=-c role=...` makes current_user a plain role while the
            # session can still go back to the superuser it authenticated as.
            superuser = conn.scalar(
                text("select rolsuper from pg_roles where rolname = session_user")
            )
    except SQLAlchemyError:
        logger.warning("the role of DATABASE_URL could not be checked at start")
        return
    if superuser:
        raise RuntimeError(
            "DATABASE_URL connects as a Postgres superuser; give the application "
            "its own role (bash scripts/db_role.sh, docs/13 §3)"
        )


def get_db() -> Iterator[Session]:
    """FastAPI-Dependency. Commit entscheidet die Fachlogik, Rollback und Close passieren hier."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
