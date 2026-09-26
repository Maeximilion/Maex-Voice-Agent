"""domain/menu/importer.apply und scripts/import_menu: Karte einspielen, idempotent, Bericht (T-4.2)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    apply,
    parse,
)
from api.models import AuditLog, ItemAlias, ItemAllergen, ItemOption, MenuItem
from api.tests.dbf_fixture import write_dbf
from api.tests.test_domain_menu_importer_parse import files
from scripts import import_menu, kasse_to_csv
from scripts.seed import seed

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def run(session, tenant_id, **kw):
    over = kw.pop("files", {})
    plan = parse(files(**over))
    assert plan.ok, plan.errors
    return apply(session, tenant_id, plan, now=NOW, **kw)


def item(session, tenant_id, number) -> MenuItem:
    session.expire_all()
    return session.scalar(
        select(MenuItem).where(
            MenuItem.tenant_id == tenant_id, MenuItem.number == number
        )
    )


def count(session, model) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model))


def test_erster_import_legt_alles_an(session, tenant_id):
    report = run(session, tenant_id)

    assert sorted(report.items_new) == ["12", "23", "47"]
    rollen = item(session, tenant_id, "23")
    assert rollen.price_cents == 690 and rollen.name == "Frühlingsrollen (4 Stück)"
    assert count(session, ItemOption) == 3
    assert count(session, ItemAllergen) == 2  # 23: A, F; 47 ohne Auskunft
    assert count(session, ItemAlias) == 4
    allergen = session.scalars(select(ItemAllergen)).first()
    assert allergen.confirmed_by == "Maxi" and allergen.confirmed_at == NOW
    [eintrag] = session.scalars(
        select(AuditLog).where(AuditLog.action == "menu.imported")
    ).all()
    assert eintrag.actor == "import" and eintrag.payload["items_new"] == 3


def test_zweimal_einspielen_aendert_nichts(session, tenant_id):
    """docs/14: Import ist idempotent."""
    run(session, tenant_id)

    report = run(session, tenant_id)

    assert not report.changed and report.price_changes == []
    assert "Keine Änderung" in report.as_text()
    # Kein zweiter Audit-Eintrag fuer einen Import, der nichts getan hat.
    assert count(session, AuditLog) == 1


def test_probelauf_schreibt_nichts(session, tenant_id):
    report = run(session, tenant_id, dry_run=True)

    assert sorted(report.items_new) == ["12", "23", "47"]
    assert "Probelauf" in report.as_text()
    assert count(session, MenuItem) == 0 and count(session, AuditLog) == 0


def test_preisaenderung_nur_mit_schalter(session, tenant_id):
    run(session, tenant_id)
    teurer = {MENU_FILE: files()[MENU_FILE].replace("6,90", "7,20")}

    ohne = run(session, tenant_id, files=teurer)
    assert ohne.price_changes == [("23", 690, 720)]
    assert "NICHT übernommen" in ohne.as_text()
    assert item(session, tenant_id, "23").price_cents == 690

    mit = run(session, tenant_id, files=teurer, apply_price_changes=True)
    assert mit.price_changes == [("23", 690, 720)] and mit.price_changes_applied
    assert item(session, tenant_id, "23").price_cents == 720


def test_andere_felder_aendern_sich_auch_ohne_preisschalter(session, tenant_id):
    run(session, tenant_id)
    neu = (
        files()[MENU_FILE]
        .replace("Ente knusprig", "Ente kross")
        .replace("5;;nein", "5;;ja")
    )

    report = run(session, tenant_id, files={MENU_FILE: neu})

    assert sorted(report.items_updated) == ["12", "47"]
    assert item(session, tenant_id, "47").name == "Ente kross"
    assert item(session, tenant_id, "12").active is True


def test_optionen_werden_angeglichen(session, tenant_id):
    run(session, tenant_id)
    optionen = (
        "number;group_name;option_name;price_delta_eur;is_default;required\n"
        "47;Fleisch;Huhn;0,00;ja;ja\n"
        "47;Fleisch;Ente;3,00;nein;ja\n"
    )

    report = run(session, tenant_id, files={OPTIONS_FILE: optionen})

    assert (report.options_added, report.options_changed, report.options_removed) == (
        0,
        1,
        1,
    )
    session.expire_all()
    ente = session.scalar(select(ItemOption).where(ItemOption.option_name == "Ente"))
    assert ente.price_delta_cents == 300
    assert count(session, ItemOption) == 2


def test_allergene_leere_zeile_loescht_bestaetigte_werte(session, tenant_id):
    """Leer heisst "keine Auskunft": der alte Wert darf nicht weiter vorgelesen werden."""
    run(session, tenant_id)
    allergene = "number;allergen_codes;confirmed_by\n23;;\n"

    report = run(session, tenant_id, files={ALLERGENS_FILE: allergene})

    assert report.allergens_changed == ["23"]
    assert count(session, ItemAllergen) == 0


def test_gericht_ohne_allergenzeile_bleibt_unberuehrt(session, tenant_id):
    run(session, tenant_id)
    allergene = "number;allergen_codes;confirmed_by\n47;G;Maxi\n"

    report = run(session, tenant_id, files={ALLERGENS_FILE: allergene})

    assert report.allergens_changed == ["47"]
    session.expire_all()
    codes = sorted(
        (a.allergen_code for a in session.scalars(select(ItemAllergen))),
    )
    # 23 behaelt A und F, 47 bekommt G.
    assert codes == ["A", "F", "G"]


def test_aliase_aus_anrufen_bleiben(session, tenant_id):
    run(session, tenant_id)
    rollen = item(session, tenant_id, "23")
    session.add(
        ItemAlias(menu_item_id=rollen.id, alias="die dinger mit gemüse", source="call")
    )
    session.commit()
    nur_einer = "number;alias\n23;Frühlingsrollen\n47;die knusprige Ente\n12;Suppe\n"

    report = run(session, tenant_id, files={ALIASES_FILE: nur_einer})

    assert report.aliases_removed == 1  # "die knusprigen rollen" aus dem Import
    session.expire_all()
    aliase = {a.alias: a.source for a in session.scalars(select(ItemAlias))}
    assert aliase["die dinger mit gemüse"] == "call"
    assert "die knusprigen rollen" not in aliase


def test_gericht_nicht_in_datei_bleibt_und_steht_im_bericht(session, tenant_id):
    run(session, tenant_id)
    ohne_suppe = "\n".join(
        z for z in files()[MENU_FILE].splitlines() if not z.startswith("12;")
    )
    aliase = "number;alias\n23;Frühlingsrollen\n47;Ente\n"

    report = run(
        session, tenant_id, files={MENU_FILE: ohne_suppe, ALIASES_FILE: aliase}
    )

    assert report.items_not_in_file == ["12"]
    assert "nicht in der Datei (unverändert): 12" in report.as_text()
    assert item(session, tenant_id, "12") is not None


def test_plan_mit_fehlern_wird_nicht_eingespielt(session, tenant_id):
    plan = parse({})
    with pytest.raises(ValueError):
        apply(session, tenant_id, plan)


def test_anderer_mandant_bleibt_getrennt(session, tenant_id):
    anderer = uuid.UUID(
        seed(session, tenant_name="Zweitbetrieb", timezone="Europe/Berlin").tenant_id
    )
    run(session, tenant_id)

    report = run(session, anderer)

    assert sorted(report.items_new) == ["12", "23", "47"]
    assert count(session, MenuItem) == 6


# --- Kommandozeile -------------------------------------------------------------


@pytest.fixture
def ordner(tmp_path):
    for name, text in files().items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def cli(engine, monkeypatch, tenant_id):
    monkeypatch.setattr(import_menu, "SessionLocal", sessionmaker(bind=engine))
    return lambda *args: import_menu.main(
        [*map(str, args), "--tenant-name", "Testbetrieb"]
    )


def test_cli_probelauf(cli, ordner, session, capsys):
    assert cli(ordner, "--dry-run") == 0
    assert "Probelauf, nichts gespeichert." in capsys.readouterr().out
    assert count(session, MenuItem) == 0


def test_cli_import(cli, ordner, session, capsys):
    assert cli(ordner) == 0
    assert "Gerichte neu: 3" in capsys.readouterr().out
    assert count(session, MenuItem) == 3


def test_cli_fehler_exit_1_und_nichts_gespeichert(cli, ordner, session, capsys):
    (ordner / MENU_FILE).write_text(
        files()[MENU_FILE].replace("6,90", "6.90"), encoding="utf-8"
    )

    assert cli(ordner) == 1
    assert "nichts eingespielt" in capsys.readouterr().err
    assert count(session, MenuItem) == 0


def test_cli_ordner_fehlt(cli, tmp_path):
    assert cli(tmp_path / "gibt-es-nicht") == 2


def test_cli_mandant_fehlt(engine, monkeypatch, ordner, capsys):
    monkeypatch.setattr(import_menu, "SessionLocal", sessionmaker(bind=engine))

    assert import_menu.main([str(ordner), "--tenant-name", "Unbekannt"]) == 2
    assert "Mandant nicht gefunden" in capsys.readouterr().err


def test_cli_liest_excel_bom(cli, ordner, session):
    text = (ordner / MENU_FILE).read_text(encoding="utf-8")
    (ordner / MENU_FILE).write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

    assert cli(ordner) == 0
    assert count(session, MenuItem) == 3


def test_neuer_pruefer_bei_gleichen_codes_wird_uebernommen(session, tenant_id):
    """Befund Codex PR #115: gleiche Codes, anderer Pruefer - der Nachweis muss stimmen."""
    run(session, tenant_id)
    spaeter = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    allergene = "number;allergen_codes;confirmed_by\n23;A,F;Küchenchef\n"

    plan = parse(files(**{ALLERGENS_FILE: allergene}))
    report = apply(session, tenant_id, plan, now=spaeter)

    assert report.allergens_changed == ["23"]
    session.expire_all()
    rows = session.scalars(select(ItemAllergen)).all()
    assert {(r.confirmed_by, r.confirmed_at) for r in rows} == {("Küchenchef", spaeter)}


