"""The export of a leaving customer (#576): `python -m api.jobs.offboarding export`.

What is checked: the four menu files go back through `scripts/import_menu.py`
into another tenant as they came out (its export is the same bytes), orders and
reservations are the tenant's only, in cents and in Berlin time, a rerun changes
nothing, and the job writes nothing to the database.
"""

import csv
import json
import stat
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.exc import InternalError
from sqlalchemy.orm import Session, sessionmaker

from api.jobs import offboarding
from api.models import (
    Call,
    DiningTable,
    FloorArea,
    ItemAlias,
    MenuItem,
    Order,
    OrderItem,
    Reservation,
    ReservationTable,
    Tenant,
)
from scripts import import_menu
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
SUMMER = datetime(2026, 7, 1, 18, 30, tzinfo=BERLIN)
WINTER = datetime(2026, 12, 5, 19, 0, tzinfo=BERLIN)

MENU = {
    "menu_items.csv": (
        "number;name;category;price_eur;description;active;pos_code\n"
        "23;Frühlingsrollen (4 Stück);Vorspeisen;6,90;mit Gemüse, dazu Sauce;ja;35B\n"
        '107;Altes Gericht;Hauptgerichte;9,00;"Reis; frisch";nein;\n'
        "47;Ente knusprig;Hauptgerichte;15,50;;ja;\n"
        "s12;Sommerrolle;Vorspeisen;5,00;;ja;S12\n"
    ),
    "item_options.csv": (
        "number;group_name;option_name;price_delta_eur;is_default;required;price_reason\n"
        "47;Fleisch;Ente;0,00;ja;ja;\n"
        "47;Fleisch;Huhn;-1,00;nein;ja;\n"
        "47;Extra;Nudeln;2,50;nein;nein;Nudeln brauchen eine zweite Station\n"
    ),
    "item_allergens.csv": "number;allergen_codes;confirmed_by\n23;F,A;Maxi\n",
    "item_aliases.csv": "number;alias\n23;die knusprigen rollen\n47;ente\n",
}


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine, monkeypatch):
    made = sessionmaker(bind=engine)
    monkeypatch.setattr(offboarding, "SessionLocal", made)
    monkeypatch.setattr(import_menu, "SessionLocal", made)
    with Session(engine) as s:
        yield s


def _tenant(session, name: str, key: str) -> uuid.UUID:
    tenant_id = uuid.UUID(
        seed(session, tenant_name=name, timezone="Europe/Berlin").tenant_id
    )
    session.execute(
        update(Tenant).where(Tenant.id == tenant_id).values(customer_key=key)
    )
    session.commit()
    return tenant_id


def _import(folder, tenant_name: str) -> None:
    assert import_menu.main([str(folder), "--tenant-name", tenant_name]) == 0


def _menu(tmp_path, session, tenant_id) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name, content in MENU.items():
        (source / name).write_text(content, encoding="utf-8")
    _import(source, session.get(Tenant, tenant_id).name)
    # An alias the calls taught: it goes out with the others.
    rolle = session.scalar(
        select(MenuItem.id).where(
            MenuItem.tenant_id == tenant_id, MenuItem.number == "s12"
        )
    )
    session.add(ItemAlias(menu_item_id=rolle, alias="sommer rolle", source="call"))
    session.commit()


def _call(session, tenant_id) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=SUMMER,
        delete_after=SUMMER.date(),
    )
    session.add(call)
    session.flush()
    return call.id


def _order(session, tenant_id, name: str) -> Order:
    ente = session.scalar(
        select(MenuItem).where(MenuItem.tenant_id == tenant_id, MenuItem.number == "47")
    )
    order = Order(
        tenant_id=tenant_id,
        call_id=_call(session, tenant_id),
        type="pickup",
        status="handed_over",
        customer_name=name,
        phone="+4972215551234",
        # As the draft path stores it: base price per position, the
        # options with their own delta (domain/ordering/pricing.py).
        items_total_cents=2 * (1700 + 250),
        total_cents=2 * (1700 + 250),
        ready_at=SUMMER,
        note="Bitte klingeln",
        idempotency_key=uuid.uuid4().hex,
    )
    session.add(order)
    session.flush()
    session.add(
        OrderItem(
            tenant_id=tenant_id,
            order_id=order.id,
            menu_item_id=ente.id,
            quantity=2,
            unit_price_cents=1700,
            options=[
                {"group": "Fleisch", "option": "Ente", "price_delta_cents": 0},
                {"group": "Extra", "option": "Nudeln", "price_delta_cents": 250},
            ],
        )
    )
    session.commit()
    return order


