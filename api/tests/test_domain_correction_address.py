"""The address of a delivery, corrected on the tablet (#525, Maxi 03.11.2026).

"Falsche Adresse" is a reason only when the team typed a new address: the
domain writes a row for the order's customer, takes zone and fee of its postal
code from the database (CLAUDE.md §2 rule 1) and sends the new revision, so
the slip at the register names the address the driver must go to. An address
outside every zone is refused and nothing changes.
"""

import dataclasses
import json
import uuid

import pytest
from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput
from api.domain.ordering import draft_order
from api.domain.ordering.board import (
    CORRECTED_SINCE,
    approve_order,
    mark_entered,
    version_of,
)
from api.domain.ordering.correction import (
    ADDRESS_INCOMPLETE,
    ADDRESS_NO_CUSTOMER,
    ADDRESS_OUT_OF_ZONE,
    ADDRESS_POSTAL_CODE,
    ADDRESS_TOO_LONG,
    ADDRESS_TWO_ZONES,
    NEW_ADDRESS_NEEDED,
    NO_CHANGE,
    STALE,
    AddressEdit,
    CorrectionRequest,
    RowEdit,
    apply_correction,
    preview_correction,
)
from api.domain.ordering.labels import ACTION_CORRECTED
from api.events.types import ORDER_CONFIRMED
from api.models import (
    Address,
    AuditLog,
    Customer,
    DeliveryZone,
    Order,
    OutboxEvent,
    ServiceConfig,
)
from api.tests.test_domain_draft_delivery import (
    FEE,
    HOME,
    PHONE,
    _zone,
    confirmed,
    delivery,
)
from api.tests.test_domain_draft_order import NOW, _call, _tenant, request
from printbridge.escpos import BOLD_OFF, BOLD_ON, render

FAR_FEE = 450
# The house number was misheard: the same postal code, the same zone.
NEXT_DOOR = AddressEdit(
    street="Lindenstraße",
    house_number="21",
    postal_code="12345",
    city="Musterdorf",
    floor_note="EG",
)
# The postal code was misheard: another zone, another fee.
FAR = AddressEdit(
    street="Kaiserstraße",
    house_number="7",
    postal_code="54321",
    city="Weiterdorf",
    floor_note="3. OG, Klingel Braun",
)
NOWHERE = AddressEdit(
    street="Irgendweg", house_number="1", postal_code="99999", city="Fernstadt"
)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tid = _tenant(session, "Testbetrieb")
    session.add(_zone(tid))
    session.add(
        _zone(
            tid,
            name="Zone 2",
            postal_codes=["54321"],
            fee_cents=FAR_FEE,
            # Above the 13,80 of the order: the minimum binds the agent, not the team.
            min_order_cents=2000,
            eta_minutes=70,
        )
    )
    # The kitchen gets its slip at the guest's yes.
    session.execute(update(ServiceConfig).values(call_mode="primary"))
    session.commit()
    return tid


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    return _call(session, tenant_id, caller_id=PHONE)


@pytest.fixture
def order(session, tenant_id, call_id) -> Order:
    """Two times dish 23 to HOME, confirmed: 13,80 plus the fee of zone 1."""
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)
    confirmed(session, tenant_id, call_id, draft.order_id)
    return session.get_one(Order, draft.order_id)


def _request(session, tenant_id, order, **edits) -> CorrectionRequest:
    plan = preview_correction(session, tenant_id, order.id)
    return CorrectionRequest(version=plan.version, edit_id=uuid.uuid4().hex, **edits)


def _fresh(session, order) -> Order:
    session.expire_all()
    return session.get_one(Order, order.id)


def _address(session, order) -> Address:
    return session.get_one(Address, _fresh(session, order).address_id)


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def _corrections(session, order) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == order.id, AuditLog.action == ACTION_CORRECTED
            )
        )
    )


def _ticket_sent(session, order) -> None:
    """The bridge printed the first slip: a correction is a KORREKTUR slip."""
    session.execute(update(OutboxEvent).values(status="sent", attempts=1))
    session.execute(
        update(Order).where(Order.id == order.id).values(handover_state="sent")
    )
    session.commit()