def test_behaltener_code_bekommt_den_neuen_nachweis(session, tenant_id):
    """Code A bleibt, G kommt dazu: beide tragen den Pruefer und Zeitpunkt der Datei."""
    run(session, tenant_id)
    spaeter = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    allergene = "number;allergen_codes;confirmed_by\n23;A,G;Küchenchef\n"

    plan = parse(files(**{ALLERGENS_FILE: allergene}))
    apply(session, tenant_id, plan, now=spaeter)

    session.expire_all()
    rows = {r.allergen_code: r for r in session.scalars(select(ItemAllergen))}
    assert set(rows) == {"A", "G"}
    assert {(r.confirmed_by, r.confirmed_at) for r in rows.values()} == {
        ("Küchenchef", spaeter)
    }


def test_gleicher_pruefer_gleiche_codes_laesst_zeitpunkt_stehen(session, tenant_id):
    """Idempotenz: unveraenderter Nachweis behaelt seinen urspruenglichen Zeitpunkt."""
    run(session, tenant_id)
    plan = parse(files())

    report = apply(session, tenant_id, plan, now=datetime(2026, 10, 1, tzinfo=UTC))

    assert report.allergens_changed == []
    session.expire_all()
    assert {r.confirmed_at for r in session.scalars(select(ItemAllergen))} == {NOW}


