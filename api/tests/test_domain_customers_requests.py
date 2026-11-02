"""Requests of data subjects (T-11.8, docs/09 §2 U2): the log and its deadline, who
the person is, access, erasure and objection, on the verified person's rows only."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput, NotFound
from api.domain.confirm import confirm
from api.domain.customers import find_customer
from api.domain.customers.requests import (
    Details,
    access_copy,
    answer_request,
    erase,
    extend_deadline,
    get_request,
    log_request,
    open_requests,
    record_objection,
    verify,
)
from api.domain.ordering.board import cancel_order, version_of
from api.domain.ordering.handover import ack_ticket
from api.models import (
    Address,
    AuditLog,
    Call,
    Callback,
    ConsentLog,
    Customer,
    DataSubjectRequest,
    MenuItem,
    Order,
    OrderItem,
    OutboxEvent,
    RecognitionObjection,
    Reservation,
    ServiceConfig,
)
from api.schemas.confirm import ConfirmRequest
from api.tests.test_domain_draft_order import _tenant, item

ZONE = "Europe/Berlin"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
PAST = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
FUTURE = NOW + timedelta(days=8)
PHONE = "+497221555123"
OTHER_PHONE = "+497221555999"
MUELLER = Details(name="Müller", days=(date(2026, 10, 12),))


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return _tenant(session, "Testbetrieb")


def _call(session, tenant_id, caller_id=PHONE, started_at=PAST) -> Call:
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        caller_id=caller_id,
        started_at=started_at,
        delete_after=started_at.date(),
    )
    session.add(call)
    session.flush()
    return call


def _order(
    session,
    tenant_id,
    name="Müller",
    phone=PHONE,
    ready_at=PAST,
    status="handed_over",
    call=None,
    customer=None,
    item_note=None,
) -> Order:
    order = Order(
        tenant_id=tenant_id,
        call_id=(call or _call(session, tenant_id, phone)).id,
        type="pickup",
        status=status,
        customer_id=customer.id if customer else None,
        customer_name=name,
        phone=phone,
        note="Bitte klingeln",
        items_total_cents=1200,
        total_cents=1200,
        ready_at=ready_at,
        idempotency_key=uuid.uuid4().hex,
    )
    session.add(order)
    session.flush()
    session.add(
        OrderItem(
            tenant_id=tenant_id,
            order_id=order.id,
            menu_item_id=item(session, tenant_id, "23"),
            quantity=1,
            unit_price_cents=1200,
            note=item_note,
        )
    )
    session.flush()
    return order


def _reservation(
    session,
    tenant_id,
    name="Müller",
    phone=PHONE,
    reserved_for=PAST,
    status="confirmed",
    call=None,
) -> Reservation:
    reservation = Reservation(
        tenant_id=tenant_id,
        call_id=(call or _call(session, tenant_id, phone)).id,
        status=status,
        guest_name=name,
        phone=phone,
        party_size=2,
        reserved_for=reserved_for,
        idempotency_key=uuid.uuid4().hex,
    )
    session.add(reservation)
    session.flush()
    return reservation


def _callback(session, tenant_id, call, phone=PHONE, status="done") -> Callback:
    callback = Callback(
        tenant_id=tenant_id,
        call_id=call.id,
        phone=phone,
        reason="out_of_scope",
        summary="Möchte seine Daten löschen lassen",
        status=status,
    )
    session.add(callback)
    session.flush()
    return callback


def _customer(session, tenant_id, name="Müller", phone=PHONE) -> Customer:
    customer = Customer(tenant_id=tenant_id, phone=phone, name=name)
    session.add(customer)
    session.flush()
    return customer


def _address(session, tenant_id, customer) -> Address:
    address = Address(
        tenant_id=tenant_id,
        customer_id=customer.id,
        street="Hauptstraße",
        house_number="12a",
        postal_code="76530",
        city="Ort",
    )
    session.add(address)
    session.flush()
    return address


def _event(session, tenant_id, event_type="order.confirmed", status="sent", **ids):
    event = OutboxEvent(
        tenant_id=tenant_id,
        event_type=event_type,
        payload={"customer_name": "Müller", "phone": PHONE, **ids},
        status=status,
    )
    session.add(event)
    session.flush()
    return event


def _request(session, tenant_id, kind="erasure", **overrides) -> DataSubjectRequest:
    fields = {
        "kind": kind,
        "channel": "mail",
        "note": "Frau Müller, Bestellung vom 12.10.",
        "received_at": NOW,
        "zone": ZONE,
    } | overrides
    return log_request(session, tenant_id, **fields)


def _fresh(session, row):
    session.expire_all()
    return session.get(type(row), row.id)


# The log and its deadline


def test_a_request_gets_a_reference_and_the_deadline_of_one_month(session, tenant_id):
    request = _request(session, tenant_id)
    assert request.reference == "dsr_2026_001"
    assert request.deadline == date(2026, 12, 2)
    assert _request(session, tenant_id).reference == "dsr_2026_002"


def test_the_deadline_of_the_31st_is_the_last_day_of_a_shorter_month(
    session, tenant_id
):
    received = datetime(2027, 1, 31, 12, 0, tzinfo=UTC)
    assert _request(session, tenant_id, received_at=received).deadline == date(
        2027, 2, 28
    )


def test_the_year_of_a_reference_and_the_day_of_the_deadline_are_the_restaurants(
    session, tenant_id
):
    # 23:30 UTC on New Year's Eve is already the new year in Berlin.
    received = datetime(2026, 12, 31, 23, 30, tzinfo=UTC)
    request = _request(session, tenant_id, received_at=received)
    assert request.reference == "dsr_2027_001"
    assert request.deadline == date(2027, 2, 1)


def test_references_count_per_tenant(session, tenant_id):
    _request(session, tenant_id)
    assert _request(session, _tenant(session, "Zwei")).reference == "dsr_2026_001"


def test_a_line_of_the_interim_file_keeps_its_reference_and_dates(session, tenant_id):
    received = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    request = _request(
        session, tenant_id, received_at=received, reference="dsr_2026_007"
    )
    answer_request(session, request, "done", NOW, ZONE)
    assert (request.reference, request.deadline) == ("dsr_2026_007", date(2026, 11, 30))
    assert request.answered_at == NOW
    assert _request(session, tenant_id).reference == "dsr_2026_008"


def test_a_reference_that_can_hold_a_name_is_refused(session, tenant_id):
    with pytest.raises(InvalidInput):
        _request(session, tenant_id, reference="dsr_mueller")


def test_a_reference_is_logged_once(session, tenant_id):
    first = _request(session, tenant_id)
    with pytest.raises(Conflict):
        _request(session, tenant_id, reference=first.reference)


def test_a_reference_is_found_in_its_tenant_only(session, tenant_id):
    request = _request(session, tenant_id)
    assert get_request(session, tenant_id, request.reference) is request
    with pytest.raises(NotFound):
        get_request(session, _tenant(session, "Zwei"), request.reference)


def test_open_requests_come_by_deadline_and_stay_in_their_tenant(session, tenant_id):
    later = _request(session, tenant_id)
    sooner = _request(session, tenant_id, received_at=NOW - timedelta(days=20))
    done = _request(session, tenant_id)
    answer_request(session, done, "done", NOW, ZONE)
    _request(session, _tenant(session, "Zwei"))
    assert open_requests(session, tenant_id) == [sooner, later]


def test_a_complex_request_gets_two_more_months_once(session, tenant_id):
    request = _request(session, tenant_id)
    extend_deadline(session, request, ZONE)
    assert request.deadline == date(2027, 2, 2)
    with pytest.raises(Conflict):
        extend_deadline(session, request, ZONE)


def test_the_answer_sets_the_end_of_the_third_year_after_it(session, tenant_id):
    request = _request(session, tenant_id)
    answer_request(session, request, "done", NOW, ZONE)
    assert request.answered_at == NOW
    assert request.outcome == "done"
    assert request.delete_after == date(2029, 12, 31)
    # What the row keeps until then needs the details no longer than the period.
    assert request.verification_note is not None


def test_the_year_of_the_answer_is_the_restaurants(session, tenant_id):
    request = _request(session, tenant_id)
    answered = datetime(2026, 12, 31, 23, 30, tzinfo=UTC)
    answer_request(session, request, "done", answered, ZONE)
    assert request.delete_after == date(2030, 12, 31)


def test_a_request_is_answered_once(session, tenant_id):
    request = _request(session, tenant_id)
    answer_request(session, request, "done", NOW, ZONE)
    with pytest.raises(Conflict):
        answer_request(session, request, "refused", NOW, ZONE)


def test_an_answer_before_the_receipt_is_refused(session, tenant_id):
    request = _request(session, tenant_id)
    with pytest.raises(InvalidInput):
        answer_request(session, request, "done", NOW - timedelta(days=2), ZONE)


def test_the_log_writes_the_audit_log_without_a_name(session, tenant_id):
    request = _request(session, tenant_id)
    answer_request(session, request, "done", NOW, ZONE)
    entries = session.scalars(
        select(AuditLog).where(AuditLog.entity_id == request.id).order_by(AuditLog.id)
    ).all()
    assert [entry.action for entry in entries] == [
        "data_subject.request_logged",
        "data_subject.request_answered",
    ]
    assert "Müller" not in json.dumps([entry.payload for entry in entries])


# Who the person is


@pytest.mark.parametrize(
    "details",
    [
        Details(name="Müller"),
        Details(name="", days=(date(2026, 10, 12),)),
        Details(name="   ", days=(date(2026, 10, 12),)),
        Details(name="Müller", street="Hauptstraße", postal_code="76530"),
    ],
)
def test_a_name_alone_or_a_date_alone_verifies_nobody(session, tenant_id, details):
    _order(session, tenant_id)
    with pytest.raises(InvalidInput):
        verify(session, tenant_id, details, ZONE, NOW)


def test_a_name_with_the_date_of_an_order_verifies_the_person(session, tenant_id):
    order = _order(session, tenant_id)
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    assert person.orders == [order]
    assert person.phones == {PHONE}


def test_the_date_is_the_restaurants(session, tenant_id):
    # 23:30 UTC is already the 13th in Berlin.
    late = datetime(2026, 10, 12, 23, 30, tzinfo=UTC)
    _order(session, tenant_id, ready_at=late)
    with pytest.raises(NotFound):
        verify(session, tenant_id, MUELLER, ZONE, NOW)
    details = Details(name="Müller", days=(date(2026, 10, 13),))
    assert verify(session, tenant_id, details, ZONE, NOW).orders


def test_a_name_with_the_date_of_a_reservation_verifies_the_person(session, tenant_id):
    reservation = _reservation(session, tenant_id)
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    assert person.reservations == [reservation]


def test_the_umlaut_spelling_of_the_name_makes_no_difference(session, tenant_id):
    order = _order(session, tenant_id)
    details = Details(name="  MUELLER ", days=MUELLER.days)
    assert verify(session, tenant_id, details, ZONE, NOW).orders == [order]


def test_a_name_with_the_delivery_address_verifies_the_person(session, tenant_id):
    customer = _customer(session, tenant_id)
    _address(session, tenant_id, customer)
    order = _order(session, tenant_id, customer=customer)
    details = Details(
        name="Müller",
        street="Hauptstrasse",
        house_number="12 A",
        postal_code="76530",
    )
    person = verify(session, tenant_id, details, ZONE, NOW)
    assert person.customers == [customer]
    assert person.orders == [order]


def test_a_customer_whose_orders_lost_their_name_is_still_reached(session, tenant_id):
    """Orders lose the name after 90 days, the customer stays for 24 months."""
    customer = _customer(session, tenant_id)
    customer.last_order_at = PAST
    other = _customer(session, tenant_id, name="Schmidt")
    other.last_order_at = PAST
    old = _order(session, tenant_id, name="", phone="")
    session.flush()
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    assert person.customers == [customer]
    assert person.orders == []
    assert old.call_id not in {call.id for call in person.calls}


def test_a_wrong_house_number_verifies_nobody(session, tenant_id):
    _address(session, tenant_id, _customer(session, tenant_id))
    details = Details(
        name="Müller", street="Hauptstraße", house_number="13", postal_code="76530"
    )
    with pytest.raises(NotFound):
        verify(session, tenant_id, details, ZONE, NOW)


def test_the_right_date_with_a_wrong_name_verifies_nobody(session, tenant_id):
    _order(session, tenant_id, name="Schmidt")
    with pytest.raises(NotFound):
        verify(session, tenant_id, MUELLER, ZONE, NOW)


def test_the_right_name_with_a_wrong_date_verifies_nobody(session, tenant_id):
    _order(session, tenant_id)
    details = Details(name="Müller", days=(date(2026, 10, 13),))
    with pytest.raises(NotFound):
        verify(session, tenant_id, details, ZONE, NOW)


def test_a_row_of_another_tenant_verifies_nobody(session, tenant_id):
    _order(session, tenant_id)
    with pytest.raises(NotFound):
        verify(session, _tenant(session, "Zwei"), MUELLER, ZONE, NOW)


def test_a_shared_number_opens_only_the_row_with_the_name(session, tenant_id):
    mine = _order(session, tenant_id)
    theirs = _order(session, tenant_id, name="Schmidt")
    table = _reservation(session, tenant_id, name="Schmidt")
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    assert person.orders == [mine]
    assert theirs not in person.orders
    assert table not in person.reservations


def test_the_same_name_on_another_number_is_another_person(session, tenant_id):
    mine = _order(session, tenant_id)
    _order(session, tenant_id, phone=OTHER_PHONE, ready_at=PAST + timedelta(days=1))
    assert verify(session, tenant_id, MUELLER, ZONE, NOW).orders == [mine]


def test_a_second_number_counts_with_a_detail_of_its_own(session, tenant_id):
    mine = _order(session, tenant_id)
    second = _order(
        session, tenant_id, phone=OTHER_PHONE, ready_at=PAST + timedelta(days=1)
    )
    details = Details(name="Müller", days=(date(2026, 10, 12), date(2026, 10, 13)))
    person = verify(session, tenant_id, details, ZONE, NOW)
    assert {order.id for order in person.orders} == {mine.id, second.id}
    assert person.phones == {PHONE, OTHER_PHONE}


def test_a_later_order_of_the_person_counts_without_a_date_of_its_own(
    session, tenant_id
):
    first = _order(session, tenant_id)
    later = _order(session, tenant_id, ready_at=PAST + timedelta(days=5))
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    assert {order.id for order in person.orders} == {first.id, later.id}


def test_a_callback_on_the_call_of_an_order_is_the_persons(session, tenant_id):
    call = _call(session, tenant_id)
    _order(session, tenant_id, call=call)
    callback = _callback(session, tenant_id, call)
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    assert person.calls == [call]
    assert person.callbacks == [callback]
    assert not person.unclaimed_calls
    assert not person.unclaimed_callbacks


def test_rows_with_only_the_number_wait_for_the_guests_word(session, tenant_id):
    _order(session, tenant_id)
    call = _call(session, tenant_id, caller_id="0049 7221 555123")
    callback = _callback(session, tenant_id, call)
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    assert person.unclaimed_calls == [call]
    assert person.unclaimed_callbacks == [callback]
    assert call not in person.calls
    assert callback not in person.callbacks


def test_a_claimed_row_counts_as_the_persons(session, tenant_id):
    _order(session, tenant_id)
    call = _call(session, tenant_id)
    callback = _callback(session, tenant_id, call)
    person = verify(session, tenant_id, MUELLER, ZONE, NOW, claimed=[call.id])
    assert call in person.calls
    assert person.unclaimed_callbacks == [callback]
    person = verify(
        session, tenant_id, MUELLER, ZONE, NOW, claimed=[call.id, callback.id]
    )
    assert callback in person.callbacks
    assert not person.unclaimed_callbacks


def test_a_claim_on_a_row_not_on_offer_is_refused(session, tenant_id):
    _order(session, tenant_id)
    theirs = _order(session, tenant_id, name="Schmidt")
    for row_id in (theirs.call_id, uuid.uuid4()):
        with pytest.raises(InvalidInput):
            verify(session, tenant_id, MUELLER, ZONE, NOW, claimed=[row_id])


def test_a_call_with_two_guests_waits_for_the_guests_word(session, tenant_id):
    call = _call(session, tenant_id)
    _order(session, tenant_id, call=call)
    _reservation(session, tenant_id, name="Schmidt", call=call)
    callback = _callback(session, tenant_id, call)
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    assert (person.calls, person.callbacks) == ([], [])
    assert person.unclaimed_calls == [call]
    assert person.unclaimed_callbacks == [callback]


def test_a_call_with_another_guests_name_is_not_offered(session, tenant_id):
    _order(session, tenant_id)
    theirs = _order(session, tenant_id, name="Schmidt")
    callback = _callback(session, tenant_id, session.get(Call, theirs.call_id))
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    assert not person.unclaimed_calls
    assert not person.unclaimed_callbacks
    assert callback not in person.callbacks


# Access


def test_the_copy_holds_the_persons_rows_and_nothing_of_another_guest(
    session, tenant_id
):
    call = _call(session, tenant_id)
    order = _order(session, tenant_id, call=call, item_note="ohne Zwiebeln")
    _reservation(session, tenant_id, call=call)
    _callback(session, tenant_id, call)
    customer = _customer(session, tenant_id)
    _address(session, tenant_id, customer)
    session.add(
        ConsentLog(
            tenant_id=tenant_id,
            call_id=call.id,
            question="Darf ich?",
            answer="yes",
            answered_at=PAST,
        )
    )
    _event(session, tenant_id, order_id=str(order.id))
    _order(session, tenant_id, name="Schmidt", phone=PHONE, item_note="Schmidts Wunsch")
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    copy = access_copy(session, tenant_id, person)
    text = json.dumps(copy, default=str, ensure_ascii=False)
    for held in (
        "Müller",
        "Bitte klingeln",
        "ohne Zwiebeln",
        "Hauptstraße",
        "Darf ich",
    ):
        assert held in text
    assert "Schmidt" not in text


def test_the_copy_keeps_number_only_rows_apart_for_the_team(session, tenant_id):
    _order(session, tenant_id)
    call = _call(session, tenant_id)
    callback = _callback(session, tenant_id, call)
    session.add(RecognitionObjection(tenant_id=tenant_id, phone=PHONE))
    session.flush()
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    copy = access_copy(session, tenant_id, person)
    assert not copy["copy"]["callbacks"]
    ask = copy["ask_the_guest"]
    assert [row["id"] for row in ask["calls"]] == [call.id]
    assert [row["id"] for row in ask["callbacks"]] == [callback.id]
    assert ask["recognition_objections"] == [PHONE]


def _labels(session, tenant_id, order, labels, action="order.draft_created"):
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor="agent",
            action=action,
            entity="order",
            entity_id=order.id,
            payload={"labels": labels},
        )
    )
    session.flush()


def _lines_of_the_copy(session, tenant_id):
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    return access_copy(session, tenant_id, person)["copy"]["orders"][0]["items"]


def test_the_copy_names_the_dish_as_it_was_ordered(session, tenant_id):
    """Renamed since the call: the guest ordered the name that was read back."""
    order = _order(session, tenant_id)
    _labels(session, tenant_id, order, [["23", "Pho wie damals"]])
    lines = _lines_of_the_copy(session, tenant_id)
    assert [(line["number"], line["dish"]) for line in lines] == [
        ("23", "Pho wie damals")
    ]


def test_the_copy_takes_the_labels_of_the_last_correction(session, tenant_id):
    order = _order(session, tenant_id)
    _labels(session, tenant_id, order, [["23", "Erst so"]])
    _labels(session, tenant_id, order, [["24", "Dann so"]], action="order.corrected")
    assert _lines_of_the_copy(session, tenant_id)[0]["dish"] == "Dann so"


def test_each_label_stays_with_its_line(session, tenant_id):
    order = _order(session, tenant_id, item_note="erste Position")
    first = session.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
    session.add(
        OrderItem(
            tenant_id=tenant_id,
            order_id=order.id,
            menu_item_id=first.menu_item_id,
            quantity=3,
            unit_price_cents=1200,
            note="zweite Position",
            created_at=first.created_at + timedelta(microseconds=1),
        )
    )
    _labels(session, tenant_id, order, [["23", "Erste"], ["24", "Zweite"]])
    lines = _lines_of_the_copy(session, tenant_id)
    assert [(line["dish"], line["note"]) for line in lines] == [
        ("Erste", "erste Position"),
        ("Zweite", "zweite Position"),
    ]


def test_the_copy_falls_back_to_the_menu_without_stored_labels(session, tenant_id):
    order = _order(session, tenant_id)
    line = session.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
    dish = session.get(MenuItem, line.menu_item_id)
    lines = _lines_of_the_copy(session, tenant_id)
    assert [(row["number"], row["dish"]) for row in lines] == [(dish.number, dish.name)]


def test_reading_the_copy_writes_nothing(session, tenant_id):
    order = _order(session, tenant_id)
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    access_copy(session, tenant_id, person)
    assert not session.dirty
    assert _fresh(session, order).customer_name == "Müller"


# Erasure


def _everything(session, tenant_id):
    """The stores of U2 for one guest, each with a row of the kind the job changes."""
    call = _call(session, tenant_id)
    customer = _customer(session, tenant_id)
    address = _address(session, tenant_id, customer)
    order = _order(
        session, tenant_id, call=call, customer=customer, item_note="ohne Zwiebeln"
    )
    reservation = _reservation(session, tenant_id, call=call)
    callback = _callback(session, tenant_id, call)
    events = [
        _event(session, tenant_id, order_id=str(order.id), revision=1),
        _event(
            session,
            tenant_id,
            "reservation.confirmed",
            reservation_id=str(reservation.id),
        ),
        _event(session, tenant_id, "callback.created", callback_id=str(callback.id)),
    ]
    consent = ConsentLog(
        tenant_id=tenant_id,
        call_id=call.id,
        question="Darf ich?",
        answer="yes",
        answered_at=PAST,
    )
    session.add(consent)
    session.flush()
    return call, customer, address, order, reservation, callback, events, consent


def _erase(session, tenant_id, details=MUELLER, keep=(), claimed=()):
    request = _request(session, tenant_id)
    person = verify(session, tenant_id, details, ZONE, NOW, claimed=claimed)
    return erase(session, request, person, keep=keep)


def test_erasure_reaches_every_store_and_the_tax_row_stays(session, tenant_id):
    call, customer, address, order, reservation, callback, events, consent = (
        _everything(session, tenant_id)
    )
    total, items = order.total_cents, order.items_total_cents
    gone = [
        (Customer, customer.id),
        (Address, address.id),
        (Reservation, reservation.id),
        (OutboxEvent, events[1].id),
    ]
    _erase(session, tenant_id)
    session.expire_all()

    assert [session.get(*row) for row in gone] == [None] * 4

    kept = session.get(Order, order.id)
    assert kept.customer_name == ""
    assert kept.phone == ""
    assert kept.note is None
    assert kept.customer_id is None
    assert kept.address_id is None
    assert (kept.total_cents, kept.items_total_cents) == (total, items)
    assert kept.ready_at == PAST
    assert kept.call_id == call.id
    line = session.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
    assert line.note is None
    assert line.unit_price_cents == 1200

    done = session.get(Callback, callback.id)
    assert (done.phone, done.summary) == ("", "")
    assert session.get(Call, call.id).caller_id is None
    assert session.get(OutboxEvent, events[0].id).payload == {
        "order_id": str(order.id),
        "revision": 1,
    }
    assert session.get(OutboxEvent, events[2].id).payload == {
        "callback_id": str(callback.id)
    }
    assert session.get(ConsentLog, consent.id) is not None


def test_erasure_leaves_another_guest_on_the_same_number_alone(session, tenant_id):
    _order(session, tenant_id)
    call = _call(session, tenant_id)
    theirs = _order(session, tenant_id, name="Schmidt", call=call)
    table = _reservation(session, tenant_id, name="Schmidt")
    customer = _customer(session, tenant_id, name="Schmidt")
    callback = _callback(session, tenant_id, call)
    event = _event(session, tenant_id, order_id=str(theirs.id))
    _erase(session, tenant_id)
    session.expire_all()
    assert session.get(Order, theirs.id).customer_name == "Schmidt"
    assert session.get(Order, theirs.id).phone == PHONE
    assert session.get(Reservation, table.id).guest_name == "Schmidt"
    assert session.get(Customer, customer.id) is not None
    assert session.get(Callback, callback.id).phone == PHONE
    assert session.get(Call, call.id).caller_id == PHONE
    assert session.get(OutboxEvent, event.id).payload["customer_name"] == "Müller"


def test_a_second_guest_on_one_call_keeps_their_event(session, tenant_id):
    call = _call(session, tenant_id)
    _order(session, tenant_id, call=call)
    table = _reservation(session, tenant_id, name="Schmidt", call=call)
    event = _event(
        session,
        tenant_id,
        "reservation.confirmed",
        reservation_id=str(table.id),
        call_id=str(call.id),
    )
    _erase(session, tenant_id)
    session.expire_all()
    assert session.get(Reservation, table.id).guest_name == "Schmidt"
    assert session.get(OutboxEvent, event.id).payload["phone"] == PHONE


def test_erasure_stays_in_the_tenant(session, tenant_id):
    other = _tenant(session, "Zwei")
    _order(session, tenant_id)
    theirs = _order(session, other)
    customer = _customer(session, other)
    table = _reservation(session, other)
    _erase(session, tenant_id)
    session.expire_all()
    assert session.get(Order, theirs.id).customer_name == "Müller"
    assert session.get(Customer, customer.id) is not None
    assert session.get(Reservation, table.id) is not None
    assert session.get(Call, theirs.call_id).caller_id == PHONE


def test_a_number_only_row_stays_until_the_guest_claims_it(session, tenant_id):
    _order(session, tenant_id)
    call = _call(session, tenant_id)
    callback = _callback(session, tenant_id, call)
    _erase(session, tenant_id)
    session.expire_all()
    assert session.get(Call, call.id).caller_id == PHONE
    assert session.get(Callback, callback.id).phone == PHONE


def test_a_claimed_row_goes_with_the_rest(session, tenant_id):
    _order(session, tenant_id)
    call = _call(session, tenant_id)
    callback = _callback(session, tenant_id, call)
    _erase(session, tenant_id, claimed=[call.id, callback.id])
    session.expire_all()
    assert session.get(Call, call.id).caller_id is None
    assert session.get(Callback, callback.id).phone == ""


def test_a_future_reservation_stops_the_erasure_until_it_is_closed(session, tenant_id):
    order = _order(session, tenant_id)
    table = _reservation(session, tenant_id, reserved_for=FUTURE)
    with pytest.raises(Conflict) as refused:
        _erase(session, tenant_id)
    assert str(table.id) in refused.value.message
    assert _fresh(session, order).customer_name == "Müller"
    assert _fresh(session, table).guest_name == "Müller"

    table.status = "cancelled"
    table_id = table.id
    _erase(session, tenant_id)
    session.expire_all()
    assert session.get(Reservation, table_id) is None
    assert session.get(Order, order.id).customer_name == ""


def test_a_future_reservation_the_guest_keeps_stays_whole(session, tenant_id):
    order = _order(session, tenant_id)
    table = _reservation(session, tenant_id, reserved_for=FUTURE)
    event = _event(
        session,
        tenant_id,
        "reservation.confirmed",
        reservation_id=str(table.id),
    )
    _erase(session, tenant_id, keep=[table.id])
    session.expire_all()
    assert session.get(Reservation, table.id).guest_name == "Müller"
    assert session.get(OutboxEvent, event.id).payload["phone"] == PHONE
    assert session.get(Order, order.id).customer_name == ""


def test_an_open_callback_stops_the_erasure_until_it_is_done(session, tenant_id):
    call = _call(session, tenant_id)
    _order(session, tenant_id, call=call)
    callback = _callback(session, tenant_id, call, status="open")
    with pytest.raises(Conflict):
        _erase(session, tenant_id)
    callback.status = "done"
    _erase(session, tenant_id)
    assert _fresh(session, callback).phone == ""


def test_an_order_still_in_the_kitchen_cannot_be_kept_and_waits(session, tenant_id):
    _order(session, tenant_id)
    cooking = _order(session, tenant_id, ready_at=FUTURE, status="confirmed")
    with pytest.raises(Conflict):
        _erase(session, tenant_id)
    with pytest.raises(InvalidInput):
        _erase(session, tenant_id, keep=[cooking.id])


@pytest.mark.parametrize(
    ("status", "handover_state"),
    [
        # Waiting for "Passt": `approve_order` builds the slip from these rows.
        ("confirmed", None),
        # A red card: "Nochmal senden" builds the slip from these rows.
        ("confirmed", "failed"),
        ("approved", "failed"),
        ("confirmed", "pending"),
        ("approved", "pending"),
    ],
)
def test_an_order_the_kitchen_has_no_slip_for_stops_the_erasure_past_its_time(
    session, tenant_id, status, handover_state
):
    order = _order(
        session,
        tenant_id,
        status=status,
        item_note="WICHTIG: Keine Erdnüsse. Grund: Allergie",
    )
    order.handover_state = handover_state
    session.flush()
    with pytest.raises(Conflict) as refused:
        _erase(session, tenant_id)
    assert str(order.id) in refused.value.message
    assert _fresh(session, order).customer_name == "Müller"
    line = session.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
    assert line.note.startswith("WICHTIG")


def test_an_order_past_its_time_with_its_slip_out_is_finished(session, tenant_id):
    order = _order(session, tenant_id, status="approved")
    order.handover_state = "sent"
    session.flush()
    _erase(session, tenant_id)
    assert _fresh(session, order).customer_name == ""


def test_a_waiting_order_goes_once_the_kitchen_has_its_slip(session, tenant_id):
    order = _order(session, tenant_id, status="confirmed")
    with pytest.raises(Conflict):
        _erase(session, tenant_id)
    order.status = "approved"
    order.handover_state = "sent"
    session.flush()
    _erase(session, tenant_id)
    assert _fresh(session, order).customer_name == ""


def test_an_order_nobody_will_serve_goes_with_the_button(session, tenant_id):
    """Nobody ever tapped "Passt", so the order would stop the erasure for good."""
    order = _order(session, tenant_id, status="confirmed")
    session.commit()
    with pytest.raises(Conflict):
        _erase(session, tenant_id)

    cancel_order(session, tenant_id, order.id, version_of(session, order), now=NOW)

    assert verify(session, tenant_id, MUELLER, ZONE, NOW).open_rows == []
    _erase(session, tenant_id)
    kept = _fresh(session, order)
    assert (kept.customer_name, kept.status) == ("", "cancelled")


def test_a_cancel_note_on_its_way_stops_the_erasure_until_it_printed(
    session, tenant_id
):
    """The note carries the guest's name, like every slip: its event is open."""
    order = _order(session, tenant_id, status="approved")
    _labels(session, tenant_id, order, [["23", "Frühlingsrollen"]])
    printed = _event(session, tenant_id, order_id=str(order.id))
    printed.attempts = 1
    session.commit()

    cancel_order(session, tenant_id, order.id, version_of(session, order), now=NOW)

    with pytest.raises(Conflict) as refused:
        _erase(session, tenant_id)
    assert str(order.id) in refused.value.message
    note = session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.status == "pending",
            OutboxEvent.payload["order_id"].astext == str(order.id),
        )
    )
    ack_ticket(session, tenant_id, note.id, True, NOW)

    _erase(session, tenant_id)
    assert _fresh(session, order).customer_name == ""


