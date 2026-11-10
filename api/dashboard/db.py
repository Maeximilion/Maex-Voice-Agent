"""The operator database: one engine per process, one session per request."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from api.dashboard.config import settings

# As api/db.py: READ COMMITTED for the row locks of the sign-in, no bound
# values in error messages (they hold hashes and addresses).
engine = create_engine(
    settings.operator_database_url,
    pool_pre_ping=True,
    isolation_level="READ COMMITTED",
    hide_parameters=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
