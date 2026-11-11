"""Staff changes and their check cards (#570 part b, D34 point 4): a team number
for today, a date's hours that hold at once when they shorten or are for today
and otherwise wait for the owner, the owner's keep and undo, and the readers of
the team number."""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import InvalidInput
from api.domain.callbacks import transfer_to_team
from api.domain.calls.routing import call_routing
from api.domain.status import staff_changes as staff
from api.domain.status.hours import load_hours, windows_for_day
from api.domain.status.staff_changes import DayHours
from api.events.types import DAY_HOURS_CHANGED, TEAM_PHONE_CHANGED
from api.models import (
    AuditLog,
    Call,
    OpeningHours,
    OutboxEvent,
    ServiceConfig,
    SpecialDay,
    StaffChange,
)
from api.schemas.transfer import TransferToTeamRequest
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Seed: Monday closed, every other day 11:30-14:00 and 17:00-22:00.
TUESDAY = date(2026, 11, 10)
SATURDAY = date(2026, 11, 14)
MONDAY = date(2026, 11, 16)
NOW = datetime.combine(TUESDAY, time(10, 0), tzinfo=BERLIN)
NUMBER = "+497215551234"


@pytest.fixture(autouse=True)
def dialable(monkeypatch):
    """The dialplan's other numbers (TRANSFER_TARGETS): the only ones staff may set."""
    monkeypatch.setattr(settings, "transfer_targets", f" {NUMBER}, +4972215550000,")


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def _config(session, tenant_id) -> ServiceConfig:
    return session.get(ServiceConfig, tenant_id, populate_existing=True)


def _cards(session, tenant_id) -> list[StaffChange]:
    return list(
        session.scalars(
            select(StaffChange)
            .where(StaffChange.tenant_id == tenant_id)
            .order_by(StaffChange.created_at)
            .execution_options(populate_existing=True)
        )
    )


def _windows(session, tenant_id, day, service="pickup") -> list[tuple[str, str]]:
    data = load_hours(session, tenant_id, day, days=0)
    return [
        (a.strftime("%H:%M"), b.strftime("%H:%M"))
        for a, b in windows_for_day(data, day, service, BERLIN)
    ]


# --- the team number ---------------------------------------------------------


def test_a_team_number_for_today_holds_to_the_end_of_the_business_day(
    session, tenant_id
):
    standard = _config(session, tenant_id).team_phone
    card = staff.change_team_phone(session, tenant_id, "0721 555-1234", NOW)
    assert card is not None and card.held and card.day == TUESDAY
    assert (card.before, card.after) == ({"number": standard}, {"number": NUMBER})
    config = _config(session, tenant_id)
    assert config.team_phone == standard
    assert staff.team_phone_at(config, NOW) == NUMBER
    assert call_routing(session, tenant_id, NOW).team_phone == NUMBER
    # The business day ends at 05:00: from then on the standard again, and no card.
    next_morning = datetime.combine(TUESDAY + timedelta(days=1), time(5), BERLIN)
    assert staff.team_phone_at(config, next_morning - timedelta(seconds=1)) == NUMBER
    assert staff.team_phone_at(config, next_morning) == standard
    assert call_routing(session, tenant_id, next_morning).team_phone == standard
    assert staff.open_cards(session, tenant_id, TUESDAY + timedelta(days=1)) == []


def test_a_second_change_keeps_the_first_before_and_back_to_standard_ends_the_card(
    session, tenant_id
):
    standard = _config(session, tenant_id).team_phone
    staff.change_team_phone(session, tenant_id, NUMBER, NOW)
    staff.change_team_phone(session, tenant_id, "+4972215550000", NOW)
    (card,) = _cards(session, tenant_id)
    assert card.before == {"number": standard}
    assert card.after == {"number": "+4972215550000"}
    staff.change_team_phone(session, tenant_id, standard, NOW)
    (card,) = _cards(session, tenant_id)
    assert card.status == "undone"
    config = _config(session, tenant_id)
    assert (config.team_phone_today, config.team_phone_today_until) == (None, None)


def test_a_number_the_dialplan_does_not_dial_is_refused(session, tenant_id):
    """The dialplan dials TEAM_PHONE and TRANSFER_TARGETS only (T-10.13): any
    other number would ring the standard instead, unnoticed."""
    with pytest.raises(InvalidInput) as refused:
        staff.change_team_phone(session, tenant_id, "+4930123456", NOW)
    assert "Betreiber" in (refused.value.say or "")
    assert _cards(session, tenant_id) == []
    assert staff.dialable_numbers() == [settings.team_phone, NUMBER, "+4972215550000"]


