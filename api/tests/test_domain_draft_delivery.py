"""draft_order for a delivery (T-6.5, docs/04 §draft_order, docs/05 §Lieferung).

Fee, minimum and time only from the database (hard rule 1), nothing confirmed
without a yes (rule 3), a stored address or name never said aloud (legal check
L12), and the total with the fee read before the yes (N2).
"""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import (
    SAY_ON_FAILURE,
    BelowMinimum,
    Closed,
    Conflict,
    InvalidInput,
    OutOfZone,
    ServiceUnavailable,
)
from api.domain.callbacks import create_callback, transfer_to_team
from api.domain.confirm import confirm
from api.domain.customers import SAY_SAME_ADDRESS, find_customer
from api.domain.delivery.address import SAY_WHICH_NAME
from api.domain.delivery.zones import SAY_OUT_OF_ZONE
from api.domain.ordering import draft_order
from api.domain.ordering.confirm import confirm_order
from api.domain.ordering.correction import (
    CorrectionRequest,
    RowEdit,
    apply_correction,
    preview_correction,
)
from api.domain.ordering.draft import WARNING_READY_AFTER_CLOSE
from api.domain.ordering.labels import ACTION_CORRECTED
from api.domain.ordering.validation import SAY_DELIVERY_CLOSED, SAY_PICKUP_CLOSED
from api.domain.status import config as status_config
from api.events.types import ORDER_CONFIRMED
from api.jobs.retention import purge
from api.models import (
    Address,
    AuditLog,
    Customer,
    DeliveryZone,
    OpeningHours,
    Order,
    OutboxEvent,
    RecognitionObjection,
    Reservation,
    ServiceConfig,
)
from api.schemas.callbacks import CreateCallbackRequest
from api.schemas.confirm import ConfirmRequest
from api.schemas.transfer import TransferToTeamRequest
from api.tests.test_domain_draft_order import NOW, _call, _tenant, request

PHONE = "+497215551234"
HOME = {
    "street": "Lindenstraße",
    "house_number": "12",
    "postal_code": "12345",
    "city": "Musterdorf",
    "floor_note": "2. OG, Klingel Braun",
}
FEE = 250
MINIMUM = 1000
ETA = 50


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
    tid = _tenant(session, "Testbetrieb")
    session.add(_zone(tid))
    session.commit()
    return tid


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    return _call(session, tenant_id, caller_id=PHONE)


def _zone(tenant_id, **fields) -> DeliveryZone:
    values = {
        "name": "Zone 1",
        "match_type": "postal_code",
        "postal_codes": ["12345"],
        "fee_cents": FEE,
        "min_order_cents": MINIMUM,
        "eta_minutes": ETA,
        **fields,
    }
    return DeliveryZone(tenant_id=tenant_id, **values)


def delivery(session, tenant_id, call_id, *, name="Braun", address=None, **customer):
    """Two times dish 23: 13,80 from the menu."""
    body = {"phone": PHONE, "address": HOME if address is None else address}
    if name is not None:
        body["name"] = name
    return request(
        session,
        tenant_id,
        call_id,
        type="delivery",
        customer={**body, **customer},
    )


def known_customer(session, tenant_id, name="Müller", phone=PHONE):
    """A customer who ordered before, with a default address."""
    row = Customer(tenant_id=tenant_id, phone=phone, name=name, order_count=1)
    session.add(row)
    session.flush()
    home = Address(
        tenant_id=tenant_id,
        customer_id=row.id,
        street="Rheinstraße",
        house_number="54",
        postal_code="12345",
        city="Musterdorf",
        is_default=True,
    )
    session.add(home)
    session.commit()
    return row, home


def confirmed(session, tenant_id, call_id, order_id, now=NOW):
    return confirm_order(
        session,
        ConfirmRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            entity="order",
            entity_id=order_id,
            idempotency_key=uuid.uuid4().hex,
        ),
        now=now,
    )


def counts(session) -> tuple[int, int, int]:
    return tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (Customer, Address, Order)
    )


def test_a_delivery_with_a_spoken_address(session, tenant_id, call_id):
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert draft.status == "draft"
    assert draft.items_total_cents == 1380
    assert draft.delivery_fee_cents == FEE
    assert draft.total_cents == 1380 + FEE
    order = session.get(Order, draft.order_id)
    assert (order.type, order.delivery_fee_cents) == ("delivery", FEE)
    home = session.get(Address, order.address_id)
    zone = session.scalar(select(DeliveryZone))
    assert home.zone_id == zone.id
    assert (home.street, home.house_number) == ("Lindenstraße", "12")
    assert (home.postal_code, home.city) == ("12345", "Musterdorf")
    assert home.floor_note == "2. OG, Klingel Braun"
    assert home.customer_id == order.customer_id
    customer = session.get(Customer, order.customer_id)
    assert (customer.name, customer.phone) == ("Braun", PHONE)


def test_the_read_back_names_total_fee_address_and_time(session, tenant_id, call_id):
    """N2: the total with the delivery fee before the yes."""
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert "Macht 16 Euro 30 inklusive 2 Euro 50 Liefergebühr" in draft.readback
    # The default wait of 60 is longer than the zone's time.
    assert "Lieferung in etwa 60 Minuten" in draft.readback
    assert "Lindenstraße 12, 12345 Musterdorf, 2. OG, Klingel Braun" in draft.readback
    assert draft.readback.endswith("auf den Namen Braun. Passt das so?")


def test_a_fee_of_zero_is_not_read(session, tenant_id, call_id):
    session.execute(update(DeliveryZone).values(fee_cents=0))
    session.commit()

    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert draft.total_cents == 1380
    assert "Liefergebühr" not in draft.readback