def _reservations(session, tenant_id) -> None:
    area = FloorArea(tenant_id=tenant_id, name="Gastraum", width_cm=800, depth_cm=600)
    session.add(area)
    session.flush()
    table = DiningTable(
        tenant_id=tenant_id,
        area_id=area.id,
        name="T5",
        seats=4,
        width_cm=80,
        depth_cm=80,
        x_cm=100,
        y_cm=100,
    )
    phone = Reservation(
        tenant_id=tenant_id,
        call_id=_call(session, tenant_id),
        status="confirmed",
        guest_name="Weber",
        phone="+4972215550000",
        party_size=4,
        reserved_for=WINTER,
        idempotency_key=uuid.uuid4().hex,
    )
    tablet = Reservation(
        tenant_id=tenant_id,
        source="tablet",
        status="confirmed",
        guest_name="@Stammtisch",
        phone="+4972215550001",
        party_size=6,
        reserved_for=SUMMER,
        idempotency_key=uuid.uuid4().hex,
    )
    session.add_all([table, phone, tablet])
    session.flush()
    session.add(
        ReservationTable(
            tenant_id=tenant_id, reservation_id=tablet.id, table_id=table.id
        )
    )
    session.commit()


def _rows(folder, name: str) -> list[dict[str, str]]:
    with (folder / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


@pytest.fixture
def alpha(session, tmp_path) -> uuid.UUID:
    tenant_id = _tenant(session, "Alpha", "alpha")
    _menu(tmp_path, session, tenant_id)
    return tenant_id


def test_the_menu_goes_back_through_the_import_as_it_came_out(
    session, alpha, tmp_path, capsys
):
    out = tmp_path / "alpha"
    assert offboarding.main(["export", str(out), "--key", "alpha"]) == 0

    _tenant(session, "Beta", "beta")
    _import(out, "Beta")
    again = tmp_path / "beta"
    assert offboarding.main(["export", str(again), "--key", "beta"]) == 0
    for name in offboarding.MENU_FILES:
        assert (again / name).read_bytes() == (out / name).read_bytes(), name

    menu = _rows(out, "menu_items.csv")
    assert [row["number"] for row in menu] == ["23", "47", "107", "s12"]
    assert menu[0]["price_eur"] == "6,90" and menu[0]["pos_code"] == "35B"
    assert menu[2]["active"] == "nein" and menu[2]["description"] == "Reis; frisch"
    assert _rows(out, "item_allergens.csv") == [
        {"number": "23", "allergen_codes": "A,F", "confirmed_by": "Maxi"}
    ]
    huhn = next(r for r in _rows(out, "item_options.csv") if r["option_name"] == "Huhn")
    assert huhn["price_delta_eur"] == "-1,00" and huhn["required"] == "ja"
    aliases = {(r["number"], r["alias"]) for r in _rows(out, "item_aliases.csv")}
    assert ("s12", "sommer rolle") in aliases

    # Into the tenant it came from: nothing to change.
    capsys.readouterr()
    assert import_menu.main([str(out), "--tenant-name", "Alpha", "--dry-run"]) == 0
    assert "Keine Änderung" in capsys.readouterr().out


def test_orders_and_reservations_are_the_tenants_own_in_cents_and_berlin_time(
    session, alpha, tmp_path
):
    beta = _tenant(session, "Beta", "beta")
    (tmp_path / "b").mkdir()
    _menu(tmp_path / "b", session, beta)
    own = _order(session, alpha, "=HYPERLINK(1)")
    _order(session, beta, "Fremd")
    _reservations(session, alpha)
    out = tmp_path / "export"
    assert offboarding.main(["export", str(out), "--key", "alpha"]) == 0

    (order,) = _rows(out, "orders.csv")
    assert order["order_id"] == str(own.id)
    assert order["ready_at"] == "2026-07-01 18:30 CEST"
    assert (order["items_total_cents"], order["total_cents"]) == ("3900", "3900")
    # A spreadsheet must not run a guest's name as a formula.
    assert order["customer_name"] == "'=HYPERLINK(1)"
    assert order["phone"] == "+4972215551234"
    (line,) = _rows(out, "order_items.csv")
    # The price paid: base price plus the option's 250 cents, as in the total.
    assert (line["number"], line["quantity"], line["unit_price_cents"]) == (
        "47",
        "2",
        "1950",
    )
    assert line["line_total_cents"] == order["items_total_cents"]
    assert line["options"] == "Fleisch: Ente, Extra: Nudeln"

    tablet, phone = _rows(out, "reservations.csv")
    assert (tablet["source"], tablet["tables"], tablet["call_id"]) == (
        "tablet",
        "T5",
        "",
    )
    assert tablet["guest_name"] == "'@Stammtisch"
    assert phone["reserved_for"] == "2026-12-05 19:00 CET"
    assert "Fremd" not in (out / "orders.csv").read_text()

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["customer_key"] == "alpha" and manifest["other_tenants"] == 1
    assert manifest["files"]["orders.csv"]["rows"] == 1
    assert manifest["files"]["reservations.csv"]["rows"] == 2
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE((out / name).stat().st_mode) == 0o600
        for name in (*offboarding.FILES, "manifest.json")
    )