ALLERGY = "WICHTIG: Keine Erdnüsse. Grund: Allergie"
# Younger than `service_config.max_call_seconds`, 420 by default.
JUST_NOW = timedelta(minutes=2)
TOO_OLD = timedelta(minutes=8)


def _confirm(session, tenant_id, row, entity):
    request = ConfirmRequest(
        call_id=row.call_id,
        tenant_id=tenant_id,
        entity=entity,
        entity_id=row.id,
        idempotency_key=uuid.uuid4().hex,
    )
    return confirm(session, request, NOW)


def _draft(session, tenant_id, age=JUST_NOW, ended_at=None) -> Order:
    """A draft of a guest with an older order, the row that opens the person."""
    _order(session, tenant_id)
    draft = _order(session, tenant_id, status="draft", ready_at=None, item_note=ALLERGY)
    draft.created_at = NOW - age
    session.get(Call, draft.call_id).ended_at = ended_at
    session.flush()
    return draft


def _table_draft(session, tenant_id, age=JUST_NOW, ended_at=None) -> Reservation:
    _order(session, tenant_id)
    draft = _reservation(session, tenant_id, reserved_for=FUTURE, status="draft")
    draft.created_at = NOW - age
    session.get(Call, draft.call_id).ended_at = ended_at
    session.flush()
    return draft


