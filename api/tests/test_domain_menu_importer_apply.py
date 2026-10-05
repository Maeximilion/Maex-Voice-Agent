"""domain/menu/importer.apply und scripts/import_menu: Karte einspielen, idempotent, Bericht (T-4.2)."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    CommitInterruptedError,
    CommitOutcomeUnknownError,
    OptionRow,
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


@pytest.mark.parametrize("dry_run", [True, False])
def test_dry_run_rejects_what_the_database_rejects_in_an_option(
    engine, tenant_id, dry_run
):
    """Real import dry run: `SessionLocal` has autoflush off, so the dry run
    rolled back without ever sending options, allergens and aliases. A row the
    database rejects passed the dry run and failed only in the real import."""
    plan = parse(files())
    assert plan.ok, plan.errors
    # Postgres text cannot hold NUL; the checks in parse let it through.
    plan.options["47"].append(OptionRow("Extras", "Mango\x00Curry", 0, False, False))

    with (
        Session(engine, autoflush=False) as s,
        pytest.raises(DBAPIError, match="NUL"),
    ):
        apply(s, tenant_id, plan, now=NOW, dry_run=dry_run)

    with Session(engine) as s:
        assert count(s, MenuItem) == 0 and count(s, ItemOption) == 0


def test_apply_names_an_error_of_the_commit_itself(engine, tenant_id, monkeypatch):
    """Codex PR #169: after a commit that raised, the server may have committed.
    apply flushes everything first, so this error is told apart from a row the
    database rejects (the DBAPIError of the test above, also without dry run)."""
    plan = parse(files())
    assert plan.ok, plan.errors
    unflushed: list[bool] = []

    with Session(engine, autoflush=False) as s:

        def lost_connection() -> None:
            unflushed.append(bool(s.new or s.dirty or s.deleted))
            raise OperationalError("COMMIT", None, Exception("connection lost"))

        monkeypatch.setattr(s, "commit", lost_connection)
        with pytest.raises(CommitOutcomeUnknownError, match="connection lost") as e:
            apply(s, tenant_id, plan, now=NOW)

    assert unflushed == [False]
    assert isinstance(e.value.__cause__, OperationalError)


def test_apply_interrupt_in_the_commit_stays_an_interrupt(
    engine, tenant_id, monkeypatch
):
    """Review PR #176: wrapped into an Exception, Ctrl-C in the commit was
    swallowed by every caller that catches Exception - the eval runner turned
    it into a red case and went on."""
    plan = parse(files())
    assert plan.ok, plan.errors

    def interrupted() -> None:
        raise KeyboardInterrupt

    with Session(engine, autoflush=False) as s:
        monkeypatch.setattr(s, "commit", interrupted)
        # Caught here in any case: a bare interrupt would stop the pytest run.
        with pytest.raises(KeyboardInterrupt) as e:
            apply(s, tenant_id, plan, now=NOW)

    assert isinstance(e.value, CommitInterruptedError)


def test_dry_run_sends_the_last_dish_to_the_database_too(session, tenant_id):
    """With autoflush on, every query flushes what is pending - except for the
    aliases of the last dish, which no query follows."""
    plan = parse(files())
    assert plan.ok, plan.errors
    last = list(plan.items)[-1]
    plan.aliases.setdefault(last, set()).add("sup\x00pe")

    with pytest.raises(DBAPIError, match="NUL"):
        apply(session, tenant_id, plan, now=NOW, dry_run=True)

    # Rolled back like every other error exit of apply: the session works on.
    assert count(session, MenuItem) == 0 and count(session, ItemAlias) == 0


def test_dry_run_without_autoflush_writes_nothing(engine, tenant_id):
    """The flush before the rollback must not turn into a commit."""
    with Session(engine, autoflush=False) as s:
        report = run(s, tenant_id, dry_run=True)

    assert sorted(report.items_new) == ["12", "23", "47"]
    assert report.options_added == 3 and report.aliases_added == 4
    with Session(engine) as s:
        for model in (MenuItem, ItemOption, ItemAllergen, ItemAlias, AuditLog):
            assert count(s, model) == 0


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


def script_sessions(engine, **kw) -> sessionmaker:
    """Sessions like api.db.SessionLocal, which the script uses outside the tests."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, **kw)