def _snapshot(session) -> list:
    session.expire_all()
    tables = (
        "tenants",
        "menu_items",
        "item_options",
        "item_aliases",
        "orders",
        "order_items",
        "reservations",
        "audit_log",
    )
    return [
        session.execute(
            text(
                f"SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {t} t"
            )
        ).scalar()
        for t in tables
    ]


def test_a_rerun_changes_nothing_and_the_database_is_only_read(
    session, alpha, tmp_path
):
    _order(session, alpha, "Müller")
    before = _snapshot(session)
    out = tmp_path / "export"
    assert offboarding.main(["export", str(out), "--key", "alpha"]) == 0
    first = {p.name: p.read_bytes() for p in out.iterdir()}
    assert offboarding.main(["export", str(out), "--key", "alpha"]) == 0
    assert {p.name: p.read_bytes() for p in out.iterdir()} == first
    assert _snapshot(session) == before
    session.commit()


def test_a_new_order_after_the_export_comes_with_the_next_run(session, alpha, tmp_path):
    out = tmp_path / "export"
    assert offboarding.main(["export", str(out), "--key", "alpha"]) == 0
    _order(session, alpha, "Später")
    assert offboarding.main(["export", str(out), "--key", "alpha"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["files"]["orders.csv"]["rows"] == 1


def test_the_database_refuses_a_write_during_the_export(
    session, alpha, tmp_path, monkeypatch
):
    def writes(s, tenant_id):
        s.execute(text("UPDATE menu_items SET price_cents = 1"))
        return {}

    monkeypatch.setattr(offboarding, "menu_files", writes)
    with pytest.raises(InternalError, match="read-only transaction"):
        offboarding.export(session, tmp_path / "x", "alpha", datetime.now(BERLIN))
    session.rollback()
    assert session.scalar(select(func.min(MenuItem.price_cents))) > 1
    assert not (tmp_path / "x").exists()


def test_an_unknown_key_and_a_folder_of_another_customer_are_refused(
    session, alpha, tmp_path, capsys
):
    _tenant(session, "Beta", "beta")
    assert offboarding.main(["export", str(tmp_path / "n"), "--key", "gamma"]) == 2
    assert not (tmp_path / "n").exists()
    out = tmp_path / "export"
    assert offboarding.main(["export", str(out), "--key", "alpha"]) == 0
    manifest = (out / "manifest.json").read_bytes()
    assert offboarding.main(["export", str(out), "--key", "beta"]) == 1
    assert "holds the export of 'alpha'" in capsys.readouterr().err
    assert (out / "manifest.json").read_bytes() == manifest


def test_a_tenant_added_after_the_export_reaches_the_manifest(session, alpha, tmp_path):
    """remove_stack.py refuses on other_tenants: an unchanged export of alpha
    must not keep the 0 of a database that now holds another tenant."""
    out = tmp_path / "export"
    assert offboarding.main(["export", str(out), "--key", "alpha"]) == 0
    assert json.loads((out / "manifest.json").read_text())["other_tenants"] == 0
    _tenant(session, "Beta", "beta")
    assert offboarding.main(["export", str(out), "--key", "alpha"]) == 0
    assert json.loads((out / "manifest.json").read_text())["other_tenants"] == 1