def test_a_draft_its_call_can_confirm_stops_the_erasure(session, tenant_id):
    draft = _draft(session, tenant_id)
    with pytest.raises(Conflict) as refused:
        _erase(session, tenant_id)
    assert str(draft.id) in refused.value.message
    assert _fresh(session, draft).customer_name == "Müller"
    line = session.scalar(select(OrderItem).where(OrderItem.order_id == draft.id))
    assert line.note == ALLERGY


def test_the_yes_after_a_refused_erasure_brings_the_kitchen_its_allergy_note(
    session, tenant_id
):
    draft = _draft(session, tenant_id)
    with pytest.raises(Conflict):
        _erase(session, tenant_id)
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(call_mode="primary")
    )
    _labels(session, tenant_id, draft, [["23", "Pho"]])
    assert _confirm(session, tenant_id, draft, "order").status == "confirmed"
    payload = session.scalar(
        select(OutboxEvent.payload).where(OutboxEvent.event_type == "order.confirmed")
    )
    assert payload["customer_name"] == "Müller"
    assert payload["items"][0]["note"] == ALLERGY


def test_an_order_draft_cannot_be_kept(session, tenant_id):
    draft = _draft(session, tenant_id)
    with pytest.raises(InvalidInput):
        _erase(session, tenant_id, keep=[draft.id])


