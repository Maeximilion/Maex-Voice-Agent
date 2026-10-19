"""Tests fuer Assignee und Label der Pull Requests (scripts/pr_metadata.py).

Geprueft wird die Ableitung ohne Netz: welches Label aus dem Titel folgt und was
an einem Pull Request fehlt.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULPFAD = Path(__file__).resolve().parents[2] / "scripts" / "pr_metadata.py"
_spec = importlib.util.spec_from_file_location("pr_metadata", _MODULPFAD)
assert _spec and _spec.loader
pr_metadata = importlib.util.module_from_spec(_spec)
sys.modules["pr_metadata"] = pr_metadata
_spec.loader.exec_module(pr_metadata)

EIGENTUEMER = "Maeximilion"


def pull(titel: str, *, labels=(), assignees=()) -> dict:
    return {
        "number": 1,
        "title": titel,
        "labels": [{"name": name} for name in labels],
        "assignees": [{"login": login} for login in assignees],
    }


@pytest.mark.parametrize(
    ("titel", "label"),
    [
        ("feat(project): etwas Neues", "feature"),
        ("fix(gui): Knopf repariert", "bug"),
        ("docs(status): Stand nachgezogen", "docs"),
        ("chore(ci): Aktion aktualisiert", "chore"),
        ("refactor: Logger vereinfacht", "refactor"),
        ("test(evals): neue Faelle", "test"),
        ("ci: Cache", "chore"),
        ("feat!: bricht die Schnittstelle", "feature"),
    ],
)
def test_label_aus_conventional_commit(titel: str, label: str) -> None:
    assert pr_metadata.derive_label(titel) == label


@pytest.mark.parametrize(
    "titel",
    [
        "Merge pull request #119 from Maeximilion/fix/code-health",
        "Refactor configure_logging loop",  # kein Doppelpunkt-Praefix
        "wip: irgendwas",  # unbekannter Typ
    ],
)
def test_ohne_erkennbaren_typ_kein_label(titel: str) -> None:
    """Randfall: lieber kein Label als ein geratenes."""
    assert pr_metadata.derive_label(titel) is None


def test_fehlt_beides_wird_beides_gesetzt() -> None:
    aenderung = pr_metadata.missing(pull("feat(x): y"), EIGENTUEMER)
    assert aenderung == {"assignees": [EIGENTUEMER], "labels": ["feature"]}


def test_vorhandenes_label_bleibt_unangetastet() -> None:
    """Ein Dependabot-PR traegt schon 'dependencies' - kein zweites Label dazuerfinden."""
    aenderung = pr_metadata.missing(
        pull("chore(deps): bump x", labels=["dependencies"]), EIGENTUEMER
    )
    assert aenderung == {"assignees": [EIGENTUEMER]}


def test_vollstaendiger_pr_braucht_nichts() -> None:
    """Idempotent: ein zweiter Lauf aendert nichts."""
    aenderung = pr_metadata.missing(
        pull("feat(x): y", labels=["feature"], assignees=[EIGENTUEMER]), EIGENTUEMER
    )
    assert aenderung == {}


def test_ohne_ableitbares_label_nur_assignee() -> None:
    aenderung = pr_metadata.missing(pull("Merge pull request #1"), EIGENTUEMER)
    assert aenderung == {"assignees": [EIGENTUEMER]}


def test_jedes_abgeleitete_label_existiert_im_repo() -> None:
    """Die Zuordnung darf nur auf Labels zeigen, die es gibt - sonst legt GitHub beim
    Setzen stillschweigend ein neues, graues Label an."""
    vorhanden = {"feature", "bug", "docs", "chore", "refactor", "test"}
    assert set(pr_metadata.TYPE_LABELS.values()) <= vorhanden
