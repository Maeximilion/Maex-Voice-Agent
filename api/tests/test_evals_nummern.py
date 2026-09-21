"""Der Eval-Satz zur Nummernerkennung, gefahren von pytest (evals/number_eval.py).

Dieselbe Datei, zwei Wege: `python -m evals.number_eval` zeigt beim Arbeiten
die Tabelle, dieser Test haelt den Satz in CI gruen. Ein Fall steht damit an
genau einer Stelle - `evals/cases/nummern.jsonl` - und nicht doppelt gepflegt
in einer Testdatei daneben.

Warum ueberhaupt neben den Unit-Tests in test_domain_numberwords.py: die Regeln
fuer Marker, Kartenendung, Menge und Verbindungswort greifen ineinander. Auf
PR #117 haben zwei Korrekturen in Folge je eine frueher richtige Form wieder
kaputt gemacht, weil jede fuer sich stimmte. Eine Tabelle aller bekannten
Saetze zeigt das sofort, einzelne Testfunktionen nicht.
"""

import pytest

from evals.number_eval import load, resolve

CASES = load()


def test_faelle_vorhanden():
    """Eine leere oder verlegte Datei darf nicht als gruener Lauf durchgehen."""
    assert len(CASES) > 50


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.say)
def test_nummernerkennung(case):
    assert resolve(case.say) == case.expect, case.why
