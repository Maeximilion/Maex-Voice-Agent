"""Wegwerf-Datenbanken auf dem Server aus DATABASE_URL: für Tests und Eval-Läufe.

Beide dürfen die Entwicklungs- oder Betriebsdaten nie berühren. Eine eigene
Datenbank je Lauf, frisch migriert, danach gelöscht. Früher in
`api/tests/conftest.py`; hier, damit `evals/runner.py` denselben Weg nimmt, ohne
aus dem Testpaket zu importieren.
"""

import uuid
from pathlib import Path
from typing import NamedTuple

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from api.config import settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "db" / "alembic.ini"


def alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def create_scratch_db(prefix: str = "maex_test", template: str | None = None) -> str:
    """Neue leere Datenbank, oder eine Kopie von `template` (URL einer migrierten DB).

    Die Kopie per TEMPLATE dauert Millisekunden, Alembic von 001 an fast eine halbe
    Sekunde; das war rund 90 % der Testzeit (CI-Analyse 03.10.2026).
    """
    base_url = make_url(settings.database_url)
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    copy = f' TEMPLATE "{make_url(template).database}"' if template else ""
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"{copy}'))
    admin.dispose()
    # str(URL) maskiert das Passwort als "***", deshalb explizit rendern.
    return base_url.set(database=name).render_as_string(hide_password=False)


def migrate(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def drop_scratch_db(url: str) -> None:
    scratch = make_url(url)
    admin = create_engine(make_url(settings.database_url), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{scratch.database}" WITH (FORCE)'))
    admin.dispose()


# Everything a test can leave in its database besides table rows: the objects it
# created, dropped, altered or granted (oids >= 16384 are the database's own),
# the database's settings and grants, and the rows of the tables the template
# already filled. Volatile columns (sizes, statistics, xids) stay out.
_DIGEST = """
SELECT md5(string_agg(line, E'\\n' ORDER BY line COLLATE "C")) FROM (
  SELECT concat_ws(' ', 'class', oid, relname, relnamespace, relkind, relowner, relacl,
                   reloptions, relpersistence, relrowsecurity, relforcerowsecurity,
                   relreplident, relispartition) AS line
    FROM pg_class WHERE oid >= 16384
  UNION ALL SELECT 'att ' || a::text FROM pg_attribute a WHERE attrelid >= 16384
  UNION ALL SELECT 'dep ' || d::text FROM pg_depend d WHERE objid >= 16384
  UNION ALL SELECT 'con ' || c::text FROM pg_constraint c WHERE oid >= 16384
  UNION ALL SELECT 'trg ' || t::text FROM pg_trigger t WHERE oid >= 16384
  UNION ALL SELECT 'idx ' || i::text FROM pg_index i WHERE indexrelid >= 16384
  UNION ALL SELECT 'def ' || d::text FROM pg_attrdef d WHERE oid >= 16384
  UNION ALL SELECT 'proc ' || p::text FROM pg_proc p WHERE oid >= 16384
  UNION ALL SELECT 'type ' || t::text FROM pg_type t WHERE oid >= 16384
  UNION ALL SELECT 'seq ' || s::text FROM pg_sequence s WHERE seqrelid >= 16384
  UNION ALL SELECT 'rule ' || r::text FROM pg_rewrite r WHERE oid >= 16384
  UNION ALL SELECT 'pol ' || p::text FROM pg_policy p
  UNION ALL SELECT 'dacl ' || d::text FROM pg_default_acl d
  UNION ALL SELECT 'desc ' || d::text FROM pg_description d WHERE objoid >= 16384
  UNION ALL SELECT 'ext ' || e::text FROM pg_extension e
  UNION ALL SELECT 'evt ' || e::text FROM pg_event_trigger e
  UNION ALL SELECT 'lo ' || l::text FROM pg_largeobject_metadata l
  UNION ALL SELECT 'xact ' || x.gid FROM pg_prepared_xacts x
    WHERE x.database = current_database()
  UNION ALL SELECT concat_ws(' ', 'nsp', oid, nspname, nspowner, nspacl)
    FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_temp\\_%' AND nspname NOT LIKE 'pg\\_toast\\_temp\\_%'
  UNION ALL SELECT concat_ws(' ', 'db', datdba, datacl, datconnlimit, datallowconn, datistemplate)
    FROM pg_database WHERE datname = current_database()
  UNION ALL SELECT 'set ' || s.setrole || ' ' || s.setconfig::text FROM pg_db_role_setting s
    JOIN pg_database d ON d.oid = s.setdatabase WHERE d.datname = current_database()
  UNION ALL SELECT concat_ws(' ', 'shdep', s.classid, s.objid, s.objsubid, s.refclassid,
                             s.refobjid, s.deptype)
    FROM pg_shdepend s JOIN pg_database d ON d.oid = s.dbid
    WHERE d.datname = current_database()
) lines
"""


def _public_tables(conn) -> list[str]:
    return list(
        conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
            )
        ).scalars()
    )