@pytest.mark.parametrize("raw", ["", "abc", "12"])
def test_a_number_that_is_none_changes_nothing(session, tenant_id, raw):
    with pytest.raises(InvalidInput):
        staff.change_team_phone(session, tenant_id, raw, NOW)
    assert _cards(session, tenant_id) == []
    assert _config(session, tenant_id).team_phone_today is None


def test_the_same_number_again_is_no_change(session, tenant_id):
    standard = _config(session, tenant_id).team_phone
    assert staff.change_team_phone(session, tenant_id, standard, NOW) is None
    assert _cards(session, tenant_id) == []


def test_the_owner_keeps_the_number_as_the_standard(session, tenant_id):
    card = staff.change_team_phone(session, tenant_id, NUMBER, NOW)
    assert card is not None
    staff.decide(session, tenant_id, card.id, keep=True, now=NOW)
    config = _config(session, tenant_id)
    assert (config.team_phone, config.team_phone_today) == (NUMBER, None)
    assert _cards(session, tenant_id)[0].status == "kept"
    assert staff.open_cards(session, tenant_id, TUESDAY) == []


def test_the_owner_undoes_the_number(session, tenant_id):
    standard = _config(session, tenant_id).team_phone
    card = staff.change_team_phone(session, tenant_id, NUMBER, NOW)
    assert card is not None
    staff.decide(session, tenant_id, card.id, keep=False, now=NOW)
    config = _config(session, tenant_id)
    assert staff.team_phone_at(config, NOW) == standard
    assert _cards(session, tenant_id)[0].status == "undone"
    actions = session.scalars(
        select(AuditLog.action).where(AuditLog.entity == "staff_change")
    ).all()
    assert sorted(actions) == ["staff_change.team_phone", "staff_change.undone"]


def test_a_decided_card_is_no_error_on_a_second_tablet(session, tenant_id):
    card = staff.change_team_phone(session, tenant_id, NUMBER, NOW)
    assert card is not None
    staff.decide(session, tenant_id, card.id, keep=False, now=NOW)
    staff.decide(session, tenant_id, card.id, keep=True, now=NOW)
    assert _cards(session, tenant_id)[0].status == "undone"
    assert _config(session, tenant_id).team_phone != NUMBER


# --- a date's hours ------------------------------------------------------------


def test_today_closed_holds_at_once(session, tenant_id):
    card = staff.change_day_hours(
        session, tenant_id, TUESDAY, DayHours(closed=True), NOW
    )
    assert card is not None
    assert card.held and card.after["closed"] is True
    assert _windows(session, tenant_id, TUESDAY) == []


def test_today_longer_hours_hold_at_once(session, tenant_id):
    longer = DayHours(opens=time(10), closes=time(23))
    card = staff.change_day_hours(session, tenant_id, TUESDAY, longer, NOW)
    assert card is not None
    assert card.held
    assert _windows(session, tenant_id, TUESDAY) == [("10:00", "23:00")]


def test_shorter_hours_for_a_later_date_hold_at_once(session, tenant_id):
    shorter = DayHours(opens=time(12), closes=time(13, 30))
    card = staff.change_day_hours(session, tenant_id, SATURDAY, shorter, NOW)
    assert card is not None
    assert card.held and card.before == DayHours().as_dict()
    assert _windows(session, tenant_id, SATURDAY) == [("12:00", "13:30")]


def test_longer_or_extra_hours_for_a_later_date_wait_for_the_owner(session, tenant_id):
    before = _windows(session, tenant_id, SATURDAY)
    longer = DayHours(opens=time(11), closes=time(23))
    card = staff.change_day_hours(session, tenant_id, SATURDAY, longer, NOW)
    assert card is not None
    assert not card.held
    assert _windows(session, tenant_id, SATURDAY) == before
    # Monday is closed in the seed: any hours are extra.
    extra = staff.change_day_hours(
        session, tenant_id, MONDAY, DayHours(opens=time(17), closes=time(22)), NOW
    )
    assert extra is not None
    assert not extra.held and _windows(session, tenant_id, MONDAY) == []
    staff.decide(session, tenant_id, card.id, keep=True, now=NOW)
    assert _windows(session, tenant_id, SATURDAY) == [("11:00", "23:00")]
    staff.decide(session, tenant_id, extra.id, keep=False, now=NOW)
    assert _windows(session, tenant_id, MONDAY) == []


