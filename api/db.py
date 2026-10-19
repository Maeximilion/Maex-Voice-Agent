"""Datenbankzugang: eine Engine je Prozess, eine Session je Request."""

from collections.abc import Iterator

from sqlalchemy import create_engine
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
engine = create_engine(
    settings.database_url, pool_pre_ping=True, isolation_level="READ COMMITTED"
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


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