def test_an_address_change_alone_is_a_change(session, tenant_id, order):
    old = order.address_id
    req = _request(session, tenant_id, order, address=NEXT_DOOR)

    plan = preview_correction(session, tenant_id, order.id, req)
    apply_correction(session, tenant_id, order.id, req, "wrong_address")

    assert plan.changed and plan.blockers == []
    assert plan.fee_cents == FEE and plan.total_cents == 1380 + FEE
    row = _address(session, order)
    assert row.id != old
    assert (row.street, row.house_number, row.postal_code, row.city) == (
        "Lindenstraße",
        "21",
        "12345",
        "Musterdorf",
    )
    assert row.floor_note == "EG"
    assert row.customer_id == order.customer_id and not row.is_default
    # The misheard row stays: it is history, and maybe another order's address.
    assert session.get(Address, old) is not None
    after = _fresh(session, order)
    assert after.delivery_fee_cents == FEE and after.total_cents == 1380 + FEE
    [audit] = _corrections(session, order)
    assert audit.payload["reason"] == "wrong_address"
    assert audit.payload["address_changed"] is True
    assert audit.payload["before"] == audit.payload["after"]
    # The audit_log outlives the order: no street, no floor note in it.
    text = json.dumps(audit.payload, ensure_ascii=False)
    assert "Lindenstraße" not in text and "EG" not in text


def test_an_address_and_a_dish_change_together(session, tenant_id, order):
    [line] = preview_correction(session, tenant_id, order.id).rows
    assert line.order_item_id is not None
    req = _request(
        session,
        tenant_id,
        order,
        rows=(RowEdit(order_item_id=line.order_item_id, quantity=3),),
        address=FAR,
    )

    plan = preview_correction(session, tenant_id, order.id, req)
    apply_correction(session, tenant_id, order.id, req, "other")

    assert plan.items_total_cents == 2070 and plan.total_cents == 2070 + FAR_FEE
    after = _fresh(session, order)
    assert after.items_total_cents == 2070
    assert after.delivery_fee_cents == FAR_FEE
    assert after.total_cents == 2070 + FAR_FEE
    assert _address(session, order).postal_code == "54321"
    [audit] = _corrections(session, order)
    assert audit.payload["after"][0]["quantity"] == 3
    assert audit.payload["before_fee_cents"] == FEE
    assert audit.payload["after_fee_cents"] == FAR_FEE
    assert audit.payload["after_total_cents"] == 2070 + FAR_FEE


def test_an_address_outside_every_zone_is_refused_and_nothing_changes(
    session, tenant_id, order
):
    before = (order.address_id, order.delivery_fee_cents, order.total_cents)
    counts = [_count(session, m) for m in (Address, OutboxEvent, AuditLog)]
    req = _request(session, tenant_id, order, address=NOWHERE)
    refused = ADDRESS_OUT_OF_ZONE.format(postal_code="99999")

    plan = preview_correction(session, tenant_id, order.id, req)
    with pytest.raises(InvalidInput) as err:
        apply_correction(session, tenant_id, order.id, req, "wrong_address")
    session.rollback()

    assert plan.blockers == [refused]
    assert err.value.say == refused
    after = _fresh(session, order)
    assert (after.address_id, after.delivery_fee_cents, after.total_cents) == before
    assert [_count(session, m) for m in (Address, OutboxEvent, AuditLog)] == counts


def test_the_fee_follows_the_zone_of_the_new_address(session, tenant_id, order):
    req = _request(session, tenant_id, order, address=FAR)

    plan = preview_correction(session, tenant_id, order.id, req)
    apply_correction(session, tenant_id, order.id, req, "wrong_address")

    assert (plan.before_fee_cents, plan.fee_cents) == (FEE, FAR_FEE)
    assert plan.total_cents == 1380 + FAR_FEE
    after = _fresh(session, order)
    # Below the minimum of zone 2 and still saved: the team decides on the tablet.
    assert after.items_total_cents == 1380
    assert after.delivery_fee_cents == FAR_FEE
    assert after.total_cents == 1380 + FAR_FEE
    row = _address(session, order)
    assert row.zone_id is not None and row.postal_code == "54321"
    # The time the guest was told stays.
    assert after.ready_at == order.ready_at