def run_script(ordner, *flags: str) -> int:
    return import_menu.main([str(ordner), "--tenant-name", "Testbetrieb", *flags])


def reject_ente(engine) -> None:
    """A rule only the database knows: no check of the files sees it coming."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE item_options ADD CONSTRAINT ck_no_ente "
            "CHECK (option_name <> 'Ente')"
        )


@pytest.fixture
def commit_fails(engine, monkeypatch, tenant_id):
    """The script's sessions lose the connection in the commit. `reaches_server`
    decides whether the server had committed before the answer got lost.
    Returns one entry per commit call: was anything still unflushed?"""

    def install(
        *, reaches_server: bool, error: BaseException | None = None
    ) -> list[bool]:
        calls: list[bool] = []

        class LostConnection(Session):
            def commit(self) -> None:
                calls.append(bool(self.new or self.dirty or self.deleted))
                if reaches_server:
                    super().commit()
                raise error or OperationalError(
                    "COMMIT",
                    None,
                    Exception("server closed the connection unexpectedly"),
                )

        monkeypatch.setattr(
            import_menu, "SessionLocal", script_sessions(engine, class_=LostConnection)
        )
        return calls

    return install


@pytest.mark.parametrize("flags", [["--dry-run"], []])
def test_cli_database_rejection_is_a_message_not_a_traceback(
    engine, monkeypatch, tenant_id, ordner, session, capsys, flags
):
    """Review PR #169: the script caught only ValueError. A row the database
    refuses - which a dry run now sends too - ended in a stack trace."""
    reject_ente(engine)
    monkeypatch.setattr(import_menu, "SessionLocal", script_sessions(engine))

    code = run_script(ordner, *flags)

    err = capsys.readouterr().err
    assert code == 1
    assert "ck_no_ente" in err and "nothing was stored" in err
    assert count(session, MenuItem) == 0 and count(session, ItemOption) == 0


def test_cli_database_rejection_never_reaches_the_commit(
    engine, commit_fails, ordner, session, capsys
):
    """Codex PR #169: "nothing was stored" is only certain for an error that
    comes before the commit. A rejected row has to fail in the flush."""
    reject_ente(engine)
    calls = commit_fails(reaches_server=False)

    code = run_script(ordner)

    err = capsys.readouterr().err
    assert code == 1
    assert calls == []
    assert "ck_no_ente" in err and "nothing was stored" in err
    assert "unknown" not in err
    assert count(session, MenuItem) == 0 and count(session, ItemOption) == 0


@pytest.mark.parametrize("reaches_server", [False, True])
def test_cli_commit_error_reports_an_unknown_outcome(
    engine, monkeypatch, commit_fails, ordner, session, capsys, reaches_server
):
    """Codex PR #169: the connection drops while the import waits for the
    answer to its commit. The server may have committed already, so the script
    must not claim that nothing was stored. A dry run afterwards tells."""
    calls = commit_fails(reaches_server=reaches_server)

    code = run_script(ordner)

    err = capsys.readouterr().err
    assert code == 1
    # Everything was flushed before the commit: the error can only be its own.
    assert calls == [False]
    assert "server closed the connection unexpectedly" in err
    assert "unknown" in err and "--dry-run" in err and "menu.imported" in err
    assert "nothing was stored" not in err
    assert count(session, MenuItem) == (3 if reaches_server else 0)

    # What the message tells the operator to do, with a working connection.
    monkeypatch.setattr(import_menu, "SessionLocal", script_sessions(engine))
    assert run_script(ordner, "--dry-run") == 0
    out = capsys.readouterr().out
    assert ("Keine Änderung" in out) == reaches_server
    assert ("Gerichte neu: 3" in out) == (not reaches_server)
    assert count(session, AuditLog) == (1 if reaches_server else 0)


def test_cli_interrupt_during_commit_reports_an_unknown_outcome(
    commit_fails, ordner, session, capsys
):
    """Codex PR #176: Ctrl-C while the driver runs COMMIT is a BaseException.
    The server may have committed all the same, so the operator gets the same
    message instead of an interrupt traceback."""
    calls = commit_fails(reaches_server=True, error=KeyboardInterrupt())

    try:
        code = run_script(ordner)
    except KeyboardInterrupt:
        # Not let through: pytest would take it for Ctrl-C and stop the run.
        pytest.fail("the interrupt escaped the script")

    err = capsys.readouterr().err
    assert code == 1
    assert calls == [False]
    assert "interrupted" in err
    assert "unknown" in err and "--dry-run" in err
    assert count(session, MenuItem) == 3


def test_cli_unapplied_price_change_is_listed_either_way(
    cli, engine, monkeypatch, commit_fails, ordner, capsys
):
    """Review PR #176: without --apply-price-changes the dry run lists the
    price change before and after a stored import. The message has to say that
    this line does not tell, or the operator repeats an import that is in."""
    assert cli(ordner) == 0
    (ordner / MENU_FILE).write_text(
        files()[MENU_FILE]
        .replace("6,90", "7,50")
        .replace("Frühlingsrollen", "Knusperrollen"),
        encoding="utf-8",
    )
    commit_fails(reaches_server=True)
    capsys.readouterr()

    assert run_script(ordner) == 1

    assert "listed either way" in capsys.readouterr().err
    monkeypatch.setattr(import_menu, "SessionLocal", script_sessions(engine))
    assert run_script(ordner, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Gerichte neu: 0, geändert: 0" in out
    assert "Preisänderung 23" in out and "NICHT übernommen" in out


def test_cli_no_connection_is_a_message_not_a_traceback(monkeypatch, ordner, capsys):
    """Review PR #176: the tenant lookup ran outside the try, so a database
    that cannot be reached (wrong DATABASE_URL, stack down) ended in a stack
    trace. Nothing was rejected there, so the message does not say so."""
    nowhere = create_engine(
        "postgresql+psycopg://nobody@127.0.0.1:1/nothing",
        connect_args={"connect_timeout": 2},
    )
    monkeypatch.setattr(import_menu, "SessionLocal", script_sessions(nowhere))

    code = run_script(ordner)

    err = capsys.readouterr().err
    assert code == 1
    assert "nothing was stored" in err and "rejected" not in err


def test_cli_dry_run_never_commits(commit_fails, ordner, session, capsys):
    calls = commit_fails(reaches_server=True)

    code = run_script(ordner, "--dry-run")

    assert code == 0
    assert calls == []
    assert "Probelauf, nichts gespeichert." in capsys.readouterr().out
    assert count(session, MenuItem) == 0 and count(session, AuditLog) == 0


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
                ("ZGRPREIS1", "N", 6, 2),
                ("ZGRPREIS2", "N", 6, 2),
            ],
            [
                {
                    "ZBEZEICH": "Extra_Garnelen",
                    "WRGSHOWALL": "T",
                    "ZPREIGRP3": "C",
                    "ZGRPREIS1": "0.00",
                    "ZGRPREIS2": "0.00",
                }
            ],
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
    "GRPREIS1": "0.00",
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
    _kasse(kasse, [SUPPE, {**SUPPE, "ARTNR": "X1"}])

    assert kasse_to_csv.main([str(kasse), "--out", str(tmp_path / "out")]) == 1
    assert "X1" in capsys.readouterr().out

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


def test_deaktivieren_verweigert_leere_oder_halbe_karte(session, tenant_id):
    """Review T-4.11: ein leerer oder kaputter Export schaltet nie die Karte ab."""
    run(session, tenant_id)  # 23, 47 aktiv, 12 inaktiv
    leer = nur(files()[MENU_FILE].splitlines()[0] + "\n")
    with pytest.raises(ValueError, match="keine Gerichte"):
        run(session, tenant_id, files=leer, deactivate_missing=True)

    extra = files()[MENU_FILE] + "30;Reis;Beilagen;3,00;;\n"
    run(session, tenant_id, files=nur(extra))  # 23, 30, 47 aktiv
    nur_23 = nur("\n".join(files()[MENU_FILE].splitlines()[:2]))
    with pytest.raises(ValueError, match="Hälfte"):
        run(session, tenant_id, files=nur_23, deactivate_missing=True)
    assert item(session, tenant_id, "47").active is True

    report = run(
        session,
        tenant_id,
        files=nur_23,
        deactivate_missing=True,
        allow_large_deactivation=True,
    )
    assert report.items_deactivated == ["30", "47"]


def test_skript_ohne_gericht_schreibt_nichts(tmp_path, capsys):
    """Review T-4.11: 0 Gerichte (falsche Warengruppen, falscher Ordner) -> Exit 2."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    (out / ALIASES_FILE).write_text("number;alias\n1;Misosuppe\n", encoding="utf-8")

    assert (
        kasse_to_csv.main([str(kasse), "--out", str(out), "--skip-groups", "001"]) == 2
    )

    assert "kein Gericht" in capsys.readouterr().err
    assert sorted(p.name for p in out.iterdir()) == [ALIASES_FILE]
    assert "Misosuppe" in (out / ALIASES_FILE).read_text(encoding="utf-8")


