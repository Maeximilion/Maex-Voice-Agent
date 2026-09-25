"""Wegwerf-Datenbanken auf dem Server aus DATABASE_URL: für Tests und Eval-Läufe.

Beide dürfen die Entwicklungs- oder Betriebsdaten nie berühren. Eine eigene
Datenbank je Lauf, frisch migriert, danach gelöscht. Früher in
`api/tests/conftest.py`; hier, damit `evals/runner.py` denselben Weg nimmt, ohne
aus dem Testpaket zu importieren.
"""

import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from api.config import settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "db" / "alembic.ini"


def alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def create_scratch_db(prefix: str = "maex_test") -> str:
    base_url = make_url(settings.database_url)
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
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