def test_back_to_the_near_zone_the_fee_goes_down_again(session, tenant_id, order):
    apply_correction(
        session,
        tenant_id,
        order.id,
        _request(session, tenant_id, order, address=FAR),
        "wrong_address",
    )
    apply_correction(
        session,
        tenant_id,
        order.id,
        _request(session, tenant_id, order, address=NEXT_DOOR),
        "wrong_address",
    )

    after = _fresh(session, order)
    assert after.delivery_fee_cents == FEE and after.total_cents == 1380 + FEE


def test_a_pickup_offers_no_address_edit(session, tenant_id, call_id):
    draft = draft_order(session, request(session, tenant_id, call_id), now=NOW)
    confirmed(session, tenant_id, call_id, draft.order_id)
    pickup = session.get_one(Order, draft.order_id)

    plan = preview_correction(session, tenant_id, pickup.id)
    req = _request(session, tenant_id, pickup, address=NEXT_DOOR)
    with pytest.raises(InvalidInput):
        preview_correction(session, tenant_id, pickup.id, req)
    with pytest.raises(InvalidInput):
        apply_correction(session, tenant_id, pickup.id, req, "other")
    session.rollback()

    assert not plan.where.delivery and plan.where.current is None
    assert "wrong_address" not in plan.reasons
    assert _fresh(session, pickup).address_id is None


def test_the_slip_of_the_new_revision_shows_the_new_address(session, tenant_id, order):
    _ticket_sent(session, order)
    req = _request(session, tenant_id, order, address=FAR)

    apply_correction(session, tenant_id, order.id, req, "wrong_address")

    payload = session.scalars(
        select(OutboxEvent.payload)
        .where(OutboxEvent.event_type == ORDER_CONFIRMED)
        .order_by(OutboxEvent.created_at.desc())
        .limit(1)
    ).one()
    assert payload["revision"] == 1
    assert payload["correction_reason"] == "wrong_address"
    slip = render(payload, printed_at=NOW)
    block = (
        BOLD_ON
        + "Kaiserstraße 7\n".encode("cp858")
        + b"54321 Weiterdorf\n"
        + BOLD_OFF
        + b"Hinweis: 3. OG, Klingel Braun\n"
    )
    assert block in slip
    assert HOME["street"].encode("cp858") not in slip
    assert b"KORREKTUR" in slip and b"Grund: falsche Adresse\n" in slip
    assert b"Liefergeb\x81hr 4,50 EUR\n" in slip
    assert b"Summe 18,30 EUR\n" in slip


def test_falsche_adresse_needs_a_new_address(session, tenant_id, order):
    [line] = preview_correction(session, tenant_id, order.id).rows
    assert line.order_item_id is not None
    req = _request(
        session,
        tenant_id,
        order,
        rows=(RowEdit(order_item_id=line.order_item_id, quantity=3),),
    )

    with pytest.raises(InvalidInput) as err:
        apply_correction(session, tenant_id, order.id, req, "wrong_address")
    session.rollback()
    apply_correction(session, tenant_id, order.id, req, "wrong_quantity")

    assert err.value.say == NEW_ADDRESS_NEEDED
    assert _fresh(session, order).items_total_cents == 2070


def test_the_same_address_typed_again_is_no_change(session, tenant_id, order):
    same = AddressEdit(
        street=" lindenstraße ",
        house_number="12",
        postal_code="12345",
        city="MUSTERDORF",
        floor_note="2. OG, Klingel Braun",
    )
    req = _request(session, tenant_id, order, address=same)

    plan = preview_correction(session, tenant_id, order.id, req)

    assert not plan.changed and plan.blockers == [NO_CHANGE]


