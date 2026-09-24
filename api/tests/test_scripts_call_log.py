"""Anrufprotokoll ohne Tonaufnahme (scripts/call_log.py, docs/17). Ohne Datenbank."""

import json
from datetime import date
from pathlib import Path

import pytest

from scripts import call_log
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
    assert case["transcript"] == [
        {"role": "customer", "text": "Auf den Namen Mueller."},
        {"role": "customer", "text": "Ja, das passt."},
    ]
    assert case["source"] == "handcrafted"


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
    assert json.loads(files[0].read_text(encoding="utf-8"))["source"] == "handcrafted"


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
    nochmal, _ = parse(HEADER + _row(intent="reservierung", time="12:00") + _row())
    assert to_case(woche1[0])["id"] != to_case(woche2[0])["id"]
    assert to_case(woche1[0])["id"] == to_case(nochmal[1])["id"]


@pytest.fixture
def eval_cases(tmp_path, monkeypatch):
    """Ein Wegwerf-evals/cases: der Test darf das echte nie beruehren, auch
    nicht, wenn die Sperre einmal kaputt ist."""
    folder = tmp_path / "evals" / "cases"
    folder.mkdir(parents=True)
    monkeypatch.setattr(call_log, "EVAL_CASES", folder)
    return folder


@pytest.mark.parametrize(
    "ziel", ["evals/cases", "evals/Cases", "evals/cases/entwuerfe"]
)
def test_entwuerfe_nie_nach_evals_cases(tmp_path, eval_cases, capsys, ziel):
    """Auch andere Schreibweise oder ein Unterordner ist die Suite."""
    good = tmp_path / "good.csv"
    good.write_text(HEADER + _row(), encoding="utf-8")
    assert main([str(good), "--cases", str(tmp_path / ziel)]) == 2
    assert list(eval_cases.iterdir()) == []
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
    # Codex PR #135: das Ja steht fest am Ende, statt es im letzten
    # Protokollsatz zu vermuten ("Danke" ist keine Bestaetigung).
    assert case["transcript"] == [
        {"role": "customer", "text": "Auf den Namen Mueller."},
        {"role": "customer", "text": "Ja, das passt."},
    ]
    assert any("erfunden" in hint for hint in case["review"])
    assert any("vor diese" in hint for hint in case["review"])


def test_abgelehnter_fall_bleibt_ohne_erfundene_daten():
    entries, _ = parse(HEADER + _row(outcome="abgelehnt", phrases="Tisch fuer sechs?"))
    case = to_case(entries[0])
    assert "caller_id" not in case
    assert case["transcript"] == []
    assert not any("Mueller" in hint for hint in case["review"])


@pytest.mark.parametrize(
    "text",
    [
        "meine Nummer ist null sieben zwei eins fuenf fuenf fuenf eins zwei drei vier",
        "null sieben einundzwanzig fuenfundfuenfzig einundfuenfzig",
    ],
)
def test_ausgeschriebene_telefonnummer(text):
    _, errors = parse(HEADER + _row(phrases=text))
    assert errors and "Telefonnummer" in errors[0], text


@pytest.mark.parametrize(
    "text", ["mueller at gmx punkt de", "mueller @ gmx.de", "Mueller (at) web punkt de"]
)
def test_ausgeschriebene_email(text):
    _, errors = parse(HEADER + _row(phrases=text))
    assert errors and "E-Mail" in errors[0], text


@pytest.mark.parametrize(
    "text", ["Lieferung an die Kaiserstrasse 12", "in die Hauptstr. 5a"]
)
def test_adresse_macht_datei_rot(text):
    _, errors = parse(HEADER + _row(phrases=text))
    assert errors and "Adresse" in errors[0], text


@pytest.mark.parametrize(
    "text",
    [
        "Kunde wollte am 25.09. 19 Uhr",
        "eine Pizza und einen Salat",
        "die dreiundzwanzig und die vierzehn",
        "Lieferung in die Weststadt",
    ],
)
def test_kein_fehlalarm(text):
    """docs/17 empfiehlt 25.09. ohne Jahr; das darf nicht als Nummer gelten."""
    _, errors = parse(HEADER + _row(problems=text))
    assert errors == [], text


