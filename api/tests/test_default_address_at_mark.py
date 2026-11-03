"""When a delivery's address becomes the customer's default (security audit
02.11.2026, P2 no. 4, #252; Maxi 03.11.2026, option B).

The guest's yes counts the order and keeps the old default. The address moves
when the team marks the order "In Kasse eingegeben" (T-11.6), the step where a
person handles every confirmed order (L18): with a spoofed caller ID a stranger
can no longer make their address the owner's "letztes Mal" by a yes alone.
"""

import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from api.domain.customers import find_customer
from api.domain.customers.lookup import SAY_SAME_ADDRESS
from api.domain.ordering import draft_order
from api.domain.ordering.board import (
    approve_order,
    cancel_order,
    mark_entered,
    settle_default,
    version_of,
)
from api.domain.ordering.correction import (
    CorrectionRequest,
    RowEdit,
    apply_correction,
    preview_correction,
)
from api.models import Address, Customer, Order
from api.tests.test_domain_draft_delivery import (
    HOME,
    OTHER_LINE,
    PHONE,
    _stored,
    _zone,
    confirmed,
    delivery,
    known_customer,
)
from api.tests.test_domain_draft_order import NOW, _call, _tenant
from api.tests.test_register_mark import _wait_for_a_lock

ELSEWHERE = {**HOME, "street": "Bahnhofstraße", "house_number": "3"}
MARKED = NOW + timedelta(minutes=30)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    # As the app runs (api/db.py): nothing is flushed before a query by itself.
    with Session(engine, autoflush=False, expire_on_commit=False) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tid = _tenant(session, "Testbetrieb")
    session.add(_zone(tid))
    session.commit()
    return tid


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    return _call(session, tenant_id, caller_id=PHONE)


def _said_yes(session, tenant_id, call_id, address, name="Müller", now=NOW):
    """A delivery the guest confirmed: the order."""
    draft = draft_order(
        session,
        delivery(session, tenant_id, call_id, name=name, address=address),
        now=now,
    )
    confirmed(session, tenant_id, call_id, draft.order_id, now)
    return session.get(Order, draft.order_id)


def _typed_in(session, tenant_id, order, now=MARKED) -> None:
    """ "Passt", then "In Kasse eingegeben" on the state the tablet shows."""
    approve_order(session, tenant_id, order.id)
    _mark(session, tenant_id, order, now)


def _mark(session, tenant_id, order, now=MARKED) -> None:
    mark_entered(session, tenant_id, order.id, version_of(session, order), now=now)


def _cancel(session, tenant_id, order) -> None:
    cancel_order(session, tenant_id, order.id, version_of(session, order), now=MARKED)


def _correct(session, tenant_id, order) -> None:
    """Three instead of two of the first dish: the mark goes (T-11.6)."""
    plan = preview_correction(session, tenant_id, order.id)
    row = plan.rows[0].order_item_id
    assert row is not None  # an existing position
    req = CorrectionRequest(
        version=plan.version, rows=(RowEdit(order_item_id=row, quantity=3),)
    )
    apply_correction(session, tenant_id, order.id, req, "wrong_quantity", now=MARKED)


def _defaults(session) -> list[uuid.UUID]:
    session.expire_all()
    return list(session.scalars(select(Address.id).where(Address.is_default)))


def test_the_yes_keeps_the_old_default(session, tenant_id, call_id):
    """The audit case: the owner's number on the line, a stranger's address."""
    known, home = known_customer(session, tenant_id)

    order = _said_yes(session, tenant_id, call_id, ELSEWHERE)

    session.refresh(known)
    assert order.customer_id == known.id
    assert order.address_id != home.id
    # Counters and contact stay at the yes (#489, #508).
    assert known.order_count == 2
    assert known.last_order_at == NOW
    assert _defaults(session) == [home.id]
    match = find_customer(session, tenant_id, PHONE)
    assert (match.address_id, match.say) == (home.id, SAY_SAME_ADDRESS)


def test_the_mark_makes_the_address_the_default(session, tenant_id, call_id):
    known_customer(session, tenant_id)
    order = _said_yes(session, tenant_id, call_id, ELSEWHERE)

    _typed_in(session, tenant_id, order)

    assert _defaults(session) == [order.address_id]
    match = find_customer(session, tenant_id, PHONE)
    assert (match.address_id, match.say) == (order.address_id, SAY_SAME_ADDRESS)


