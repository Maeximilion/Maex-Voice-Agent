"""The mark "In Kasse eingegeben" and the cards it ends (T-11.6, docs/09 §1 L18)."""

import threading
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.orm import Session

from api.core.errors import Conflict, NotFound
from api.domain.ordering import board
from api.domain.ordering.board import (
    ACTION_REGISTER_ENTERED,
    CORRECTED_SINCE,
    approve_order,
    list_new,
    mark_entered,
    new_orders_change_token,
    unmarked_before,
)
from api.domain.ordering.correction import RowEdit
from api.domain.ordering.labels import ACTION_CORRECTED
from api.models import AuditLog, Order, OrderItem
from api.tests.test_domain_confirm_order import _mode
from api.tests.test_domain_draft_order import NOW, _call, _tenant
from api.tests.test_domain_ordering_board import TZ, _ids, _order, _seen
from api.tests.test_domain_ordering_correction import _fix, _rows
from api.tests.test_gui_orders import _fresh

DAY = timedelta(days=1)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tid = _tenant(session, "Testbetrieb")
    _mode(session, tid, "primary")
    return tid


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    return _call(session, tenant_id)


def _marks(session, order_id) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == order_id,
                AuditLog.action == ACTION_REGISTER_ENTERED,
            )
        )
    )


def test_card_stays_after_passt_until_marked(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    approve_order(session, tenant_id, order_id)

    [card] = list_new(session, tenant_id, TZ, NOW)
    assert card.order_id == order_id and card.status == "approved"
    assert card.register_entered_at is None and not card.register_late

    mark_entered(session, tenant_id, order_id, _seen(session, order_id), now=NOW)
    assert _ids(session, tenant_id) == []
    assert _fresh(session, order_id).register_entered_at == NOW
    [audit] = _marks(session, order_id)
    assert audit.actor == "gui:tablet"
    assert audit.payload == {
        "call_id": str(call_id),
        "version": _seen(session, order_id),
    }


def test_mark_twice_or_on_two_tablets_is_one_mark(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    approve_order(session, tenant_id, order_id)
    mark_entered(session, tenant_id, order_id, _seen(session, order_id), now=NOW)
    mark_entered(session, tenant_id, order_id, _seen(session, order_id), now=NOW + DAY)

    assert _fresh(session, order_id).register_entered_at == NOW
    assert len(_marks(session, order_id)) == 1


@pytest.mark.parametrize("status", ["confirmed", "cancelled", "draft"])
def test_mark_before_passt_or_on_a_cancelled_order_is_refused(
    session, tenant_id, call_id, status
):
    order_id = _order(session, tenant_id, call_id)
    session.execute(update(Order).where(Order.id == order_id).values(status=status))
    session.commit()

    with pytest.raises(Conflict):
        mark_entered(session, tenant_id, order_id, _seen(session, order_id), now=NOW)
    session.rollback()
    assert _fresh(session, order_id).register_entered_at is None
    assert _marks(session, order_id) == []


def test_mark_of_another_tenant_is_not_found(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    approve_order(session, tenant_id, order_id)
    other = _tenant(session, "Anderer Betrieb")

    with pytest.raises(NotFound):
        mark_entered(session, other, order_id, _seen(session, order_id), now=NOW)
    session.rollback()
    assert _fresh(session, order_id).register_entered_at is None


def test_correction_clears_the_mark_and_brings_the_card_back(
    session, tenant_id, call_id
):
    order_id = _order(session, tenant_id, call_id)
    approve_order(session, tenant_id, order_id)
    mark_entered(session, tenant_id, order_id, _seen(session, order_id), now=NOW)
    [row] = _rows(session, order_id)

    _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, 3),))

    assert _fresh(session, order_id).register_entered_at is None
    [card] = list_new(session, tenant_id, TZ, NOW)
    assert card.order_id == order_id
    assert card.corrected == "wrong_quantity"


def test_tablet_with_the_card_from_before_a_correction_cannot_mark(
    session, tenant_id, call_id
):
    order_id = _order(session, tenant_id, call_id)
    approve_order(session, tenant_id, order_id)
    [card] = list_new(session, tenant_id, TZ, NOW)
    [row] = _rows(session, order_id)
    _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, 3),))

    with pytest.raises(Conflict) as stale:
        mark_entered(session, tenant_id, order_id, card.version, now=NOW)
    assert stale.value.say == CORRECTED_SINCE
    session.rollback()
    assert _fresh(session, order_id).register_entered_at is None

    [fresh] = list_new(session, tenant_id, TZ, NOW)
    assert fresh.version != card.version
    mark_entered(session, tenant_id, order_id, fresh.version, now=NOW)
    assert _fresh(session, order_id).register_entered_at == NOW


def test_a_stale_card_stays_refused_after_the_audit_log_expires(
    session, tenant_id, call_id
):
    """The deletion job removes audit entries after RETENTION_AUDIT_LOG_DAYS
    (one day at the least): a count of corrections would fall back to the
    stale card's, the lines themselves do not (Codex #423)."""
    order_id = _order(session, tenant_id, call_id)
    approve_order(session, tenant_id, order_id)
    [card] = list_new(session, tenant_id, TZ, NOW)
    [row] = _rows(session, order_id)
    _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, 3),))
    session.execute(delete(AuditLog).where(AuditLog.action == ACTION_CORRECTED))
    session.commit()

    with pytest.raises(Conflict):
        mark_entered(session, tenant_id, order_id, card.version, now=NOW)
    session.rollback()
    assert _fresh(session, order_id).register_entered_at is None