def test_skript_holt_aliase_zurueck_wenn_das_gericht_wiederkommt(tmp_path):
    """Review T-4.11: ein Gericht fehlt nur einen Lauf lang -> Aliase kommen zurück."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    (out / ALIASES_FILE).write_text(
        "number;alias\n1;Misosuppe\n2;Pekingsuppe\n", encoding="utf-8"
    )
    _kasse(kasse, [SUPPE])
    kasse_to_csv.main([str(kasse), "--out", str(out)])
    assert "Pekingsuppe" not in (out / ALIASES_FILE).read_text(encoding="utf-8")

    _kasse(kasse, [SUPPE, {**SUPPE, "ARTNR": "2", "BEZEICH": "Peking Suppe"}])
    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 0

    assert "2;Pekingsuppe" in (out / ALIASES_FILE).read_text(encoding="utf-8")
    assert not (out / "item_aliases.verworfen.csv").exists()


def test_skript_alias_datei_mit_extra_feld_oder_falschem_zeichensatz(tmp_path, capsys):
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    (out / ALIASES_FILE).write_text(
        "number;alias\n1;Miso; warm\n\n65;Wasser\n", encoding="utf-8"
    )

    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 0
    assert "1;Miso; warm" in (out / ALIASES_FILE).read_text(encoding="utf-8")

    (out / ALIASES_FILE).write_bytes("number;alias\n1;Suppe groß\n".encode("cp1252"))
    capsys.readouterr()
    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 2
    assert "nicht UTF-8" in capsys.readouterr().err


def test_skript_unlesbare_datei_exit_2(tmp_path, capsys):
    """Codex PR #149: gesperrte oder unlesbare Kopie (z. B. von der Kasse noch
    geöffnet) ist eine Meldung mit Exit 2, kein Traceback."""
    kasse = tmp_path / "kasse"
    kasse.mkdir()
    _kasse(kasse, [SUPPE])
    (kasse / "zutgrp.DBF").unlink()
    (kasse / "zutgrp.DBF").mkdir()  # read_bytes() wirft dann einen OSError

    assert kasse_to_csv.main([str(kasse), "--out", str(tmp_path / "out")]) == 2
    assert "zutgrp" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_skript_verliert_keine_aliase_wenn_die_nebendatei_nicht_schreibbar_ist(
    tmp_path, capsys
):
    """Codex PR #149: erst die abgetrennten Zeilen sichern, dann die Alias-Datei
    umschreiben; scheitert das Sichern, bleibt die Alias-Datei unverändert."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    original = "number;alias\n1;Misosuppe\n65;Wasser\n"
    (out / ALIASES_FILE).write_text(original, encoding="utf-8")
    (out / "item_aliases.verworfen.csv").mkdir()  # nicht schreibbar

    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 2

    assert (out / ALIASES_FILE).read_text(encoding="utf-8") == original
    assert not (out / MENU_FILE).exists()  # alles oder nichts
    assert "item_aliases.verworfen.csv" in capsys.readouterr().err


