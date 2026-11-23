"""The start refuses a Postgres superuser outside dev and test (#252, #827 F5).

Apart from `api/db.py` so the operator dashboard (`api/dashboard/`) can use it:
importing `api.db` builds the customer stack's settings and connection.
"""

from typing import Any

from sqlalchemy import Engine, event, text
from sqlalchemy.exc import SQLAlchemyError

from api.core.logging import get_logger
from api.core.tokens import LOCAL_ENVS

logger = get_logger("api.core.database_role")

# session_user, the role that logged in: a URL with `options=-c role=...` makes
# current_user a plain role while the session can still go back to the
# superuser it authenticated as.
IS_SUPERUSER = "select rolsuper from pg_roles where rolname = session_user"


def _refusal(variable: str) -> RuntimeError:
    return RuntimeError(
        f"{variable} connects as a Postgres superuser; give the application "
        "its own role (bash scripts/db_role.sh, docs/13 §3)"
    )


def refuse_a_superuser_role(
    env: str, db: Engine, variable: str = "DATABASE_URL"
) -> None:
    """Outside dev and test the application does not start as a Postgres superuser.

    The application reads and writes rows. A superuser can also drop every
    table, read files of the server and run programs there, so a flaw in the
    application would reach all of that (#252). On the server the URL in
    `variable` names a role of its own, set up by scripts/db_role.sh, and the
    owner of the schema stays with migrations, backup and restore (docs/13 §3).

    A database that cannot be asked does not stop the start: it could not be
    used either, and a process that stays up connects again once it answers.
    The check then runs on each new connection until one passes, so a
    superuser is still refused once the database answers (#888 C3).
    """
    if env in LOCAL_ENVS:
        return
    try:
        with db.connect() as conn:
            superuser = conn.scalar(text(IS_SUPERUSER))
    except SQLAlchemyError:
        logger.warning("the role of %s could not be checked at start", variable)
        _check_on_connect(db, variable)
        return
    if superuser:
        raise _refusal(variable)


def _check_on_connect(db: Engine, variable: str) -> None:
    """Ask each new connection of `db` until one answers as no superuser; a
    superuser's connection is refused every time, so nothing runs on it."""
    checked = False

    def check(dbapi_connection: Any, _record: Any) -> None:
        nonlocal checked
        if checked:
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(IS_SUPERUSER)
            row = cursor.fetchone()
        finally:
            cursor.close()
            dbapi_connection.rollback()
        if row and row[0]:
            raise _refusal(variable)
        checked = True

    event.listen(db, "connect", check)
