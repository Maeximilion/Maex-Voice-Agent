"""Tests fuer das Entscheidungs-Issue der Projektpflege (scripts/decision_issue.py).

Geprueft wird plan(): die ganze Entscheidung, was mit dem Issue passiert, ohne Netz.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULPFAD = Path(__file__).resolve().parents[2] / "scripts" / "decision_issue.py"
_spec = importlib.util.spec_from_file_location("decision_issue", _MODULPFAD)
assert _spec and _spec.loader
decision_issue = importlib.util.module_from_spec(_spec)
sys.modules["decision_issue"] = decision_issue
_spec.loader.exec_module(decision_issue)

WER = "@Maeximilion"
PR_82 = {"label": "#82 bump python", "url": "https://github.com/o/r/pull/82"}
PR_90 = {"label": "#90 anderer PR", "url": "https://github.com/o/r/pull/90"}
OHNE_MERGE = "Pull Request ohne Merge geschlossen"


def test_nichts_offen_und_kein_issue_tut_nichts() -> None:
    assert decision_issue.plan(None, {}, WER).action == "none"


def test_erster_befund_legt_issue_mit_erwaehnung_an() -> None:
    """Normalfall: der erste offene Punkt erreicht Maxi, darum die Erwaehnung im Text."""
    ergebnis = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    assert ergebnis.action == "create"
    assert ergebnis.body.startswith(WER)
    assert "[#82 bump python](https://github.com/o/r/pull/82)" in ergebnis.body


def test_gleicher_befund_am_naechsten_tag_erwaehnt_nicht_erneut() -> None:
    """Der Kern: wer taeglich fuer dasselbe erwaehnt wird, liest bald gar nichts mehr."""
    angelegt = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    folgetag = decision_issue.plan(angelegt.body, {OHNE_MERGE: [PR_82]}, WER)
    assert folgetag.comment is None
    assert WER not in (folgetag.body or "")


def test_unveraenderter_text_schreibt_gar_nicht() -> None:
    """Randfall: ist das Issue schon auf Stand, bleibt es unberuehrt - kein Rauschen im Verlauf."""
    angelegt = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    aktualisiert = decision_issue.plan(angelegt.body, {OHNE_MERGE: [PR_82]}, WER)
    stand = decision_issue.plan(aktualisiert.body, {OHNE_MERGE: [PR_82]}, WER)
    assert stand.action == "none"


def test_neuer_befund_erwaehnt_nur_das_neue() -> None:
    angelegt = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    ergebnis = decision_issue.plan(angelegt.body, {OHNE_MERGE: [PR_82, PR_90]}, WER)
    assert ergebnis.action == "update"
    assert ergebnis.comment.startswith(WER)
    assert "#90" in ergebnis.comment
    assert "#82" not in ergebnis.comment


def test_alles_erledigt_schliesst_das_issue() -> None:
    angelegt = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    ergebnis = decision_issue.plan(angelegt.body, {}, WER)
    assert ergebnis.action == "close"
    assert WER not in ergebnis.comment


def test_derselbe_eintrag_unter_anderem_befund_ist_neu() -> None:
    """Randfall: wechselt ein Eintrag den Befund, ist das eine neue Lage und wird gemeldet."""
    angelegt = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    ergebnis = decision_issue.plan(angelegt.body, {"Ohne Status": [PR_82]}, WER)
    assert ergebnis.comment is not None


def test_leerer_issue_text_zaehlt_als_vorhandenes_issue() -> None:
    """Randfall: body = null bei GitHub ist ein Issue ohne Text, nicht 'kein Issue'.
    Sonst legte jeder Lauf ein weiteres Issue an."""
    ergebnis = decision_issue.plan("", {OHNE_MERGE: [PR_82]}, WER)
    assert ergebnis.action == "update"
    assert ergebnis.comment is not None


def test_entwurf_ohne_url_bekommt_stabilen_schluessel() -> None:
    entwurf = {"label": "(Entwurf) Idee", "url": None}
    erster = decision_issue.finding_keys({"Ohne Status": [entwurf]})
    zweiter = decision_issue.finding_keys({"Ohne Status": [entwurf]})
    assert erster == zweiter
    assert len(erster) == 1


def test_schluessel_ueberleben_den_weg_durch_den_issue_text() -> None:
    befunde = {OHNE_MERGE: [PR_82, PR_90]}
    text = decision_issue.render_body(befunde, "Einleitung")
    assert decision_issue.parse_keys(text) == set(decision_issue.finding_keys(befunde))