def test_the_fee_comes_from_the_zone_of_the_postal_code(session, tenant_id, call_id):
    session.add(_zone(tenant_id, name="Zone 2", postal_codes=["12346"], fee_cents=390))
    session.commit()

    draft = draft_order(
        session,
        delivery(
            session, tenant_id, call_id, address={**HOME, "postal_code": "1 2 3 4 6"}
        ),
        now=NOW,
    )

    assert draft.delivery_fee_cents == 390
    assert session.scalar(select(Address.postal_code)) == "12346"


def test_a_fee_in_the_request_counts_for_nothing(session, tenant_id, call_id):
    req = delivery(session, tenant_id, call_id, delivery_fee_cents=0, zone_id="x")

    assert draft_order(session, req, now=NOW).delivery_fee_cents == FEE


def test_the_time_is_the_longer_of_zone_and_current_wait(session, tenant_id, call_id):
    session.execute(update(ServiceConfig).values(delivery_wait_standard_minutes=80))
    session.commit()

    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert draft.ready_at == NOW + timedelta(minutes=80)
    assert "in etwa 80 Minuten" in draft.readback


def test_todays_delivery_wait_is_read_with_the_zone_time(session, tenant_id, call_id):
    """T-3.14: today's wait, not the standard, is what is held against the zone."""
    status_config.set_wait(session, tenant_id, "delivery", 80, now=NOW)

    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert "in etwa 80 Minuten" in draft.readback


def test_the_zone_time_counts_when_the_wait_is_shorter(session, tenant_id, call_id):
    session.execute(update(ServiceConfig).values(delivery_wait_standard_minutes=10))
    session.commit()

    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert draft.ready_at == NOW + timedelta(minutes=ETA)


def test_ready_after_delivery_closes_warns(session, tenant_id, call_id):
    late = datetime(2026, 9, 15, 21, 30, tzinfo=ZoneInfo("Europe/Berlin"))

    draft = draft_order(session, delivery(session, tenant_id, call_id), now=late)

    assert WARNING_READY_AFTER_CLOSE in draft.warnings


def test_out_of_zone_writes_nothing(session, tenant_id, call_id):
    before = counts(session)

    with pytest.raises(OutOfZone) as err:
        draft_order(
            session,
            delivery(
                session, tenant_id, call_id, address={**HOME, "postal_code": "54321"}
            ),
            now=NOW,
        )

    session.rollback()
    assert err.value.say == SAY_OUT_OF_ZONE
    assert counts(session) == before


def test_a_postal_code_not_understood_is_asked_again(session, tenant_id, call_id):
    with pytest.raises(InvalidInput) as err:
        draft_order(
            session,
            delivery(
                session, tenant_id, call_id, address={**HOME, "postal_code": "123"}
            ),
            now=NOW,
        )

    assert "Postleitzahl" in err.value.say


def test_two_zones_for_one_code_go_to_the_team(session, tenant_id, call_id):
    session.add(_zone(tenant_id, name="Zweite", fee_cents=999))
    session.commit()

    with pytest.raises(ServiceUnavailable):
        draft_order(session, delivery(session, tenant_id, call_id), now=NOW)


def test_below_the_minimum_names_the_missing_amount(session, tenant_id, call_id):
    session.execute(update(DeliveryZone).values(min_order_cents=2000))
    session.commit()
    before = counts(session)

    with pytest.raises(BelowMinimum) as err:
        draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    session.rollback()
    assert "6 Euro 20" in err.value.say
    assert counts(session) == before


def test_the_fee_does_not_count_towards_the_minimum(session, tenant_id, call_id):
    session.execute(update(DeliveryZone).values(min_order_cents=1381))
    session.commit()

    with pytest.raises(BelowMinimum) as err:
        draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert "ein Cent" in err.value.say


def test_exactly_the_minimum_is_enough(session, tenant_id, call_id):
    session.execute(update(DeliveryZone).values(min_order_cents=1380))
    session.commit()

    assert draft_order(session, delivery(session, tenant_id, call_id), now=NOW)


def test_closed_by_the_hours(session, tenant_id, call_id):
    """No delivery window while pickup runs: the sentence offers pickup."""
    session.execute(
        delete(OpeningHours).where(
            OpeningHours.tenant_id == tenant_id, OpeningHours.service == "delivery"
        )
    )
    session.commit()

    with pytest.raises(Closed) as err:
        draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert err.value.say == f"{SAY_DELIVERY_CLOSED} Möchten Sie abholen?"