class ScratchState(NamedTuple):
    """What `empty_scratch_db` compares against, read once from a fresh copy."""

    url: str
    # NullPool: no connection stays open between tests; the engine is kept only
    # because building one costs about 35 ms, the reset itself about 10.
    engine: Engine
    digest: str
    # The tables to empty. A table the template filled (alembic_version) is not
    # emptied: its rows count in the digest.
    empty: list[str]


def scratch_db_state(url: str) -> ScratchState:
    engine = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with engine.connect() as conn:
        empty = [t for t in _public_tables(conn) if not _has_rows(conn, t)]
        return ScratchState(url, engine, _digest(conn, empty), empty)


def _has_rows(conn, table: str) -> bool:
    return conn.execute(text(f'SELECT EXISTS (SELECT FROM "{table}")')).scalar_one()


def _digest(conn, empty: list[str]) -> str:
    kept = [t for t in _public_tables(conn) if t not in empty]
    rows = [
        conn.execute(
            text(
                f"SELECT md5(coalesce(string_agg(t::text, ',' ORDER BY t::text), '')) FROM \"{t}\" t"
            )
        ).scalar_one()
        for t in kept
    ]
    return conn.execute(text(_DIGEST)).scalar_one() + ",".join(kept) + ",".join(rows)


def empty_scratch_db(state: ScratchState) -> bool:
    """Empties a scratch database after a test, about 10 ms against about 100 ms for
    dropping it and copying the template again (measured 13.11.2026).

    True when the database is back in the state of a fresh copy. False when the
    test left anything but rows (a table, a grant, a setting, a role's default,
    a lock that does not let go): the caller then drops it and makes a new one.
    """
    try:
        with state.engine.connect() as conn:
            # Connections the test left open die, as with DROP DATABASE WITH (FORCE).
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            )
            if _digest(conn, state.empty) != state.digest:
                return False
            # One transaction, sent as one string: the engine runs in autocommit, so
            # SET LOCAL needs its own BEGIN. The lock waits until no transaction of
            # those connections still holds a write it could commit after the
            # deletes. Without foreign key checks and triggers, since every table is
            # emptied anyway. setval, not ALTER SEQUENCE RESTART, which gives the
            # sequence a new file and costs about 20 ms.
            tables = ", ".join(f'"{table}"' for table in state.empty)
            statements = [
                "BEGIN",
                "SET LOCAL lock_timeout = '5s'",
                "SET LOCAL session_replication_role = replica",
                f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE",
                *(f'DELETE FROM "{table}"' for table in state.empty),
                "SELECT setval((quote_ident(schemaname) || '.' || quote_ident(sequencename))"
                "::regclass, start_value, false) FROM pg_sequences WHERE schemaname = 'public'",
                "COMMIT",
            ]
            conn.exec_driver_sql("; ".join(statements))
        return True
    except DBAPIError:
        # Not allowed (a role without superuser) or a lock held past the timeout.
        return False
