"""Daily deletion job (T-11.2): every period of docs/03 "Loeschkonzept" at its border."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.config import Settings, settings
from api.jobs.retention import purge
from api.models import (
    AuditLog,
    Call,
    Callback,
    ConsentLog,
    Order,
    OrderItem,
    OutboxEvent,
    Reservation,
    Tenant,
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


def _copy(session, tenant_id, event_type, payload, status="sent"):
    """The copy confirm writes into the outbox (domain/ordering/confirm.py,
    domain/confirm.py), sent yesterday evening."""
    event = OutboxEvent(
        tenant_id=tenant_id,
        event_type=event_type,
        payload=payload,
        status=status,
        sent_at=YESTERDAY_EVENING if status == "sent" else None,
    )
    session.add(event)
    session.flush()
    return event


def test_allergy_note_goes_from_the_outbox_copies_the_same_night(session, tenant_id):
    """Codex PR #371: confirm copies the notes into the outbox payload, where
    they would stay 7 days after dispatch; L13 says the night after."""
    order = _order(
        session, tenant_id, YESTERDAY_EVENING, note=ALLERGY, item_note=ALLERGY
    )
    ticket = _copy(
        session,
        tenant_id,
        "order.confirmed",
        {
            "order_id": str(order.id),
            "customer_name": "Mueller",
            "note": ALLERGY,
            "items": [
                {"number": "23", "note": ALLERGY},
                {"number": "12", "note": "ohne Zwiebeln"},
            ],
        },
        status="pending",
    )
    table = _reservation(session, tenant_id, YESTERDAY_EVENING, note="Nussallergie")
    confirmed = _copy(
        session,
        tenant_id,
        "reservation.confirmed",
        {
            "reservation_id": str(table.id),
            "guest_name": "Mueller",
            "note": "Nussallergie",
        },
    )
    session.commit()

    counts = purge(session, NOW)

    assert _fresh(session, ticket).payload == {
        "order_id": str(order.id),
        "customer_name": "Mueller",
        "note": None,
        "items": [
            {"number": "23", "note": None},
            {"number": "12", "note": "ohne Zwiebeln"},
        ],
    }
    assert _fresh(session, confirmed).payload == {
        "reservation_id": str(table.id),
        "guest_name": "Mueller",
        "note": None,
    }
    # Two notes on the order, the reservation's, and the two copies.
    assert counts["allergy_notes"] == 5
    assert set(purge(session, NOW).values()) == {0}


def test_outbox_copy_of_a_coming_order_keeps_its_allergy(session, tenant_id):
    """The kitchen still needs the note of today's order; before 05:00 the
    copy of yesterday evening's stays as well."""
    today = _order(session, tenant_id, NOW + timedelta(hours=12), item_note=ALLERGY)
    copy = {"order_id": str(today.id), "items": [{"number": "23", "note": ALLERGY}]}
    ticket = _copy(session, tenant_id, "order.confirmed", copy, status="pending")
    evening = _order(session, tenant_id, YESTERDAY_EVENING, item_note=ALLERGY)
    late = {"order_id": str(evening.id), "items": [{"number": "23", "note": ALLERGY}]}
    sent = _copy(session, tenant_id, "order.confirmed", late)
    session.commit()
    before_five = datetime(2026, 10, 15, 4, 59, tzinfo=BERLIN).astimezone(UTC)

    purge(session, before_five)
    assert _fresh(session, sent).payload == late

    purge(session, NOW)
    assert _fresh(session, ticket).payload == copy
    assert _fresh(session, sent).payload["items"][0]["note"] is None


# --- callbacks: 90 days after the callback is done ----------------------------------


def test_callback_loses_phone_and_summary_90_days_after_done(session, tenant_id):
    old = _callback(session, tenant_id, done_at=NOW - 91 * DAY)
    old.created_at = NOW - 95 * DAY
    young = _callback(session, tenant_id, done_at=NOW - 89 * DAY)
    young.created_at = NOW - 120 * DAY
    session.commit()

    counts = purge(session, NOW)

    old = _fresh(session, old)
    assert (old.phone, old.summary, old.reason) == ("", "", "human_requested")
    # Done 89 days ago: the period counts from "done", not from the creation.
    assert _fresh(session, young).phone == "+497215551234"
    assert counts["callbacks"] == 1


def test_open_callback_loses_phone_and_summary_90_days_after_creation(
    session, tenant_id
):
    """Ultrareview PR #371, decided by Maxi on 26.10.2026 (option A): a
    callback the team never marked done keeps nothing past 90 days; nobody
    calls back after three months. The card stays open with its reason."""
    old = _callback(session, tenant_id, status="open")
    old.created_at = NOW - 91 * DAY
    young = _callback(session, tenant_id, status="open")
    young.created_at = NOW - 89 * DAY
    session.commit()

    counts = purge(session, NOW)

    old = _fresh(session, old)
    assert (old.phone, old.summary, old.status, old.reason) == (
        "",
        "",
        "open",
        "human_requested",
    )
    assert _fresh(session, young).summary == "Moechte den Chef sprechen"
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