def test_a_new_customer_has_no_default_before_the_mark(session, tenant_id, call_id):
    order = _said_yes(session, tenant_id, call_id, HOME, name="Braun")

    match = find_customer(session, tenant_id, PHONE, "Braun")
    assert (match.found, match.address_id, match.say) == (True, None, None)

    _typed_in(session, tenant_id, order)

    assert find_customer(session, tenant_id, PHONE, "Braun").address_id == (
        order.address_id
    )


def test_an_order_cancelled_before_the_mark_never_moves_it(session, tenant_id, call_id):
    _, home = known_customer(session, tenant_id)
    order = _said_yes(session, tenant_id, call_id, ELSEWHERE)

    _cancel(session, tenant_id, order)

    assert _defaults(session) == [home.id]


def test_a_cancel_after_the_mark_gives_the_earlier_address_back(
    session, tenant_id, call_id
):
    """The owner's own delivery, then a stranger's on the owner's number that
    the team typed in and then cancelled ("Storniert" keeps the mark)."""
    own = _said_yes(session, tenant_id, call_id, HOME)
    _typed_in(session, tenant_id, own)
    later = _call(session, tenant_id, caller_id=PHONE)
    stranger = _said_yes(
        session, tenant_id, later, ELSEWHERE, now=NOW + timedelta(hours=1)
    )
    _typed_in(session, tenant_id, stranger, MARKED + timedelta(hours=1))
    assert _defaults(session) == [stranger.address_id]

    _cancel(session, tenant_id, stranger)

    session.refresh(stranger)
    assert stranger.register_entered_at is not None
    assert _defaults(session) == [own.address_id]


def test_a_cancel_after_a_correction_still_gives_the_default_back(
    session, tenant_id, call_id
):
    """The correction cleared the mark, not the default the mark gave."""
    own = _said_yes(session, tenant_id, call_id, HOME)
    _typed_in(session, tenant_id, own)
    later = _call(session, tenant_id, caller_id=PHONE)
    order = _said_yes(
        session, tenant_id, later, ELSEWHERE, now=NOW + timedelta(hours=1)
    )
    _typed_in(session, tenant_id, order, MARKED + timedelta(hours=1))
    _correct(session, tenant_id, order)

    _cancel(session, tenant_id, order)

    assert _defaults(session) == [own.address_id]


def test_a_cancel_after_the_only_mark_leaves_no_default(session, tenant_id, call_id):
    """Fails closed: nothing the team typed in is left, so the guest is asked."""
    order = _said_yes(session, tenant_id, call_id, HOME, name="Braun")
    _typed_in(session, tenant_id, order)

    _cancel(session, tenant_id, order)

    assert _defaults(session) == []
    assert find_customer(session, tenant_id, PHONE, "Braun").say is None


def test_a_second_mark_changes_nothing(session, tenant_id, call_id):
    known_customer(session, tenant_id)
    order = _said_yes(session, tenant_id, call_id, ELSEWHERE)
    _typed_in(session, tenant_id, order)

    # The second tablet, on the same card.
    _mark(session, tenant_id, order, MARKED + timedelta(minutes=1))

    assert _defaults(session) == [order.address_id]


def test_a_correction_after_the_mark_keeps_the_default(session, tenant_id, call_id):
    """A correction changes dishes, never the address: the default the mark
    gave stays, and the new mark the new revision needs leaves one default."""
    known_customer(session, tenant_id)
    order = _said_yes(session, tenant_id, call_id, ELSEWHERE)
    _typed_in(session, tenant_id, order)

    _correct(session, tenant_id, order)

    session.refresh(order)
    assert order.register_entered_at is None
    assert _defaults(session) == [order.address_id]

    _mark(session, tenant_id, order, MARKED + timedelta(minutes=5))

    assert _defaults(session) == [order.address_id]


def test_a_mark_after_the_customer_was_erased(session, tenant_id, call_id):
    """The row went (erasure on request or the deletion job): the order keeps
    no customer and no address, the mark still holds and moves nothing."""
    order = _said_yes(session, tenant_id, call_id, HOME, name="Braun")
    approve_order(session, tenant_id, order.id)
    session.execute(delete(Customer).where(Customer.id == order.customer_id))
    session.commit()

    _mark(session, tenant_id, order)

    session.refresh(order)
    assert (order.customer_id, order.address_id) == (None, None)
    assert order.register_entered_at == MARKED
    assert session.scalar(select(func.count()).select_from(Address)) == 0


