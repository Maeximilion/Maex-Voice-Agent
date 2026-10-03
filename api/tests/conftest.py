"""Gemeinsame Fixtures: Wegwerf-Datenbanken, damit die Entwicklungsdaten unberührt bleiben."""

import os
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

# Gesetzt von _latenz_messung, solange ein Test mit @pytest.mark.latency läuft.
_latenz_test_aktiv = False


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "latency: misst das Latenzbudget mit echter Uhr; läuft in CI seriell, "
        "nicht unter xdist (docs/13 §6)",
    )


@pytest.fixture(autouse=True)
def _latenz_messung(request):
    """Latenztests messen nur seriell: unter xdist-Workern messen sie die Last."""
    global _latenz_test_aktiv
    if request.node.get_closest_marker("latency") is None:
        yield
        return
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.skip("Latenz misst nur seriell: pytest -m latency")
    _latenz_test_aktiv = True
    try:
        yield
    finally:
        _latenz_test_aktiv = False


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

    Mit echter Uhr nur in Tests mit @pytest.mark.latency; die laufen in CI seriell
    nach der parallelen Suite, sonst misst p95 die Konkurrenz der xdist-Worker.
    """
    # Jede Uhr aus dem Modul time ist echt (perf_counter, monotonic, ...); Fakes nicht.
    if getattr(clock, "__module__", None) == "time" and not _latenz_test_aktiv:
        pytest.fail(
            "p95_ms mit echter Uhr braucht @pytest.mark.latency am Test",
            pytrace=False,
        )
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


@pytest.fixture(scope="session")
def migrated_template_url():
    """Einmal je Testlauf migriert; jeder Test bekommt davon eine Kopie."""
    url = create_scratch_db(prefix="maex_tmpl")
    try:
        migrate(url)
        yield url
    finally:
        drop_scratch_db(url)


@pytest.fixture
def migrated_db_url(migrated_template_url):
    """Frische Datenbank auf dem aktuellen Schema, je Test eine eigene."""
    url = create_scratch_db(template=migrated_template_url)
    try:
        yield url
    finally:
        drop_scratch_db(url)
