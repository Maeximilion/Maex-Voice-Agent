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
from api.tests.test_domain_menu_importer_parse import files
from scripts import import_menu
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