def test_zeilennummer_stimmt_nach_leerzeile():
    text = HEADER + _row() + "\n" + _row(phrases="ruf an 0721 5551234")
    _, errors = parse(text)
    assert errors[0].startswith("Zeile 4:")


def test_mehrzeilige_zelle_sind_mehrere_saetze():
    text = HEADER + _row(phrases='"zweimal die 23\nja passt so"')
    entries, errors = parse(text)
    assert errors == []
    assert entries[0].phrases == ["zweimal die 23", "ja passt so"]


def test_falsche_kodierung_gibt_meldung_statt_absturz(tmp_path, capsys):
    bad = tmp_path / "excel.csv"
    bad.write_bytes((HEADER + _row(phrases="für zwei")).encode("cp1252"))
    assert main([str(bad)]) == 1
    assert "UTF-8" in capsys.readouterr().err


def test_durchgesehene_datei_im_entwurfsordner_bleibt(tmp_path, eval_cases):
    entries, _ = parse(HEADER + _row())
    write_cases(entries, tmp_path)
    draft = next(tmp_path.glob("protokoll_*.json"))
    case = json.loads(draft.read_text(encoding="utf-8"))
    del case["review"]
    draft.write_text(json.dumps(case), encoding="utf-8")
    write_cases(entries, tmp_path)
    assert "review" not in json.loads(draft.read_text(encoding="utf-8"))


def test_schon_in_evals_cases_wird_nicht_neu_entworfen(tmp_path, eval_cases):
    """Sonst ueberschreibt das naechste Verschieben den durchgesehenen Fall."""
    entries, _ = parse(HEADER + _row() + _row(intent="reservierung", time="19:10"))
    fertig = to_case(entries[0])
    (eval_cases / f"{fertig['id']}_abholung.json").write_text("{}", encoding="utf-8")
    assert write_cases(entries, tmp_path / "entwuerfe") == 1
    names = [p.name for p in (tmp_path / "entwuerfe").iterdir()]
    assert names == [f"{to_case(entries[1])['id']}_reservierung.json"]


def test_wochentag_je_tag_gemittelt():
    """Donnerstag dreimal im Zeitraum, Mittwoch zweimal: rohe Summen verzerren."""
    rows = "".join(
        _row(date=d)
        for d in ("24.09.2026", "30.09.2026", "01.10.2026", "07.10.2026", "08.10.2026")
    )
    entries, _ = parse(HEADER + rows)
    text = report(entries)
    assert "Do 1,0" in text
    assert "Mi 1,0" in text
    assert "Mo 0,0" in text


def test_entwurf_enthaelt_keinen_echten_kundensatz(tmp_path, eval_cases):
    """DSFA M15 und CLAUDE.md §8: echte Kundensaetze nie ins Git. Der Entwurf
    traegt nur den Aufbau des Falls, die Saetze stellt das Team nach."""
    satz = "haett gern zweimal die dreiundzwanzig zum Abholen"
    entries, _ = parse(HEADER + _row(phrases=f"{satz} | ja passt so"))
    write_cases(entries, tmp_path)
    text = next(tmp_path.glob("protokoll_*.json")).read_text(encoding="utf-8")
    assert satz not in text
    assert "ja passt so" not in text
    case = json.loads(text)
    # Nur die erfundenen Zeilen fuer Name und Ja, kein Satz aus dem Protokoll.
    assert [t["text"] for t in case["transcript"]] == [
        "Auf den Namen Mueller.",
        "Ja, das passt.",
    ]
    assert case["source"] == "handcrafted"
    assert any("nachstellen" in hint for hint in case["review"])


@pytest.mark.parametrize(
    "text",
    [
        "ich habe eine Nussallergie",
        "mein Sohn ist allergisch gegen Erdnuss",
        "Laktoseintoleranz",
    ],
)
def test_allergie_mit_personenbezug_macht_datei_rot(text):
    """DSFA M8: Allergien nie als Merkmal einer Person, nur produktbezogen."""
    _, errors = parse(HEADER + _row(phrases=text))
    assert errors and "Allergie" in errors[0], text