def test_skript_gesperrte_zieldatei_tauscht_nichts(tmp_path, capsys, monkeypatch):
    """Review T-4.11: ist eine der drei Dateien gesperrt (Excel), wird keine
    getauscht - nie neue Karte neben alten Optionen."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    for name in (MENU_FILE, OPTIONS_FILE, ALLERGENS_FILE):
        (out / name).write_text("alt\n", encoding="utf-8")
    real_open = Path.open

    def locked(self, mode="r", *args, **kw):
        if self.name == OPTIONS_FILE and "a" in mode:
            raise PermissionError(13, "Datei ist geöffnet")
        return real_open(self, mode, *args, **kw)

    monkeypatch.setattr(Path, "open", locked)

    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 2

    assert "item_options.csv" in capsys.readouterr().err
    for name in (MENU_FILE, OPTIONS_FILE, ALLERGENS_FILE):
        assert (out / name).read_text(encoding="utf-8") == "alt\n"


def test_skript_aliasfehler_tauscht_keine_datei(tmp_path, capsys, monkeypatch):
    """Codex PR #149: ist die Alias-Datei nicht lesbar, bleibt der ganze Ordner
    auf dem alten Stand - der Import liest ihn als ein Satz Dateien."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    (out / MENU_FILE).write_text("alt\n", encoding="utf-8")
    (out / ALIASES_FILE).write_text("number;alias\n65;Wasser\n", encoding="utf-8")
    real_read = Path.read_text

    def locked(self, *args, **kw):
        if self.name == ALIASES_FILE:
            raise PermissionError(13, "gesperrt", str(self))
        return real_read(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", locked)

    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 2

    assert "item_aliases.csv" in capsys.readouterr().err
    assert (out / MENU_FILE).read_text(encoding="utf-8") == "alt\n"
    assert sorted(p.name for p in out.iterdir()) == [ALIASES_FILE, MENU_FILE]


def test_skript_nennt_die_datei_die_nicht_utf8_ist(tmp_path, capsys):
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    (out / ALIASES_FILE).write_text("number;alias\n1;Miso\n", encoding="utf-8")
    (out / "item_aliases.verworfen.csv").write_bytes(
        "number;alias\n65;Wasser groß\n".encode("cp1252")
    )

    # Codex PR #149: der Import läse den Ordner danach nicht - also nichts schreiben.
    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 2

    assert "item_aliases.verworfen.csv ist nicht UTF-8" in capsys.readouterr().err
    assert not (out / MENU_FILE).exists()


def test_skript_holt_keine_ersetzten_aliase_zurueck(tmp_path):
    """Review T-4.11: hat der Chat die Aliase eines fehlenden Gerichts neu
    geschrieben, gelten nur die neuen, wenn es zurückkommt."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    (out / ALIASES_FILE).write_text(
        "number;alias\n1;Miso\n2;Ente kross\n", encoding="utf-8"
    )
    _kasse(kasse, [SUPPE])
    kasse_to_csv.main([str(kasse), "--out", str(out)])  # 2 fehlt -> beiseite
    with (out / ALIASES_FILE).open("a", encoding="utf-8") as handle:
        handle.write("2;knusprige Ente\n")  # der Chat schreibt 2 neu

    _kasse(kasse, [SUPPE, {**SUPPE, "ARTNR": "2", "BEZEICH": "Peking Suppe"}])
    kasse_to_csv.main([str(kasse), "--out", str(out)])

    aliases = (out / ALIASES_FILE).read_text(encoding="utf-8")
    assert "2;knusprige Ente" in aliases and "Ente kross" not in aliases


def test_cli_verweigert_halbe_karte_und_schalter_kommt_an(cli, ordner, session, capsys):
    """Review T-4.11: die Kommandozeile reicht --allow-large-deactivation durch
    und nennt den Schalter; die Fachschicht kennt ihn nicht."""
    assert cli(ordner) == 0  # 23, 47 aktiv, 12 inaktiv
    (ordner / MENU_FILE).write_text(
        files()[MENU_FILE].replace("23;", "99;").replace("47;", "98;"), encoding="utf-8"
    )
    for name in (OPTIONS_FILE, ALLERGENS_FILE, ALIASES_FILE):
        (ordner / name).unlink()
    capsys.readouterr()

    assert cli(ordner, "--deactivate-missing") == 1
    assert "--allow-large-deactivation" in capsys.readouterr().err
    assert cli(ordner, "--deactivate-missing", "--allow-large-deactivation") == 0
    session.expire_all()
    assert (
        session.scalar(select(MenuItem).where(MenuItem.number == "47")).active is False
    )


def test_pos_code_doppelt_mit_einem_gericht_im_bestand(session, tenant_id):
    """Codex PR #149: ein Gericht, das nicht in der Datei steht, bleibt - seine
    Kassennummer darf nicht an ein zweites Gericht gehen."""
    run(session, tenant_id, files=nur(MIT_KASSE))  # 47 hat pos_code 47B
    nur_24 = nur("number;pos_code;name;category;price_eur\n24;47B;Neu;Haupt;9,00\n")

    with pytest.raises(ValueError, match="47B"):
        run(session, tenant_id, files=nur_24)
    assert item(session, tenant_id, "24") is None

    # Dasselbe Gericht erneut mit seiner Nummer ist kein Konflikt.
    run(session, tenant_id, files=nur(MIT_KASSE))
    assert item(session, tenant_id, "47").pos_code == "47B"


def test_skript_ersetzte_aliase_auch_wenn_das_gericht_noch_fehlt(tmp_path):
    """Codex PR #149: schreibt der Chat die Aliase eines Gerichts neu, das auch im
    nächsten Lauf noch fehlt, fallen die alten trotzdem weg."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    (out / ALIASES_FILE).write_text(
        "number;alias\n1;Miso\n2;Ente kross\n", encoding="utf-8"
    )
    _kasse(kasse, [SUPPE])
    kasse_to_csv.main([str(kasse), "--out", str(out)])  # 2 fehlt -> beiseite
    with (out / ALIASES_FILE).open("a", encoding="utf-8") as handle:
        handle.write("2;knusprige Ente\n")
    kasse_to_csv.main([str(kasse), "--out", str(out)])  # 2 fehlt weiter

    _kasse(kasse, [SUPPE, {**SUPPE, "ARTNR": "2", "BEZEICH": "Peking Suppe"}])
    kasse_to_csv.main([str(kasse), "--out", str(out)])

    aliases = (out / ALIASES_FILE).read_text(encoding="utf-8")
    assert "2;knusprige Ente" in aliases and "Ente kross" not in aliases