def test_a_foreign_line_order_never_makes_a_default(session, tenant_id):
    """A guest on another line names the owner's number (#489): the row it
    writes never gets a default, the owner's stays."""
    known, home = known_customer(session, tenant_id)
    call = _call(session, tenant_id, caller_id=OTHER_LINE)
    order = _said_yes(session, tenant_id, call, ELSEWHERE)

    _typed_in(session, tenant_id, order)

    assert order.customer_id != known.id
    assert _defaults(session) == [home.id]
    assert find_customer(session, tenant_id, PHONE).address_id == home.id


def test_a_late_mark_keeps_the_newer_address(session, tenant_id, call_id):
    """The guest moved: the order to the new address came later. A card of the
    old one marked after it does not bring the old address back."""
    old = _said_yes(session, tenant_id, call_id, HOME)
    later = _call(session, tenant_id, caller_id=PHONE)
    new = _said_yes(session, tenant_id, later, ELSEWHERE, now=NOW + timedelta(hours=1))
    assert new.customer_id == old.customer_id
    _typed_in(session, tenant_id, new)

    _typed_in(session, tenant_id, old, MARKED + timedelta(hours=2))

    assert _defaults(session) == [new.address_id]


def test_a_correction_does_not_let_an_older_mark_take_the_default(
    session, tenant_id, call_id
):
    """Codex #520: the newer delivery was marked, then corrected, which clears
    its mark until the team marks the new revision. A card of an older delivery
    marked meanwhile does not bring the older address back."""
    old = _said_yes(session, tenant_id, call_id, HOME)
    later = _call(session, tenant_id, caller_id=PHONE)
    new = _said_yes(session, tenant_id, later, ELSEWHERE, now=NOW + timedelta(hours=1))
    _typed_in(session, tenant_id, new)
    _correct(session, tenant_id, new)

    _typed_in(session, tenant_id, old, MARKED + timedelta(hours=2))

    assert _defaults(session) == [new.address_id]


def test_a_newer_draft_holds_no_default(session, tenant_id, call_id):
    """A draft nobody said yes to, to the stored address, also one from a
    spoofed caller ID: the owner's marked delivery still moves the default."""
    _, home = known_customer(session, tenant_id)
    order = _said_yes(session, tenant_id, call_id, ELSEWHERE)
    later = _call(session, tenant_id, caller_id=PHONE)
    draft_order(
        session,
        _stored(session, tenant_id, later, home.id),
        now=NOW + timedelta(hours=1),
    )

    _typed_in(session, tenant_id, order)

    assert _defaults(session) == [order.address_id]


def test_a_newer_delivery_never_marked_holds_no_default(session, tenant_id, call_id):
    """Codex #520, round 2: the older delivery was marked at the home address,
    a newer one to it was only confirmed. Marking the delivery in between
    makes its address the default: the newest one the team marked."""
    first = _said_yes(session, tenant_id, call_id, HOME)
    _typed_in(session, tenant_id, first)
    call = _call(session, tenant_id, caller_id=PHONE)
    middle = _said_yes(
        session, tenant_id, call, ELSEWHERE, now=NOW + timedelta(hours=1)
    )
    call = _call(session, tenant_id, caller_id=PHONE)
    last = _said_yes(session, tenant_id, call, HOME, now=NOW + timedelta(hours=2))
    assert last.address_id == first.address_id

    _typed_in(session, tenant_id, middle, MARKED + timedelta(hours=2))

    assert _defaults(session) == [middle.address_id]


def test_a_cancel_before_any_mark_keeps_a_default_from_before(
    session, tenant_id, call_id
):
    """Codex #520, round 2: a default from before this change (set at a yes)
    and a delivery to it the team cancels before its mark: that delivery
    never moved the default, so its cancel does not remove it."""
    _, home = known_customer(session, tenant_id)
    draft = draft_order(session, _stored(session, tenant_id, call_id, home.id), now=NOW)
    confirmed(session, tenant_id, call_id, draft.order_id)
    order = session.get(Order, draft.order_id)

    _cancel(session, tenant_id, order)

    assert _defaults(session) == [home.id]


