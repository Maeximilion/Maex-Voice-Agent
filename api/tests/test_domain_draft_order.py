"""domain/ordering: draft_order rechnet, prüft und liest vor (T-4.5, docs/04 §draft_order)."""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.core.errors import Closed, Conflict, InvalidInput, NotFound
from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    apply,
    parse,
)
from api.domain.ordering import draft_order
from api.domain.ordering.draft import WARNING_READY_AFTER_CLOSE
from api.domain.ordering.readback import spoken_euro, spoken_times
from api.models import AuditLog, Call, MenuItem, OpeningHours, Order, OrderItem
from api.schemas.orders import DraftOrderRequest
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Seed: Montag Ruhetag, sonst 11:30-14:00 und 17:00-22:00 für alle Services.
DIENSTAG = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 18, 0, tzinfo=BERLIN)

KARTE = {
    MENU_FILE: (
        "number;name;category;price_eur;description;active\n"
        "23;Frühlingsrollen;Vorspeisen;6,90;;ja\n"
        "13;Pho Bo;Suppen;11,90;;ja\n"
        "47;Ente knusprig;Hauptgerichte;15,50;;ja\n"
        "50;Altes Gericht;Hauptgerichte;9,00;;nein\n"
    ),
    OPTIONS_FILE: (
        "number;group_name;option_name;price_delta_eur;is_default;required\n"
        "47;Fleisch;Ente;0,00;ja;ja\n"
        "47;Fleisch;Huhn;-1,00;nein;ja\n"
        "47;Sauce;Erdnuss;0,50;nein;nein\n"
    ),
    ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n",
    ALIASES_FILE: "number;alias\n",
}


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _tenant(session, name: str) -> uuid.UUID:
    tid = uuid.UUID(seed(session, tenant_name=name, timezone="Europe/Berlin").tenant_id)
    plan = parse(KARTE)
    assert plan.ok, plan.errors
    apply(session, tid, plan, now=NOW)
    return tid


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return _tenant(session, "Testbetrieb")


def _call(session, tenant_id) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return call.id


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    return _call(session, tenant_id)


def item(session, tenant_id, number: str) -> uuid.UUID:
    return session.scalar(
        select(MenuItem.id).where(
            MenuItem.tenant_id == tenant_id, MenuItem.number == number
        )
    )


def request(session, tenant_id, call_id, items=None, **overrides) -> DraftOrderRequest:
    body = {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "idempotency_key": uuid.uuid4().hex,
        "type": "pickup",
        "customer": {"name": "Müller", "phone": "0721 555 1234"},
        "items": items
        if items is not None
        else [{"menu_item_id": item(session, tenant_id, "23"), "quantity": 2}],
        **overrides,
    }
    return DraftOrderRequest(**body)


def ente(session, tenant_id, *options, quantity=1) -> dict:
    return {
        "menu_item_id": item(session, tenant_id, "47"),
        "quantity": quantity,
        "options": [{"group": g, "name": n} for g, n in options],
    }


# --- Normalfall --------------------------------------------------------------------


def test_entwurf_mit_summe_readback_und_audit(session, tenant_id, call_id):
    items = [
        {
            "menu_item_id": item(session, tenant_id, "23"),
            "quantity": 2,
            "note": "ohne Zwiebeln",
        },
        ente(session, tenant_id, ("Fleisch", "Huhn"), ("Sauce", "Erdnuss")),
    ]
    draft = draft_order(session, request(session, tenant_id, call_id, items), now=NOW)

    # 2 x 6,90 + (15,50 - 1,00 + 0,50) = 13,80 + 15,00
    assert draft.items_total_cents == 2880
    assert draft.delivery_fee_cents == 0
    assert draft.total_cents == 2880
    assert draft.status == "draft"
    assert draft.warnings == []
    assert draft.ready_at == NOW + timedelta(minutes=20)
    assert draft.readback == (
        "Zweimal Nummer 23 Frühlingsrollen, ohne Zwiebeln. "
        "Einmal Nummer 47 Ente knusprig mit Huhn und Erdnuss. "
        "Macht 28,80 Euro, abholbereit in etwa 20 Minuten, auf den Namen Müller. "
        "Passt das so?"
    )

    order = session.get(Order, draft.order_id)
    assert order.phone == "+497215551234"
    assert order.call_id == call_id
    rows = session.scalars(
        select(OrderItem)
        .where(OrderItem.order_id == order.id)
        .order_by(OrderItem.created_at)
    ).all()
    # Eingefroren ist der Kartenpreis, die Optionen tragen ihre Differenz selbst.
    assert [(r.quantity, r.unit_price_cents) for r in rows] == [(2, 690), (1, 1550)]
    assert rows[1].options == [
        {"group": "Fleisch", "option": "Huhn", "price_delta_cents": -100},
        {"group": "Sauce", "option": "Erdnuss", "price_delta_cents": 50},
    ]
    audit = session.scalar(select(AuditLog).where(AuditLog.entity_id == order.id))
    assert (
        audit.action == "order.draft_created" and audit.payload["total_cents"] == 2880
    )