def test_skript_rollt_zurueck_wenn_ein_spaeterer_tausch_scheitert(
    tmp_path, capsys, monkeypatch
):
    """Codex PR #149: scheitert der Tausch der zweiten Datei, sind auch die
    schon getauschten wieder alt - nie neue Karte neben alten Optionen."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    for name in (MENU_FILE, OPTIONS_FILE, ALLERGENS_FILE):
        (out / name).write_text("alt\n", encoding="utf-8")
    real_replace = kasse_to_csv.os.replace

    def flaky(src, dst):
        if str(src).endswith(OPTIONS_FILE + ".tmp"):
            raise OSError(5, "E/A-Fehler", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(kasse_to_csv.os, "replace", flaky)

    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 2

    assert "alten Stand" in capsys.readouterr().err
    for name in (MENU_FILE, OPTIONS_FILE, ALLERGENS_FILE):
        assert (out / name).read_text(encoding="utf-8") == "alt\n", name
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [MENU_FILE, OPTIONS_FILE, ALLERGENS_FILE]
    )


def test_skript_gemischter_ordner_bleibt_gemeldet_wenn_tmp_nicht_loeschbar(
    tmp_path, capsys, monkeypatch
):
    """Codex PR #149: scheitert das Zurückrollen, darf ein Fehler beim Aufräumen
    der .tmp die Meldung "gemischt" nicht zu "alter Stand" machen."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    for name in (MENU_FILE, OPTIONS_FILE, ALLERGENS_FILE):
        (out / name).write_text("alt\n", encoding="utf-8")
    real_replace = kasse_to_csv.os.replace
    real_unlink = Path.unlink

    def flaky(src, dst):
        if str(src).endswith(OPTIONS_FILE + ".tmp"):
            raise OSError(5, "E/A-Fehler", str(dst))
        if str(src).endswith(MENU_FILE + ".bak"):
            raise PermissionError(13, "in Benutzung", str(src))
        return real_replace(src, dst)

    def stuck(self, *args, **kw):
        if self.name.endswith(".tmp"):
            raise PermissionError(13, "in Benutzung", str(self))
        return real_unlink(self, *args, **kw)

    monkeypatch.setattr(kasse_to_csv.os, "replace", flaky)
    monkeypatch.setattr(Path, "unlink", stuck)

    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 2

    err = capsys.readouterr().err
    assert "gemischt" in err and "alten Stand" not in err