def test_closed_offers_no_pickup_when_there_is_none(session, tenant_id, call_id):
    night = datetime(2026, 9, 15, 23, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    with pytest.raises(Closed) as err:
        draft_order(session, delivery(session, tenant_id, call_id), now=night)

    assert err.value.say == SAY_DELIVERY_CLOSED


def test_the_team_switch_holds_in_the_middle_of_a_call(
    engine, session, tenant_id, call_id
):
    """The session of a call lives for the whole call and does not expire on
    commit: a config it read before the tablet's tap must not decide."""
    assert session.get(ServiceConfig, tenant_id).delivery_enabled is True
    with Session(engine) as tablet:
        tablet.execute(update(ServiceConfig).values(delivery_enabled=False))
        tablet.commit()

    with pytest.raises(Closed):
        draft_order(session, delivery(session, tenant_id, call_id), now=NOW)


def test_the_emergency_stop_closes_delivery(session, tenant_id, call_id):
    session.execute(update(ServiceConfig).values(call_mode="paused"))
    session.commit()

    with pytest.raises(Closed):
        draft_order(session, delivery(session, tenant_id, call_id), now=NOW)


def test_a_delivery_needs_an_address(session, tenant_id, call_id):
    with pytest.raises(InvalidInput) as err:
        draft_order(
            session,
            request(
                session,
                tenant_id,
                call_id,
                type="delivery",
                customer={"name": "Braun", "phone": PHONE},
            ),
            now=NOW,
        )

    assert "Adresse" in err.value.say


def test_a_pickup_needs_a_name(session, tenant_id, call_id):
    """With the sentence that asks: a bare `invalid_input` gives a model nothing
    to act on."""
    with pytest.raises(InvalidInput) as err:
        draft_order(
            session,
            request(session, tenant_id, call_id, customer={"phone": PHONE}),
            now=NOW,
        )

    assert "Namen" in err.value.say


def test_a_pickup_carries_no_customer_and_no_fee(session, tenant_id, call_id):
    draft = draft_order(session, request(session, tenant_id, call_id), now=NOW)

    order = session.get(Order, draft.order_id)
    assert draft.delivery_fee_cents == 0
    assert order.customer_id is None
    assert order.address_id is None
    assert session.scalar(select(func.count()).select_from(Customer)) == 0


def test_the_same_key_gives_the_same_answer_and_no_second_row(
    session, tenant_id, call_id
):
    req = delivery(session, tenant_id, call_id)
    first = draft_order(session, req, now=NOW)
    session.execute(update(DeliveryZone).values(fee_cents=999))
    session.commit()
    before = counts(session)

    second = draft_order(session, req, now=NOW + timedelta(minutes=5))

    assert second == first
    assert counts(session) == before


def test_a_second_draft_with_the_same_address_keeps_one_row(
    session, tenant_id, call_id
):
    draft_order(session, delivery(session, tenant_id, call_id), now=NOW)
    draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert session.scalar(select(func.count()).select_from(Customer)) == 1
    assert session.scalar(select(func.count()).select_from(Address)) == 1


def test_a_corrected_address_is_a_second_row_and_none_is_default(
    session, tenant_id, call_id
):
    draft_order(session, delivery(session, tenant_id, call_id), now=NOW)
    draft_order(
        session,
        delivery(session, tenant_id, call_id, address={**HOME, "house_number": "41"}),
        now=NOW,
    )

    rows = session.scalars(select(Address)).all()
    assert {row.house_number for row in rows} == {"12", "41"}
    assert not any(row.is_default for row in rows)


def test_a_name_that_does_not_match_is_a_new_customer(session, tenant_id, call_id):
    """L12: the same number with another name opens nobody's record."""
    known, _ = known_customer(session, tenant_id)

    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    order = session.get(Order, draft.order_id)
    assert order.customer_id != known.id
    assert session.get(Customer, order.customer_id).name == "Braun"
    assert session.scalar(select(func.count()).select_from(Customer)) == 2


def test_the_same_name_is_the_same_customer(session, tenant_id, call_id):
    known, home = known_customer(session, tenant_id)

    draft = draft_order(
        session, delivery(session, tenant_id, call_id, name="MUELLER"), now=NOW
    )

    order = session.get(Order, draft.order_id)
    assert order.customer_id == known.id
    assert order.address_id != home.id


def test_a_blocked_number_goes_to_the_team(session, tenant_id, call_id):
    session.add(Customer(tenant_id=tenant_id, phone=PHONE, name="Anders", blocked=True))
    session.commit()

    with pytest.raises(ServiceUnavailable) as err:
        draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert err.value.say == SAY_ON_FAILURE


def test_a_stored_address_is_never_said(session, tenant_id, call_id):
    """L12: "Ja genügt", the read-back names no stored word."""
    known, home = known_customer(session, tenant_id)

    draft = draft_order(
        session,
        request(
            session,
            tenant_id,
            call_id,
            type="delivery",
            customer={"phone": PHONE, "address_id": home.id},
        ),
        now=NOW,
    )

    assert "Rheinstraße" not in draft.readback
    assert "Musterdorf" not in draft.readback
    assert "Müller" not in draft.readback
    assert "an dieselbe Adresse wie beim letzten Mal" in draft.readback
    assert draft.readback.endswith(
        "Minuten an dieselbe Adresse wie beim letzten Mal. Passt das so?"
    )
    order = session.get(Order, draft.order_id)
    assert (order.customer_id, order.address_id) == (known.id, home.id)
    assert order.customer_name == "Müller"
    assert draft.delivery_fee_cents == FEE


def test_a_name_the_guest_said_is_read_with_a_stored_address(
    session, tenant_id, call_id
):
    _, home = known_customer(session, tenant_id)

    draft = draft_order(session, _stored(session, tenant_id, call_id, home.id), now=NOW)

    assert draft.readback.endswith("auf den Namen Müller. Passt das so?")
    assert "Rheinstraße" not in draft.readback


def test_a_replay_says_no_stored_word_either(session, tenant_id, call_id):
    _, home = known_customer(session, tenant_id)
    req = request(
        session,
        tenant_id,
        call_id,
        type="delivery",
        customer={"phone": PHONE, "address_id": home.id},
    )
    first = draft_order(session, req, now=NOW)

    assert draft_order(session, req, now=NOW).readback == first.readback


def test_a_stored_address_gets_the_zone_of_today(session, tenant_id, call_id):
    _, home = known_customer(session, tenant_id)
    assert home.zone_id is None

    draft = draft_order(
        session,
        request(
            session,
            tenant_id,
            call_id,
            type="delivery",
            customer={"phone": PHONE, "address_id": home.id},
        ),
        now=NOW,
    )

    session.refresh(home)
    assert home.zone_id == session.scalar(select(DeliveryZone.id))
    assert draft.delivery_fee_cents == FEE


def test_a_stored_address_outside_every_zone_today(session, tenant_id, call_id):
    _, home = known_customer(session, tenant_id)
    session.execute(update(DeliveryZone).values(active=False))
    session.commit()

    with pytest.raises(OutOfZone):
        draft_order(
            session,
            request(
                session,
                tenant_id,
                call_id,
                type="delivery",
                customer={"phone": PHONE, "address_id": home.id},
            ),
            now=NOW,
        )


def _stored(session, tenant_id, call_id, address_id, name="Müller", phone=PHONE):
    return request(
        session,
        tenant_id,
        call_id,
        type="delivery",
        customer={"name": name, "phone": phone, "address_id": address_id},
    )


def test_another_name_opens_no_stored_address(session, tenant_id, call_id):
    _, home = known_customer(session, tenant_id)

    with pytest.raises(InvalidInput) as err:
        draft_order(
            session,
            _stored(session, tenant_id, call_id, home.id, name="Braun"),
            now=NOW,
        )

    assert "Rheinstraße" not in err.value.say
    assert "Adresse" in err.value.say


def test_the_address_of_another_number_is_refused(session, tenant_id, call_id):
    _, home = known_customer(session, tenant_id, phone="+497215559999")

    with pytest.raises(InvalidInput):
        draft_order(session, _stored(session, tenant_id, call_id, home.id), now=NOW)


def test_an_address_of_another_tenant_is_refused(session, tenant_id, call_id):
    other = _tenant(session, "Anderer Betrieb")
    _, home = known_customer(session, other)

    with pytest.raises(InvalidInput):
        draft_order(session, _stored(session, tenant_id, call_id, home.id), now=NOW)


def test_an_invented_address_id_is_refused(session, tenant_id, call_id):
    known_customer(session, tenant_id)

    with pytest.raises(InvalidInput):
        draft_order(
            session, _stored(session, tenant_id, call_id, uuid.uuid4()), now=NOW
        )


def test_an_objection_switches_the_stored_address_off(session, tenant_id, call_id):
    """L12, also when it arrives in the middle of the call."""
    _, home = known_customer(session, tenant_id)
    session.add(RecognitionObjection(tenant_id=tenant_id, phone=PHONE))
    session.commit()

    with pytest.raises(InvalidInput):
        draft_order(session, _stored(session, tenant_id, call_id, home.id), now=NOW)


def test_several_customers_on_a_number_need_a_name(session, tenant_id, call_id):
    _, home = known_customer(session, tenant_id)
    known_customer(session, tenant_id, name="Braun")

    with pytest.raises(InvalidInput):
        draft_order(
            session,
            request(
                session,
                tenant_id,
                call_id,
                type="delivery",
                customer={"phone": PHONE, "address_id": home.id},
            ),
            now=NOW,
        )


def test_a_new_address_for_a_recognized_customer(session, tenant_id, call_id):
    known, _ = known_customer(session, tenant_id)

    draft = draft_order(
        session, delivery(session, tenant_id, call_id, name=None), now=NOW
    )

    order = session.get(Order, draft.order_id)
    assert order.customer_id == known.id
    assert "Lindenstraße" in draft.readback
    assert "Müller" not in draft.readback


def test_an_unknown_caller_without_a_name_is_asked(session, tenant_id, call_id):
    with pytest.raises(InvalidInput) as err:
        draft_order(session, delivery(session, tenant_id, call_id, name=None), now=NOW)

    assert "Namen" in err.value.say


def test_nothing_about_the_address_in_the_audit_log(session, tenant_id, call_id):
    """The audit log outlives the order and holds no personal data."""
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    row = session.scalar(select(AuditLog).where(AuditLog.entity_id == draft.order_id))
    text = str(row.payload)
    assert "Lindenstraße" not in text
    assert "Braun" not in text
    assert "12345" not in text


def test_nothing_is_confirmed_without_confirm(session, tenant_id, call_id):
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    order = session.get(Order, draft.order_id)
    customer = session.get(Customer, order.customer_id)
    assert order.status == "draft"
    assert customer.order_count == 0
    assert customer.last_order_at is None
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_a_draft_offers_nobody_its_address(session, tenant_id, call_id):
    """A draft nobody said yes to is no "letztes Mal"."""
    draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    match = find_customer(session, tenant_id, PHONE, "Braun")

    assert match.address_id is None
    assert match.say is None


def test_confirm_counts_the_order_and_makes_the_address_the_default(
    session, tenant_id, call_id
):
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)
    later = NOW + timedelta(minutes=1)

    confirmed(session, tenant_id, call_id, draft.order_id, later)

    order = session.get(Order, draft.order_id)
    customer = session.get(Customer, order.customer_id)
    session.refresh(customer)
    assert customer.order_count == 1
    assert customer.last_order_at == later
    assert customer.last_contact_at == later
    match = find_customer(session, tenant_id, PHONE, "Braun")
    assert match.address_id == order.address_id
    assert match.say == SAY_SAME_ADDRESS