def test_optionsnamen_ohne_gross_klein(session, tenant_id, call_id):
    items = [ente(session, tenant_id, ("fleisch", "  ENTE "))]
    draft = draft_order(session, request(session, tenant_id, call_id, items), now=NOW)
    assert draft.total_cents == 1550


# --- Idempotenz --------------------------------------------------------------------


def test_gleicher_schluessel_gleiche_antwort(session, tenant_id, call_id):
    items = [
        ente(session, tenant_id, ("Fleisch", "Ente")),
        {"menu_item_id": item(session, tenant_id, "13"), "quantity": 1},
        {"menu_item_id": item(session, tenant_id, "23"), "quantity": 3},
    ]
    req = request(session, tenant_id, call_id, items)
    first = draft_order(session, req, now=NOW)
    # Später, nach Schluss: der Replay rechnet nichts neu und sagt dasselbe.
    again = draft_order(session, req, now=NOW + timedelta(hours=5))

    assert again == first
    assert session.scalar(select(func.count()).select_from(Order)) == 1
    assert session.scalar(select(func.count()).select_from(OrderItem)) == 3


def test_replay_nach_umbenennung_sagt_dasselbe(session, tenant_id, call_id):
    """Codex PR #124: der Replay las Name und Nummer aus der aktuellen Karte."""
    req = request(session, tenant_id, call_id)
    first = draft_order(session, req, now=NOW)
    session.execute(
        update(MenuItem)
        .where(MenuItem.tenant_id == tenant_id, MenuItem.number == "23")
        .values(number="99", name="Sommerrollen")
    )
    session.commit()
    assert draft_order(session, req, now=NOW) == first


def test_replay_nach_neuen_oeffnungszeiten_warnt_gleich(session, tenant_id, call_id):
    """Codex PR #124: die Warnung wurde beim Replay aus dem aktuellen Plan neu berechnet."""
    req = request(session, tenant_id, call_id)
    first = draft_order(session, req, now=NOW)
    assert first.warnings == []
    session.execute(
        update(OpeningHours)
        .where(
            OpeningHours.tenant_id == tenant_id, OpeningHours.opens_at == time(17, 0)
        )
        .values(closes_at=time(18, 10))
    )
    session.commit()
    assert draft_order(session, req, now=NOW).warnings == []


@pytest.mark.parametrize("sekunde", [0, 1, 31, 59])
def test_wartezeit_wird_nie_kuerzer_angesagt(session, tenant_id, call_id, sekunde):
    """Codex PR #124: um 18:00:45 wurden aus 20 Minuten Wartezeit 19 angesagt."""
    now = NOW.replace(second=sekunde, microsecond=500)
    draft = draft_order(session, request(session, tenant_id, call_id), now=now)
    assert draft.ready_at >= now + timedelta(minutes=20)
    assert draft.ready_at.second == 0 and draft.ready_at.microsecond == 0
    assert "abholbereit in etwa 20 Minuten" in draft.readback


def test_schluessel_eines_anderen_mandanten(session, tenant_id, call_id):
    req = request(session, tenant_id, call_id)
    draft_order(session, req, now=NOW)
    other = _tenant(session, "Anderer Betrieb")
    foreign = request(
        session, other, _call(session, other), idempotency_key=req.idempotency_key
    )
    with pytest.raises(Conflict):
        draft_order(session, foreign, now=NOW)


# --- Prüfungen ---------------------------------------------------------------------


def test_abholung_geschlossen(session, tenant_id, call_id):
    montag = datetime.combine(DIENSTAG - timedelta(days=1), time(18, 0), tzinfo=BERLIN)
    with pytest.raises(Closed) as err:
        draft_order(session, request(session, tenant_id, call_id), now=montag)
    assert err.value.say == "Abholung ist gerade leider nicht möglich."
    assert session.scalar(select(func.count()).select_from(Order)) == 0