def test_skript_bak_nicht_loeschbar_ist_warnung_nach_erfolg(
    tmp_path, capsys, monkeypatch
):
    """Codex PR #149: sind alle Dateien getauscht und nur eine .bak bleibt
    hängen (Virenscanner), ist das kein "nichts geschrieben"."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    (out / MENU_FILE).write_text("alt\n", encoding="utf-8")
    real_unlink = Path.unlink

    def stuck(self, *args, **kw):
        if self.name.endswith(".bak"):
            raise PermissionError(13, "in Benutzung", str(self))
        return real_unlink(self, *args, **kw)

    monkeypatch.setattr(Path, "unlink", stuck)

    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 0

    captured = capsys.readouterr()
    assert "nichts geschrieben" not in captured.err
    assert "menu_items.csv.bak" in captured.out
    assert "Miso Suppe" in (out / MENU_FILE).read_text(encoding="utf-8")


def test_skript_alias_datei_ohne_pflichtspalte_schreibt_nichts(tmp_path, capsys):
    """Codex PR #149: der Import läse die Alias-Datei danach nicht."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    (out / ALIASES_FILE).write_text("number;name\n1;Miso\n", encoding="utf-8")

    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 2

    assert "alias" in capsys.readouterr().err
    assert not (out / MENU_FILE).exists()


