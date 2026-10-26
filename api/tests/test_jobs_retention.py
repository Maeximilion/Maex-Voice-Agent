"""Daily deletion job (T-11.2): every period of docs/03 "Loeschkonzept" at its border."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.config import Settings, settings
from api.jobs.retention import purge
from api.models import (
    AuditLog,
    Call,
    Callback,
    Order,
    OrderItem,
    OutboxEvent,
    Reservation,
)
from api.tests.test_domain_draft_order import _tenant, item

BERLIN = ZoneInfo("Europe/Berlin")
# A Wednesday after 05:00 local: the business day of 14.10. is over.
NOW = datetime(2026, 10, 15, 6, 0, tzinfo=BERLIN).astimezone(UTC)
DAY = timedelta(days=1)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return _tenant(session, "Testbetrieb")


def _call(session, tenant_id, started_at=NOW, caller_id="+497215551234") -> Call:
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        caller_id=caller_id,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=2),
        duration_seconds=120,
        outcome="completed",
        delete_after=started_at.date(),
    )
    session.add(call)
    session.flush()
    return call


def _order(session, tenant_id, ready_at, note="ohne Zwiebeln", item_note=None):
    order = Order(
        tenant_id=tenant_id,
        call_id=_call(session, tenant_id).id,
        type="pickup",
        status="confirmed",
        customer_id=uuid.uuid4(),
        customer_name="Mueller",
        phone="+497215551234",
        items_total_cents=1980,
        total_cents=1980,
        ready_at=ready_at,
        note=note,
        idempotency_key=uuid.uuid4().hex,
    )
    session.add(order)
    session.flush()
    session.add(
        OrderItem(
            tenant_id=tenant_id,
            order_id=order.id,
            menu_item_id=item(session, tenant_id, "23"),
            quantity=2,
            unit_price_cents=990,
            note=item_note,
        )
    )
    session.flush()
    return order


def _reservation(session, tenant_id, reserved_for, note="Kinderstuhl"):
    reservation = Reservation(
        tenant_id=tenant_id,
        call_id=_call(session, tenant_id).id,
        status="confirmed",
        guest_name="Mueller",
        phone="+497215551234",
        party_size=4,
        reserved_for=reserved_for,
        note=note,
        idempotency_key=uuid.uuid4().hex,
    )
    session.add(reservation)
    session.flush()
    return reservation


def _callback(session, tenant_id, status="done", done_at=None):
    callback = Callback(
        tenant_id=tenant_id,
        call_id=_call(session, tenant_id).id,
        phone="+497215551234",
        reason="human_requested",
        summary="Moechte den Chef sprechen",
        status=status,
        done_at=done_at,
    )
    session.add(callback)
    session.flush()
    return callback


PAYLOAD = {
    "order_id": "8a1c2f10-0000-4000-8000-000000000001",
    "call_id": "8a1c2f10-0000-4000-8000-000000000002",
    "revision": 1,
    "customer_name": "Mueller",
    "phone": "+497215551234",
    "items": [{"number": "23", "note": "WICHTIG: Keine Erdnuesse. Grund: Allergie"}],
}


def _event(session, tenant_id, status, created_at, sent_at=None):
    event = OutboxEvent(
        tenant_id=tenant_id,
        event_type="order.confirmed",
        payload=PAYLOAD,
        status=status,
        created_at=created_at,
        sent_at=sent_at,
    )
    session.add(event)
    session.flush()
    return event


def _fresh(session, row):
    session.refresh(row)
    return row


# --- calls.caller_id: 90 days after the call ---------------------------------------


def test_caller_number_goes_after_90_days_and_the_statistics_stay(session, tenant_id):
    old = _call(session, tenant_id, started_at=NOW - 90 * DAY - timedelta(minutes=1))
    young = _call(session, tenant_id, started_at=NOW - 89 * DAY)
    withheld = _call(session, tenant_id, started_at=NOW - 200 * DAY, caller_id=None)
    session.commit()

    counts = purge(session, NOW)

    assert _fresh(session, old).caller_id is None
    assert (old.duration_seconds, old.outcome) == (120, "completed")
    assert _fresh(session, young).caller_id == "+497215551234"
    assert _fresh(session, withheld).caller_id is None
    assert counts["caller_ids"] == 1


# --- orders and reservations: guest details 90 days after their date ---------------


def test_order_loses_name_phone_notes_and_link_after_90_days(session, tenant_id):
    old = _order(session, tenant_id, NOW - 91 * DAY, item_note="extra scharf")
    young = _order(session, tenant_id, NOW - 89 * DAY, item_note="extra scharf")
    session.commit()

    counts = purge(session, NOW)

    old = _fresh(session, old)
    assert (old.customer_name, old.phone, old.note, old.customer_id) == (
        "",
        "",
        None,
        None,
    )
    assert (old.total_cents, old.status, old.ready_at) == (
        1980,
        "confirmed",
        NOW - 91 * DAY,
    )
    items = session.scalars(select(OrderItem).where(OrderItem.order_id == old.id)).all()
    assert [(i.note, i.quantity, i.unit_price_cents) for i in items] == [(None, 2, 990)]
    young = _fresh(session, young)
    assert (young.customer_name, young.note) == ("Mueller", "ohne Zwiebeln")
    young_items = session.scalars(
        select(OrderItem.note).where(OrderItem.order_id == young.id)
    ).all()
    assert young_items == ["extra scharf"]
    assert counts["orders"] == 1


def test_order_without_ready_time_counts_from_its_creation(session, tenant_id):
    draft = _order(session, tenant_id, ready_at=None, item_note="extra scharf")
    draft.status = "draft"
    draft.created_at = NOW - 91 * DAY
    session.commit()

    purge(session, NOW)

    assert _fresh(session, draft).customer_name == ""
    notes = session.scalars(
        select(OrderItem.note).where(OrderItem.order_id == draft.id)
    )
    assert list(notes) == [None]


def test_reservation_loses_name_phone_and_note_90_days_after_its_date(
    session, tenant_id
):
    old = _reservation(session, tenant_id, NOW - 91 * DAY)
    coming = _reservation(session, tenant_id, NOW + 30 * DAY)
    session.commit()

    counts = purge(session, NOW)

    old = _fresh(session, old)
    assert (old.guest_name, old.phone, old.note, old.party_size) == ("", "", None, 4)
    assert _fresh(session, coming).guest_name == "Mueller"
    assert counts["reservations"] == 1


# --- a caller's allergy: the night after the order or reservation ------------------

ALLERGY = "WICHTIG: Keine Erdnuesse. Grund: Allergie"
YESTERDAY_EVENING = datetime(2026, 10, 14, 20, 0, tzinfo=BERLIN).astimezone(UTC)


def test_allergy_note_goes_once_the_business_day_is_over(session, tenant_id):
    order = _order(
        session, tenant_id, YESTERDAY_EVENING, note=ALLERGY, item_note=ALLERGY
    )
    plain = _order(session, tenant_id, YESTERDAY_EVENING, item_note="ohne Zwiebeln")
    table = _reservation(session, tenant_id, YESTERDAY_EVENING, note="Nussallergie")
    session.commit()

    counts = purge(session, NOW)

    order = _fresh(session, order)
    assert order.note is None
    assert order.customer_name == "Mueller"
    notes = session.scalars(
        select(OrderItem.note).where(OrderItem.order_id == order.id)
    )
    assert list(notes) == [None]
    assert _fresh(session, plain).note == "ohne Zwiebeln"
    plain_notes = session.scalars(
        select(OrderItem.note).where(OrderItem.order_id == plain.id)
    )
    assert list(plain_notes) == ["ohne Zwiebeln"]
    assert _fresh(session, table).note is None
    assert counts["allergy_notes"] == 3


def test_allergy_note_stays_until_05_00_of_the_next_morning(session, tenant_id):
    """00:30 still belongs to the evening before (core/time.py DAY_STARTS_AT)."""
    order = _order(session, tenant_id, YESTERDAY_EVENING, note=ALLERGY)
    table = _reservation(session, tenant_id, YESTERDAY_EVENING, note=ALLERGY)
    session.commit()
    before_five = datetime(2026, 10, 15, 4, 59, tzinfo=BERLIN).astimezone(UTC)

    purge(session, before_five)

    assert _fresh(session, order).note == ALLERGY
    assert _fresh(session, table).note == ALLERGY


@pytest.mark.parametrize(
    "note",
    ["Unverträglichkeit Laktose", "LAKTOSEINTOLERANZ", "allergisch gegen Sesam"],
)
def test_other_words_for_an_allergy_count_as_well(session, tenant_id, note):
    order = _order(session, tenant_id, YESTERDAY_EVENING, note=note)
    session.commit()

    purge(session, NOW)

    assert _fresh(session, order).note is None


# --- callbacks: 90 days after the callback is done ----------------------------------


def test_callback_loses_phone_and_summary_90_days_after_done(session, tenant_id):
    old = _callback(session, tenant_id, done_at=NOW - 91 * DAY)
    young = _callback(session, tenant_id, done_at=NOW - 89 * DAY)
    still_open = _callback(session, tenant_id, status="open")
    still_open.created_at = NOW - 200 * DAY
    session.commit()

    counts = purge(session, NOW)

    old = _fresh(session, old)
    assert (old.phone, old.summary, old.reason) == ("", "", "human_requested")
    assert _fresh(session, young).phone == "+497215551234"
    assert _fresh(session, still_open).summary == "Moechte den Chef sprechen"
    assert counts["callbacks"] == 1


# --- outbox: payload 7 days after dispatch, unsent at most 30, row 90 ---------------


def test_sent_payload_keeps_only_its_ids_after_7_days(session, tenant_id):
    old = _event(session, tenant_id, "sent", NOW - 9 * DAY, sent_at=NOW - 8 * DAY)
    young = _event(session, tenant_id, "sent", NOW - 7 * DAY, sent_at=NOW - 6 * DAY)
    session.commit()

    counts = purge(session, NOW)

    # The kitchen finds every slip of an order by its id and revision
    # (domain/ordering/handover.py), so those stay; the guest goes.
    assert _fresh(session, old).payload == {
        "order_id": PAYLOAD["order_id"],
        "call_id": PAYLOAD["call_id"],
        "revision": 1,
    }
    assert _fresh(session, young).payload == PAYLOAD
    assert counts["outbox_payloads"] == 1


def test_unsent_payload_goes_after_30_days_and_is_never_sent_without_it(
    session, tenant_id
):
    stuck = _event(session, tenant_id, "pending", NOW - 31 * DAY)
    failed = _event(session, tenant_id, "failed", NOW - 31 * DAY)
    recent = _event(session, tenant_id, "failed", NOW - 29 * DAY)
    session.commit()

    counts = purge(session, NOW)

    stuck = _fresh(session, stuck)
    assert stuck.status == "failed"
    assert "customer_name" not in stuck.payload
    assert "30" in stuck.last_error
    assert "phone" not in _fresh(session, failed).payload
    assert _fresh(session, recent).payload == PAYLOAD
    assert counts["outbox_payloads"] == 2


def test_event_row_goes_after_90_days(session, tenant_id):
    old = _event(session, tenant_id, "sent", NOW - 91 * DAY, sent_at=NOW - 91 * DAY)
    young = _event(session, tenant_id, "sent", NOW - 89 * DAY, sent_at=NOW - 89 * DAY)
    old_id, young_id = old.id, young.id
    session.commit()

    counts = purge(session, NOW)

    assert session.get(OutboxEvent, old_id) is None
    assert session.get(OutboxEvent, young_id) is not None
    assert counts["outbox_rows"] == 1


# --- audit_log: 12 months -----------------------------------------------------------


def test_audit_entries_go_after_12_months(session, tenant_id):
    for age in (366, 364):
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                at=NOW - age * DAY,
                actor="system",
                action="config.update",
                entity="service_config",
            )
        )
    session.commit()

    counts = purge(session, NOW)

    ages = session.scalars(
        select(AuditLog.at).where(AuditLog.action == "config.update")
    ).all()
    assert ages == [NOW - 364 * DAY]
    assert counts["audit_entries"] == 1


# --- the run as a whole -------------------------------------------------------------


def test_second_pass_changes_nothing_and_each_pass_logs_its_counts(
    session, tenant_id, caplog
):
    _call(session, tenant_id, started_at=NOW - 91 * DAY)
    _order(session, tenant_id, NOW - 91 * DAY, note=ALLERGY, item_note=ALLERGY)
    _reservation(session, tenant_id, NOW - 91 * DAY)
    _callback(session, tenant_id, done_at=NOW - 91 * DAY)
    _event(session, tenant_id, "sent", NOW - 10 * DAY, sent_at=NOW - 10 * DAY)
    session.commit()

    caplog.set_level(logging.INFO, logger="api.jobs.retention")

    first = purge(session, NOW)
    second = purge(session, NOW)

    assert all(first[key] for key in ("caller_ids", "orders", "callbacks"))
    assert set(second.values()) == {0}
    lines = [r for r in caplog.records if r.getMessage() == "Retention pass"]
    assert [r.extra for r in lines] == [first, second]


def test_periods_come_from_the_settings(session, tenant_id, monkeypatch):
    call = _call(session, tenant_id, started_at=NOW - 31 * DAY)
    session.commit()
    monkeypatch.setattr(settings, "retention_caller_id_days", 30)

    purge(session, NOW)

    assert _fresh(session, call).caller_id is None


@pytest.mark.parametrize(
    "name",
    [
        "retention_caller_id_days",
        "retention_guest_details_days",
        "retention_callback_days",
        "retention_outbox_sent_days",
        "retention_outbox_unsent_days",
        "retention_outbox_row_days",
        "retention_audit_log_days",
    ],
)
def test_a_period_of_zero_days_is_refused(name):
    """0 would strip the orders the kitchen is cooking right now."""
    with pytest.raises(ValidationError):
        Settings(**{name: 0})


def test_cold_path_runs_the_job_in_its_own_loop(migrated_db_url, session, tenant_id):
    from api.jobs.cold_path import watch_forever

    call = _call(session, tenant_id, started_at=NOW - 91 * DAY)
    session.commit()

    class Once:
        done = False

        def is_set(self):
            return self.done

        def wait(self, timeout=None):
            self.done = True
            return True

    engine = create_engine(migrated_db_url)
    try:
        watch_forever(
            Once(),
            0,
            session_factory=lambda: Session(engine),
            clock=lambda: NOW,
            job=purge,
        )
    finally:
        engine.dispose()
    assert _fresh(session, call).caller_id is None
