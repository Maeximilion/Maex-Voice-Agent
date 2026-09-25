"""Gemeinsame Fixtures: Wegwerf-Datenbanken, damit die Entwicklungsdaten unberührt bleiben."""

import time
from collections.abc import Callable

import pytest

# Von hier importieren die Migrationstests weiterhin alembic_config.
from evals.scratch_db import (  # noqa: F401
    ALEMBIC_INI,
    alembic_config,
    create_scratch_db,
    drop_scratch_db,
    migrate,
)

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
        migrate(url)
        yield url
    finally:
        drop_scratch_db(url)