def test_a_second_confirm_counts_once(session, tenant_id, call_id):
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)
    confirmed(session, tenant_id, call_id, draft.order_id)

    confirmed(session, tenant_id, call_id, draft.order_id)

    assert session.scalar(select(Customer.order_count)) == 1


def test_the_new_address_replaces_the_default(session, tenant_id, call_id):
    known, home = known_customer(session, tenant_id)
    draft = draft_order(
        session, delivery(session, tenant_id, call_id, name="Müller"), now=NOW
    )

    confirmed(session, tenant_id, call_id, draft.order_id)

    session.refresh(home)
    order = session.get(Order, draft.order_id)
    assert home.is_default is False
    assert session.get(Address, order.address_id).is_default is True
    assert order.customer_id == known.id


def test_the_order_event_carries_the_address_and_the_fee(session, tenant_id, call_id):
    session.execute(update(ServiceConfig).values(call_mode="primary"))
    session.commit()
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    confirmed(session, tenant_id, call_id, draft.order_id)

    payload = session.scalar(
        select(OutboxEvent.payload).where(OutboxEvent.event_type == ORDER_CONFIRMED)
    )
    assert payload["delivery_fee_cents"] == FEE
    assert payload["total_cents"] == 1380 + FEE
    assert payload["address"] == HOME


