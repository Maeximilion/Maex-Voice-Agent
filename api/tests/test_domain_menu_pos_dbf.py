"""domain/menu/pos_dbf: dBase-Tabellen der Kasse lesen (T-4.11), synthetische Dateien."""

import pytest

from api.domain.menu.pos_dbf import DbfError, read_table
from api.tests.dbf_fixture import BLOCK, write_dbf

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


@pytest.mark.parametrize("memo_iv", [True, False])
def test_abgeschnittenes_memo_ist_dbf_fehler(memo_iv):
    """Codex PR #149: ein halbes Memo wäre sonst eine still gekürzte Liste."""
    dbf, dbt = write_dbf(FIELDS, ROWS[:1], memo_iv=memo_iv)
    assert dbt is not None
    block = 1024 if memo_iv else 512
    cut = block + 20  # Blockkopf und ein Teil des Textes, Rest fehlt

    with pytest.raises(DbfError, match="Memo"):
        read_table(dbf, dbt[:cut])


def test_geloeschte_zeile_mit_totem_memo_zeiger_stoert_nicht():
    """Review T-4.11: Memo gelöschter Zeilen wird nie gelesen, ein toter Zeiger
    dort darf den Import nicht blockieren."""
    dbf, dbt = write_dbf(FIELDS, ROWS, deleted=(0,))
    pointer = 32 + 32 * len(FIELDS) + 1 + 1 + sum(f[2] for f in FIELDS[:-1])
    tot = dbf[:pointer] + b"       999" + dbf[pointer + 10 :]

    table = read_table(tot, dbt)

    assert table.deleted[0] and table.rows[0]["ZUTATEN"] == ""
    assert [r["ARTNR"] for r in table.live()] == ["1", "99"]


def test_dbase_iv_block_ohne_kennung_ist_fehler():
    """Codex PR #149: das Memo-Format folgt aus der Tabelle, nicht aus jedem
    Block; ein IV-Block ohne Kennung ist kaputt, kein dBase-III-Text."""
    dbf, dbt = write_dbf(FIELDS, ROWS[:1])
    assert dbt is not None
    # Ein 0x1A irgendwo dahinter: der alte Rückfall hätte Kopf und Fremdtext gelesen.
    kaputt = dbt[:1024] + b"\x00\x00\x00\x00" + dbt[1028:] + b"\x1a"

    with pytest.raises(DbfError, match="Memo"):
        read_table(dbf, kaputt)


def test_dbase_iii_memo_ignoriert_bytes_20_21():
    """dBase III kennt nur 512er-Blöcke; Bytes 20-21 der .DBT sind dort frei."""
    dbf, dbt = write_dbf(FIELDS, ROWS[:1], memo_iv=False)
    assert dbt is not None
    muell = dbt[:20] + b"\x20\x20" + dbt[22:]

    assert read_table(dbf, muell).rows[0]["ZUTATEN"] == "Gebratene_Nudeln, Ei, Sojasoße"


MEMO_ROWS = [
    {"ZBEZEICH": "Mango", "WRGSHOW": "001 002"},
    {"ZBEZEICH": "Erdnuss", "WRGSHOW": "007"},
]
MEMO_FIELDS = [("ZBEZEICH", "C", 16, 0), ("WRGSHOW", "M", 10, 0)]


def test_dbase_iii_memo_without_end_does_not_read_the_next_memo():
    """Codex PR #149 (docs/24): a dBase III memo that lost its 0x1A ends at the
    next memo's block, never in another record's text."""
    dbf, dbt = write_dbf(MEMO_FIELDS, MEMO_ROWS, memo_iv=False)
    assert dbt is not None and dbt[512 + 7 : 512 + 9] == b"\x1a\x1a"
    damaged = dbt[: 512 + 7] + b"  " + dbt[512 + 9 :]

    with pytest.raises(DbfError, match=r"Zeile 1: WRGSHOW: .*block 1.*next memo"):
        read_table(dbf, damaged)