def test_probelauf_mit_preisschalter_sagt_wuerde(session, tenant_id):
    """Befund Codex PR #115: Probelauf speichert nichts, also auch keinen Preis."""
    run(session, tenant_id)
    teurer = {MENU_FILE: files()[MENU_FILE].replace("6,90", "7,20")}

    report = run(
        session, tenant_id, files=teurer, apply_price_changes=True, dry_run=True
    )

    text = report.as_text()
    assert "Probelauf, nichts gespeichert." in text
    assert "würde übernommen" in text
    assert "(übernommen)" not in text
    assert item(session, tenant_id, "23").price_cents == 690


def test_alte_grossschreibung_wird_angeglichen_statt_verdoppelt(session, tenant_id):
    """Codex PR #117: ein frueher als "23A" importiertes Gericht bleibt ein Gericht."""
    session.add(
        MenuItem(
            tenant_id=tenant_id,
            number="23A",
            name="Alt",
            category="Test",
            price_cents=100,
        )
    )
    session.commit()
    karte = "number;name;category;price_eur\n23a;Neu;Test;1,00\n"

    leer = {
        OPTIONS_FILE: "number;group_name;option_name;price_delta_eur;is_default;required\n",
        ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n",
        ALIASES_FILE: "number;alias\n23a;neu\n",
    }

    report = run(session, tenant_id, files={MENU_FILE: karte, **leer})

    assert report.items_new == [] and "23a" in report.items_updated
    assert count(session, MenuItem) == 1
    neu = item(session, tenant_id, "23a")
    assert neu is not None and neu.name == "Neu"


def test_zwei_altzeilen_mit_gleicher_nummer_brechen_ab(session, tenant_id):
    """Codex PR #117: "23A" und "23a" im Bestand - nicht still eine verdecken."""
    for nummer in ("23A", "23a"):
        session.add(
            MenuItem(
                tenant_id=tenant_id,
                number=nummer,
                name=nummer,
                category="T",
                price_cents=1,
            )
        )
    session.commit()

    with pytest.raises(ValueError, match=r"23A und 23a"):
        run(session, tenant_id)
    session.rollback()
    assert count(session, MenuItem) == 2


