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
        # Wie bei GitHub: ein Entwurf ohne Nummer hat auch keine URL.
        url=f"https://example.com/{number}" if number is not None else None,
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


def test_parse_item_behaelt_archivierte_mit_markierung() -> None:
    """Archivierte Eintraege werden markiert, nicht verworfen: der Abgleich mit dem
    Repo braucht ihre URL, sonst gilt ein wieder geoeffnetes Issue als fehlend."""
    item = project_report.parse_item(
        {
            "id": "X",
            "isArchived": True,
            "updatedAt": "2026-09-01T08:00:00Z",
            "content": {"number": 3, "title": "t", "state": "OPEN", "url": "u3"},
        }
    )
    assert item is not None
    assert item.is_archived


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


# Codex-Review PR #129 (P2 x3)


def test_korrigierter_eintrag_wird_nicht_im_selben_lauf_archiviert(monkeypatch) -> None:
    """--fix --archive: was eben auf Done gesetzt wurde, bleibt erst einmal sichtbar."""

    def falsches_graphql(query: str, variables: dict, token: str) -> dict:
        if "field(name" in query:
            return {
                "user": {
                    "projectV2": {
                        "field": {"id": "F", "options": [{"id": "D", "name": "Done"}]}
                    }
                }
            }
        return {}

    monkeypatch.setattr(project_report, "graphql", falsches_graphql)
    item = eintrag(82, state="CLOSED", status="In progress", tage_alt=30)
    project_report.mark_done("owner", 2, "PROJ", [item], "token")

    jetzt = datetime.now(UTC)
    assert project_report.find_issues([item], jetzt)[ARCHIV_SCHLUESSEL] == []


def test_offener_eintrag_auf_done_wird_nie_archiviert() -> None:
    """Wieder geoeffnet, aber auf Done haengen geblieben: aktive Arbeit, kein Archiv."""
    eintraege = [eintrag(7, state="OPEN", status="Done", tage_alt=30)]
    befunde = project_report.find_issues(eintraege, JETZT)
    assert befunde[ARCHIV_SCHLUESSEL] == []
    assert len(befunde["Offen, steht aber auf Done"]) == 1


def test_offenes_issue_ohne_board_eintrag_faellt_auf() -> None:
    """Auto-add hat etwas verpasst: der Bericht darf dann nicht 'keine Abweichungen' sagen."""
    board = [eintrag(1)]
    repo = [eintrag(1), eintrag(99)]
    befunde = project_report.find_issues(board, JETZT, open_in_repo=repo)
    assert [item.number for item in befunde["Offen im Repo, fehlt auf dem Board"]] == [
        99
    ]


def test_abgleich_vergleicht_url_nicht_nummer() -> None:
    """Randfall: dieselbe Nummer aus einem anderen Repo zaehlt nicht als vorhanden."""
    fremd = eintrag(5)
    fremd.url = "https://github.com/andere/repo/issues/5"
    eigen = eintrag(5)
    befunde = project_report.find_issues([fremd], JETZT, open_in_repo=[eigen])
    assert befunde["Offen im Repo, fehlt auf dem Board"] == [eigen]


def test_ohne_repo_abgleich_fehlt_der_befund_statt_leer_zu_luegen() -> None:
    """Randfall: lief der Abgleich nicht, darf der Bericht nicht 'nichts fehlt' behaupten."""
    befunde = project_report.find_issues([eintrag(1)], JETZT)
    assert "Offen im Repo, fehlt auf dem Board" not in befunde


def test_gleiche_nummer_aus_zwei_repos_ist_keine_dopplung() -> None:
    """Codex-Review PR #129: #5 aus zwei Repos sind zwei Vorgaenge. Nach Nummer
    gezaehlt wuerde /project einen davon als Dopplung entfernen."""
    fremd = eintrag(5, node_id="A")
    fremd.url = "https://github.com/andere/repo/issues/5"
    eigen = eintrag(5, node_id="B")
    assert (
        project_report.find_issues([fremd, eigen], JETZT)["Doppelt auf dem Board"] == []
    )


# Codex-Review PR #129, dritte Runde: archivierte und wieder geoeffnete Eintraege


def archiviert(number: int, *, state: str, status: str = "Done") -> project_report.Item:
    item = eintrag(number, state=state, status=status, tage_alt=30)
    item.is_archived = True
    return item


def test_wieder_geoeffnetes_archiviertes_gilt_nicht_als_fehlend() -> None:
    """Sonst wuerde /project es neu hinzufuegen statt es zurueckzuholen."""
    item = archiviert(3, state="OPEN")
    befunde = project_report.find_issues([item], JETZT, open_in_repo=[eintrag(3)])
    assert befunde["Offen im Repo, fehlt auf dem Board"] == []
    assert befunde["Archiviert, aber wieder offen"] == [item]