def test_beschreibung_bleibt_ohne_spalte(session, tenant_id):
    """Review T-4.11: die Kasse liefert keine Beschreibung; ihr Import darf die
    aus dem Chat nicht leeren. Eine leere Spalte löscht weiter."""
    run(session, tenant_id)
    assert item(session, tenant_id, "23").description == "mit Gemüsefüllung"

    run(session, tenant_id, files=nur(MIT_KASSE))
    assert item(session, tenant_id, "23").description == "mit Gemüsefüllung"

    leer = "number;name;category;price_eur;description\n23;Frühlingsrollen (4 Stück);Vorspeisen;6,90;\n"
    run(session, tenant_id, files=nur(leer))
    assert item(session, tenant_id, "23").description is None


def test_hinweis_auf_schalter_nur_ohne_schalter_und_nur_fuer_die_kasse(
    session, tenant_id
):
    """Review T-4.11: kein Hinweis auf --deactivate-missing, wenn er schon
    gesetzt ist oder die Datei nicht aus der Kasse kommt."""
    run(session, tenant_id)  # 12 inaktiv
    nur_23 = nur("\n".join(MIT_KASSE.splitlines()[:2]))

    mit = run(session, tenant_id, files=nur_23, deactivate_missing=True, dry_run=True)
    assert "unverändert" in mit.as_text()
    assert "--deactivate-missing" not in mit.as_text()

    chat = run(session, tenant_id, files=nur("\n".join(MENU_OHNE_47.splitlines())))
    assert "--deactivate-missing" not in chat.as_text()


MENU_OHNE_47 = """number;name;category;price_eur
23;Frühlingsrollen (4 Stück);Vorspeisen;6,90
"""


@pytest.mark.parametrize("alias", ["", "bitte"])
def test_skript_prueft_behaltene_aliase_wie_der_import(tmp_path, capsys, alias):
    """Codex PR #149: ein Alias, den import_menu ablehnt, macht den ganzen Satz
    unbrauchbar; dann wird nichts geschrieben."""
    kasse, out = tmp_path / "kasse", tmp_path / "out"
    kasse.mkdir()
    out.mkdir()
    _kasse(kasse, [SUPPE])
    (out / ALIASES_FILE).write_text(
        f"number;alias\n1;Miso\n1;{alias}\n", encoding="utf-8"
    )

    assert kasse_to_csv.main([str(kasse), "--out", str(out)]) == 2

    err = capsys.readouterr().err
    assert "item_aliases.csv Zeile 3" in err and "nichts geschrieben" in err.lower()
    assert sorted(p.name for p in out.iterdir()) == [ALIASES_FILE]
