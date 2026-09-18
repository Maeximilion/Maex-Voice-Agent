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


LATENZ_BUDGET_MS = 300
LATENZ_RUNDEN = 3


def p95_ms(
    call: Callable[[], object],
    n: int = 20,
    rounds: int = LATENZ_RUNDEN,
    budget_ms: float = LATENZ_BUDGET_MS,
    clock: Callable[[], float] = time.perf_counter,
) -> float:
    """Latenz-Helfer für Tools im heißen Pfad: p95 über n Aufrufe, Budget 300 ms (docs/04 §1).

    Bis zu `rounds` Messreihen. p95 gilt immer über alle bisher gemessenen Aufrufe,
    keine Reihe wird verworfen; liegt es unter dem Budget, ist Schluss. Wenige
    Ausreißer durch Last auf einem geteilten CI-Runner verteilen sich so auf mehr
    Stichproben (bei 60 Aufrufen bis zu 3), ein Überschreiten auch nur in jedem
    dritten Aufruf bleibt rot. Das Budget selbst wird nicht gelockert.
    """
    samples: list[float] = []
    for _ in range(rounds):
        for _ in range(n):
            started = clock()
            call()
            samples.append((clock() - started) * 1000)
        ordered = sorted(samples)
        p95 = ordered[min(len(ordered) - 1, round(0.95 * len(ordered)) - 1)]
        if p95 < budget_ms:
            break
    return p95


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