def test_zwischen_den_fenstern_geschlossen(session, tenant_id, call_id):
    nachmittag = datetime.combine(DIENSTAG, time(15, 0), tzinfo=BERLIN)
    with pytest.raises(Closed):
        draft_order(session, request(session, tenant_id, call_id), now=nachmittag)


def test_fertig_nach_schluss_warnt(session, tenant_id, call_id):
    spaet = datetime.combine(DIENSTAG, time(21, 50), tzinfo=BERLIN)
    draft = draft_order(session, request(session, tenant_id, call_id), now=spaet)
    assert draft.warnings == [WARNING_READY_AFTER_CLOSE]


def test_ausverkauft(session, tenant_id, call_id):
    session.execute(
        update(MenuItem)
        .where(MenuItem.tenant_id == tenant_id, MenuItem.number == "23")
        .values(sold_out_until=NOW + timedelta(hours=3))
    )
    session.commit()
    with pytest.raises(Conflict) as err:
        draft_order(session, request(session, tenant_id, call_id), now=NOW)
    assert err.value.say == "Frühlingsrollen ist heute leider aus."


def test_inaktives_gericht(session, tenant_id, call_id):
    items = [{"menu_item_id": item(session, tenant_id, "50"), "quantity": 1}]
    with pytest.raises(NotFound):
        draft_order(session, request(session, tenant_id, call_id, items), now=NOW)


def test_gericht_eines_anderen_mandanten(session, tenant_id, call_id):
    other = _tenant(session, "Anderer Betrieb")
    items = [{"menu_item_id": item(session, other, "23"), "quantity": 1}]
    with pytest.raises(NotFound):
        draft_order(session, request(session, tenant_id, call_id, items), now=NOW)


def test_pflichtgruppe_fehlt_wird_erfragt_nicht_geraten(session, tenant_id, call_id):
    # "Ente" ist Voreinstellung - trotzdem wird gefragt, nicht still gewählt.
    items = [ente(session, tenant_id, ("Sauce", "Erdnuss"))]
    with pytest.raises(InvalidInput) as err:
        draft_order(session, request(session, tenant_id, call_id, items), now=NOW)
    assert err.value.say == "Welche Auswahl bei Fleisch möchten Sie zu Ente knusprig?"


def test_pflichtgruppe_zweimal(session, tenant_id, call_id):
    items = [ente(session, tenant_id, ("Fleisch", "Ente"), ("Fleisch", "Huhn"))]
    with pytest.raises(InvalidInput) as err:
        draft_order(session, request(session, tenant_id, call_id, items), now=NOW)
    assert "nur eine Auswahl" in err.value.say


def test_unbekannte_option(session, tenant_id, call_id):
    items = [ente(session, tenant_id, ("Fleisch", "Tofu"))]
    with pytest.raises(InvalidInput) as err:
        draft_order(session, request(session, tenant_id, call_id, items), now=NOW)
    assert err.value.say == "Tofu gibt es zu Ente knusprig nicht."


def test_option_eines_anderen_gerichts(session, tenant_id, call_id):
    items = [
        {
            "menu_item_id": item(session, tenant_id, "23"),
            "quantity": 1,
            "options": [{"group": "Sauce", "name": "Erdnuss"}],
        }
    ]
    with pytest.raises(InvalidInput):
        draft_order(session, request(session, tenant_id, call_id, items), now=NOW)


def test_lieferung_noch_nicht(session, tenant_id, call_id):
    with pytest.raises(InvalidInput) as err:
        draft_order(
            session, request(session, tenant_id, call_id, type="delivery"), now=NOW
        )
    assert "Möchten Sie abholen?" in err.value.say


def test_unbekannter_anruf(session, tenant_id):
    with pytest.raises(NotFound):
        draft_order(session, request(session, tenant_id, uuid.uuid4()), now=NOW)


def test_leerer_name(session, tenant_id, call_id):
    req = request(session, tenant_id, call_id)
    req.customer.name = "   "
    with pytest.raises(InvalidInput):
        draft_order(session, req, now=NOW)


# --- Vorlesen ohne Datenbank -------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "wort"), [(1, "einmal"), (2, "zweimal"), (12, "zwölfmal"), (15, "15 mal")]
)
def test_spoken_times(n, wort):
    assert spoken_times(n) == wort


@pytest.mark.parametrize(
    ("cents", "text"), [(1730, "17,30 Euro"), (700, "7 Euro"), (5, "0,05 Euro")]
)
def test_spoken_euro(cents, text):
    assert spoken_euro(cents) == text