def test_dbase_iii_memo_bound_counts_deleted_rows():
    """A deleted row still owns its block: the memo before it ends there too."""
    dbf, dbt = write_dbf(MEMO_FIELDS, MEMO_ROWS, deleted=(1,), memo_iv=False)
    assert dbt is not None
    damaged = dbt[: 512 + 7] + b"  " + dbt[512 + 9 :]

    with pytest.raises(DbfError, match="next memo"):
        read_table(dbf, damaged)


@pytest.mark.parametrize("memo_iv", [False, True])
def test_memo_longer_than_one_block_is_read_whole(memo_iv):
    """The bound is the next memo, not the block size: long memos stay whole."""
    long = " ".join(f"{n:03d}" for n in range(300))  # 1199 bytes, several blocks
    dbf, dbt = write_dbf(
        MEMO_FIELDS,
        [{"ZBEZEICH": "Mango", "WRGSHOW": long}, MEMO_ROWS[1]],
        memo_iv=memo_iv,
    )

    rows = read_table(dbf, dbt).rows
    assert rows[0]["WRGSHOW"] == long and rows[1]["WRGSHOW"] == "007"


def test_dbase_iii_pointer_padding_does_not_skip_the_bound():
    """Deep review of #1074: both pointer readers agree on padding, so a pointer
    padded with 0x1C still bounds the memo before it."""
    dbf, dbt = write_dbf(MEMO_FIELDS, MEMO_ROWS, memo_iv=False)
    assert dbt is not None and b"         2" in dbf
    odd = dbf.replace(b"         2", b"\x1c" * 9 + b"2")
    damaged = dbt[: 512 + 7] + b"  " + dbt[512 + 9 :]

    assert read_table(odd, dbt).rows[1]["WRGSHOW"] == "007"
    with pytest.raises(DbfError, match=r"Zeile 1: WRGSHOW: .*next memo"):
        read_table(odd, damaged)


def test_dbase_iv_length_past_the_next_memo_is_refused():
    """Deep review of #1074: a dBase IV length word that grew by a block would
    read the next extra's groups; the next memo's block bounds it."""
    dbf, dbt = write_dbf(MEMO_FIELDS, MEMO_ROWS)
    assert dbt is not None
    length = int.from_bytes(dbt[BLOCK + 4 : BLOCK + 8], "little")
    damaged = (
        dbt[: BLOCK + 4] + (length + BLOCK).to_bytes(4, "little") + dbt[BLOCK + 8 :]
    )

    with pytest.raises(DbfError, match=r"Zeile 1: WRGSHOW: .*next memo \(block 2\)"):
        read_table(dbf, damaged)


@pytest.mark.parametrize("memo_iv", [False, True])
def test_memo_with_a_control_byte_is_refused(memo_iv):
    """Deep review of #1074: "002\\x00" is no group 002, so a NUL or another
    control byte in a memo stops the import instead of dropping the extra."""
    rows = [{"ZBEZEICH": "Mango", "WRGSHOW": "001 002\x00"}, MEMO_ROWS[1]]
    dbf, dbt = write_dbf(MEMO_FIELDS, rows, memo_iv=memo_iv)

    with pytest.raises(DbfError, match=r"Zeile 1: WRGSHOW: .*control byte 0x00"):
        read_table(dbf, dbt)


def test_dbase_iv_length_one_byte_too_long_is_refused():
    """Deep review of #1074: a length that reaches into the block's NUL padding
    leaves "002\\x00", which no product group matches."""
    dbf, dbt = write_dbf(MEMO_FIELDS, MEMO_ROWS)
    assert dbt is not None
    length = int.from_bytes(dbt[BLOCK + 4 : BLOCK + 8], "little")
    damaged = dbt[: BLOCK + 4] + (length + 1).to_bytes(4, "little") + dbt[BLOCK + 8 :]

    with pytest.raises(DbfError, match="control byte"):
        read_table(dbf, damaged)