@pytest.mark.parametrize("case", ["postal_code", "two_zones", "erased", "too_long"])
def test_an_address_that_cannot_be_saved_changes_nothing(
    session, tenant_id, order, case
):
    """Fails closed: a sentence for the team, no row, no fee, no slip."""
    address, refused = FAR, ADDRESS_TWO_ZONES.format(postal_code="54321")
    if case == "postal_code":
        address = dataclasses.replace(FAR, postal_code="5432")
        refused = ADDRESS_POSTAL_CODE.format(postal_code="5432")
    elif case == "two_zones":
        # One fee would be a guess (hard rule 2).
        session.add(_zone(tenant_id, name="Zone 3", postal_codes=["54321"]))
    elif case == "erased":
        # Erasure on request: the order keeps no customer and no address.
        session.execute(delete(Customer).where(Customer.id == order.customer_id))
        refused = ADDRESS_NO_CUSTOMER
    else:
        address = dataclasses.replace(FAR, street="K" * 201)
        refused = ADDRESS_TOO_LONG
    session.commit()
    before = _fresh(session, order)
    kept = (before.address_id, before.delivery_fee_cents, before.total_cents)
    counts = [_count(session, m) for m in (Address, OutboxEvent, AuditLog)]
    req = _request(session, tenant_id, order, address=address)

    plan = preview_correction(session, tenant_id, order.id, req)
    with pytest.raises(InvalidInput) as err:
        apply_correction(session, tenant_id, order.id, req, "wrong_address")
    session.rollback()

    assert plan.blockers == [refused] and err.value.say == refused
    after = _fresh(session, order)
    assert (after.address_id, after.delivery_fee_cents, after.total_cents) == kept
    assert [_count(session, m) for m in (Address, OutboxEvent, AuditLog)] == counts


def test_an_address_without_a_house_number_is_not_taken(session, tenant_id, order):
    gap = AddressEdit(
        street="Lindenstraße", house_number=" ", postal_code="12345", city="Musterdorf"
    )
    req = _request(session, tenant_id, order, address=gap)

    plan = preview_correction(session, tenant_id, order.id, req)
    with pytest.raises(InvalidInput):
        apply_correction(session, tenant_id, order.id, req, "wrong_address")

    assert plan.blockers == [ADDRESS_INCOMPLETE]


def test_a_card_from_before_the_address_change_is_stale(session, tenant_id, order):
    """The register holds the address the team typed: a card older than the
    change can neither mark nor correct the new state."""
    approve_order(session, tenant_id, order.id)
    seen = version_of(session, order)
    old_card = _request(session, tenant_id, order, address=NEXT_DOOR)
    apply_correction(
        session,
        tenant_id,
        order.id,
        _request(session, tenant_id, order, address=FAR),
        "wrong_address",
    )

    with pytest.raises(Conflict) as marked:
        mark_entered(session, tenant_id, order.id, seen)
    session.rollback()
    with pytest.raises(Conflict) as corrected:
        apply_correction(session, tenant_id, order.id, old_card, "wrong_address")
    session.rollback()

    assert marked.value.say == CORRECTED_SINCE
    assert corrected.value.say == STALE
    assert _address(session, order).postal_code == "54321"


def test_back_at_the_first_address_with_another_fee_the_card_is_stale(
    session, tenant_id, order
):
    """Codex #537: the zone's fee changed since the yes. Away and back, the
    order points at its first row again with the dishes unchanged, but the
    fee of today's zone makes another total: a card from before can neither
    mark that state nor correct it."""
    approve_order(session, tenant_id, order.id)
    seen = version_of(session, order)
    old_card = _request(session, tenant_id, order, address=NEXT_DOOR)
    first = order.address_id
    session.execute(
        update(DeliveryZone).where(DeliveryZone.name == "Zone 1").values(fee_cents=300)
    )
    session.commit()
    for address in (FAR, AddressEdit(**HOME)):
        apply_correction(
            session,
            tenant_id,
            order.id,
            _request(session, tenant_id, order, address=address),
            "wrong_address",
        )
    after = _fresh(session, order)
    assert after.address_id == first and after.total_cents == 1380 + 300

    with pytest.raises(Conflict) as marked:
        mark_entered(session, tenant_id, order.id, seen)
    session.rollback()
    with pytest.raises(Conflict):
        apply_correction(session, tenant_id, order.id, old_card, "wrong_address")
    session.rollback()

    assert marked.value.say == CORRECTED_SINCE
