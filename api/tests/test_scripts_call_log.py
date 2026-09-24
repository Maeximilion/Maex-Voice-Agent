"""Anrufprotokoll ohne Tonaufnahme (scripts/call_log.py, docs/17). Ohne Datenbank."""

import json
from pathlib import Path

from scripts.call_log import main, parse, report, to_case, write_cases

HEADER = "date;time;duration_min;intent;outcome;phrases;items;problems\n"
VORLAGE = (
    Path(__file__).resolve().parents[2] / "docs" / "vorlagen" / "anrufprotokoll.csv"
)


def _row(**kw: str) -> str:
    base = {
        "date": "24.09.2026",
        "time": "18:42",
        "duration_min": "3",
        "intent": "abholung",
        "outcome": "erledigt",
        "phrases": "zweimal die 23 | ja passt so",
        "items": "2x 23",
        "problems": "",
    }
    base.update(kw)
    return ";".join(base.values()) + "\n"


def test_vorlage_ist_gueltig():
    """Die Vorlage im Repo ist das Beispiel fuer das Team, sie muss durchgehen."""
    entries, errors = parse(VORLAGE.read_text(encoding="utf-8"))
    assert errors == []
    assert len(entries) == 2


def test_normalfall_report_und_fall():
    entries, errors = parse(HEADER + _row() + _row(intent="Reservierung", time="19.05"))
    assert errors == []
    text = report(entries)
    assert "Anrufe: 2 an 1 Tagen" in text
    assert "18 Uhr 1 · 19 Uhr 1" in text
    assert "Do 2" in text  # 24.09.2026 ist ein Donnerstag
    case = to_case(entries[0])
    assert case is not None
    assert case["expected"] == {
        "intent": "pickup",
        "confirmed": True,
        "escalated": False,
    }
    assert [t["text"] for t in case["transcript"]] == [
        "zweimal die 23",
        "Auf den Namen Mueller.",
        "ja passt so",
    ]


def test_telefonnummer_macht_datei_rot():
    _, errors = parse(HEADER + _row(phrases="rufen Sie mich an unter 0721 555 12 34"))
    assert len(errors) == 1
    assert "Telefonnummer" in errors[0]


def test_kartennummern_und_mengen_sind_keine_telefonnummer():
    _, errors = parse(HEADER + _row(phrases="die 147 und zweimal 23", items="2x 147"))
    assert errors == []


def test_email_macht_datei_rot():
    _, errors = parse(HEADER + _row(problems="schickt Bestaetigung an a@b.de"))
    assert "E-Mail" in errors[0]


def test_semikolon_im_text_wird_gemeldet_statt_still_verschoben():
    _, errors = parse(HEADER + _row(items="2x 23; 1x Rolle"))
    assert "Semikolon" in errors[0]


def test_unbekanntes_anliegen_und_ergebnis():
    _, errors = parse(HEADER + _row(intent="catering") + _row(outcome="vielleicht"))
    assert "Anliegen 'catering'" in errors[0]
    assert "Ergebnis 'vielleicht'" in errors[1]


def test_fehlende_spalte():
    _, errors = parse("date;time;intent\n24.09.2026;18:42;abholung\n")
    assert errors[0].startswith("Spalten fehlen: duration_min")


def test_umlaute_und_bom():
    """Excel schreibt ein BOM und das Team tippt Umlaute."""
    entries, errors = parse("\ufeff" + HEADER + _row(outcome="Rückruf"))
    assert errors == []
    assert entries[0].outcome == "rueckruf"


def test_rueckruf_und_beschwerde_sind_eskalation():
    entries, _ = parse(
        HEADER
        + _row(outcome="rueckruf")
        + _row(intent="beschwerde", outcome="erledigt")
    )
    assert to_case(entries[0])["expected"] == {"intent": "pickup", "escalated": True}
    assert to_case(entries[1])["expected"] == {"escalated": True}


def test_frage_ohne_kundensaetze_wird_kein_fall():
    entries, _ = parse(HEADER + _row(intent="frage") + _row(phrases=""))
    assert to_case(entries[0]) is None
    assert to_case(entries[1]) is None


def test_entwuerfe_idempotent(tmp_path):
    entries, _ = parse(HEADER + _row() + _row(intent="frage"))
    assert write_cases(entries, tmp_path) == 1
    assert write_cases(entries, tmp_path) == 1
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["source"] == "call_log"


def test_exit_codes(tmp_path, capsys):
    assert main([str(tmp_path / "fehlt.csv")]) == 2
    bad = tmp_path / "bad.csv"
    bad.write_text(HEADER + _row(time="25:00"), encoding="utf-8")
    assert main([str(bad)]) == 1
    good = tmp_path / "good.csv"
    good.write_text(HEADER + _row(), encoding="utf-8")
    assert main([str(good), "--cases", str(tmp_path / "cases")]) == 0
    assert "Eval-Entwuerfe: 1" in capsys.readouterr().out