@pytest.mark.parametrize(
    "text",
    [
        "hat die 23 irgendwelche Allergien?",
        "ich habe eine Allergie gegen Sesam",
        "Er hat Allergien",
        "der Kunde hat Allergien",
    ],
)
def test_jedes_allergi_ist_rot(text):
    """Codex PR #135: eine Liste von Personenwoertern wird nie vollstaendig.
    Jedes "Allergi..." ist rot; die Frage zum Gericht heisst "Allergene"."""
    _, errors = parse(HEADER + _row(phrases=text))
    assert errors and "Allergene" in errors[0], text


def test_produktbezogene_allergenfrage_ist_erlaubt():
    _, errors = parse(
        HEADER
        + _row(phrases="sind in der 23 Nuesse drin? | welche Allergene hat die 12")
    )
    assert errors == []


def test_kaputter_entwurf_wird_gemeldet(tmp_path, eval_cases, capsys):
    """Ein Entwurf, der kein JSON mehr ist, bleibt liegen, aber nicht still."""
    (tmp_path / "protokoll_kaputt_abholung.json").write_text("{", encoding="utf-8")
    entries, _ = parse(HEADER + _row())
    write_cases(entries, tmp_path)
    assert "protokoll_kaputt_abholung.json" in capsys.readouterr().err


def _alt_und_neu(tmp_path):
    csv_file = tmp_path / "protokoll.csv"
    csv_file.write_text(
        HEADER + _row(date="01.06.2026") + _row(date="20.09.2026"), encoding="utf-8"
    )
    return csv_file