def test_cut_dbt_is_reported_as_cut_also_with_a_later_pointer():
    """Deep review of #1074: a copy cut inside the first memo says "abgeschnitten"
    (copy again), not "damaged" with a block that is not in the file."""
    dbf, dbt = write_dbf(MEMO_FIELDS, MEMO_ROWS, memo_iv=False)
    assert dbt is not None

    with pytest.raises(DbfError, match=r"Zeile 1: WRGSHOW: .*abgeschnitten"):
        read_table(dbf, dbt[: 512 + 4])


def test_dbase_iv_cut_inside_the_block_head_names_row_and_field():
    """Deep review of #1074: a .DBT cut inside a block's length word is a cut
    memo of that row, not an anonymous "Datei beschädigt"."""
    dbf, dbt = write_dbf(MEMO_FIELDS, MEMO_ROWS[:1])
    assert dbt is not None

    with pytest.raises(DbfError, match=r"Zeile 1: WRGSHOW: .*abgeschnitten"):
        read_table(dbf, dbt[: BLOCK + 6])


def test_kaputte_loeschmarke_ist_dbf_fehler():
    """Codex PR #149: nur Leerzeichen und * sind gültig; sonst könnte ein
    gesperrter Artikel still zurückkommen."""
    dbf, dbt = write_dbf(FIELDS, ROWS[:1], deleted=(0,))
    start = 32 + 32 * len(FIELDS) + 1
    assert dbf[start : start + 1] == b"*"
    kaputt = dbf[:start] + b"#" + dbf[start + 1 :]

    with pytest.raises(DbfError, match="Löschmarke"):
        read_table(kaputt, dbt)


def test_zeilen_hinter_der_satzzahl_sind_fehler():
    """Review T-4.11: Kopie mitten im Anhängen - der Kopf zählt eine Zeile zu
    wenig, das neue Gericht fehlte sonst still."""
    dbf, dbt = write_dbf(FIELDS, ROWS)
    weniger = dbf[:4] + (len(ROWS) - 1).to_bytes(4, "little") + dbf[8:]

    with pytest.raises(DbfError, match="länger"):
        read_table(weniger, dbt)


def test_weicher_zeilenumbruch_im_memo():
    """Review T-4.11: 0x8D 0x0A ist ein weicher Umbruch des Memo-Editors,
    kein "ì" am Wort."""
    fields = [("ZBEZEICH", "C", 16, 0), ("WRGSHOW", "M", 10, 0)]
    dbf, dbt = write_dbf(fields, [{"ZBEZEICH": "Mango", "WRGSHOW": "001ì\n002"}])

    assert read_table(dbf, dbt).rows[0]["WRGSHOW"].split() == ["001", "002"]


@pytest.mark.parametrize("rest", [b"\x1a\x1a", b"  ", b" 35"])
def test_halbe_zeile_hinter_der_satzzahl_ist_fehler(rest):
    """Codex PR #149: auch ein Teil einer angehängten Zeile ist mehr als das
    Endezeichen; nur ein einzelnes 0x1A darf folgen."""
    dbf, dbt = write_dbf(FIELDS, ROWS)

    with pytest.raises(DbfError, match="länger"):
        read_table(dbf[:-1] + rest, dbt)


def test_ohne_oder_mit_einem_endezeichen():
    dbf, dbt = write_dbf(FIELDS, ROWS)

    assert len(read_table(dbf, dbt).rows) == len(ROWS)
    assert len(read_table(dbf[:-1], dbt).rows) == len(ROWS)


@pytest.mark.parametrize("wert", ["X", "1", "#"])
def test_kaputter_logischer_wert_ist_fehler(wert):
    """Codex PR #149: ein kaputtes WRGSHOWALL darf ein Extra nicht still auf
    Warengruppen einschränken."""
    dbf, dbt = write_dbf(FIELDS, [dict(ROWS[1], BONUS=wert)])

    with pytest.raises(DbfError, match="BONUS"):
        read_table(dbf, dbt)


@pytest.mark.parametrize("wert", ["T", "t", "Y", "y", "F", "f", "N", "n", "?", ""])
def test_gueltige_logische_werte(wert):
    dbf, dbt = write_dbf(FIELDS, [dict(ROWS[1], BONUS=wert)])

    assert read_table(dbf, dbt).rows[0]["BONUS"] == wert