def test_cli_meldet_altzeilen_konflikt(cli, ordner, session, tenant_id, capsys):
    for nummer in ("23A", "23a"):
        session.add(
            MenuItem(
                tenant_id=tenant_id,
                number=nummer,
                name=nummer,
                category="T",
                price_cents=1,
            )
        )
    session.commit()

    assert cli(ordner) == 1
    assert "doppelt" in capsys.readouterr().err


# --- Kasse als Quelle (T-4.11): pos_code, fehlende Gerichte inaktiv, Umwandler ---

MIT_KASSE = """number;pos_code;name;category;price_eur
23;23;Frühlingsrollen (4 Stück);Vorspeisen;6,90
47;47B;Ente knusprig;Hauptgerichte;15,50
"""


def nur(menu: str) -> dict[str, str | None]:
    """Nur die Karte, ohne Optionen, Allergene und Aliase der Grunddateien."""
    return {
        MENU_FILE: menu,
        OPTIONS_FILE: None,
        ALLERGENS_FILE: None,
        ALIASES_FILE: None,
    }


def test_pos_code_wird_gespeichert_und_bleibt_ohne_spalte(session, tenant_id):
    run(session, tenant_id, files=nur(MIT_KASSE))
    assert item(session, tenant_id, "47").pos_code == "47B"

    # Alte Datei aus dem Chat ohne Spalte: die Kassennummer bleibt stehen.
    run(session, tenant_id)
    assert item(session, tenant_id, "47").pos_code == "47B"
    assert item(session, tenant_id, "12").pos_code is None


def test_pos_code_doppelt_ist_fehler():
    doppelt = MIT_KASSE.replace("23;23;", "23;47B;")

    plan = parse(files(**{MENU_FILE: doppelt}))

    assert any("pos_code 47B doppelt" in e for e in plan.errors)


def test_fehlende_gerichte_nur_mit_schalter_inaktiv(session, tenant_id):
    run(session, tenant_id)  # 12, 23, 47; 12 schon inaktiv
    nur_23 = nur("\n".join(MIT_KASSE.splitlines()[:2]))

    ohne = run(session, tenant_id, files=nur_23)
    assert ohne.items_deactivated == []
    assert "--deactivate-missing" in ohne.as_text()
    assert item(session, tenant_id, "47").active is True

    probe = run(session, tenant_id, files=nur_23, deactivate_missing=True, dry_run=True)
    assert probe.items_deactivated == ["47"]
    assert "würden deaktiviert: 47" in probe.as_text()
    assert item(session, tenant_id, "47").active is True

    mit = run(session, tenant_id, files=nur_23, deactivate_missing=True)
    assert mit.items_deactivated == ["47"]  # 12 war schon inaktiv
    assert item(session, tenant_id, "47").active is False
    # Nie gelöscht: order_items verweisen auf das Gericht.
    assert session.scalar(select(MenuItem).where(MenuItem.number == "47")) is not None
    eintrag = session.scalars(
        select(AuditLog).where(AuditLog.action == "menu.imported")
    ).all()[-1]
    assert eintrag.payload["items_deactivated"] == ["47"]

    # Steht es wieder in der Kasse, ist es wieder aktiv.
    run(session, tenant_id, files=nur(MIT_KASSE))
    assert item(session, tenant_id, "47").active is True


ARTIKEL_FIELDS = [
    ("ARTNR", "C", 5, 0),
    ("WRG", "C", 3, 0),
    ("BEZEICH", "C", 40, 0),
    ("VK1_PREIS", "N", 7, 2),
    ("VK2_PREIS", "N", 7, 2),
    ("GROESSE", "C", 15, 0),
    ("GRPREIS1", "N", 7, 2),
    ("GRPREIS2", "N", 7, 2),
    ("ZUTATEN", "M", 10, 0),
    ("ALLERGENE", "C", 26, 0),
]