@pytest.mark.parametrize(
    ("age", "ended_at"),
    [(JUST_NOW, NOW - timedelta(minutes=1)), (TOO_OLD, None)],
    ids=["call ended", "older than any call"],
)
def test_a_draft_no_call_can_confirm_goes_and_cannot_be_confirmed(
    session, tenant_id, age, ended_at
):
    """`confirm` itself takes any draft of its call, also of a call that ended,
    so an erasure closes it instead of leaving it to be confirmed without its
    notes, and waits for nobody."""
    draft = _draft(session, tenant_id, age, ended_at)
    _erase(session, tenant_id)
    kept = _fresh(session, draft)
    assert (kept.customer_name, kept.status) == ("", "cancelled")
    with pytest.raises(Conflict):
        _confirm(session, tenant_id, kept, "order")
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_a_reservation_draft_its_call_can_confirm_stops_the_erasure(session, tenant_id):
    draft = _table_draft(session, tenant_id)
    with pytest.raises(Conflict) as refused:
        _erase(session, tenant_id)
    assert str(draft.id) in refused.value.message
    assert _fresh(session, draft).guest_name == "Müller"


def test_the_yes_after_a_refused_erasure_keeps_the_reservation(session, tenant_id):
    draft = _table_draft(session, tenant_id)
    with pytest.raises(Conflict):
        _erase(session, tenant_id)
    assert _confirm(session, tenant_id, draft, "reservation").status == "confirmed"
    assert _fresh(session, draft).guest_name == "Müller"