def test_archivierte_zaehlen_in_keiner_anderen_pruefung() -> None:
    """Archiviert heisst erledigt und weggelegt: kein Drift, kein Archiv-Kandidat."""
    eintraege = [archiviert(4, state="CLOSED", status="In progress")]
    befunde = project_report.find_issues(eintraege, JETZT)
    assert befunde["Abgeschlossen, steht aber nicht auf Done"] == []
    assert befunde["Archiviert, aber wieder offen"] == []


def test_fix_fasst_archivierte_nicht_an() -> None:
    """--fix korrigiert nur, was sichtbar auf dem Board steht."""
    item = archiviert(4, state="CLOSED", status="In progress")
    assert project_report.items_to_mark_done([item]) == []


# Codex-Review PR #129, vierte Runde: ohne Merge geschlossene Pull Requests


def pull_request(number: int, *, state: str, status: str) -> project_report.Item:
    item = eintrag(number, state=state, status=status)
    item.kind = "PullRequest"
    return item


def test_abgelehnter_pull_request_wird_nicht_auf_done_gesetzt() -> None:
    """Done kommt bei Pull Requests nur vom Merge, nicht vom Schliessen."""
    abgelehnt = pull_request(40, state="CLOSED", status="In progress")
    gemergt = pull_request(41, state="MERGED", status="In progress")
    assert project_report.items_to_mark_done([abgelehnt, gemergt]) == [gemergt]


def test_abgelehnter_pull_request_ist_kein_drift_sondern_eigener_befund() -> None:
    abgelehnt = pull_request(40, state="CLOSED", status="In progress")
    befunde = project_report.find_issues([abgelehnt], JETZT)
    assert befunde["Abgeschlossen, steht aber nicht auf Done"] == []
    assert befunde["Pull Request ohne Merge geschlossen"] == [abgelehnt]


def test_geschlossenes_issue_bleibt_erledigt() -> None:
    """Gegenprobe: bei Issues ist CLOSED weiterhin erledigt."""
    issue = eintrag(42, state="CLOSED", status="Todo")
    issue.kind = "Issue"
    assert project_report.items_to_mark_done([issue]) == [issue]


def test_abgelehnter_pull_request_auf_done_bleibt_archivierbar() -> None:
    """Archivieren braucht nur 'vorbei', nicht 'gemergt': auch ein abgelehnter PR ist durch."""
    abgelehnt = pull_request(43, state="CLOSED", status="Done")
    abgelehnt.updated_at = JETZT - timedelta(days=30)
    befunde = project_report.find_issues([abgelehnt], JETZT)
    assert befunde[ARCHIV_SCHLUESSEL] == [abgelehnt]
    assert befunde["Pull Request ohne Merge geschlossen"] == []


def test_parse_item_liest_den_inhaltstyp() -> None:
    item = project_report.parse_item(
        {
            "id": "P",
            "isArchived": False,
            "updatedAt": "2026-09-20T08:00:00Z",
            "content": {
                "__typename": "PullRequest",
                "number": 7,
                "title": "t",
                "state": "CLOSED",
                "url": "u7",
            },
        }
    )
    assert item is not None
    assert item.kind == "PullRequest"
    assert not item.is_finished


def test_archivierte_dopplung_wird_nicht_zum_zurueckholen_gemeldet() -> None:
    """Codex-Review PR #129, fuenfte Runde: /project archiviert die ueberzaehlige
    Dopplung eines offenen Issues. Die darf danach nicht als 'wieder offen'
    erscheinen, sonst holt der naechste Lauf sie zurueck und die Dopplung ist wieder da."""
    aktiv = eintrag(8, node_id="AKTIV")
    kopie = eintrag(8, node_id="KOPIE", status="Todo")
    kopie.is_archived = True
    befunde = project_report.find_issues([aktiv, kopie], JETZT)
    assert befunde["Archiviert, aber wieder offen"] == []
    assert befunde["Doppelt auf dem Board"] == []


def test_archiviert_ohne_aktiven_zwilling_wird_weiter_gemeldet() -> None:
    """Gegenprobe: ohne aktiven Eintrag gleicher URL bleibt der Befund bestehen."""
    kopie = eintrag(9, status="Done")
    kopie.is_archived = True
    befunde = project_report.find_issues([kopie], JETZT)
    assert befunde["Archiviert, aber wieder offen"] == [kopie]