def _kasse(folder, artikel_rows):
    tables = {
        "artikel": (ARTIKEL_FIELDS, artikel_rows),
        "WARENGRP": (
            [("W_WRG", "C", 3, 0), ("W_BEZEICH", "C", 16, 0)],
            [{"W_WRG": "001", "W_BEZEICH": "Suppe"}],
        ),
        "zutaten": (
            [
                ("ZBEZEICH", "C", 16, 0),
                ("WRGSHOWALL", "L", 1, 0),
                ("WRGSHOW", "M", 10, 0),
                ("ZPREIGRP3", "C", 3, 0),
            ],
            [{"ZBEZEICH": "Extra_Garnelen", "WRGSHOWALL": "T", "ZPREIGRP3": "C"}],
        ),
        "zutgrp": (
            [("ZGRP", "C", 1, 0), ("ZPREIS", "N", 6, 2), ("ZGRP3", "C", 3, 0)],
            [{"ZGRP": "C", "ZPREIS": "1.20", "ZGRP3": "C"}],
        ),
    }
    for name, (fields, rows) in tables.items():
        dbf, dbt = write_dbf(fields, rows)
        (folder / f"{name}.DBF").write_bytes(dbf)
        if dbt is not None:
            (folder / f"{name}.DBT").write_bytes(dbt)


SUPPE = {
    "ARTNR": "1",
    "WRG": "001",
    "BEZEICH": "Miso Suppe",
    "VK1_PREIS": "6.50",
    "VK2_PREIS": "6.50",
    "GROESSE": "+-23",
    "GRPREIS2": "4.50",
}


def test_skript_von_dbf_bis_zur_karte(tmp_path, session, tenant_id):
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    _kasse(kasse, [SUPPE])

    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 0

    csv_files = {p.name: p.read_text(encoding="utf-8") for p in out.iterdir()}
    report = run(
        session, tenant_id, files={**files(), **csv_files, "item_aliases.csv": None}
    )
    assert report.items_new == ["1"]
    suppe = item(session, tenant_id, "1")
    assert (suppe.pos_code, suppe.price_cents, suppe.category) == ("1", 650, "Suppe")


def test_skript_meldet_fehler_und_fehlende_datei(tmp_path, capsys):
    kasse = tmp_path / "kasse"
    kasse.mkdir()
    _kasse(kasse, [SUPPE, {**SUPPE, "ARTNR": "S1"}])

    assert kasse_to_csv.main([str(kasse), "--out", str(tmp_path / "out")]) == 1
    assert "S1" in capsys.readouterr().out

    (kasse / "zutgrp.DBF").unlink()
    assert kasse_to_csv.main([str(kasse), "--out", str(tmp_path / "x")]) == 2
    assert "zutgrp.dbf fehlt" in capsys.readouterr().err
    assert not (tmp_path / "x").exists()


def test_cli_deaktiviert_fehlende_nur_mit_schalter(cli, ordner, session, capsys):
    """Review T-4.11: der Schalter muss auf der Kommandozeile ankommen."""
    assert cli(ordner) == 0
    nur_23 = "\n".join(files()[MENU_FILE].splitlines()[:2]) + "\n"
    (ordner / MENU_FILE).write_text(nur_23, encoding="utf-8")
    for name in (OPTIONS_FILE, ALLERGENS_FILE, ALIASES_FILE):
        (ordner / name).unlink()
    capsys.readouterr()

    assert cli(ordner, "--dry-run", "--deactivate-missing") == 0
    assert "würden deaktiviert: 47" in capsys.readouterr().out
    assert cli(ordner, "--deactivate-missing") == 0
    session.expire_all()
    assert (
        session.scalar(select(MenuItem).where(MenuItem.number == "47")).active is False
    )


def test_skript_legt_verwaiste_aliase_beiseite(tmp_path, capsys):
    """Review T-4.11: Aliase aus dem Chat zu Nummern, die die Kasse nicht liefert,
    würden den ganzen Import blockieren. Sie kommen in eine eigene Datei."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    (out / ALIASES_FILE).write_text(
        "number;alias\n1;Misosuppe\n65;Wasser\n", encoding="utf-8"
    )

    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 0

    kept = (out / ALIASES_FILE).read_text(encoding="utf-8")
    assert "Misosuppe" in kept and "Wasser" not in kept
    assert "65;Wasser" in (out / "item_aliases.verworfen.csv").read_text(
        encoding="utf-8"
    )
    assert "65" in capsys.readouterr().out
    plan = parse(import_menu.read_files(out))
    assert plan.ok, plan.errors


def test_skript_kaputte_datei_exit_2(tmp_path, capsys):
    """Review T-4.11: abgeschnittene Kopie ist eine Meldung, kein Traceback."""
    kasse = tmp_path / "kasse"
    kasse.mkdir()
    _kasse(kasse, [SUPPE])
    data = (kasse / "artikel.DBF").read_bytes()
    (kasse / "artikel.DBF").write_bytes(data[:40])

    assert kasse_to_csv.main([str(kasse), "--out", str(tmp_path / "out")]) == 2
    assert "artikel" in capsys.readouterr().err