def test_unmarked_order_of_an_earlier_day_stays_red_and_first(
    session, tenant_id, call_id
):
    today = _order(session, tenant_id, call_id)
    old = _order(session, tenant_id, call_id, now=NOW - 7 * DAY)
    approve_order(session, tenant_id, old)
    # Confirmed with a slip but nobody tapped "Passt": not in the register either.
    unapproved = _order(session, tenant_id, call_id, now=NOW - 2 * DAY)
    marked = _order(session, tenant_id, call_id, now=NOW - 3 * DAY)
    approve_order(session, tenant_id, marked)
    mark_entered(session, tenant_id, marked, _seen(session, marked), now=NOW - 3 * DAY)

    cards = list_new(session, tenant_id, TZ, NOW)
    assert [c.order_id for c in cards] == [old, unapproved, today]
    assert [c.register_late for c in cards] == [True, True, False]


def test_failed_slip_stays_after_the_mark(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    approve_order(session, tenant_id, order_id)
    mark_entered(session, tenant_id, order_id, _seen(session, order_id), now=NOW)
    session.execute(
        update(Order).where(Order.id == order_id).values(handover_state="failed")
    )
    session.commit()

    [card] = list_new(session, tenant_id, TZ, NOW)
    assert card.order_id == order_id and not card.register_late


def test_unmarked_before_counts_only_earlier_unmarked_orders(
    session, tenant_id, call_id
):
    _order(session, tenant_id, call_id)  # today: the team still has time
    oldest = _order(session, tenant_id, call_id, now=NOW - 5 * DAY)
    second = _order(session, tenant_id, call_id, now=NOW - 2 * DAY)
    approve_order(session, tenant_id, second)
    marked = _order(session, tenant_id, call_id, now=NOW - 2 * DAY)
    approve_order(session, tenant_id, marked)
    mark_entered(session, tenant_id, marked, _seen(session, marked), now=NOW)
    cancelled = _order(session, tenant_id, call_id, now=NOW - 3 * DAY)
    deleted = _order(session, tenant_id, call_id, now=NOW - 3 * DAY)
    session.execute(
        update(Order).where(Order.id == cancelled).values(status="cancelled")
    )
    session.execute(update(Order).where(Order.id == deleted).values(deleted_at=NOW))
    session.commit()
    other = _tenant(session, "Anderer Betrieb")
    _mode(session, other, "primary")
    _order(session, other, _call(session, other), now=NOW - 5 * DAY)

    found = unmarked_before(session, tenant_id, NOW - timedelta(hours=1))
    assert [o.id for o in found] == [oldest, second]


def test_change_token_follows_the_mark_and_the_day(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    approve_order(session, tenant_id, order_id)
    before = new_orders_change_token(session, tenant_id, TZ, NOW)

    # The next business day turns the card red without any row changing.
    assert new_orders_change_token(session, tenant_id, TZ, NOW + DAY) != before

    mark_entered(session, tenant_id, order_id, _seen(session, order_id), now=NOW)
    assert new_orders_change_token(session, tenant_id, TZ, NOW) != before


def test_a_mark_waiting_on_a_correction_sees_it_and_is_refused(
    session, tenant_id, call_id, migrated_db_url
):
    """The check holds only because the mark reads the lines after it has the
    order's row lock: a correction that commits while the mark waits must be
    seen (READ COMMITTED), or a stale card marks the new state."""
    order_id = _order(session, tenant_id, call_id)
    approve_order(session, tenant_id, order_id)
    seen = _seen(session, order_id)
    [row] = _rows(session, order_id)
    engine = create_engine(migrated_db_url)
    outcome: list[BaseException | None] = []

    def tap() -> None:
        with Session(engine) as tablet:
            try:
                mark_entered(tablet, tenant_id, order_id, seen, now=NOW)
                outcome.append(None)
            except BaseException as exc:  # noqa: BLE001 - handed to the test
                outcome.append(exc)

    try:
        with Session(engine) as correction:
            correction.execute(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            thread = threading.Thread(target=tap)
            thread.start()
            _wait_for_a_lock(session)
            # What apply_correction changes under the same lock: the lines.
            correction.execute(
                update(OrderItem).where(OrderItem.id == row.id).values(quantity=3)
            )
            correction.commit()
        thread.join(timeout=10)
    finally:
        engine.dispose()

    [exc] = outcome
    assert isinstance(exc, Conflict) and exc.say == CORRECTED_SINCE
    assert _fresh(session, order_id).register_entered_at is None


def _wait_for_a_lock(session) -> None:
    """Until another connection waits on a row lock, at most five seconds."""
    for _ in range(100):
        waiting = session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
            )
        )
        session.rollback()
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("the mark never waited on the order's lock")


def test_a_card_torn_by_a_correction_names_the_lines_it_shows(
    session, tenant_id, call_id, migrated_db_url, monkeypatch
):
    """A correction that commits while the column is read, after the order's
    lines: the card shows the old lines, so its mark must be refused
    (ultrareview #423). The fingerprint comes from the very rows on the card."""
    order_id = _order(session, tenant_id, call_id)
    approve_order(session, tenant_id, order_id)
    [row] = _rows(session, order_id)
    real = board.current_labels_many
    engine = create_engine(migrated_db_url)

    def correction_commits_now(*args, **kwargs):
        with Session(engine) as tablet:
            tablet.execute(
                update(OrderItem).where(OrderItem.id == row.id).values(quantity=3)
            )
            tablet.commit()
        return real(*args, **kwargs)

    monkeypatch.setattr(board, "current_labels_many", correction_commits_now)
    try:
        [card] = list_new(session, tenant_id, TZ, NOW)
    finally:
        engine.dispose()
    session.rollback()

    assert [line.quantity for line in card.lines] == [2]
    with pytest.raises(Conflict):
        mark_entered(session, tenant_id, order_id, card.version, now=NOW)
