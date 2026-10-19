"""sim/cli.py: Eingabeschleife im Terminal, ohne Datenbank geprueft."""

import random

import pytest

from sim.cli import _converse, build_parser
from sim.session import Turn, render_turn


class FakeCall:
    """Nimmt Kundenzuege entgegen und endet, wenn der vorbereitete Vorrat leer ist."""

    def __init__(self, enden_nach: int | None = None):
        self.gehoert: list[str] = []
        self._enden_nach = enden_nach

    def say(self, text: str) -> Turn:
        self.gehoert.append(text)
        ende = self._enden_nach is not None and len(self.gehoert) >= self._enden_nach
        return Turn(customer=text, say=["Antwort"], ended=ende)


def eingaben(monkeypatch, zeilen):
    it = iter(zeilen)

    def fake_input(_prompt=""):
        try:
            return next(it)
        except StopIteration as exc:
            raise EOFError from exc

    monkeypatch.setattr("builtins.input", fake_input)


@pytest.mark.parametrize("befehl", [":quit", ":ende", "q", ":Q"])
def test_abbruchbefehle_beenden_das_gespraech(monkeypatch, capsys, befehl):
    eingaben(monkeypatch, ["Guten Tag", befehl, "Noch ein Satz"])
    call = FakeCall()

    _converse(call, noise=0.0, rng=random.Random(0))

    assert call.gehoert == ["Guten Tag"]
    capsys.readouterr()


def test_leere_zeile_wird_uebersprungen(monkeypatch, capsys):
    eingaben(monkeypatch, ["", "  ", "Guten Tag"])
    call = FakeCall()

    _converse(call, noise=0.0, rng=random.Random(0))

    assert call.gehoert == ["Guten Tag"]
    capsys.readouterr()


def test_beendeter_zug_haelt_die_schleife_an(monkeypatch, capsys):
    """Nach der Verabschiedung wird der Kunde nicht noch einmal angesprochen."""
    eingaben(monkeypatch, ["Guten Tag", "Hallo?"])
    call = FakeCall(enden_nach=1)

    _converse(call, noise=0.0, rng=random.Random(0))

    assert call.gehoert == ["Guten Tag"]
    capsys.readouterr()


def test_rauschen_wird_auf_die_eingabe_angewendet(monkeypatch, capsys):
    eingaben(monkeypatch, ["Reservierung fuer vier Personen"])
    call = FakeCall(enden_nach=1)

    _converse(call, noise=1.0, rng=random.Random(2))

    assert call.gehoert[0] != "Reservierung fuer vier Personen"
    capsys.readouterr()


def test_standardwerte_der_schalter():
    args = build_parser().parse_args([])

    assert args.noise == 0.0
    assert args.tenant is None
    assert args.now is None


def test_fehlgeschlagener_tool_aufruf_steht_in_der_ausgabe():
    turn = Turn(
        customer="Guten Tag",
        say=["Das hat nicht geklappt."],
        tools=[
            {
                "name": "check_slot",
                "ok": False,
                "duration_ms": 12.0,
                "error_code": "conflict",
            }
        ],
        state={"stage": "start"},
    )

    zeilen = render_turn(turn).splitlines()

    assert zeilen[0] == "Kunde: Guten Tag"
    assert zeilen[1] == "  tool check_slot fehler 12.0 ms [conflict]"
    assert zeilen[2] == "Agent: Das hat nicht geklappt."
