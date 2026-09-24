"""Tests fuer das Entscheidungs-Issue der Projektpflege (scripts/decision_issue.py).

Geprueft wird plan(): die ganze Entscheidung, was mit dem Issue passiert, ohne Netz.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

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


# Codex-Review PR #134


def test_geschlossenes_issue_wird_wieder_geoeffnet_statt_neu_angelegt() -> None:
    """Sonst sammelt jeder Zyklus aus 'erledigt' und 'wieder etwas offen' ein Issue mehr."""
    angelegt = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    ergebnis = decision_issue.plan(
        angelegt.body, {OHNE_MERGE: [PR_82]}, WER, closed=True
    )
    assert ergebnis.action == "reopen"


def test_wiedereroeffnung_erwaehnt_alles_auch_bekannte_punkte() -> None:
    """Nach dem Schliessen galt alles als erledigt - was wiederkommt, ist wieder neu."""
    angelegt = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    ergebnis = decision_issue.plan(
        angelegt.body, {OHNE_MERGE: [PR_82]}, WER, closed=True
    )
    assert ergebnis.comment.startswith(WER)
    assert "#82" in ergebnis.comment


def test_geschlossenes_issue_ohne_befunde_bleibt_zu() -> None:
    angelegt = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    assert decision_issue.plan(angelegt.body, {}, WER, closed=True).action == "none"


def _aufzeichnen(
    monkeypatch, scheitert_bei: str | None = None
) -> list[tuple[str, str, dict]]:
    aufrufe: list[tuple[str, str, dict]] = []

    def falsches_rest(method: str, path: str, token: str, payload: dict | None = None):
        if scheitert_bei and scheitert_bei in path:
            raise decision_issue.IssueError(f"{method} {path} -> 502: kaputt")
        aufrufe.append((method, path, payload or {}))
        return {}

    monkeypatch.setattr(decision_issue, "rest", falsches_rest)
    return aufrufe


def test_scheitert_die_erwaehnung_bleiben_die_schluessel_ungespeichert(
    monkeypatch,
) -> None:
    """Der Kern von Befund 2: ohne erfolgreiche Erwaehnung darf der Issue-Text die neuen
    Punkte nicht als gemeldet fuehren, sonst versucht es der naechste Lauf nie wieder."""
    aufrufe = _aufzeichnen(monkeypatch, scheitert_bei="/comments")
    angelegt = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    todo = decision_issue.plan(angelegt.body, {OHNE_MERGE: [PR_82, PR_90]}, WER)

    # Der Fehler muss durchschlagen: ein roter Lauf ist hier das richtige Signal.
    with pytest.raises(decision_issue.IssueError):
        decision_issue.apply("o/r", "t", {"number": 7}, todo)

    assert not any(payload.get("body") == todo.body for _, _, payload in aufrufe)


def test_erst_erwaehnen_dann_text_schreiben(monkeypatch) -> None:
    aufrufe = _aufzeichnen(monkeypatch)
    angelegt = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    todo = decision_issue.plan(angelegt.body, {OHNE_MERGE: [PR_82, PR_90]}, WER)
    decision_issue.apply("o/r", "t", {"number": 7}, todo)
    pfade = [pfad for _, pfad, _ in aufrufe]
    assert pfade.index("/repos/o/r/issues/7/comments") < pfade.index(
        "/repos/o/r/issues/7"
    )


def test_wiedereroeffnen_setzt_status_open(monkeypatch) -> None:
    aufrufe = _aufzeichnen(monkeypatch)
    angelegt = decision_issue.plan(None, {OHNE_MERGE: [PR_82]}, WER)
    todo = decision_issue.plan(angelegt.body, {OHNE_MERGE: [PR_82]}, WER, closed=True)
    decision_issue.apply("o/r", "t", {"number": 7}, todo)
    assert any(payload.get("state") == "open" for _, _, payload in aufrufe)


# Codex-Review PR #134, dritte Runde


DOPPELT = "Doppelt auf dem Board"


def _kopie(item_id: str) -> dict:
    return {
        "id": item_id,
        "label": "#5 Issue",
        "url": "https://github.com/o/r/issues/5",
    }


def test_dopplungen_bekommen_je_eintrag_einen_eigenen_schluessel() -> None:
    """Zwei Board-Eintraege desselben Issues teilen die URL - der Schluessel darf das nicht."""
    schluessel = decision_issue.finding_keys({DOPPELT: [_kopie("A"), _kopie("B")]})
    assert len(schluessel) == 2


def test_dritte_kopie_loest_eine_erwaehnung_aus() -> None:
    angelegt = decision_issue.plan(None, {DOPPELT: [_kopie("A"), _kopie("B")]}, WER)
    ergebnis = decision_issue.plan(
        angelegt.body, {DOPPELT: [_kopie("A"), _kopie("B"), _kopie("C")]}, WER
    )
    assert ergebnis.comment is not None


def test_probelauf_ohne_token_bricht_ab(monkeypatch, tmp_path) -> None:
    """Ohne Token kennt der Probelauf das bestehende Issue nicht und saehe falsch 'create'."""
    datei = tmp_path / "befunde.json"
    datei.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(sys, "argv", ["decision_issue.py", str(datei), "--dry-run"])
    assert decision_issue.main() == 2
