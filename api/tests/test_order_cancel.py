"""The button "Storniert" and the note a slip at the register gets (docs/06 §3,
docs/09 §1 L18)."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.core.errors import Conflict, NotFound
from api.domain.ordering.board import (
    approve_order,
    cancel_order,
    list_new,
    mark_entered,
    new_orders_change_token,
    resend_order,
)
from api.domain.ordering.correction import RowEdit, preview_correction
from api.domain.ordering.handover import (
    PICKUP_TIMEOUT_SECONDS,
    WITHDRAWN,
    ack_ticket,
    claim_tickets,
    sweep,
)
from api.domain.ordering.ticket import ACTION_CANCELLED
from api.events.types import ORDER_CONFIRMED
from api.models import AuditLog, OrderItem, OutboxEvent
from api.tests.test_domain_confirm_order import _draft, _mode
from api.tests.test_domain_draft_order import NOW, _call, _tenant
from api.tests.test_domain_ordering_board import TZ, _ids, _order, _seen
from api.tests.test_domain_ordering_correction import _fix
from api.tests.test_gui_orders import _fresh

# A moment of the test, not of the clock: every ticket is made due at it.
AT = NOW + timedelta(minutes=5)


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    # As `api/db.py` makes them: without autoflush, nothing here may rest on one.
    with Session(engine, autoflush=False, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tid = _tenant(session, "Testbetrieb")
    _mode(session, tid, "primary")
    return tid


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    return _call(session, tenant_id)


def _tickets(session, order_id) -> list[OutboxEvent]:
    return list(
        session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type == ORDER_CONFIRMED,
                OutboxEvent.payload["order_id"].astext == str(order_id),
            )
            .order_by(OutboxEvent.created_at)
            .execution_options(populate_existing=True)
        )
    )


def _claim(session, tenant_id) -> list[dict]:
    """What the bridge picks up. A row's `next_attempt_at` starts on the
    database's clock, so everything waiting is made due at the test's moment."""
    session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.tenant_id == tenant_id, OutboxEvent.status == "pending")
        .values(next_attempt_at=AT)
    )
    session.commit()
    return claim_tickets(session, tenant_id, AT)


def _ack(session, tenant_id, claimed: dict, ok: bool = True) -> None:
    ack_ticket(session, tenant_id, uuid.UUID(claimed["id"]), ok, AT, "Papier leer")


def _print(session, tenant_id) -> dict:
    [claimed] = _claim(session, tenant_id)
    _ack(session, tenant_id, claimed)
    return claimed["ticket"]


def _cancel(session, tenant_id, order_id) -> None:
    cancel_order(session, tenant_id, order_id, _seen(session, order_id), now=AT)


def _cancels(session, order_id) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == order_id, AuditLog.action == ACTION_CANCELLED
            )
        )
    )


def _fix_quantity(session, tenant_id, order_id) -> None:
    row = session.scalars(
        select(OrderItem).where(OrderItem.order_id == order_id)
    ).first()
    _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, row.quantity + 1),))


# --- A slip not yet printed is not sent --------------------------------------------


def test_an_order_waiting_for_passt_leaves_without_a_slip(session, tenant_id, call_id):
    _mode(session, tenant_id, "overflow")
    order_id = _order(session, tenant_id, call_id)
    seen = _seen(session, order_id)
    assert _ids(session, tenant_id) == [order_id]

    cancel_order(session, tenant_id, order_id, seen, now=AT)

    order = _fresh(session, order_id)
    assert (order.status, order.handover_state) == ("cancelled", None)
    assert _ids(session, tenant_id) == []
    assert _tickets(session, order_id) == []
    [audit] = _cancels(session, order_id)
    assert audit.actor == "gui:tablet"
    assert audit.payload == {
        "call_id": str(call_id),
        "version": seen,
        "note_sent": False,
    }


def test_a_ticket_nobody_picked_up_is_withdrawn(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    assert _fresh(session, order_id).handover_state == "pending"

    _cancel(session, tenant_id, order_id)

    assert _fresh(session, order_id).handover_state is None
    [ticket] = _tickets(session, order_id)
    assert (ticket.status, ticket.attempts) == ("sent", 0)
    assert ticket.last_error == WITHDRAWN
    assert _claim(session, tenant_id) == []
    payload = _cancels(session, order_id)[0].payload
    assert payload is not None
    assert payload["note_sent"] is False


def test_a_red_card_whose_print_failed_ends_without_a_slip(session, tenant_id, call_id):
    """The printer reported an error, so no slip exists; left due, the ticket
    would print the order the moment the printer is back."""
    order_id = _order(session, tenant_id, call_id)
    [claimed] = _claim(session, tenant_id)
    _ack(session, tenant_id, claimed, ok=False)
    assert _fresh(session, order_id).handover_state == "failed"

    _cancel(session, tenant_id, order_id)

    assert _fresh(session, order_id).handover_state is None
    assert _ids(session, tenant_id) == []
    assert _claim(session, tenant_id) == []
    assert [t.last_error for t in _tickets(session, order_id)] == [WITHDRAWN]


# --- A slip at the register gets a note --------------------------------------------


def test_a_printed_slip_gets_a_note_through_the_handover(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    assert _print(session, tenant_id)["revision"] == 0

    _cancel(session, tenant_id, order_id)

    order = _fresh(session, order_id)
    assert (order.status, order.handover_state) == ("cancelled", "pending")
    assert _ids(session, tenant_id) == []
    payload = _cancels(session, order_id)[0].payload
    assert payload is not None
    assert payload["note_sent"] is True

    note = _print(session, tenant_id)
    assert (note["revision"], note["correction_reason"]) == (1, "cancelled")
    assert note["pickup_code"] == order.pickup_code
    assert _fresh(session, order_id).handover_state == "sent"
    assert _ids(session, tenant_id) == []
    assert _claim(session, tenant_id) == []


def test_a_slip_whose_answer_never_came_counts_as_printed(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    assert len(_claim(session, tenant_id)) == 1  # printed, the answer lost

    _cancel(session, tenant_id, order_id)

    # Due again after its lease, the old ticket is passed over: only the note.
    [note] = _claim(session, tenant_id)
    assert note["ticket"]["correction_reason"] == "cancelled"


def test_a_lost_answer_still_counts_once_a_correction_passed_over_it(
    session, tenant_id, call_id
):
    """The first slip may lie at the register though its answer never came. The
    correction after it failed to print, so only that first slip says so."""
    order_id = _order(session, tenant_id, call_id)
    assert len(_claim(session, tenant_id)) == 1
    _fix_quantity(session, tenant_id, order_id)
    [newer] = _claim(session, tenant_id)
    assert newer["ticket"]["revision"] == 1
    _ack(session, tenant_id, newer, ok=False)

    _cancel(session, tenant_id, order_id)

    [note] = _claim(session, tenant_id)
    assert (note["ticket"]["revision"], note["ticket"]["correction_reason"]) == (
        2,
        "cancelled",
    )


def test_a_ticket_a_pickup_holds_right_now_counts_as_printed(
    engine, session, tenant_id, call_id
):
    """The pickup locks the ticket, and cancelling must neither wait for it (an
    answer takes ticket and order in the other order) nor take it for unsent."""
    order_id = _order(session, tenant_id, call_id)
    with Session(engine) as bridge:
        bridge.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.payload["order_id"].astext == str(order_id))
            .with_for_update()
        )
        _cancel(session, tenant_id, order_id)
        bridge.rollback()

    tickets = _tickets(session, order_id)
    assert [t.status for t in tickets] == ["pending", "pending"]
    assert tickets[1].payload["correction_reason"] == "cancelled"
    assert _fresh(session, order_id).handover_state == "pending"


def test_a_correction_nobody_printed_goes_and_the_note_stands_above_it(
    session, tenant_id, call_id
):
    order_id = _order(session, tenant_id, call_id)
    _print(session, tenant_id)
    _fix_quantity(session, tenant_id, order_id)
    assert _tickets(session, order_id)[-1].payload["revision"] == 1

    _cancel(session, tenant_id, order_id)

    [first, correction, note] = _tickets(session, order_id)
    assert (correction.status, correction.last_error) == ("sent", WITHDRAWN)
    assert note.payload["revision"] == 2
    assert first.status == "sent"
    [claimed] = _claim(session, tenant_id)
    assert claimed["id"] == str(note.id)


def test_the_late_answer_of_the_old_slip_leaves_the_note_in_charge(
    session, tenant_id, call_id
):
    order_id = _order(session, tenant_id, call_id)
    [old] = _claim(session, tenant_id)

    _cancel(session, tenant_id, order_id)
    _ack(session, tenant_id, old)

    assert _fresh(session, order_id).handover_state == "pending"


# --- The note is the team's to see through (CLAUDE.md §2 rule 5) --------------------


def _printed_then_cancelled(session, tenant_id, call_id) -> uuid.UUID:
    order_id = _order(session, tenant_id, call_id)
    _print(session, tenant_id)
    _cancel(session, tenant_id, order_id)
    return order_id


def test_a_note_that_cannot_print_is_a_red_card_until_it_does(
    session, tenant_id, call_id
):
    order_id = _printed_then_cancelled(session, tenant_id, call_id)
    [note] = _claim(session, tenant_id)
    _ack(session, tenant_id, note, ok=False)

    [card] = list_new(session, tenant_id, TZ, NOW)
    assert (card.order_id, card.status) == (order_id, "cancelled")
    assert card.handover_state == "failed"

    resend_order(session, tenant_id, order_id)
    assert _ids(session, tenant_id) == []
    # The failed ticket stays due as well; the bridge's log prints the note once.
    tickets = _claim(session, tenant_id)
    assert {t["ticket"]["correction_reason"] for t in tickets} == {"cancelled"}
    for ticket in tickets:
        _ack(session, tenant_id, ticket)
    assert _fresh(session, order_id).handover_state == "sent"
    assert _ids(session, tenant_id) == []


def test_a_note_nobody_picks_up_turns_the_card_red(session, tenant_id, call_id):
    order_id = _printed_then_cancelled(session, tenant_id, call_id)
    session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.status == "pending")
        .values(next_attempt_at=AT)
    )
    session.commit()

    assert sweep(session, AT + timedelta(seconds=PICKUP_TIMEOUT_SECONDS)) == 1

    assert _ids(session, tenant_id) == [order_id]


def test_nochmal_senden_on_a_cancelled_card_never_prints_the_order(
    session, tenant_id, call_id
):
    """Whatever the outbox still holds (the deletion job thins it): a cancelled
    order prints as cancelled."""
    order_id = _printed_then_cancelled(session, tenant_id, call_id)
    session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.status == "pending")
        .values(status="failed", payload={})
    )
    order = _fresh(session, order_id)
    order.handover_state = "failed"
    session.commit()

    resend_order(session, tenant_id, order_id)

    [note] = _claim(session, tenant_id)
    assert note["ticket"]["correction_reason"] == "cancelled"


def test_a_cancelled_order_takes_no_other_action(session, tenant_id, call_id):
    order_id = _printed_then_cancelled(session, tenant_id, call_id)
    seen = _seen(session, order_id)

    with pytest.raises(Conflict):
        approve_order(session, tenant_id, order_id)
    session.rollback()
    with pytest.raises(Conflict):
        mark_entered(session, tenant_id, order_id, seen, now=AT)
    session.rollback()
    with pytest.raises(Conflict):
        preview_correction(session, tenant_id, order_id)
    session.rollback()
    assert _fresh(session, order_id).status == "cancelled"


# --- The guards of every card action ------------------------------------------------


@pytest.mark.parametrize("printed", [True, False])
def test_a_double_tap_is_one_cancellation(session, tenant_id, call_id, printed):
    order_id = _order(session, tenant_id, call_id)
    if printed:
        _print(session, tenant_id)
    seen = _seen(session, order_id)

    cancel_order(session, tenant_id, order_id, seen, now=AT)
    before = [t.id for t in _tickets(session, order_id)]
    cancel_order(session, tenant_id, order_id, seen, now=AT)

    assert [t.id for t in _tickets(session, order_id)] == before
    assert len(_cancels(session, order_id)) == 1


def test_a_card_older_than_a_correction_is_refused(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    stale = _seen(session, order_id)
    _fix_quantity(session, tenant_id, order_id)

    with pytest.raises(Conflict) as refused:
        cancel_order(session, tenant_id, order_id, stale, now=AT)
    session.rollback()

    assert refused.value.say
    order = _fresh(session, order_id)
    assert order.status == "confirmed"
    assert _cancels(session, order_id) == []
    assert _ids(session, tenant_id) == [order_id]


def test_another_tenant_cannot_cancel(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    other = _tenant(session, "Anderer Betrieb")

    with pytest.raises(NotFound):
        cancel_order(session, other, order_id, _seen(session, order_id), now=AT)
    session.rollback()

    assert _fresh(session, order_id).status == "confirmed"
    assert _cancels(session, order_id) == []
    assert _tickets(session, order_id)[0].status == "pending"


def test_a_draft_is_no_order_to_cancel(session, tenant_id, call_id):
    order_id = _draft(session, tenant_id, call_id)

    with pytest.raises(Conflict):
        _cancel(session, tenant_id, order_id)
    session.rollback()

    assert _fresh(session, order_id).status == "draft"


@pytest.mark.parametrize("status", ["approved", "handed_over"])
def test_every_status_after_the_yes_can_be_cancelled(
    session, tenant_id, call_id, status
):
    order_id = _order(session, tenant_id, call_id)
    _print(session, tenant_id)
    order = _fresh(session, order_id)
    order.status = status
    session.commit()

    _cancel(session, tenant_id, order_id)

    assert _fresh(session, order_id).status == "cancelled"


def test_the_change_token_follows_the_cancellation(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    before = new_orders_change_token(session, tenant_id, TZ, NOW)

    _cancel(session, tenant_id, order_id)

    assert new_orders_change_token(session, tenant_id, TZ, NOW) != before