def test_a_pickup_event_carries_no_address(session, tenant_id, call_id):
    session.execute(update(ServiceConfig).values(call_mode="primary"))
    session.commit()
    draft = draft_order(session, request(session, tenant_id, call_id), now=NOW)

    confirmed(session, tenant_id, call_id, draft.order_id)

    payload = session.scalar(select(OutboxEvent.payload))
    assert payload["address"] is None


def test_the_switch_after_the_read_back_holds_at_the_yes(
    engine, session, tenant_id, call_id
):
    """The team switched delivery off between the read-back and the yes."""
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)
    with Session(engine) as tablet:
        status_config.set_delivery(tablet, tenant_id, False)

    with pytest.raises(Conflict) as err:
        confirmed(session, tenant_id, call_id, draft.order_id)

    session.rollback()
    assert SAY_DELIVERY_CLOSED in err.value.say
    assert session.get(Order, draft.order_id).status == "draft"


def test_a_pickup_is_refused_with_the_switch_off(session, tenant_id, call_id):
    status_config.set_pickup(session, tenant_id, False)

    with pytest.raises(Closed) as err:
        draft_order(session, request(session, tenant_id, call_id), now=NOW)

    assert err.value.say == f"{SAY_PICKUP_CLOSED} Möchten Sie liefern lassen?"


def test_the_pickup_switch_holds_in_the_middle_of_a_call(
    engine, session, tenant_id, call_id
):
    """The session of a call outlives a tap on another tablet."""
    assert session.get(ServiceConfig, tenant_id).pickup_enabled is True
    with Session(engine) as tablet:
        status_config.set_pickup(tablet, tenant_id, False)

    with pytest.raises(Closed):
        draft_order(session, request(session, tenant_id, call_id), now=NOW)


def test_the_pickup_switch_after_the_read_back_holds_at_the_yes(
    engine, session, tenant_id, call_id
):
    draft = draft_order(session, request(session, tenant_id, call_id), now=NOW)
    with Session(engine) as tablet:
        status_config.set_pickup(tablet, tenant_id, False)

    with pytest.raises(Conflict) as err:
        confirmed(session, tenant_id, call_id, draft.order_id)

    session.rollback()
    assert SAY_PICKUP_CLOSED in err.value.say
    assert session.get(Order, draft.order_id).status == "draft"


def test_the_pickup_switch_leaves_a_delivery_alone(session, tenant_id, call_id):
    status_config.set_pickup(session, tenant_id, False)

    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert confirmed(session, tenant_id, call_id, draft.order_id)


def test_a_closed_delivery_offers_no_pickup_that_is_switched_off(
    session, tenant_id, call_id
):
    """Availability from the database: a switched-off pickup is not on offer."""
    status_config.set_pickup(session, tenant_id, False)
    status_config.set_delivery(session, tenant_id, False)

    with pytest.raises(Closed) as err:
        draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert err.value.say == SAY_DELIVERY_CLOSED


def test_a_closed_pickup_offers_no_delivery_that_is_switched_off(
    session, tenant_id, call_id
):
    status_config.set_pickup(session, tenant_id, False)
    status_config.set_delivery(session, tenant_id, False)

    with pytest.raises(Closed) as err:
        draft_order(session, request(session, tenant_id, call_id), now=NOW)

    assert err.value.say == SAY_PICKUP_CLOSED


def test_the_switch_leaves_a_pickup_alone(engine, session, tenant_id, call_id):
    draft = draft_order(session, request(session, tenant_id, call_id), now=NOW)
    with Session(engine) as tablet:
        status_config.set_delivery(tablet, tenant_id, False)

    assert confirmed(session, tenant_id, call_id, draft.order_id).status == "confirmed"


def test_a_correction_keeps_the_fee_in_the_total(session, tenant_id, call_id):
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)
    confirmed(session, tenant_id, call_id, draft.order_id)
    plan = preview_correction(session, tenant_id, draft.order_id)
    row = plan.rows[0]
    req = CorrectionRequest(
        version=plan.version,
        rows=(RowEdit(order_item_id=row.order_item_id, quantity=3),),
    )

    changed = preview_correction(session, tenant_id, draft.order_id, req)
    apply_correction(session, tenant_id, draft.order_id, req, "wrong_quantity")

    order = session.get(Order, draft.order_id)
    session.refresh(order)
    assert changed.before_total_cents == 1380 + FEE
    assert changed.total_cents == 2070 + FEE
    assert order.items_total_cents == 2070
    assert order.delivery_fee_cents == FEE
    assert order.total_cents == 2070 + FEE
    audit = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == order.id, AuditLog.action == ACTION_CORRECTED
        )
    )
    assert audit.payload["before_total_cents"] == 1380 + FEE
    assert audit.payload["after_total_cents"] == 2070 + FEE


def test_a_correction_may_fall_below_the_minimum(session, tenant_id, call_id):
    """The team decides on the tablet; the rule binds the agent, not the team."""
    session.execute(update(DeliveryZone).values(min_order_cents=1380))
    session.commit()
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)
    confirmed(session, tenant_id, call_id, draft.order_id)
    plan = preview_correction(session, tenant_id, draft.order_id)
    req = CorrectionRequest(
        version=plan.version,
        rows=(RowEdit(order_item_id=plan.rows[0].order_item_id, quantity=1),),
    )

    apply_correction(session, tenant_id, draft.order_id, req, "wrong_quantity")

    order = session.get(Order, draft.order_id)
    session.refresh(order)
    assert order.total_cents == 690 + FEE


def test_a_correction_ticket_carries_the_address(session, tenant_id, call_id):
    session.execute(update(ServiceConfig).values(call_mode="primary"))
    session.commit()
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)
    confirmed(session, tenant_id, call_id, draft.order_id)
    plan = preview_correction(session, tenant_id, draft.order_id)
    req = CorrectionRequest(
        version=plan.version,
        rows=(RowEdit(order_item_id=plan.rows[0].order_item_id, quantity=3),),
    )

    apply_correction(session, tenant_id, draft.order_id, req, "wrong_quantity")

    payloads = session.scalars(select(OutboxEvent.payload)).all()
    assert payloads[-1]["address"] == HOME
    assert payloads[-1]["total_cents"] == 2070 + FEE