def test_a_reservation_draft_the_guest_keeps_stays_whole(session, tenant_id):
    draft = _table_draft(session, tenant_id)
    _erase(session, tenant_id, keep=[draft.id])
    assert _fresh(session, draft).guest_name == "Müller"


@pytest.mark.parametrize(
    ("age", "ended_at"),
    [(JUST_NOW, NOW - timedelta(minutes=1)), (TOO_OLD, None)],
    ids=["call ended", "older than any call"],
)
def test_a_reservation_draft_no_call_can_confirm_goes_for_good(
    session, tenant_id, age, ended_at
):
    draft = _table_draft(session, tenant_id, age, ended_at)
    gone = SimpleNamespace(id=draft.id, call_id=draft.call_id)
    _erase(session, tenant_id)
    session.expire_all()
    assert session.get(Reservation, gone.id) is None
    with pytest.raises(NotFound):
        _confirm(session, tenant_id, gone, "reservation")


def test_a_soft_deleted_draft_stops_nothing(session, tenant_id):
    """A soft-deleted draft: `confirm` refuses it, so it stops nothing."""
    draft = _draft(session, tenant_id)
    draft.deleted_at = NOW
    session.flush()
    _erase(session, tenant_id)
    assert _fresh(session, draft).customer_name == ""


def test_an_unsent_event_stops_the_erasure_until_it_went_out(session, tenant_id):
    order = _order(session, tenant_id)
    event = _event(session, tenant_id, status="pending", order_id=str(order.id))
    with pytest.raises(Conflict):
        _erase(session, tenant_id)
    assert _fresh(session, order).customer_name == "Müller"
    event.status = "failed"
    _erase(session, tenant_id)
    assert _fresh(session, event).payload == {"order_id": str(order.id)}