def test_kuerzel_vom_druckbogen():
    entries, errors = parse(
        HEADER + _row(intent="A", outcome="RR") + _row(intent="r", outcome="al")
    )
    assert errors == []
    assert [(e.intent, e.outcome) for e in entries] == [
        ("abholung", "rueckruf"),
        ("reservierung", "abgelehnt"),
    ]


def test_telefonnummer_mit_punkten_oder_klammern():
    """Codex PR #135: 0176.123.45.67 und (0721) 12 34 56 rutschten durch."""
    for text in ("ruf an 0176.123.45.67", "Nummer (0721) 12 34 56"):
        _, errors = parse(HEADER + _row(phrases=text))
        assert errors and "Telefonnummer" in errors[0], text


def test_zweistelliges_jahr_wird_abgelehnt():
    """Codex PR #135: 24.09.26 wurde still zum Jahr 26."""
    _, errors = parse(HEADER + _row(date="24.09.26"))
    assert "TT.MM.JJJJ" in errors[0]


def test_unsinnige_dauer_wird_abgelehnt():
    """Codex PR #135: nan, inf und negative Minuten verdarben den Mittelwert."""
    for value in ("nan", "inf", "-2", "1e999"):
        _, errors = parse(HEADER + _row(duration_min=value))
        assert errors and "Dauer" in errors[0], value


def test_veraltete_entwuerfe_verschwinden(tmp_path):
    """Codex PR #135: korrigiertes Anliegen liess den alten Entwurf liegen."""
    (tmp_path / "notizen.txt").write_text("bleibt", encoding="utf-8")
    entries, _ = parse(HEADER + _row() + _row(intent="reservierung"))
    assert write_cases(entries, tmp_path) == 2
    entries, _ = parse(HEADER + _row(intent="lieferung"))
    assert write_cases(entries, tmp_path) == 1
    names = sorted(p.name for p in tmp_path.iterdir())
    assert len(names) == 2
    assert names[0] == "notizen.txt"
    assert names[1].startswith("protokoll_") and names[1].endswith("_lieferung.json")


def test_fall_id_haengt_am_inhalt_nicht_an_der_zeile():
    """Zeile 2 der Woche 1 und Zeile 2 der Woche 2 duerfen sich in evals/cases/
    nicht ueberschreiben; derselbe Anruf behaelt seine ID."""
    woche1, _ = parse(HEADER + _row())
    woche2, _ = parse(HEADER + _row(date="01.10.2026"))
    nochmal, _ = parse(HEADER + _row(intent="reservierung") + _row())
    assert to_case(woche1[0])["id"] != to_case(woche2[0])["id"]
    assert to_case(woche1[0])["id"] == to_case(nochmal[1])["id"]


def test_entwuerfe_nie_direkt_nach_evals_cases(tmp_path, capsys):
    """Der Lauf raeumt protokoll_*.json weg; in evals/cases/ waeren das
    durchgesehene Faelle."""
    good = tmp_path / "good.csv"
    good.write_text(HEADER + _row(), encoding="utf-8")
    cases = Path(__file__).resolve().parents[2] / "evals" / "cases"
    vorher = sorted(cases.iterdir())
    assert main([str(good), "--cases", str(cases)]) == 2
    assert sorted(cases.iterdir()) == vorher
    assert "evals/cases" in capsys.readouterr().err


def test_gleicher_wortlaut_in_derselben_stunde_bleibt_getrennt(tmp_path):
    """Codex PR #135: ohne Minute fielen zwei Anrufe um 18:05 und 18:40 zu
    einer ID zusammen, der zweite ueberschrieb den ersten still."""
    entries, _ = parse(
        HEADER
        + _row(time="18:05", intent="reservierung", phrases="Tisch fuer zwei | ja")
        + _row(time="18:40", intent="reservierung", phrases="Tisch fuer zwei | ja")
    )
    assert to_case(entries[0])["id"] != to_case(entries[1])["id"]
    assert write_cases(entries, tmp_path) == 2
    assert len(list(tmp_path.iterdir())) == 2


def test_doppelt_abgetippte_zeile_wird_nicht_doppelt_gezaehlt(tmp_path):
    entries, _ = parse(HEADER + _row() + _row())
    assert write_cases(entries, tmp_path) == 1


def test_bestaetigter_fall_bekommt_erfundenen_namen_und_rufnummer():
    """Codex PR #135: ohne Name und Rufnummer kann der Agent nie bestaetigen,
    der Entwurf mit confirmed true waere immer rot."""
    entries, _ = parse(HEADER + _row(phrases="zweimal die 23 | ja passt so"))
    case = to_case(entries[0])
    assert case["caller_id"] == "+497215551234"
    assert [t["text"] for t in case["transcript"]] == [
        "zweimal die 23",
        "Auf den Namen Mueller.",
        "ja passt so",
    ]
    assert any("erfunden" in hint for hint in case["review"])


def test_abgelehnter_fall_bleibt_ohne_erfundene_daten():
    entries, _ = parse(HEADER + _row(outcome="abgelehnt", phrases="Tisch fuer sechs?"))
    case = to_case(entries[0])
    assert "caller_id" not in case
    assert [t["text"] for t in case["transcript"]] == ["Tisch fuer sechs?"]