def test_contact_on_a_pickup_counts_for_that_name_only(session, tenant_id, call_id):
    known, _ = known_customer(session, tenant_id)
    other, _ = known_customer(session, tenant_id, name="Braun")
    draft = draft_order(session, request(session, tenant_id, call_id), now=NOW)
    later = NOW + timedelta(days=1)

    confirmed(session, tenant_id, call_id, draft.order_id, later)

    session.refresh(known)
    session.refresh(other)
    assert known.last_contact_at == later
    assert other.last_contact_at != later
    assert known.order_count == 1


def test_contact_on_a_callback_counts_for_the_number(session, tenant_id, call_id):
    known, _ = known_customer(session, tenant_id)
    later = NOW + timedelta(days=1)

    create_callback(
        session,
        CreateCallbackRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            phone=PHONE,
            reason="out_of_scope",
            summary="Bitte zurückrufen.",
        ),
        now=later,
    )

    session.refresh(known)
    assert known.last_contact_at == later


def test_contact_on_a_complaint_counts_for_the_number(session, tenant_id, call_id):
    known, _ = known_customer(session, tenant_id)

    result = transfer_to_team(
        session,
        TransferToTeamRequest(call_id=call_id, tenant_id=tenant_id, reason="complaint"),
        now=NOW,
    )

    session.refresh(known)
    assert result.available is True
    assert known.last_contact_at == NOW


def test_no_contact_on_another_transfer(session, tenant_id, call_id):
    known, _ = known_customer(session, tenant_id)
    before = known.last_contact_at

    transfer_to_team(
        session,
        TransferToTeamRequest(
            call_id=call_id, tenant_id=tenant_id, reason="human_requested"
        ),
        now=NOW,
    )

    session.refresh(known)
    assert known.last_contact_at == before


def test_contact_on_a_reservation(session, tenant_id, call_id):
    known, _ = known_customer(session, tenant_id)
    table = Reservation(
        tenant_id=tenant_id,
        call_id=call_id,
        status="draft",
        guest_name="Müller",
        phone=PHONE,
        party_size=2,
        reserved_for=NOW + timedelta(days=1),
        idempotency_key=uuid.uuid4().hex,
    )
    session.add(table)
    session.commit()

    confirm(
        session,
        ConfirmRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            entity="reservation",
            entity_id=table.id,
            idempotency_key=uuid.uuid4().hex,
        ),
        now=NOW,
    )

    session.refresh(known)
    assert known.last_contact_at == NOW


def test_contact_never_on_a_draft(session, tenant_id, call_id):
    known, _ = known_customer(session, tenant_id)
    before = known.last_contact_at

    draft_order(session, delivery(session, tenant_id, call_id, name="Müller"), now=NOW)

    session.refresh(known)
    assert known.last_contact_at == before


def _object(session, tenant_id):
    session.add(RecognitionObjection(tenant_id=tenant_id, phone=PHONE))
    session.commit()


def test_with_an_objection_a_name_links_to_no_old_row(session, tenant_id, call_id):
    """L12: an objection switches recognition off for the number, so the same
    name does not reach the old record either."""
    known, _ = known_customer(session, tenant_id)
    _object(session, tenant_id)

    draft = draft_order(
        session, delivery(session, tenant_id, call_id, name="Müller"), now=NOW
    )

    order = session.get(Order, draft.order_id)
    assert order.customer_id != known.id
    assert session.get(Customer, order.customer_id).name == "Müller"
    assert session.scalar(select(func.count()).select_from(Customer)) == 2


def test_with_an_objection_every_order_is_a_new_row(session, tenant_id, call_id):
    _object(session, tenant_id)

    first = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)
    second = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    assert (
        session.get(Order, first.order_id).customer_id
        != session.get(Order, second.order_id).customer_id
    )


def test_with_an_objection_a_confirmed_order_is_not_offered(
    session, tenant_id, call_id
):
    _object(session, tenant_id)
    draft = draft_order(session, delivery(session, tenant_id, call_id), now=NOW)

    confirmed(session, tenant_id, call_id, draft.order_id)

    assert find_customer(session, tenant_id, PHONE, "Braun").found is False


def _customers(session) -> int:
    return session.scalar(select(func.count()).select_from(Customer))


# A contact inside the opening hours of the seed (a Tuesday at 18:00), and the
# day of the draft before it. Fixed: a row dated by the real clock would put the
# contact in or out of the hours by the time the suite runs, and on a Monday
# after 14:00 or on a Sunday it failed (CI on #472).
CONTACT = NOW
DRAFTED = CONTACT - timedelta(days=1)


def _abandoned_draft(session, tenant_id, call_id) -> None:
    """A delivery draft nobody said yes to. Its customer row is what the insert
    wrote, `created_at` equal to `last_contact_at`, the mark of a draft-only
    customer; only the day moves, to before `CONTACT`."""
    draft_order(session, delivery(session, tenant_id, call_id), now=NOW)
    row = session.scalar(select(Customer))
    assert row.last_contact_at == row.created_at
    row.created_at = row.last_contact_at = DRAFTED
    session.commit()


def _past(period_days: int) -> timedelta:
    return timedelta(days=period_days + 1)


def test_a_draft_nobody_said_yes_to_goes_with_its_guest_details(
    session, tenant_id, call_id
):
    _abandoned_draft(session, tenant_id, call_id)

    purge(session, DRAFTED + _past(settings.retention_guest_details_days))

    assert _customers(session) == 0


def _pickup(session, tenant_id, call_id):
    return request(
        session, tenant_id, call_id, customer={"name": "Braun", "phone": PHONE}
    )