def test_keeping_a_row_that_is_not_open_is_refused(session, tenant_id):
    order = _order(session, tenant_id)
    with pytest.raises(InvalidInput):
        _erase(session, tenant_id, keep=[order.id])


def test_the_ledger_names_every_row_without_a_name_or_a_number(session, tenant_id):
    call, customer, address, order, reservation, callback, events, _ = _everything(
        session, tenant_id
    )
    line = session.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
    expected = {
        ("customers", customer.id, "deleted"),
        ("addresses", address.id, "deleted"),
        ("reservations", reservation.id, "deleted"),
        ("orders", order.id, "cleared"),
        ("order_items", line.id, "cleared"),
        ("callbacks", callback.id, "cleared"),
        ("calls", call.id, "cleared"),
        ("outbox", events[0].id, "thinned"),
        ("outbox", events[1].id, "deleted"),
        ("outbox", events[2].id, "thinned"),
    }
    ledger = _erase(session, tenant_id)
    assert {tuple(entry) for entry in ledger} == expected
    text = " ".join(str(entry) for entry in ledger)
    assert "Müller" not in text
    assert PHONE not in text


def test_the_erasure_writes_the_audit_log_with_counts_only(session, tenant_id):
    _everything(session, tenant_id)
    _erase(session, tenant_id)
    entry = session.scalar(
        select(AuditLog).where(AuditLog.action == "data_subject.erased")
    )
    assert entry.entity == "data_subject_request"
    assert entry.payload == {
        "addresses": 1,
        "calls": 1,
        "callbacks": 1,
        "customers": 1,
        "order_items": 1,
        "orders": 1,
        "outbox": 3,
        "reservations": 1,
    }


