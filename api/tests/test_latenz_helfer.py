"""Latenz-Helfer p95_ms: Messreihen, Abbruch unter Budget, echtes Überschreiten bleibt rot."""

import pytest

from api.tests.conftest import LATENZ_BUDGET_MS, p95_ms


class FakeClock:
    """Uhr ohne Wartezeit: jeder Aufruf dauert so lange, wie die Liste vorgibt."""

    def __init__(self, durations_ms: list[float]):
        self.durations = iter(durations_ms)
        self.now = 0.0
        self.calls = 0

    def clock(self) -> float:
        return self.now

    def call(self) -> None:
        self.calls += 1
        self.now += next(self.durations) / 1000


def test_schnelle_erste_reihe_misst_nur_einmal():
    fake = FakeClock([10.0] * 20)

    p95 = p95_ms(fake.call, n=20, clock=fake.clock)

    assert p95 < LATENZ_BUDGET_MS
    assert fake.calls == 20


def test_ausreisser_in_erster_reihe_kippt_den_test_nicht():
    """Nachgestellt: der CI-Fall mit p95 560 ms bei sonst schnellen Aufrufen."""
    slow_round = [10.0] * 18 + [560.0, 560.0]
    fake = FakeClock(slow_round + [12.0] * 20)

    p95 = p95_ms(fake.call, n=20, clock=fake.clock)

    assert p95 < LATENZ_BUDGET_MS
    assert fake.calls == 40


def test_echtes_ueberschreiten_bleibt_ueber_dem_budget():
    fake = FakeClock([400.0] * 60)

    p95 = p95_ms(fake.call, n=20, clock=fake.clock)

    assert p95 >= LATENZ_BUDGET_MS
    assert fake.calls == 60


def test_zeitweises_ueberschreiten_bleibt_rot():
    """Codex-Review PR #110: 40 langsame, dann 20 schnelle Aufrufe dürfen nicht durch."""
    fake = FakeClock([400.0] * 40 + [10.0] * 20)

    p95 = p95_ms(fake.call, n=20, clock=fake.clock)

    assert p95 == pytest.approx(400.0)
    assert fake.calls == 60


def test_p95_gilt_ueber_alle_reihen():
    """Vier Ausreißer in 60 Aufrufen sind mehr als 5 Prozent: rot."""
    fake = FakeClock([10.0] * 16 + [560.0] * 4 + [10.0] * 40)

    assert p95_ms(fake.call, n=20, clock=fake.clock) == pytest.approx(560.0)
    assert fake.calls == 60
