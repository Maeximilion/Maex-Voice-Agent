"""domain/menu/pos_dbf: dBase-Tabellen der Kasse lesen (T-4.11), synthetische Dateien."""

import pytest

from api.domain.menu.pos_dbf import DbfError, read_table
from api.tests.dbf_fixture import write_dbf

FIELDS = [
    ("ARTNR", "C", 5, 0),
    ("BEZEICH", "C", 40, 0),
    ("VK1_PREIS", "N", 7, 2),
    ("BONUS", "L", 1, 0),
    ("ZUTATEN", "M", 10, 0),
]
ROWS = [
    {
        "ARTNR": "35B",
        "BEZEICH": "Gebr. Nudeln mit Hühnerbrust",
        "VK1_PREIS": "13.50",
        "BONUS": "T",
        "ZUTATEN": "Gebratene_Nudeln, Ei, Sojasoße",
    },
    {"ARTNR": "1", "BEZEICH": "Miso Suppe", "VK1_PREIS": "6.50", "BONUS": "F"},
    {"ARTNR": "99", "BEZEICH": "Gestrichen", "VK1_PREIS": "1.00", "BONUS": "F"},
]


def test_liest_zeilen_umlaute_und_memo():
    dbf, dbt = write_dbf(FIELDS, ROWS, deleted=(2,))

    table = read_table(dbf, dbt)

    assert table.encoding == "cp437"
    assert [f.name for f in table.fields] == [f[0] for f in FIELDS]
    first = table.rows[0]
    assert first["BEZEICH"] == "Gebr. Nudeln mit Hühnerbrust"
    assert first["VK1_PREIS"] == "13.50"  # Text, Cent erst beim Umwandeln
    assert first["ZUTATEN"] == "Gebratene_Nudeln, Ei, Sojasoße"
    assert table.rows[1]["ZUTATEN"] == ""
    assert table.deleted == (False, False, True)
    assert [r["ARTNR"] for r in table.live()] == ["35B", "1"]


def test_dbase_iii_memo_mit_endezeichen():
    dbf, dbt = write_dbf(FIELDS, ROWS[:1], memo_iv=False)

    assert read_table(dbf, dbt).rows[0]["ZUTATEN"] == "Gebratene_Nudeln, Ei, Sojasoße"


def test_unbekannter_zeichensatz_ist_fehler():
    """Nie eine Codepage raten: Umlaute wären still falsch."""
    dbf, dbt = write_dbf(FIELDS, ROWS, driver=0x00)

    with pytest.raises(DbfError, match="Zeichensatz"):
        read_table(dbf, dbt)


def test_memo_datei_fehlt():
    dbf, _ = write_dbf(FIELDS, ROWS)

    with pytest.raises(DbfError, match="DBT"):
        read_table(dbf, None)


def test_abgeschnittene_datei():
    dbf, dbt = write_dbf(FIELDS, ROWS)

    with pytest.raises(DbfError, match="kürzer"):
        read_table(dbf[:-100], dbt)
    with pytest.raises(DbfError, match="zu kurz"):
        read_table(dbf[:10], dbt)


def test_tabelle_ohne_memo_braucht_keine_dbt():
    dbf, dbt = write_dbf(
        [("W_WRG", "C", 3, 0), ("W_BEZEICH", "C", 16, 0)],
        [{"W_WRG": "001", "W_BEZEICH": "Suppe"}],
    )

    assert dbt is None
    assert read_table(dbf).live() == [{"W_WRG": "001", "W_BEZEICH": "Suppe"}]


@pytest.mark.parametrize("cut", [40, 100, 200])
def test_kaputter_kopf_ist_dbf_fehler(cut):
    """Review T-4.11: abgeschnittener Kopf wirft DbfError, keinen IndexError."""
    dbf, dbt = write_dbf(FIELDS, ROWS)

    with pytest.raises(DbfError):
        read_table(dbf[:cut], dbt)


def test_nicht_dekodierbares_byte_ist_dbf_fehler():
    dbf, dbt = write_dbf(FIELDS, ROWS[:1], driver=0x03, encoding="cp437")

    with pytest.raises(DbfError, match="Zeichensatz"):
        read_table(dbf, dbt)  # 0x81 (ü in cp437) ist in cp1252 nicht belegt


def test_kaputter_memo_zeiger_ist_dbf_fehler():
    """Codex PR #149: ein unlesbarer Memo-Zeiger ist kein leeres Memo."""
    dbf, dbt = write_dbf(FIELDS, ROWS[:1])
    # Kopf + Löschmarke + Felder vor ZUTATEN: dort steht der Zeiger der ersten Zeile.
    pointer = 32 + 32 * len(FIELDS) + 1 + 1 + sum(f[2] for f in FIELDS[:-1])
    assert dbf[pointer : pointer + 10] == b"         1"
    kaputt = dbf[:pointer] + b"      x1?!" + dbf[pointer + 10 :]

    with pytest.raises(DbfError, match="Memo"):
        read_table(kaputt, dbt)
