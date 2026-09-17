"""Gemeinsame Fixtures: Wegwerf-Datenbanken, damit die Entwicklungsdaten unberührt bleiben."""

import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from api.config import settings

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "db" / "alembic.ini"


def p95_ms(call: Callable[[], object], n: int = 20) -> float:
    """Latenz-Helfer für Tools im heißen Pfad: p95 über n Aufrufe, Budget 300 ms (docs/04 §1)."""
    samples = []
    for _ in range(n):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return samples[min(n - 1, round(0.95 * n) - 1)]


def alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def create_scratch_db() -> str:
    base_url = make_url(settings.database_url)
    name = f"maex_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    # str(URL) maskiert das Passwort als "***", deshalb explizit rendern.
    return base_url.set(database=name).render_as_string(hide_password=False)


def drop_scratch_db(url: str) -> None:
    scratch = make_url(url)
    admin = create_engine(make_url(settings.database_url), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{scratch.database}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope="module")
def scratch_db_url():
    """Leere Datenbank ohne Schema, für Migrationstests."""
    url = create_scratch_db()
    try:
        yield url
    finally:
        drop_scratch_db(url)


@pytest.fixture
def migrated_db_url():
    """Frische Datenbank auf dem aktuellen Schema, je Test eine eigene."""
    url = create_scratch_db()
    try:
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        drop_scratch_db(url)