def test_frist_zeigt_abgelaufene_ohne_zu_loeschen(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(call_log, "_today", lambda: date(2026, 9, 24))
    csv_file = _alt_und_neu(tmp_path)
    vorher = csv_file.read_text(encoding="utf-8")
    assert main([str(csv_file), "--frist-tage", "90"]) == 0
    out = capsys.readouterr().out
    assert "1 Eintraege aelter als 90 Tage (vor 26.06.2026)" in out
    assert csv_file.read_text(encoding="utf-8") == vorher


def test_loeschen_entfernt_nur_alte_zeilen(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(call_log, "_today", lambda: date(2026, 9, 24))
    csv_file = _alt_und_neu(tmp_path)
    assert main([str(csv_file), "--frist-tage", "90", "--loeschen"]) == 0
    entries, errors = parse(csv_file.read_text(encoding="utf-8-sig"))
    assert errors == []
    assert [e.day for e in entries] == [date(2026, 9, 20)]
    assert "Anrufe: 1 an 1 Tagen" in capsys.readouterr().out


def test_frist_aus_umgebung(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(call_log, "_today", lambda: date(2026, 9, 24))
    monkeypatch.setenv("CALL_LOG_RETENTION_DAYS", "90")
    assert main([str(_alt_und_neu(tmp_path))]) == 0
    assert "aelter als 90 Tage" in capsys.readouterr().out


def test_loeschen_ohne_frist_ist_ein_fehler(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("CALL_LOG_RETENTION_DAYS", raising=False)
    csv_file = _alt_und_neu(tmp_path)
    vorher = csv_file.read_text(encoding="utf-8")
    assert main([str(csv_file), "--loeschen"]) == 2
    assert "Frist" in capsys.readouterr().err
    assert csv_file.read_text(encoding="utf-8") == vorher


def test_fehlerhafte_datei_wird_nicht_geloescht(tmp_path, monkeypatch):
    """Erst pruefen, dann loeschen: eine rote Datei bleibt, wie sie ist."""
    monkeypatch.setattr(call_log, "_today", lambda: date(2026, 9, 24))
    csv_file = tmp_path / "protokoll.csv"
    csv_file.write_text(
        HEADER + _row(date="01.06.2026") + _row(intent="catering"), encoding="utf-8"
    )
    vorher = csv_file.read_text(encoding="utf-8")
    assert main([str(csv_file), "--frist-tage", "90", "--loeschen"]) == 1
    assert csv_file.read_text(encoding="utf-8") == vorher


@pytest.mark.parametrize(
    "text",
    [
        "Lieferung Am Stadtgarten 5",
        "an der Alten Post 12",
        "in die Kaiser-Str. 12",
        "Kaiser Strasse 12a",
        "Hausnummer 7",
        "nach 76547 Sinzheim",
    ],
)
def test_adresse_ohne_bekannte_endung(text):
    """Codex PR #135: Adressen ohne Strassen-Endung rutschten durch."""
    _, errors = parse(HEADER + _row(phrases=text))
    assert errors and "Adresse" in errors[0], text


@pytest.mark.parametrize(
    "text",
    [
        "am Freitag 19 Uhr",
        "am Freitag um 19 Uhr",
        "zum Abholen 2 mal",
        "am Tisch fuer 4 Personen",
    ],
)
def test_adresse_kein_fehlalarm_bei_uhrzeit_und_menge(text):
    _, errors = parse(HEADER + _row(problems=text))
    assert errors == [], text


@pytest.mark.parametrize(
    "text", ["name@mail.example.de", "name@example.io", "a.b+c@x.co.uk"]
)
def test_email_mit_beliebiger_domain(text):
    """Codex PR #135: nur eine Domain-Stufe und feste Endungen wurden erkannt."""
    _, errors = parse(HEADER + _row(phrases=f"schickt es an {text}"))
    assert errors and "E-Mail" in errors[0], text


def test_korrektur_an_durchgesehenem_fall_wird_gemeldet(tmp_path, eval_cases, capsys):
    """Codex PR #135: wird eine Zeile korrigiert, nachdem ihr Fall schon in
    evals/cases/ liegt, darf die Abweichung nicht still bleiben."""
    entries, _ = parse(HEADER + _row())
    fertig = to_case(entries[0])
    name = f"{fertig['id']}_abholung.json"
    (eval_cases / name).write_text(json.dumps(fertig), encoding="utf-8")
    korrigiert, _ = parse(HEADER + _row(outcome="abgebrochen"))
    assert write_cases(korrigiert, tmp_path / "entwuerfe") == 0
    err = capsys.readouterr().err
    assert name in err
    assert "confirmed" in err


def test_unveraenderter_durchgesehener_fall_bleibt_still(tmp_path, eval_cases, capsys):
    entries, _ = parse(HEADER + _row())
    fertig = to_case(entries[0])
    fertig["expected"]["items"] = [{"number": "23", "quantity": 2}]
    (eval_cases / f"{fertig['id']}_abholung.json").write_text(
        json.dumps(fertig), encoding="utf-8"
    )
    write_cases(entries, tmp_path / "entwuerfe")
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "text",
    [
        "Ich habe Allergien gegen Nuesse",
        "meine Allergien sind schlimm",
        "mein Sohn hat Allergien",
        "Allergien gegen Sesam",
    ],
)
def test_allergien_einer_person_im_plural(text):
    """Codex PR #135: der Plural zaehlte nie, auch nicht fuer eine Person."""
    _, errors = parse(HEADER + _row(phrases=text))
    assert errors and "Allergie" in errors[0], text


def test_korrektur_zu_frage_meldet_durchgesehenen_fall(tmp_path, eval_cases, capsys):
    """Codex PR #135: wird eine Bestellung zur Frage korrigiert, entsteht kein
    Fall mehr; der durchgesehene Fall darf nicht still aktiv bleiben."""
    entries, _ = parse(HEADER + _row())
    fertig = to_case(entries[0])
    name = f"{fertig['id']}_abholung.json"
    (eval_cases / name).write_text(json.dumps(fertig), encoding="utf-8")
    korrigiert, _ = parse(HEADER + _row(intent="frage"))
    assert write_cases(korrigiert, tmp_path / "entwuerfe") == 0
    err = capsys.readouterr().err
    assert name in err
    assert "keinen Fall" in err


@pytest.mark.parametrize(
    "text",
    [
        "Neuer Markt 3",
        "Sonnenhof 3",
        "Bahnhofchaussee 10",
        "Lieferung an Neuer Weg Nord 3",
        "meine Adresse ist Lindenblick 4",
    ],
)
def test_adresse_weitere_formen(text):
    """Codex PR #135: weitere Strassenformen und Hinweiswoerter."""
    _, errors = parse(HEADER + _row(phrases=text))
    assert errors and "Adresse" in errors[0], text


def test_lieferung_nach_minuten_ist_keine_adresse():
    _, errors = parse(HEADER + _row(problems="Lieferung nach 30 Minuten zugesagt"))
    assert errors == []


def test_id_bleibt_bei_korrigiertem_wortlaut():
    """Codex PR #135: ein korrigierter Tippfehler im Wortlaut darf die ID des
    Anrufs nicht aendern, sonst findet das Script den durchgesehenen Fall nicht."""
    vorher, _ = parse(HEADER + _row(phrases="zweimal die 32"))
    nachher, _ = parse(HEADER + _row(phrases="zweimal die 23"))
    assert to_case(vorher[0])["id"] == to_case(nachher[0])["id"]


def test_zwei_anrufe_in_derselben_minute_bleiben_getrennt():
    entries, _ = parse(
        HEADER + _row(phrases="Tisch fuer zwei") + _row(phrases="die 23")
    )
    assert to_case(entries[0])["id"] != to_case(entries[1])["id"]


def test_salz_macht_id_unberechenbar():
    """Ohne das lokale Salz laesst sich aus der ID keine Anrufzeit zurueckrechnen."""
    entries, _ = parse(HEADER + _row())
    assert to_case(entries[0], salt="a")["id"] != to_case(entries[0], salt="b")["id"]


def test_salz_wird_neben_der_csv_angelegt_und_wiederverwendet(tmp_path, eval_cases):
    good = tmp_path / "protokoll.csv"
    good.write_text(HEADER + _row(), encoding="utf-8")
    assert main([str(good), "--cases", str(tmp_path / "e")]) == 0
    salt = (tmp_path / ".call_log_salt").read_text(encoding="utf-8")
    first = sorted(p.name for p in (tmp_path / "e").iterdir())
    assert main([str(good), "--cases", str(tmp_path / "e")]) == 0
    assert (tmp_path / ".call_log_salt").read_text(encoding="utf-8") == salt
    assert sorted(p.name for p in (tmp_path / "e").iterdir()) == first


def test_bestaetigte_lieferung_bekommt_erfundene_adresse():
    """Codex PR #135: ohne Adresse erreicht ein Lieferfall confirm nie."""
    entries, _ = parse(HEADER + _row(intent="lieferung"))
    texts = [t["text"] for t in to_case(entries[0])["transcript"]]
    assert texts[0].startswith("Die Adresse ist Musterstrasse 1")
    assert texts[-2:] == ["Auf den Namen Mueller.", "Ja, das passt."]


def test_korrektur_die_ein_feld_entfernt_wird_gemeldet(tmp_path, eval_cases, capsys):
    """Codex PR #135: aus einem Rueckruf zur Abholung wird eine Beschwerde, das
    neue expected hat kein intent mehr; der alte Fall erwartet es aber noch."""
    entries, _ = parse(HEADER + _row(outcome="rueckruf"))
    fertig = to_case(entries[0])
    assert fertig["expected"] == {"intent": "pickup", "escalated": True}
    name = f"{fertig['id']}_abholung.json"
    (eval_cases / name).write_text(json.dumps(fertig), encoding="utf-8")
    korrigiert, _ = parse(HEADER + _row(intent="beschwerde", outcome="rueckruf"))
    write_cases(korrigiert, tmp_path / "entwuerfe")
    err = capsys.readouterr().err
    assert name in err
    assert "intent" in err


@pytest.mark.parametrize(
    "text",
    [
        "Die Adresse ist Hauptstrasse zwoelf",
        "Ich wohne am Stadtgarten fuenf",
        "Kaiserstrasse dreiundzwanzig",
    ],
)
def test_adresse_mit_ausgeschriebener_hausnummer(text):
    """Codex PR #135: Zahlwoerter werden vor der Adresspruefung zu Ziffern."""
    _, errors = parse(HEADER + _row(phrases=text))
    assert errors and "Adresse" in errors[0], text


@pytest.mark.parametrize(
    "text",
    [
        "am Freitag eine Pizza",
        "Tisch fuer sechs um acht",
        "zum Abholen zwei mal die dreiundzwanzig",
    ],
)
def test_zahlwoerter_ohne_adresse_bleiben_erlaubt(text):
    _, errors = parse(HEADER + _row(phrases=text))
    assert errors == [], text