def test_the_owner_undoes_a_held_change_back_to_what_held_before(session, tenant_id):
    before = _windows(session, tenant_id, SATURDAY)
    card = staff.change_day_hours(
        session, tenant_id, SATURDAY, DayHours(closed=True), NOW
    )
    assert card is not None
    staff.decide(session, tenant_id, card.id, keep=False, now=NOW)
    assert _windows(session, tenant_id, SATURDAY) == before
    assert session.scalars(select(SpecialDay)).all() == []


def test_staff_taking_back_their_own_change_needs_no_owner(session, tenant_id):
    before = _windows(session, tenant_id, SATURDAY)
    staff.change_day_hours(session, tenant_id, SATURDAY, DayHours(closed=True), NOW)
    # Normal again is longer than closed, but it is what held before the staff came.
    card = staff.change_day_hours(session, tenant_id, SATURDAY, DayHours(), NOW)
    assert card is not None
    assert card.held and card.status == "undone"
    assert _windows(session, tenant_id, SATURDAY) == before
    assert staff.open_cards(session, tenant_id, TUESDAY) == []


def test_a_holiday_row_keeps_its_name_and_comes_back_on_undo(session, tenant_id):
    session.add(
        SpecialDay(
            tenant_id=tenant_id,
            date=SATURDAY,
            closed=True,
            note="Feiertag",
            status="set",
        )
    )
    session.commit()
    card = staff.change_day_hours(
        session, tenant_id, SATURDAY, DayHours(opens=time(12), closes=time(14)), NOW
    )
    assert card is not None
    assert not card.held  # closed before: any hours are extra
    staff.decide(session, tenant_id, card.id, keep=True, now=NOW)
    row = session.scalars(select(SpecialDay)).one()
    assert (row.closed, row.note) == (False, "Feiertag")
    card = staff.change_day_hours(
        session, tenant_id, SATURDAY, DayHours(opens=time(12), closes=time(13)), NOW
    )
    assert card is not None
    staff.decide(session, tenant_id, card.id, keep=False, now=NOW)
    row = session.scalars(
        select(SpecialDay).execution_options(populate_existing=True)
    ).one()
    assert (row.opens_at, row.closes_at, row.note) == (time(12), time(14), "Feiertag")


@pytest.mark.parametrize(
    "hours",
    [DayHours(opens=time(12), closes=time(12)), DayHours(opens=time(12))],
)
def test_hours_without_a_span_are_refused(session, tenant_id, hours):
    with pytest.raises(InvalidInput):
        staff.change_day_hours(session, tenant_id, SATURDAY, hours, NOW)
    assert _cards(session, tenant_id) == []


def test_a_past_date_is_refused(session, tenant_id):
    with pytest.raises(InvalidInput):
        staff.change_day_hours(
            session, tenant_id, TUESDAY - timedelta(days=1), DayHours(closed=True), NOW
        )


def test_open_cards_by_date_from_today_on(session, tenant_id):
    staff.change_day_hours(session, tenant_id, MONDAY, DayHours(closed=True), NOW)
    staff.change_team_phone(session, tenant_id, NUMBER, NOW)
    staff.change_day_hours(session, tenant_id, SATURDAY, DayHours(closed=True), NOW)
    assert [c.day for c in staff.open_cards(session, tenant_id, TUESDAY)] == [
        TUESDAY,
        SATURDAY,
        MONDAY,
    ]
    token = staff.cards_change_token(session, tenant_id, TUESDAY)
    later = staff.open_cards(session, tenant_id, SATURDAY + timedelta(days=1))
    assert [c.day for c in later] == [MONDAY]
    assert staff.cards_change_token(session, tenant_id, TUESDAY) == token
    staff.decide(session, tenant_id, later[0].id, keep=True, now=NOW)
    assert staff.cards_change_token(session, tenant_id, TUESDAY) != token


def test_asking_for_what_holds_withdraws_a_waiting_request(session, tenant_id):
    staff.change_day_hours(session, tenant_id, SATURDAY, DayHours(closed=True), NOW)
    longer = DayHours(opens=time(10), closes=time(23))
    withdrawn = staff.change_day_hours(session, tenant_id, SATURDAY, longer, NOW)
    assert withdrawn is not None
    assert not withdrawn.held
    card = staff.change_day_hours(
        session, tenant_id, SATURDAY, DayHours(closed=True), NOW
    )
    assert card is not None
    # The closing still holds and still waits for the owner's look.
    assert card.held and card.status == "open" and card.after["closed"] is True
    assert _windows(session, tenant_id, SATURDAY) == []