def test_a_cancel_falls_back_to_a_delivery_a_correction_unmarked(
    session, tenant_id, call_id
):
    """The fallback counts a delivery the team marked even while a correction
    holds its mark back, so the guest's newer address comes back."""
    oldest = _said_yes(session, tenant_id, call_id, HOME)
    _typed_in(session, tenant_id, oldest)
    call = _call(session, tenant_id, caller_id=PHONE)
    corrected = _said_yes(
        session, tenant_id, call, ELSEWHERE, now=NOW + timedelta(hours=1)
    )
    _typed_in(session, tenant_id, corrected, MARKED + timedelta(hours=1))
    _correct(session, tenant_id, corrected)
    call = _call(session, tenant_id, caller_id=PHONE)
    newest = _said_yes(
        session,
        tenant_id,
        call,
        {**HOME, "house_number": "99"},
        now=NOW + timedelta(hours=2),
    )
    _typed_in(session, tenant_id, newest, MARKED + timedelta(hours=2))

    _cancel(session, tenant_id, newest)

    assert _defaults(session) == [corrected.address_id]


def test_two_marks_of_one_customer_at_once(
    session, tenant_id, call_id, migrated_db_url
):
    """Two tablets mark two deliveries of one customer at the same moment. The
    second waits for the first, so it sees the first mark, does not trip over
    the one default the index allows, and the newer address wins."""
    old = _said_yes(session, tenant_id, call_id, HOME)
    later = _call(session, tenant_id, caller_id=PHONE)
    new = _said_yes(session, tenant_id, later, ELSEWHERE, now=NOW + timedelta(hours=1))
    approve_order(session, tenant_id, old.id)
    approve_order(session, tenant_id, new.id)
    seen = version_of(session, new)
    engine = create_engine(migrated_db_url)
    outcome: list[BaseException | None] = []

    def tap() -> None:
        with Session(engine, autoflush=False, expire_on_commit=False) as tablet:
            try:
                mark_entered(tablet, tenant_id, new.id, seen, now=MARKED)
                outcome.append(None)
            except BaseException as exc:  # noqa: BLE001 - handed to the test
                outcome.append(exc)

    try:
        with Session(engine, autoflush=False, expire_on_commit=False) as first:
            # The first tablet's mark of the older delivery, up to its commit.
            mine = first.get_one(Order, old.id)
            mine.register_entered_at = MARKED
            settle_default(first, mine)
            thread = threading.Thread(target=tap)
            thread.start()
            _wait_for_a_lock(session)
            first.commit()
        thread.join(timeout=10)
    finally:
        engine.dispose()

    assert outcome == [None]
    assert _defaults(session) == [new.address_id]


def test_two_cancels_of_one_customer_at_once(
    session, tenant_id, call_id, migrated_db_url
):
    """Codex #520: two tablets cancel both marked deliveries of one customer at
    the same moment. The second waits for the first on the customer row, so
    the address the first fell back to, the second's own, does not stay."""
    old = _said_yes(session, tenant_id, call_id, HOME)
    later = _call(session, tenant_id, caller_id=PHONE)
    new = _said_yes(session, tenant_id, later, ELSEWHERE, now=NOW + timedelta(hours=1))
    _typed_in(session, tenant_id, old)
    _typed_in(session, tenant_id, new, MARKED + timedelta(hours=1))
    seen = version_of(session, old)
    engine = create_engine(migrated_db_url)
    outcome: list[BaseException | None] = []

    def tap() -> None:
        with Session(engine, autoflush=False, expire_on_commit=False) as tablet:
            try:
                cancel_order(tablet, tenant_id, old.id, seen, now=MARKED)
                outcome.append(None)
            except BaseException as exc:  # noqa: BLE001 - handed to the test
                outcome.append(exc)

    try:
        with Session(engine, autoflush=False, expire_on_commit=False) as first:
            # The first tablet's cancel of the newer delivery, up to its commit:
            # it falls back to the older one, not cancelled yet.
            mine = first.get_one(Order, new.id)
            mine.status = "cancelled"
            settle_default(first, mine)
            thread = threading.Thread(target=tap)
            thread.start()
            _wait_for_a_lock(session)
            first.commit()
        thread.join(timeout=10)
    finally:
        engine.dispose()

    assert outcome == [None]
    assert _defaults(session) == []
