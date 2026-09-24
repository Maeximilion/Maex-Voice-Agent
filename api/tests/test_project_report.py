"""Tests fuer die Pruefregeln der taeglichen Projektpflege (scripts/project_report.py).

Geprueft wird nur die Logik auf fertigen Daten. Der GraphQL-Teil bleibt aussen vor:
er spricht mit GitHub und gehoert in einen echten Lauf, nicht in die Testsuite.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_MODULPFAD = Path(__file__).resolve().parents[2] / "scripts" / "project_report.py"
_spec = importlib.util.spec_from_file_location("project_report", _MODULPFAD)
assert _spec and _spec.loader
project_report = importlib.util.module_from_spec(_spec)
# Vor exec_module eintragen: @dataclass schlaegt das Modul waehrend des Ladens in
# sys.modules nach und scheitert sonst mit AttributeError auf None.
sys.modules["project_report"] = project_report
_spec.loader.exec_module(project_report)

JETZT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)

ARCHIV_SCHLUESSEL = f"Fertig, aelter als {project_report.DONE_ARCHIVE_AFTER_DAYS} Tage (Archiv-Kandidat)"
LIEGT_SCHLUESSEL = (
    f"In Arbeit, seit {project_report.IN_PROGRESS_STALE_AFTER_DAYS} Tagen ohne Bewegung"
)


def eintrag(
    number: int | None,
    *,
    status: str | None = "Todo",
    state: str | None = "OPEN",
    iteration: str | None = "Iteration 1",
    tage_alt: int = 0,
    node_id: str | None = None,
) -> project_report.Item:
    """Ein Board-Eintrag mit sinnvollen Vorgaben; jeder Test aendert nur, was er prueft."""
    felder: dict[str, str] = {}
    if status:
        felder["Status"] = status
    if iteration:
        felder["Iteration"] = iteration
    return project_report.Item(
        node_id=node_id or f"PVTI_{number}",
        title=f"T-1.{number} Beispielaufgabe",
        updated_at=JETZT - timedelta(days=tage_alt),
        number=number,
        state=state,
        url=f"https://example.com/{number}",
        fields=felder,
    )


def test_sauberes_board_meldet_nichts() -> None:
    """Normalfall: passende Eintraege, keine einzige Auffaelligkeit."""
    befunde = project_report.find_issues([eintrag(1), eintrag(2)], JETZT)
    assert sum(len(treffer) for treffer in befunde.values()) == 0


def test_doppelter_eintrag_wird_erkannt() -> None:
    """Dasselbe Issue zweimal auf dem Board - genau die Dopplung, die vermieden werden soll."""
    eintraege = [eintrag(7, node_id="A"), eintrag(7, node_id="B"), eintrag(8)]
    doppelt = project_report.find_issues(eintraege, JETZT)["Doppelt auf dem Board"]
    assert {item.node_id for item in doppelt} == {"A", "B"}


def test_entwurf_ohne_nummer_zaehlt_nicht_als_dopplung() -> None:
    """Randfall: mehrere Entwuerfe haben alle number=None und duerfen sich nicht gegenseitig melden."""
    eintraege = [eintrag(None, node_id="A"), eintrag(None, node_id="B")]
    assert project_report.find_issues(eintraege, JETZT)["Doppelt auf dem Board"] == []


@pytest.mark.parametrize("geschrieben", ["In progress", "In Progress", "in arbeit"])
def test_status_wird_unabhaengig_von_der_schreibweise_erkannt(geschrieben: str) -> None:
    """GitHub liefert die Option so, wie sie angelegt wurde - der Vergleich darf daran nicht scheitern."""
    eintraege = [eintrag(11, status=geschrieben, tage_alt=9)]
    assert len(project_report.find_issues(eintraege, JETZT)[LIEGT_SCHLUESSEL]) == 1


def test_backlog_ohne_iteration_wird_nicht_gemeldet() -> None:
    """Nur was in Arbeit ist, braucht eine Iteration - sonst meldet der Bericht taeglich den ganzen Backlog."""
    eintraege = [eintrag(12, status="Todo", iteration=None)]
    befunde = project_report.find_issues(eintraege, JETZT)
    assert befunde["In Arbeit, aber ohne Iteration"] == []


def test_arbeit_ohne_iteration_faellt_auf() -> None:
    """Umgekehrt: was laeuft, gehoert in eine Iteration."""
    eintraege = [eintrag(13, status="In progress", iteration=None)]
    assert (
        len(
            project_report.find_issues(eintraege, JETZT)[
                "In Arbeit, aber ohne Iteration"
            ]
        )
        == 1
    )


@pytest.mark.parametrize("zustand", ["CLOSED", "MERGED"])
def test_abgeschlossenes_ohne_done_faellt_auf(zustand: str) -> None:
    """Der wichtigste Drift. MERGED muss mitzaehlen: ein Pull Request landet nie auf CLOSED."""
    befunde = project_report.find_issues(
        [eintrag(3, state=zustand, status="Todo")], JETZT
    )
    assert len(befunde["Abgeschlossen, steht aber nicht auf Done"]) == 1


def test_offenes_issue_gilt_nicht_als_abgeschlossen() -> None:
    """Gegenprobe, damit die Pruefung nicht einfach alles meldet."""
    befunde = project_report.find_issues(
        [eintrag(3, state="OPEN", status="Todo")], JETZT
    )
    assert befunde["Abgeschlossen, steht aber nicht auf Done"] == []


@pytest.mark.parametrize(
    ("tage_alt", "erwartet"),
    [
        (project_report.DONE_ARCHIVE_AFTER_DAYS - 1, 0),
        (project_report.DONE_ARCHIVE_AFTER_DAYS, 1),
    ],
)
def test_archivgrenze_greift_erst_ab_dem_schwellwert(
    tage_alt: int, erwartet: int
) -> None:
    """Randfall an der Grenze: einen Tag darunter passiert nichts, auf der Grenze schon."""
    eintraege = [eintrag(4, status="Done", state="CLOSED", tage_alt=tage_alt)]
    assert (
        len(project_report.find_issues(eintraege, JETZT)[ARCHIV_SCHLUESSEL]) == erwartet
    )


def test_liegengebliebene_arbeit_wird_gemeldet_nicht_verschoben() -> None:
    """Liegt zu lange in Arbeit: taucht im Bericht auf, aber nirgends in einer Aenderung."""
    eintraege = [eintrag(5, status="In Progress", tage_alt=9)]
    befunde = project_report.find_issues(eintraege, JETZT)
    assert len(befunde[LIEGT_SCHLUESSEL]) == 1
    assert befunde[ARCHIV_SCHLUESSEL] == []


def test_fehlende_felder_werden_getrennt_gemeldet() -> None:
    """Ohne Status und ohne Iteration - zwei verschiedene Befunde fuer denselben Eintrag."""
    befunde = project_report.find_issues(
        [eintrag(6, status=None, iteration=None)], JETZT
    )
    assert len(befunde["Ohne Status"]) == 1
    assert befunde["In Arbeit, aber ohne Iteration"] == []


def test_bericht_ohne_befunde_bleibt_kurz() -> None:
    """Der taegliche Lauf soll an ruhigen Tagen nicht laermen."""
    eintraege = [eintrag(1)]
    text = project_report.render_report(
        "Maex Voice-Agent",
        eintraege,
        project_report.find_issues(eintraege, JETZT),
        JETZT,
    )
    assert "Keine Abweichungen" in text
    assert "##" not in text


def test_bericht_nennt_jeden_befund_mit_nummer() -> None:
    """Mit Befund: Ueberschrift, Anzahl und die Issue-Nummer zum Nachsehen."""
    eintraege = [eintrag(9, node_id="A"), eintrag(9, node_id="B")]
    text = project_report.render_report(
        "Maex Voice-Agent",
        eintraege,
        project_report.find_issues(eintraege, JETZT),
        JETZT,
    )
    assert "## Doppelt auf dem Board (2)" in text
    assert "#9" in text


def test_parse_item_ueberspringt_archivierte() -> None:
    """Archivierte Eintraege sind erledigt und gehoeren in keine Pruefung."""
    assert project_report.parse_item({"id": "X", "isArchived": True}) is None


def test_parse_item_liest_single_select_und_iteration() -> None:
    """Beide Feldtypen landen in derselben Feldtabelle - Status als name, Iteration als title."""
    item = project_report.parse_item(
        {
            "id": "PVTI_1",
            "isArchived": False,
            "updatedAt": "2026-09-20T08:00:00Z",
            "fieldValues": {
                "nodes": [
                    {"name": "Done", "field": {"name": "Status"}},
                    {"title": "Iteration 2", "field": {"name": "Iteration"}},
                    {"name": "wird ignoriert", "field": {}},
                ]
            },
            "content": {
                "number": 42,
                "title": "T-4.2 Import",
                "state": "CLOSED",
                "url": "u",
            },
        }
    )
    assert item is not None
    assert item.fields == {"Status": "Done", "Iteration": "Iteration 2"}
    assert item.number == 42
    assert item.label == "#42 T-4.2 Import"


def test_nur_abgeschlossenes_ohne_done_wird_korrigiert() -> None:
    """--fix fasst genau die Luecke an, die die eingebauten Workflows hinterlassen - sonst nichts."""
    eintraege = [
        eintrag(82, state="CLOSED", status="In progress"),
        eintrag(105, state="MERGED", status="In progress"),
        eintrag(49, state="OPEN", status="In progress"),
        eintrag(4, state="CLOSED", status="Done"),
    ]
    zu_korrigieren = project_report.items_to_mark_done(eintraege)
    assert [item.number for item in zu_korrigieren] == [82, 105]


@pytest.mark.parametrize("geschrieben", ["Done", "done", " Fertig "])
def test_done_option_wird_in_jeder_schreibweise_gefunden(geschrieben: str) -> None:
    optionen = [{"id": "a", "name": "Todo"}, {"id": "b", "name": geschrieben}]
    assert project_report.pick_done_option(optionen) == "b"


def test_fehlende_done_option_bricht_mit_klartext_ab() -> None:
    """Randfall: ein Board ohne Done-Spalte. Lieber laut abbrechen als irgendwohin schreiben."""
    with pytest.raises(project_report.ProjectError, match="keine Option fuer Done"):
        project_report.pick_done_option([{"id": "a", "name": "Todo"}])


def test_mark_done_setzt_status_und_zieht_bericht_nach(monkeypatch) -> None:
    """Die Mutation bekommt die richtigen IDs, und der Bericht sieht danach den neuen Stand."""
    aufrufe: list[dict] = []

    def falsches_graphql(query: str, variables: dict, token: str) -> dict:
        aufrufe.append(variables)
        if "field(name" in query:
            return {
                "user": {
                    "projectV2": {
                        "field": {
                            "id": "FELD",
                            "options": [
                                {"id": "OPT_TODO", "name": "Todo"},
                                {"id": "OPT_DONE", "name": "Done"},
                            ],
                        }
                    }
                }
            }
        return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "x"}}}

    monkeypatch.setattr(project_report, "graphql", falsches_graphql)
    item = eintrag(105, state="MERGED", status="In progress", node_id="PVTI_105")

    korrigiert = project_report.mark_done("owner", 2, "PROJ", [item], "token")

    assert korrigiert == [item.label]
    assert aufrufe[-1] == {
        "projectId": "PROJ",
        "itemId": "PVTI_105",
        "fieldId": "FELD",
        "optionId": "OPT_DONE",
    }
    assert item.is_done
    befunde = project_report.find_issues([item], JETZT)
    assert befunde["Abgeschlossen, steht aber nicht auf Done"] == []


def test_mark_done_ohne_kandidaten_ruft_github_nicht_auf(monkeypatch) -> None:
    """Der taegliche Lauf an einem ruhigen Tag darf keine einzige Anfrage schreiben."""

    def darf_nicht_laufen(*_args: object) -> dict:
        raise AssertionError("graphql wurde ohne Anlass aufgerufen")

    monkeypatch.setattr(project_report, "graphql", darf_nicht_laufen)
    assert project_report.mark_done("owner", 2, "PROJ", [], "token") == []