# --- consent_log: end of the third calendar year after the answer --------------------


def _consent(session, tenant_id, answered_at, answer="yes") -> ConsentLog:
    row = ConsentLog(
        tenant_id=tenant_id,
        call_id=_call(session, tenant_id).id,
        question="Darf ich die Allergie als Hinweis für die Küche notieren?",
        answer=answer,
        answered_at=answered_at,
    )
    session.add(row)
    session.flush()
    return row


def _berlin(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BERLIN).astimezone(UTC)


def test_consent_answer_goes_at_the_end_of_the_third_year_after(session, tenant_id):
    """Maxi, 26.10.2026: an answer from 2026 stays through 31.12.2029 and goes
    on 01.01.2030, the proof of a stored note for as long as a claim can arise
    (§§ 195, 199 BGB)."""
    old = _consent(session, tenant_id, _berlin(2026, 6, 1, 18, 0))
    young = _consent(session, tenant_id, _berlin(2027, 3, 1, 12, 0), answer="no")
    old_id, young_id = old.id, young.id
    session.commit()

    counts = purge(session, _berlin(2029, 12, 31, 23, 59))
    assert session.get(ConsentLog, old_id) is not None
    assert counts["consent_answers"] == 0

    counts = purge(session, _berlin(2030, 1, 1, 0, 1))
    session.expire_all()
    assert session.get(ConsentLog, old_id) is None
    assert session.get(ConsentLog, young_id) is not None
    assert counts["consent_answers"] == 1


def test_the_year_of_an_answer_is_the_restaurants_not_utc(session, tenant_id):
    """00:30 on New Year's Day in Berlin is still the old year in UTC: the
    answer belongs to the new year and stays a year longer."""
    late = _consent(session, tenant_id, _berlin(2026, 12, 31, 23, 30))
    early = _consent(session, tenant_id, _berlin(2027, 1, 1, 0, 30))
    assert early.answered_at.astimezone(UTC).year == 2026
    late_id, early_id = late.id, early.id
    session.commit()

    purge(session, _berlin(2030, 1, 1, 6, 0))

    session.expire_all()
    assert session.get(ConsentLog, late_id) is None
    assert session.get(ConsentLog, early_id) is not None


def test_consent_years_come_from_the_setting(session, tenant_id, monkeypatch):
    row = _consent(session, tenant_id, _berlin(2028, 5, 1, 12, 0))
    row_id = row.id
    session.commit()
    monkeypatch.setattr(settings, "retention_consent_log_years", 1)

    purge(session, _berlin(2030, 1, 1, 6, 0))

    session.expire_all()
    assert session.get(ConsentLog, row_id) is None


@pytest.mark.parametrize("years", [0, 11])
def test_consent_years_are_bounded(years):
    """0 would delete the proof of tonight's notes; past ten years the
    period has no reason left."""
    with pytest.raises(ValidationError):
        Settings(retention_consent_log_years=years)
    Settings(retention_consent_log_years=10)


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


@pytest.mark.parametrize(
    "name",
    [
        "retention_caller_id_days",
        "retention_audit_log_days",
        "retention_allergy_note_days",
        "retention_interval_seconds",
    ],
)
def test_a_period_beyond_ten_years_is_refused(name):
    """Ultrareview PR #371: 999,999,999 days, the usual way to switch a period
    off, overflowed the date arithmetic of start_call and of every pass."""
    with pytest.raises(ValidationError):
        Settings(**{name: 999_999_999})
    Settings(**{name: 3660 if name.endswith("_days") else 86400})


def test_one_failing_period_does_not_stop_the_others(session, tenant_id, caplog):
    """Ultrareview PR #371: a tenant time zone Python does not know broke the
    allergy step, the first, and with it every deletion of the pass."""
    caplog.set_level(logging.INFO, logger="api.jobs.retention")
    call = _call(session, tenant_id, started_at=NOW - 91 * DAY)
    order = _order(session, tenant_id, YESTERDAY_EVENING, note=ALLERGY)
    session.execute(
        update(Tenant).where(Tenant.id == tenant_id).values(timezone="Mars/Base")
    )
    session.commit()

    counts = purge(session, NOW)

    assert _fresh(session, call).caller_id is None
    assert _fresh(session, order).note == ALLERGY
    assert "allergy_notes" not in counts
    assert counts["caller_ids"] == 1
    failed = [r for r in caplog.records if r.getMessage() == "Retention step failed"]
    assert [r.extra["step"] for r in failed] == ["allergy_notes", "consent_answers"]
    passes = [r for r in caplog.records if r.getMessage() == "Retention pass"]
    assert passes[-1].extra["failed"] == ["allergy_notes", "consent_answers"]


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
