"""domain/menu/sold_out: Schalter "Gericht aus" (T-4.8, docs/06 §3)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.domain.menu import search_menu
from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    apply,
    parse,
)
from api.domain.menu.sold_out import (
    ACTION_SOLD_OUT,
    list_switches,
    number_key,
    set_sold_out,
    sold_out_change_token,
    sold_out_count,
)
from api.models import AuditLog, MenuItem
from scripts.seed import seed

TZ = "Europe/Berlin"
# Donnerstagabend, 18:00 Ortszeit.
NOW = datetime(2026, 9, 17, 16, 0, tzinfo=UTC)

KARTE = {
    MENU_FILE: (
        "number;name;category;price_eur;description;active\n"
        "2;Edamame;Vorspeisen;4,50;;ja\n"
        "23;Frühlingsrollen;Vorspeisen;6,90;;ja\n"
        "23a;Frühlingsrollen vegan;Vorspeisen;6,90;;ja\n"
        "24;Sommerrollen;Vorspeisen;7,50;;ja\n"
        "12;Wan-Tan-Suppe;Suppen;5,00;;ja\n"
        "13;Pho Bo;Suppen;11,90;;ja\n"
        "50;Altes Gericht;Vorspeisen;9,00;;nein\n"
    ),
    OPTIONS_FILE: "number;group_name;option_name;price_delta_eur;is_default;required\n",
    ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n",
    ALIASES_FILE: "number;alias\n",
}


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tid = uuid.UUID(seed(session, tenant_name="Aus", timezone=TZ).tenant_id)
    plan = parse(KARTE)
    assert plan.ok, plan.errors
    apply(session, tid, plan, now=NOW)
    return tid


def _item(session, tenant_id, number) -> MenuItem:
    return session.scalars(
        select(MenuItem).where(
            MenuItem.tenant_id == tenant_id, MenuItem.number == number
        )
    ).one()


def test_liste_in_kartenreihenfolge_ohne_inaktive(session, tenant_id):
    numbers = [d.number for d in list_switches(session, tenant_id, now=NOW)]
    assert numbers == ["2", "12", "13", "23", "23a", "24"]


@pytest.mark.parametrize(
    ("suche", "nummern"),
    [
        ("2", ["2", "23", "23a", "24"]),
        ("rollen", ["23", "23a", "24"]),
        ("pho", ["13"]),
        ("", None),
    ],
)
def test_suchfeld_nummer_oder_name(session, tenant_id, suche, nummern):
    found = [d.number for d in list_switches(session, tenant_id, suche, now=NOW)]
    assert found == (nummern or ["2", "12", "13", "23", "23a", "24"])


def test_aus_bis_ende_des_betriebstags(session, tenant_id):
    """Ein Tap: bis zum naechsten Morgen 05:00 Ortszeit (Betriebstag, core/time)."""
    item = _item(session, tenant_id, "23")
    set_sold_out(session, tenant_id, item.id, True, TZ, now=NOW)
    session.refresh(item)
    # 18.09.2026 05:00 in Berlin (Sommerzeit) = 03:00 UTC.
    assert item.sold_out_until == datetime(2026, 9, 18, 3, 0, tzinfo=UTC)
    assert sold_out_count(session, tenant_id, NOW) == 1
    # Am naechsten Morgen von selbst wieder da.
    assert sold_out_count(session, tenant_id, NOW + timedelta(hours=12)) == 0


def test_nach_mitternacht_gilt_derselbe_betriebstag(session, tenant_id):
    item = _item(session, tenant_id, "23")
    set_sold_out(
        session,
        tenant_id,
        item.id,
        True,
        TZ,
        now=datetime(2026, 9, 17, 23, 30, tzinfo=UTC),
    )
    session.refresh(item)
    assert item.sold_out_until == datetime(2026, 9, 18, 3, 0, tzinfo=UTC)


def test_wieder_da_und_audit(session, tenant_id):
    item = _item(session, tenant_id, "23")
    set_sold_out(session, tenant_id, item.id, True, TZ, now=NOW)
    set_sold_out(session, tenant_id, item.id, False, TZ, now=NOW)
    session.refresh(item)
    assert item.sold_out_until is None
    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.action == ACTION_SOLD_OUT, AuditLog.entity_id == item.id)
        .order_by(AuditLog.id)
    ).all()
    assert [(r.payload["from"], r.payload["to"]) for r in rows] == [
        (False, True),
        (True, False),
    ]


def test_doppelter_tap_schreibt_einmal(session, tenant_id):
    """Zwei Tablets tippen gleichzeitig "Heute aus": ein Eintrag, kein Fehler."""
    item = _item(session, tenant_id, "23")
    set_sold_out(session, tenant_id, item.id, True, TZ, now=NOW)
    set_sold_out(session, tenant_id, item.id, True, TZ, now=NOW)
    count = len(
        session.scalars(
            select(AuditLog).where(
                AuditLog.action == ACTION_SOLD_OUT, AuditLog.entity_id == item.id
            )
        ).all()
    )
    assert count == 1


@pytest.mark.parametrize("nummer", ["50", None])
def test_inaktives_oder_fremdes_gericht(session, tenant_id, nummer):
    item_id = _item(session, tenant_id, nummer).id if nummer else uuid.uuid4()
    with pytest.raises(NotFound):
        set_sold_out(session, tenant_id, item_id, True, TZ, now=NOW)


def test_token_aendert_sich_mit_dem_schalter(session, tenant_id):
    before = sold_out_change_token(session, tenant_id, NOW)
    item = _item(session, tenant_id, "23")
    set_sold_out(session, tenant_id, item.id, True, TZ, now=NOW)
    after = sold_out_change_token(session, tenant_id, NOW)
    assert before != after
    # Abgelaufen: wieder der Stand ohne ausverkaufte Gerichte.
    assert (
        sold_out_change_token(session, tenant_id, NOW + timedelta(hours=12)) == before
    )


def test_agent_nennt_alternativen_derselben_kategorie(session, tenant_id):
    """docs/06 §3: der Agent bietet es nicht mehr an und nennt eine Alternative -
    die naechsten Nummern derselben Kategorie, die heute zu haben sind."""
    for number in ("23", "23a"):
        set_sold_out(
            session, tenant_id, _item(session, tenant_id, number).id, True, TZ, now=NOW
        )
    result = search_menu(session, tenant_id, "die 23", now=NOW)
    assert result.results[0].sold_out
    assert result.say == (
        "Frühlingsrollen ist heute leider aus. "
        "Stattdessen hätte ich Nummer 24 Sommerrollen oder Nummer 2 Edamame."
    )


def test_ohne_alternative_nur_heute_aus(session, tenant_id):
    for number in ("12", "13"):
        set_sold_out(
            session, tenant_id, _item(session, tenant_id, number).id, True, TZ, now=NOW
        )
    result = search_menu(session, tenant_id, "die 13", now=NOW)
    assert result.say == "Pho Bo ist heute leider aus."


@pytest.mark.parametrize(
    ("nummern", "sortiert"),
    [(["12", "2", "23a", "23", "B1"], ["2", "12", "23", "23a", "B1"])],
)
def test_kartenreihenfolge(nummern, sortiert):
    assert sorted(nummern, key=number_key) == sortiert


def test_name_mit_ziffer_vorn_wird_gefunden(session, tenant_id):
    """Codex PR #146, P2: "8 Kostbarkeiten" beginnt mit einer Ziffer und ist
    trotzdem ein Name - das Suchfeld findet es."""
    item = _item(session, tenant_id, "12")
    item.name = "8 Kostbarkeiten"
    session.commit()
    found = [d.number for d in list_switches(session, tenant_id, "8 Kostbar", now=NOW)]
    assert found == ["12"]