def test_a_transfer_goes_to_todays_number(session, tenant_id):
    staff.change_team_phone(session, tenant_id, NUMBER, NOW)
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=NOW,
        delete_after=TUESDAY,
    )
    session.add(call)
    session.commit()
    request = TransferToTeamRequest(
        tenant_id=tenant_id, call_id=call.id, reason="human_requested"
    )
    assert transfer_to_team(session, request, NOW).transfer_to == NUMBER


def test_hours_a_service_never_had_wait_even_inside_another_services_hours(
    session, tenant_id
):
    """A date's hours hold for every service: delivery closed at noon on a
    Saturday gains noon hours, so the change waits for the owner."""
    for row in session.scalars(
        select(OpeningHours).where(
            OpeningHours.tenant_id == tenant_id,
            OpeningHours.service == "delivery",
            OpeningHours.weekday == SATURDAY.weekday(),
            OpeningHours.opens_at < time(15),
        )
    ):
        session.delete(row)
    session.commit()
    noon = DayHours(opens=time(12), closes=time(13))
    narrowed = staff.change_day_hours(session, tenant_id, SATURDAY, noon, NOW)
    assert narrowed is not None
    assert not narrowed.held
    assert _windows(session, tenant_id, SATURDAY, "delivery") == [("17:00", "22:00")]


def test_the_same_request_again_writes_nothing(session, tenant_id):
    closed = DayHours(closed=True)
    staff.change_day_hours(session, tenant_id, SATURDAY, closed, NOW)
    assert staff.change_day_hours(session, tenant_id, SATURDAY, closed, NOW) is None
    entries = session.scalars(
        select(AuditLog).where(AuditLog.action == "staff_change.day_hours")
    ).all()
    assert len(entries) == 1


def test_a_card_of_a_past_day_changes_nothing_today(session, tenant_id):
    """Codex on #589: a stale tablet answers yesterday's card after 05:00; today's
    number and its card must stay."""
    yesterday = NOW - timedelta(days=1)
    old = staff.change_team_phone(session, tenant_id, "+4972215550000", yesterday)
    assert old is not None
    today = staff.change_team_phone(session, tenant_id, NUMBER, NOW)
    assert today is not None
    assert old.id != today.id
    for keep in (False, True):
        staff.decide(session, tenant_id, old.id, keep=keep, now=NOW)
    config = _config(session, tenant_id)
    assert staff.team_phone_at(config, NOW) == NUMBER
    assert config.team_phone == settings.team_phone
    assert [c.status for c in _cards(session, tenant_id)] == ["open", "open"]


# --- the push to the owner and ops (#588, D34 point 4, T12) ----------------------


def _events(session, tenant_id) -> list[tuple[str, dict]]:
    rows = session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.tenant_id == tenant_id)
        .order_by(OutboxEvent.created_at)
    )
    return [(row.event_type, row.payload) for row in rows]


def test_each_team_number_change_pushes_once(session, tenant_id):
    standard = _config(session, tenant_id).team_phone
    staff.change_team_phone(session, tenant_id, NUMBER, NOW)
    staff.change_team_phone(session, tenant_id, "+4972215550000", NOW)
    staff.change_team_phone(session, tenant_id, standard, NOW)
    day = TUESDAY.isoformat()
    # Nothing but the day and the numbers: no device, no name.
    assert _events(session, tenant_id) == [
        (TEAM_PHONE_CHANGED, {"day": day, "from": standard, "to": NUMBER}),
        (TEAM_PHONE_CHANGED, {"day": day, "from": NUMBER, "to": "+4972215550000"}),
        (TEAM_PHONE_CHANGED, {"day": day, "from": "+4972215550000", "to": standard}),
    ]