def test_a_second_run_finds_nobody_and_changes_nothing(session, tenant_id):
    _everything(session, tenant_id)
    _erase(session, tenant_id)
    with pytest.raises(NotFound):
        _erase(session, tenant_id)


def test_erasure_wants_an_erasure_request_that_is_still_open(session, tenant_id):
    _order(session, tenant_id)
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    with pytest.raises(Conflict):
        erase(session, _request(session, tenant_id, kind="access"), person)
    answered = _request(session, tenant_id)
    answer_request(session, answered, "done", NOW, ZONE)
    with pytest.raises(Conflict):
        erase(session, answered, person)


# Objection


def test_an_objection_switches_recognition_off_for_the_persons_number(
    session, tenant_id
):
    customer = _customer(session, tenant_id)
    call = _call(session, tenant_id)
    _order(session, tenant_id, call=call)
    request = _request(session, tenant_id, kind="objection")
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    assert find_customer(session, tenant_id, PHONE).found

    ledger = record_objection(session, request, person)
    assert not find_customer(session, tenant_id, PHONE).found
    assert _fresh(session, call).caller_id is None
    assert [(entry.table, entry.action) for entry in ledger] == [("calls", "cleared")]
    # The customer stays: an objection is no erasure.
    assert _fresh(session, customer).name == "Müller"


def test_a_second_objection_on_the_number_changes_nothing(session, tenant_id):
    _order(session, tenant_id)
    session.add(RecognitionObjection(tenant_id=tenant_id, phone=PHONE))
    session.flush()
    request = _request(session, tenant_id, kind="objection")
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    record_objection(session, request, person)
    rows = session.scalars(select(RecognitionObjection)).all()
    assert [row.phone for row in rows] == [PHONE]


def test_an_objection_stays_in_the_tenant(session, tenant_id):
    other = _tenant(session, "Zwei")
    _customer(session, other)
    _order(session, tenant_id)
    request = _request(session, tenant_id, kind="objection")
    record_objection(session, request, verify(session, tenant_id, MUELLER, ZONE, NOW))
    assert find_customer(session, other, PHONE).found
    assert not find_customer(session, tenant_id, PHONE).found


def test_an_objection_wants_an_objection_request(session, tenant_id):
    _order(session, tenant_id)
    person = verify(session, tenant_id, MUELLER, ZONE, NOW)
    with pytest.raises(Conflict):
        record_objection(session, _request(session, tenant_id), person)
