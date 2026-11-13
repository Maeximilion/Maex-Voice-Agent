"""Gemeinsame Fixtures: Wegwerf-Datenbanken, damit die Entwicklungsdaten unberührt bleiben."""

import os
import time
import uuid
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from alembic import command

from api.models import ServiceConfig

# Von hier importieren die Migrationstests weiterhin alembic_config.
from evals.scratch_db import (  # noqa: F401
    ALEMBIC_INI,
    alembic_config,
    create_scratch_db,
    drop_scratch_db,
    empty_scratch_db,
    migrate,
    scratch_db_state,
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
    config.addinivalue_line(
        "markers",
        "gui_login: the real sign-in of the operations view, not the signed-in "
        "device every other test acts as (#570)",
    )


def in_shard(nodeid: str, spec: str) -> bool:
    """`spec` "i/n" (0 <= i < n): true for about one test id in n, the same
    one on every run and every xdist worker, so n jobs together run each test once."""
    index, total = (int(part) for part in spec.split("/"))
    if not 0 <= index < total:
        raise ValueError(f"PYTEST_SHARD {spec!r}: want i/n with 0 <= i < n")
    return zlib.crc32(nodeid.encode()) % total == index


def pytest_collection_modifyitems(config, items):
    """CI splits the suite over parallel jobs with `PYTEST_SHARD` (#609)."""
    spec = os.environ.get("PYTEST_SHARD")
    if not spec:
        return
    keep: list[pytest.Item] = []
    drop: list[pytest.Item] = []
    for item in items:
        (keep if in_shard(item.nodeid, spec) else drop).append(item)
    config.hook.pytest_deselected(items=drop)
    items[:] = keep


@pytest.fixture(autouse=True)
def _gui_signed_in(request):
    """Every test of the operations view acts as a signed-in, unlocked device.

    The login itself is tested with the marker `gui_login`
    (test_gui_login.py), including that every route refuses without it."""
    if request.node.get_closest_marker("gui_login"):
        yield
        return
    from api.domain.access.sessions import Access
    from api.gui.session import require_admin, require_gui_session
    from api.main import app

    app.dependency_overrides[require_gui_session] = lambda: None
    # With the admin PIN, too; no real device has this id.
    app.dependency_overrides[require_admin] = lambda: Access(
        uuid.UUID(int=0), locked=False, admin=True
    )
    yield
    app.dependency_overrides.pop(require_gui_session, None)
    app.dependency_overrides.pop(require_admin, None)


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


@contextmanager
def scratch_db_at(revision: str) -> Iterator[str]:
    """Eigene Wegwerf-Datenbank, auf `revision` migriert, danach gelöscht."""
    url = create_scratch_db()
    try:
        command.upgrade(alembic_config(url), revision)
        yield url
    finally:
        drop_scratch_db(url)


@pytest.fixture
def scratch_db_url():
    """Leere Datenbank ohne Schema, für Migrationstests; je Test eine eigene.

    Je Test, nicht je Modul: unter pytest-xdist landen die Tests einer Datei auf
    verschiedenen Workern, und ein Test, der auf der Revision eines vorigen
    aufbaut, sieht dort eine leere Datenbank. Umgekehrt ist `upgrade` auf eine
    ältere Revision still ein No-op, ein Upgrade-Test prüfte dann nichts.
    """
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
def bounded_db(migrated_db_url):
    """`db.bounded_sessions` on the test's database, by bound in ms; every
    engine it made is disposed after the test."""
    from api.db import bounded_sessions

    made = []

    def at(ms: int):
        made.append(bounded_sessions(ms, url=migrated_db_url))
        return made[-1]

    yield at
    for sessions in made:
        sessions.kw["bind"].dispose()


@pytest.fixture(scope="session")
def _reused_db(migrated_template_url):
    """One copy of the template per test process (xdist worker), emptied after each
    test instead of dropped: the copy and the drop were half the time of a test.
    A list, so `migrated_db_url` can swap in a new copy."""
    held = [scratch_db_state(create_scratch_db(template=migrated_template_url))]
    yield held
    drop_scratch_db(held[0].url)


@pytest.fixture
def migrated_db_url(_reused_db, migrated_template_url):
    """A database on the current schema, fresh for every test.

    Each test sees the state of a fresh copy of the template. A test that left
    more than rows behind (a table, a grant, a setting) gets its database dropped
    and the next test a new copy (`empty_scratch_db`)."""
    yield _reused_db[0].url
    if not empty_scratch_db(_reused_db[0]):
        drop_scratch_db(_reused_db[0].url)
        _reused_db[0] = scratch_db_state(
            create_scratch_db(template=migrated_template_url)
        )


def lift_party_limits(session, tenant_id) -> None:
    """For tests about capacity, which fill a window with one large party. What
    the agent books itself is limited (#252, `service_config`); those limits
    have tests of their own (`test_domain_slots.py`)."""
    config = session.get(ServiceConfig, tenant_id)
    config.reservation_max_party_size = 1000
    config.reservation_max_party_size_same_day = 1000
    session.commit()