def test_a_confirmed_pickup_after_a_draft_keeps_the_full_period(
    session, tenant_id, call_id
):
    """Codex PR #472: a pickup counts no order on the customer, so `order_count`
    alone would delete a guest 90 days after a confirmed contact."""
    _abandoned_draft(session, tenant_id, call_id)
    order = draft_order(session, _pickup(session, tenant_id, call_id), now=CONTACT)
    confirmed(session, tenant_id, call_id, order.order_id, CONTACT)

    purge(session, CONTACT + _past(settings.retention_guest_details_days))
    assert _customers(session) == 1

    purge(session, CONTACT + _past(settings.retention_customer_days))
    assert _customers(session) == 0


def test_a_confirmed_table_after_a_draft_keeps_the_full_period(
    session, tenant_id, call_id
):
    _abandoned_draft(session, tenant_id, call_id)
    table = Reservation(
        tenant_id=tenant_id,
        call_id=call_id,
        status="draft",
        guest_name="Braun",
        phone=PHONE,
        party_size=2,
        reserved_for=CONTACT + timedelta(days=1),
        idempotency_key=uuid.uuid4().hex,
    )
    session.add(table)
    session.commit()
    confirm(
        session,
        ConfirmRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            entity="reservation",
            entity_id=table.id,
            idempotency_key=uuid.uuid4().hex,
        ),
        now=CONTACT,
    )

    purge(session, CONTACT + _past(settings.retention_guest_details_days))

    assert _customers(session) == 1


def test_a_callback_after_a_draft_keeps_the_full_period(session, tenant_id, call_id):
    _abandoned_draft(session, tenant_id, call_id)
    create_callback(
        session,
        CreateCallbackRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            phone=PHONE,
            reason="out_of_scope",
            summary="Bitte zurückrufen.",
        ),
        now=CONTACT,
    )

    purge(session, CONTACT + _past(settings.retention_guest_details_days))

    assert _customers(session) == 1


def test_with_an_objection_a_pickup_moves_no_period(session, tenant_id, call_id):
    """L12: the old record's period must not grow by a contact on that number."""
    known, _ = known_customer(session, tenant_id)
    before = known.last_contact_at
    _object(session, tenant_id)
    later = NOW + timedelta(days=1)
    order = draft_order(session, request(session, tenant_id, call_id), now=later)

    confirmed(session, tenant_id, call_id, order.order_id, later)

    session.refresh(known)
    assert known.last_contact_at == before


def test_with_an_objection_a_callback_moves_no_period(session, tenant_id, call_id):
    known, _ = known_customer(session, tenant_id)
    before = known.last_contact_at
    _object(session, tenant_id)

    create_callback(
        session,
        CreateCallbackRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            phone=PHONE,
            reason="out_of_scope",
            summary="Bitte zurückrufen.",
        ),
        now=NOW + timedelta(days=1),
    )

    session.refresh(known)
    assert known.last_contact_at == before


def test_with_an_objection_a_complaint_moves_no_period(session, tenant_id, call_id):
    known, _ = known_customer(session, tenant_id)
    before = known.last_contact_at
    _object(session, tenant_id)

    transfer_to_team(
        session,
        TransferToTeamRequest(call_id=call_id, tenant_id=tenant_id, reason="complaint"),
        now=NOW,
    )

    session.refresh(known)
    assert known.last_contact_at == before


def test_with_an_objection_a_new_row_still_counts_its_own_order(
    session, tenant_id, call_id
):
    """The yes of a delivery touches the row that draft wrote, no old record."""
    known, _ = known_customer(session, tenant_id)
    before = known.last_contact_at
    _object(session, tenant_id)
    draft = draft_order(
        session, delivery(session, tenant_id, call_id, name="Müller"), now=NOW
    )

    confirmed(session, tenant_id, call_id, draft.order_id)

    order = session.get(Order, draft.order_id)
    fresh = session.get(Customer, order.customer_id)
    session.refresh(fresh)
    session.refresh(known)
    assert fresh.order_count == 1
    assert known.last_contact_at == before


# Security audit 02.11.2026 (docs/09 §5): the number a guest names is the number
# the team calls back on, never a key to someone's record (L12). Recognition
# goes by the line the call came from, as `find_customer` in the core does.
OTHER_LINE = "+497215559999"


def _from(session, tenant_id, caller_id=OTHER_LINE):
    """A call from another line than the number the booking runs under."""
    return _call(session, tenant_id, caller_id=caller_id)


@pytest.mark.parametrize("caller_id", [OTHER_LINE, None, "anonymous"])
def test_a_named_number_opens_no_stored_record(session, tenant_id, caller_id):
    """The caller names a stored customer's number and name and gives an
    address of their own: at the yes it must not become that customer's
    "letztes Mal"."""
    known, home = known_customer(session, tenant_id)
    before = known.last_contact_at
    call = _from(session, tenant_id, caller_id)

    draft = draft_order(
        session, delivery(session, tenant_id, call, name="Müller"), now=NOW
    )
    confirmed(session, tenant_id, call, draft.order_id)

    order = session.get(Order, draft.order_id)
    fresh = session.get(Customer, order.customer_id)
    session.refresh(fresh)
    session.refresh(known)
    session.refresh(home)
    assert order.customer_id != known.id
    assert order.phone == PHONE
    assert (fresh.order_count, fresh.last_order_at) == (0, None)
    assert home.is_default is True
    assert (known.order_count, known.last_contact_at) == (1, before)
    match = find_customer(session, tenant_id, PHONE)
    assert (match.customer_id, match.address_id) == (known.id, home.id)


@pytest.mark.parametrize("caller_id", [OTHER_LINE, None])
def test_a_named_number_gives_no_stored_name(session, tenant_id, caller_id):
    known_customer(session, tenant_id)
    call = _from(session, tenant_id, caller_id)

    with pytest.raises(InvalidInput) as err:
        draft_order(session, delivery(session, tenant_id, call, name=None), now=NOW)

    assert err.value.say == SAY_WHICH_NAME