def test_a_date_change_pushes_in_the_tablets_words_and_says_when_it_waits(
    session, tenant_id
):
    staff.change_day_hours(session, tenant_id, TUESDAY, DayHours(closed=True), NOW)
    longer = DayHours(opens=time(11), closes=time(23))
    staff.change_day_hours(session, tenant_id, SATURDAY, longer, NOW)
    assert _events(session, tenant_id) == [
        (
            DAY_HOURS_CHANGED,
            {
                "day": TUESDAY.isoformat(),
                "from": "normale Zeiten der Woche",
                "to": "geschlossen",
                "held": True,
            },
        ),
        (
            DAY_HOURS_CHANGED,
            {
                "day": SATURDAY.isoformat(),
                "from": "normale Zeiten der Woche",
                "to": "11:00 bis 23:00 Uhr",
                "held": False,
            },
        ),
    ]


def test_a_withdrawn_request_pushes_from_the_request_not_from_what_held(
    session, tenant_id
):
    """Otherwise the owner reads 'geschlossen, vorher geschlossen'."""
    staff.change_day_hours(session, tenant_id, SATURDAY, DayHours(closed=True), NOW)
    longer = DayHours(opens=time(10), closes=time(23))
    staff.change_day_hours(session, tenant_id, SATURDAY, longer, NOW)
    staff.change_day_hours(session, tenant_id, SATURDAY, DayHours(closed=True), NOW)
    last = _events(session, tenant_id)[-1]
    assert last == (
        DAY_HOURS_CHANGED,
        {
            "day": SATURDAY.isoformat(),
            "from": "10:00 bis 23:00 Uhr",
            "to": "geschlossen",
            "held": True,
        },
    )


def test_a_tap_that_changes_nothing_pushes_nothing(session, tenant_id):
    standard = _config(session, tenant_id).team_phone
    staff.change_team_phone(session, tenant_id, standard, NOW)
    with pytest.raises(InvalidInput):
        staff.change_team_phone(session, tenant_id, "+4930123456", NOW)
    with pytest.raises(InvalidInput):
        staff.change_day_hours(
            session, tenant_id, TUESDAY - timedelta(days=1), DayHours(closed=True), NOW
        )
    closed = DayHours(closed=True)
    staff.change_day_hours(session, tenant_id, SATURDAY, closed, NOW)
    staff.change_day_hours(session, tenant_id, SATURDAY, closed, NOW)
    assert len(_events(session, tenant_id)) == 1


def test_a_waiting_request_sent_again_pushes_and_writes_nothing(session, tenant_id):
    """Codex on #591: the same longer hours twice before the owner looked."""
    longer = DayHours(opens=time(11), closes=time(23))
    first = staff.change_day_hours(session, tenant_id, SATURDAY, longer, NOW)
    assert first is not None
    assert staff.change_day_hours(session, tenant_id, SATURDAY, longer, NOW) is None
    assert len(_events(session, tenant_id)) == 1
    entries = session.scalars(
        select(AuditLog).where(AuditLog.action == "staff_change.day_hours")
    ).all()
    assert len(entries) == 1
    assert not _cards(session, tenant_id)[0].held and first.status == "open"


def test_a_waiting_request_sent_again_on_its_day_holds_and_pushes(session, tenant_id):
    """The same request, once its date is today, holds at once: a change."""
    longer = DayHours(opens=time(11), closes=time(23))
    staff.change_day_hours(session, tenant_id, SATURDAY, longer, NOW)
    on_the_day = datetime.combine(SATURDAY, time(10), BERLIN)
    card = staff.change_day_hours(session, tenant_id, SATURDAY, longer, on_the_day)
    assert card is not None and card.held
    assert _windows(session, tenant_id, SATURDAY) == [("11:00", "23:00")]
    assert [p["held"] for _, p in _events(session, tenant_id)] == [False, True]


def test_the_owners_answer_pushes_nothing(session, tenant_id):
    """The owner decides with the PIN: nothing to tell the owner about."""
    card = staff.change_team_phone(session, tenant_id, NUMBER, NOW)
    assert card is not None
    staff.decide(session, tenant_id, card.id, keep=False, now=NOW)
    assert [kind for kind, _ in _events(session, tenant_id)] == [TEAM_PHONE_CHANGED]


@pytest.mark.parametrize(
    ("hours", "text"),
    [
        (DayHours(), "normale Zeiten der Woche"),
        (DayHours(closed=True, note="Neujahr"), "geschlossen"),
        (DayHours(opens=time(9, 5), closes=time(14)), "09:05 bis 14:00 Uhr"),
        (DayHours(like_weekday=6), "wie Sonntag"),
    ],
)
def test_day_hours_read_as_on_the_tablet(hours, text):
    assert hours.text() == text
