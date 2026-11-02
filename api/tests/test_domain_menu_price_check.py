"""domain/menu/price_check and scripts/price_check: register against database (T-4.9)."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from api.core.errors import NotFound
from api.domain.menu import price_check
from api.domain.menu.importer import MENU_FILE, OPTIONS_FILE, apply, parse
from api.models import AuditLog, ItemOption, MenuItem
from api.tests.test_domain_menu_importer_apply import SUPPE, _kasse, item, nur, run
from api.tests.test_domain_menu_importer_parse import files
from scripts import price_check as script
from scripts.seed import seed

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
PRICE_720 = {MENU_FILE: files()[MENU_FILE].replace("6,90", "7,20")}
PRICE_750 = {MENU_FILE: files()[MENU_FILE].replace("6,90", "7,50")}
WAITING = text(
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
)


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
    tid = uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )
    run(session, tid)
    return tid


def check(session, tenant_id, over=None):
    """One run of the script without the files: compare, then keep what it found."""
    plan = parse(files(**(over or {})))
    assert plan.ok, plan.errors
    report = apply(session, tenant_id, plan, dry_run=True, now=NOW)
    price_check.record(session, tenant_id, report.price_changes)
    return report


def open_ones(session, tenant_id):
    session.expire_all()
    return [
        (d.number, d.price_cents, d.register_cents)
        for d in price_check.open_differences(session, tenant_id)
    ]


def entries(session, action):
    session.expire_all()
    return session.scalars(select(AuditLog).where(AuditLog.action == action)).all()


# --- comparing ---------------------------------------------------------------


def test_a_difference_shows_and_changes_no_price(session, tenant_id):
    report = check(session, tenant_id, PRICE_720)

    assert report.price_changes == [("23", 690, 720)]
    assert open_ones(session, tenant_id) == [("23", 690, 720)]
    assert item(session, tenant_id, "23").price_cents == 690
    [entry] = entries(session, price_check.ACTION_CHECKED)
    assert entry.actor == price_check.ACTOR_CHECK
    assert entry.payload["differences"] == [{"number": "23", "from": 690, "to": 720}]


def test_no_difference_writes_nothing(session, tenant_id):
    check(session, tenant_id)

    assert open_ones(session, tenant_id) == []
    assert entries(session, price_check.ACTION_CHECKED) == []


def test_the_same_copy_twice_writes_one_audit_entry(session, tenant_id):
    check(session, tenant_id, PRICE_720)
    check(session, tenant_id, PRICE_720)

    assert open_ones(session, tenant_id) == [("23", 690, 720)]
    assert len(entries(session, price_check.ACTION_CHECKED)) == 1


def test_a_newer_copy_replaces_the_open_price(session, tenant_id):
    check(session, tenant_id, PRICE_720)
    check(session, tenant_id, PRICE_750)

    assert open_ones(session, tenant_id) == [("23", 690, 750)]


def test_a_register_back_in_line_ends_the_difference(session, tenant_id):
    check(session, tenant_id, PRICE_720)
    check(session, tenant_id)

    assert open_ones(session, tenant_id) == []
    assert item(session, tenant_id, "23").register_price_cents is None


def test_a_dish_gone_from_the_copy_ends_its_difference(session, tenant_id):
    """The register no longer sells it: its price there is no offer any more."""
    check(session, tenant_id, PRICE_720)
    plan = parse(nur("number;name;category;price_eur\n47;Ente knusprig;Haupt;15,50\n"))
    report = apply(session, tenant_id, plan, dry_run=True, now=NOW)
    price_check.record(session, tenant_id, report.price_changes)

    assert "23" in report.items_not_in_file
    assert open_ones(session, tenant_id) == []


def test_an_inactive_dish_shows_nothing_and_cannot_be_taken(session, tenant_id):
    """12 is inactive: the agent does not offer it, so the team has no card."""
    check(session, tenant_id, {MENU_FILE: files()[MENU_FILE].replace("5;;nein", "6;;")})
    assert item(session, tenant_id, "12").register_price_cents == 600

    assert open_ones(session, tenant_id) == []
    with pytest.raises(NotFound):
        price_check.decide(
            session, tenant_id, item(session, tenant_id, "12").id, 600, take=True
        )


def test_the_change_token_moves_with_the_open_differences(session, tenant_id):
    none = price_check.change_token(session, tenant_id)
    check(session, tenant_id, PRICE_720)
    first = price_check.change_token(session, tenant_id)
    check(session, tenant_id, PRICE_750)
    second = price_check.change_token(session, tenant_id)

    assert len({none, first, second}) == 3


# --- the team's answer -------------------------------------------------------


def test_take_sets_the_register_price_and_writes_audit_log(session, tenant_id):
    check(session, tenant_id, PRICE_720)
    dish = item(session, tenant_id, "23")

    price_check.decide(session, tenant_id, dish.id, 720, take=True)

    dish = item(session, tenant_id, "23")
    assert dish.price_cents == 720 and dish.register_price_cents is None
    assert open_ones(session, tenant_id) == []
    [entry] = entries(session, price_check.ACTION_TAKEN)
    assert entry.actor == "gui:tablet" and entry.entity_id == dish.id
    assert entry.payload == {"number": "23", "from": 690, "to": 720}


def test_reject_keeps_the_price_and_writes_audit_log(session, tenant_id):
    check(session, tenant_id, PRICE_720)
    dish = item(session, tenant_id, "23")

    price_check.decide(session, tenant_id, dish.id, 720, take=False)

    assert item(session, tenant_id, "23").price_cents == 690
    assert open_ones(session, tenant_id) == []
    [entry] = entries(session, price_check.ACTION_REJECTED)
    assert entry.payload == {"number": "23", "price": 690, "register": 720}


def test_a_rejected_price_stays_gone_on_the_next_copy(session, tenant_id):
    check(session, tenant_id, PRICE_720)
    price_check.decide(
        session, tenant_id, item(session, tenant_id, "23").id, 720, take=False
    )

    check(session, tenant_id, PRICE_720)

    assert open_ones(session, tenant_id) == []
    assert len(entries(session, price_check.ACTION_CHECKED)) == 1


def test_another_register_price_after_a_rejection_shows_again(session, tenant_id):
    check(session, tenant_id, PRICE_720)
    price_check.decide(
        session, tenant_id, item(session, tenant_id, "23").id, 720, take=False
    )

    check(session, tenant_id, PRICE_750)

    assert open_ones(session, tenant_id) == [("23", 690, 750)]


def test_a_rejection_ends_when_the_register_comes_back_in_line(session, tenant_id):
    check(session, tenant_id, PRICE_720)
    price_check.decide(
        session, tenant_id, item(session, tenant_id, "23").id, 720, take=False
    )
    check(session, tenant_id)

    check(session, tenant_id, PRICE_720)

    assert open_ones(session, tenant_id) == [("23", 690, 720)]


@pytest.mark.parametrize("take", [True, False])
def test_a_tap_on_a_price_that_moved_changes_nothing(session, tenant_id, take):
    """The tablet still shows 7,20 while a newer copy says 7,50: nobody saw that
    price, so it does not go through."""
    check(session, tenant_id, PRICE_720)
    check(session, tenant_id, PRICE_750)
    dish = item(session, tenant_id, "23")

    with pytest.raises(NotFound):
        price_check.decide(session, tenant_id, dish.id, 720, take=take)

    session.rollback()
    assert open_ones(session, tenant_id) == [("23", 690, 750)]
    assert item(session, tenant_id, "23").price_cents == 690


def test_a_second_tap_changes_nothing(session, tenant_id):
    """Two tablets, or a double tap: one answer, one audit entry."""
    check(session, tenant_id, PRICE_720)
    dish = item(session, tenant_id, "23")
    price_check.decide(session, tenant_id, dish.id, 720, take=True)

    with pytest.raises(NotFound):
        price_check.decide(session, tenant_id, dish.id, 720, take=True)

    session.rollback()
    assert item(session, tenant_id, "23").price_cents == 720
    assert len(entries(session, price_check.ACTION_TAKEN)) == 1


def test_a_tap_that_waited_on_another_finds_nothing_open(engine, session, tenant_id):
    """The check stands after the row lock: the second tablet reads the row the
    first one left. The first holds the row by hand until the second waits, so
    the order is certain; two free threads pass even without a lock."""
    check(session, tenant_id, PRICE_720)
    dish = item(session, tenant_id, "23").id
    session.commit()

    def second_tablet() -> None:
        with Session(engine) as own:
            price_check.decide(own, tenant_id, dish, 720, take=True)

    def someone_waits() -> bool:
        with engine.connect() as conn:
            return bool(conn.scalar(WAITING))

    with Session(engine) as first, ThreadPoolExecutor(max_workers=1) as pool:
        row = first.scalar(
            select(MenuItem).where(MenuItem.id == dish).with_for_update()
        )
        row.price_cents, row.register_price_cents = 720, None
        first.flush()
        tap = pool.submit(second_tablet)
        deadline = time.monotonic() + 10
        while not someone_waits():
            assert time.monotonic() < deadline, "the second tap never reached the row"
            time.sleep(0.01)
        first.commit()
        with pytest.raises(NotFound):
            tap.result(timeout=10)

    assert entries(session, price_check.ACTION_TAKEN) == []
    assert item(session, tenant_id, "23").price_cents == 720


def test_a_rejected_difference_cannot_be_taken_later(session, tenant_id):
    check(session, tenant_id, PRICE_720)
    dish = item(session, tenant_id, "23")
    price_check.decide(session, tenant_id, dish.id, 720, take=False)

    with pytest.raises(NotFound):
        price_check.decide(session, tenant_id, dish.id, 720, take=True)

    session.rollback()
    assert item(session, tenant_id, "23").price_cents == 690


def test_a_dish_of_another_tenant_is_not_found(session, tenant_id):
    check(session, tenant_id, PRICE_720)
    other = uuid.UUID(
        seed(session, tenant_name="Anderer", timezone="Europe/Berlin").tenant_id
    )

    with pytest.raises(NotFound):
        price_check.decide(
            session, other, item(session, tenant_id, "23").id, 720, take=True
        )

    session.rollback()
    assert item(session, tenant_id, "23").price_cents == 690


# --- the import and an open difference ----------------------------------------


def test_the_import_with_the_price_ends_the_open_difference(session, tenant_id):
    check(session, tenant_id, PRICE_720)

    run(session, tenant_id, files=PRICE_750, apply_price_changes=True)

    assert open_ones(session, tenant_id) == []
    assert item(session, tenant_id, "23").price_cents == 750


def test_an_import_without_the_switch_leaves_the_difference_open(session, tenant_id):
    check(session, tenant_id, PRICE_720)

    run(session, tenant_id, files=PRICE_720)

    assert open_ones(session, tenant_id) == [("23", 690, 720)]


def test_a_dry_run_of_the_import_leaves_the_difference_open(session, tenant_id):
    check(session, tenant_id, PRICE_720)

    run(session, tenant_id, files=PRICE_720, apply_price_changes=True, dry_run=True)

    assert open_ones(session, tenant_id) == [("23", 690, 720)]


# --- the script --------------------------------------------------------------


@pytest.fixture
def copy_folder(tmp_path):
    folder = tmp_path / "kasse"
    folder.mkdir()
    return folder


@pytest.fixture
def run_script(engine, monkeypatch):
    monkeypatch.setattr(script, "SessionLocal", sessionmaker(bind=engine))

    def go(folder, *flags):
        return script.main([str(folder), "--tenant-name", "Testbetrieb", *flags])

    return go


def on_the_card(session, tenant_id, price_cents=650, delta=450):
    """The soup of the register copy as the database already has it."""
    soup = MenuItem(
        tenant_id=tenant_id,
        number="1",
        name="Miso Suppe",
        category="Suppe",
        price_cents=price_cents,
        pos_code="1",
    )
    session.add(soup)
    session.flush()
    for name, is_default, cents in (("klein", True, 0), ("groß", False, delta)):
        session.add(
            ItemOption(
                menu_item_id=soup.id,
                group_name="Größe",
                option_name=name,
                price_delta_cents=cents,
                is_default=is_default,
                required=True,
            )
        )
    session.commit()
    return soup


def test_script_reports_cents_per_dish_and_stores_the_difference(
    copy_folder, session, tenant_id, run_script, capsys
):
    on_the_card(session, tenant_id, price_cents=600)
    _kasse(copy_folder, [SUPPE])

    assert run_script(copy_folder) == 0

    out = capsys.readouterr().out
    assert "1 Miso Suppe: 600 -> 650 cents (6,00 € -> 6,50 €)" in out
    assert "1 difference" in out
    assert "1 open on the tablet." in out
    assert open_ones(session, tenant_id) == [("1", 600, 650)]
    assert item(session, tenant_id, "1").price_cents == 600


def test_script_counts_what_really_is_on_the_tablet(
    copy_folder, session, tenant_id, run_script, capsys
):
    """An inactive dish has no card, so the report must not claim one."""
    soup = on_the_card(session, tenant_id, price_cents=600)
    soup.active = False
    session.commit()
    _kasse(copy_folder, [SUPPE])

    assert run_script(copy_folder) == 0

    out = capsys.readouterr().out
    assert "600 -> 650 cents" in out
    assert "open on the tablet" not in out


def test_script_dry_run_reports_and_stores_nothing(
    copy_folder, session, tenant_id, run_script, capsys
):
    on_the_card(session, tenant_id, price_cents=600)
    _kasse(copy_folder, [SUPPE])

    assert run_script(copy_folder, "--dry-run") == 0

    out = capsys.readouterr().out
    assert "600 -> 650 cents" in out and "Dry run" in out
    assert open_ones(session, tenant_id) == []
    assert entries(session, price_check.ACTION_CHECKED) == []


def test_script_says_when_the_prices_agree(
    copy_folder, session, tenant_id, run_script, capsys
):
    on_the_card(session, tenant_id)
    _kasse(copy_folder, [SUPPE])

    assert run_script(copy_folder) == 0

    assert "No price difference" in capsys.readouterr().out
    assert open_ones(session, tenant_id) == []


def test_script_names_dishes_only_on_one_side(
    copy_folder, session, tenant_id, run_script, capsys
):
    """A dish on one side only has no second price: "no difference" alone
    would read as if it had been compared."""
    _kasse(copy_folder, [SUPPE])

    assert run_script(copy_folder) == 0

    out = capsys.readouterr().out
    assert "In the register, not in the database: 1" in out
    assert "In the database, not in the register copy: 12, 23, 47" in out


def test_script_reports_a_surcharge_and_changes_no_option(
    copy_folder, session, tenant_id, run_script, capsys
):
    on_the_card(session, tenant_id, delta=400)
    _kasse(copy_folder, [SUPPE])

    assert run_script(copy_folder) == 0

    out = capsys.readouterr().out
    assert "Surcharge 1 Miso Suppe, Größe/groß: 400 -> 450 cents" in out
    assert "--apply-price-changes" in out
    assert "open on the tablet" not in out
    session.expire_all()
    groß = session.scalar(select(ItemOption).where(ItemOption.option_name == "groß"))
    assert groß.price_delta_cents == 400


def test_script_marks_a_rejected_difference(
    copy_folder, session, tenant_id, run_script, capsys
):
    soup = on_the_card(session, tenant_id, price_cents=600)
    _kasse(copy_folder, [SUPPE])
    assert run_script(copy_folder) == 0
    price_check.decide(session, tenant_id, soup.id, 650, take=False)
    capsys.readouterr()

    assert run_script(copy_folder) == 0

    out = capsys.readouterr().out
    assert "600 -> 650 cents (6,00 € -> 6,50 €), rejected on the tablet" in out
    assert "open on the tablet" not in out


def test_script_names_register_errors(
    copy_folder, session, tenant_id, run_script, capsys
):
    """An article the converter drops is not compared, so it must not vanish."""
    on_the_card(session, tenant_id)
    _kasse(copy_folder, [SUPPE, {**SUPPE, "ARTNR": "2", "VK1_PREIS": "x"}])

    assert run_script(copy_folder) == 0

    assert "Artikel 2" in capsys.readouterr().out


def test_script_refuses_a_missing_folder_and_file(
    copy_folder, tenant_id, run_script, capsys
):
    assert run_script(copy_folder / "nope") == 2
    assert "not found" in capsys.readouterr().err

    _kasse(copy_folder, [SUPPE])
    (copy_folder / "zutgrp.DBF").unlink()
    assert run_script(copy_folder) == 2
    assert "zutgrp.dbf" in capsys.readouterr().err


def test_script_refuses_a_copy_without_a_dish(
    copy_folder, session, tenant_id, run_script, capsys
):
    check(session, tenant_id, PRICE_720)
    _kasse(copy_folder, [])

    assert run_script(copy_folder) == 2

    assert "no dish" in capsys.readouterr().err
    # A broken copy says nothing about the prices: the open difference stays.
    assert open_ones(session, tenant_id) == [("23", 690, 720)]


def test_script_refuses_an_unknown_tenant(copy_folder, engine, run_script, capsys):
    _kasse(copy_folder, [SUPPE])

    assert run_script(copy_folder) == 2

    assert "Testbetrieb" in capsys.readouterr().err


def test_script_stops_on_a_register_number_taken_by_another_dish(
    copy_folder, session, tenant_id, run_script, capsys
):
    """The comparison of the import refuses the set: nothing is stored."""
    item(session, tenant_id, "23").pos_code = "1"
    session.commit()
    _kasse(copy_folder, [SUPPE])

    assert run_script(copy_folder) == 1

    assert "pos_code" in capsys.readouterr().err
    assert session.scalar(select(func.count()).select_from(MenuItem)) == 3
    assert entries(session, price_check.ACTION_CHECKED) == []


def test_script_stops_on_a_set_the_import_checks_refuse(
    copy_folder, session, tenant_id, run_script, capsys
):
    """A NUL passes the converter, the database could not store it: the operator
    gets the line of the file, and nothing is compared or stored."""
    on_the_card(session, tenant_id, price_cents=600)
    _kasse(copy_folder, [{**SUPPE, "BEZEICH": "Miso\x00Suppe"}])

    assert run_script(copy_folder) == 1

    assert "NUL" in capsys.readouterr().err
    assert open_ones(session, tenant_id) == []
    assert entries(session, price_check.ACTION_CHECKED) == []


def test_script_reports_a_database_error_without_a_stack_trace(
    copy_folder, session, tenant_id, monkeypatch, capsys
):
    nowhere = create_engine("postgresql+psycopg://x:y@127.0.0.1:1/z")
    monkeypatch.setattr(script, "SessionLocal", sessionmaker(bind=nowhere))
    _kasse(copy_folder, [SUPPE])

    assert script.main([str(copy_folder), "--tenant-name", "Testbetrieb"]) == 1

    assert "database error" in capsys.readouterr().err
    nowhere.dispose()


def test_script_writes_nothing_into_the_copy_folder(
    copy_folder, session, tenant_id, run_script
):
    on_the_card(session, tenant_id, price_cents=600)
    _kasse(copy_folder, [SUPPE])
    before = sorted(p.name for p in copy_folder.iterdir())

    assert run_script(copy_folder) == 0

    assert sorted(p.name for p in copy_folder.iterdir()) == before


def test_the_comparison_sees_a_surcharge(session, tenant_id):
    changed = files()[OPTIONS_FILE].replace("2,50", "3,00")

    report = check(session, tenant_id, {OPTIONS_FILE: changed})

    assert report.option_price_changes == [("47", "Fleisch", "Ente", 250, 300)]