def test_a_named_number_opens_no_stored_address(session, tenant_id):
    _, home = known_customer(session, tenant_id)
    call = _from(session, tenant_id)

    with pytest.raises(InvalidInput) as err:
        draft_order(session, _stored(session, tenant_id, call, home.id), now=NOW)

    assert "Rheinstraße" not in err.value.say
    assert counts(session) == (1, 1, 0)


def test_the_own_line_in_another_spelling_still_recognizes(session, tenant_id):
    """`calls.caller_id` keeps what the line gave when it is no E.164."""
    known, home = known_customer(session, tenant_id)
    call = _from(session, tenant_id, "0721 555-1234")

    draft = draft_order(session, _stored(session, tenant_id, call, home.id), now=NOW)

    assert session.get(Order, draft.order_id).customer_id == known.id


def test_a_blocked_line_that_names_another_number_goes_to_the_team(session, tenant_id):
    session.add(
        Customer(tenant_id=tenant_id, phone=OTHER_LINE, name="Anders", blocked=True)
    )
    session.commit()
    call = _from(session, tenant_id)

    with pytest.raises(ServiceUnavailable) as err:
        draft_order(session, delivery(session, tenant_id, call), now=NOW)

    assert err.value.say == SAY_ON_FAILURE
    # The log line and the HTTP answer name no state of the number.
    assert "block" not in err.value.message
    assert counts(session) == (1, 0, 0)


def test_a_row_a_named_number_wrote_is_offered_to_nobody(session, tenant_id, call_id):
    """Nobody was stored on the number. The row that booking writes must not
    give its name or its address to the owner of the number later."""
    planted = _from(session, tenant_id)
    draft = draft_order(
        session, delivery(session, tenant_id, planted, name="Braun"), now=NOW
    )
    confirmed(session, tenant_id, planted, draft.order_id)

    assert find_customer(session, tenant_id, PHONE).found is False
    assert find_customer(session, tenant_id, PHONE, "Braun").found is False
    with pytest.raises(InvalidInput) as err:
        draft_order(session, delivery(session, tenant_id, call_id, name=None), now=NOW)
    assert err.value.say == SAY_WHICH_NAME


def test_the_owner_of_a_named_number_gets_a_row_of_their_own(
    session, tenant_id, call_id
):
    planted_call = _from(session, tenant_id)
    first = draft_order(
        session, delivery(session, tenant_id, planted_call, name="Braun"), now=NOW
    )
    confirmed(session, tenant_id, planted_call, first.order_id)
    planted = session.get(Order, first.order_id)

    own = draft_order(
        session,
        delivery(
            session,
            tenant_id,
            call_id,
            name="Braun",
            address={**HOME, "house_number": "41"},
        ),
        now=NOW,
    )
    confirmed(session, tenant_id, call_id, own.order_id)

    order = session.get(Order, own.order_id)
    assert order.customer_id != planted.customer_id
    match = find_customer(session, tenant_id, PHONE, "Braun")
    assert (match.customer_id, match.address_id) == (
        order.customer_id,
        order.address_id,
    )


def test_two_drafts_from_a_named_number_write_two_rows(session, tenant_id):
    """Like an objection: no row is reused, not even one of the same call."""
    call = _from(session, tenant_id)

    first = draft_order(session, delivery(session, tenant_id, call), now=NOW)
    second = draft_order(session, delivery(session, tenant_id, call), now=NOW)

    assert (
        session.get(Order, first.order_id).customer_id
        != session.get(Order, second.order_id).customer_id
    )


def _contact_under(kind, session, tenant_id, call, at):
    """A pickup, a callback or a table booked under PHONE in `call`."""
    if kind == "pickup":
        order = draft_order(session, request(session, tenant_id, call), now=NOW)
        confirmed(session, tenant_id, call, order.order_id, at)
    elif kind == "callback":
        create_callback(
            session,
            CreateCallbackRequest(
                call_id=call,
                tenant_id=tenant_id,
                phone=PHONE,
                reason="out_of_scope",
                summary="Bitte zurückrufen.",
            ),
            now=at,
        )
    else:
        table = Reservation(
            tenant_id=tenant_id,
            call_id=call,
            status="draft",
            guest_name="Müller",
            phone=PHONE,
            party_size=2,
            reserved_for=NOW + timedelta(days=1),
            idempotency_key=uuid.uuid4().hex,
        )
        session.add(table)
        session.commit()
        confirm(
            session,
            ConfirmRequest(
                call_id=call,
                tenant_id=tenant_id,
                entity="reservation",
                entity_id=table.id,
                idempotency_key=uuid.uuid4().hex,
            ),
            now=at,
        )


@pytest.mark.parametrize("caller_id", [OTHER_LINE, None])
@pytest.mark.parametrize("kind", ["pickup", "callback", "reservation"])
def test_a_named_number_moves_no_period(session, tenant_id, kind, caller_id):
    """The customers of a number the caller only named keep their period: a
    booking under it from another line is no contact of theirs (L12)."""
    known, _ = known_customer(session, tenant_id)
    before = (known.last_contact_at, known.last_order_at, known.order_count)

    _contact_under(kind, session, tenant_id, _from(session, tenant_id, caller_id), NOW)

    session.refresh(known)
    assert (known.last_contact_at, known.last_order_at, known.order_count) == before


@pytest.mark.parametrize("kind", ["pickup", "callback", "reservation"])
def test_the_own_line_still_moves_the_period(session, tenant_id, call_id, kind):
    known, _ = known_customer(session, tenant_id)

    _contact_under(kind, session, tenant_id, call_id, NOW + timedelta(days=1))

    session.refresh(known)
    assert known.last_contact_at == NOW + timedelta(days=1)
