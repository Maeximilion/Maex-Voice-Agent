"""domain/reservations: create_reservation als Entwurf mit readback, Idempotenz und Grenzfällen."""

import threading
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from psycopg.errors import QueryCanceled
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput, NotFound, TeamDecides
from api.domain.calls import end_call
from api.domain.confirm import confirm
from api.domain.reservations import capacity as reservations_capacity
from api.domain.reservations import create_reservation
from api.domain.reservations.create import readback
from api.models import (
    AuditLog,
    Call,
    Capacity,
    OpeningHours,
    Reservation,
    ServiceConfig,
)
from api.schemas.calls import EndCallRequest
from api.schemas.confirm import ConfirmRequest
from api.schemas.reservations import CreateReservationRequest
from api.tests.conftest import lift_party_limits
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Seed: Montag Ruhetag; Kapazität 11:30-14:00 mit 30 und 17:00-22:00 mit 40 Gästen.
DIENSTAG = date(2026, 9, 15)
MITTWOCH = date(2026, 9, 16)
NOW = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)


def berlin(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=BERLIN)


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


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return call.id


def request(tenant_id, call_id, **overrides) -> CreateReservationRequest:
    body = {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "idempotency_key": uuid.uuid4().hex,
        "guest_name": "Müller",
        "phone": "+4972215551234",
        "party_size": 4,
        "reserved_for": berlin(DIENSTAG, 18, 30),
        "note": None,
        **overrides,
    }
    return CreateReservationRequest(**body)


def test_entwurf_mit_readback_und_audit(session, tenant_id, call_id):
    draft = create_reservation(session, request(tenant_id, call_id), now=NOW)

    assert draft.status == "draft"
    assert draft.readback == (
        "Ein Tisch für vier Personen heute um halb sieben abends, auf den Namen Müller."
        " Passt das so?"
    )
    row = session.get(Reservation, draft.reservation_id)
    assert row is not None and row.status == "draft" and row.call_id == call_id
    assert row.reserved_for == berlin(DIENSTAG, 18, 30)

    audit = session.scalars(select(AuditLog)).all()
    assert len(audit) == 1
    assert audit[0].action == "reservation.draft_created"
    assert audit[0].entity_id == draft.reservation_id
    assert audit[0].payload["call_id"] == str(call_id)


def test_gleicher_schluessel_gleiche_antwort_kein_zweiter_vorgang(
    session, tenant_id, call_id
):
    key = "k-1"
    first = create_reservation(
        session, request(tenant_id, call_id, idempotency_key=key), now=NOW
    )
    second = create_reservation(
        session,
        request(
            tenant_id, call_id, idempotency_key=key, guest_name="Anders", party_size=2
        ),
        now=NOW,
    )
    assert second == first
    assert session.scalar(select(func.count()).select_from(Reservation)) == 1
    assert session.scalar(select(func.count()).select_from(AuditLog)) == 1


def test_schluessel_eines_anderen_mandanten_ist_conflict(session, tenant_id, call_id):
    key = "k-2"
    create_reservation(
        session, request(tenant_id, call_id, idempotency_key=key), now=NOW
    )
    other = uuid.UUID(
        seed(session, tenant_name="Anderer", timezone="Europe/Berlin").tenant_id
    )
    with pytest.raises(Conflict):
        create_reservation(
            session, request(other, call_id, idempotency_key=key), now=NOW
        )


def test_schluessel_eines_anderen_anrufs_ist_conflict(session, tenant_id, call_id):
    """Codex PR #127, P1: wie bei Bestellungen - ein Schluessel eines anderen
    Anrufs liefert nie dessen Reservierung."""
    key = "gleicher-schluessel-anderer-anruf"
    create_reservation(
        session, request(tenant_id, call_id, idempotency_key=key), now=NOW
    )
    other_call = Call(
        tenant_id=tenant_id,
        external_session_id="ext-2",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(other_call)
    session.commit()
    with pytest.raises(Conflict):
        create_reservation(
            session, request(tenant_id, other_call.id, idempotency_key=key), now=NOW
        )


def test_voller_abend_ist_conflict_ohne_mittag(session, tenant_id, call_id):
    lift_party_limits(session, tenant_id)
    # T-1.14: Alternativen nur im selben Service; der Abend ist ganz voll.
    create_reservation(session, request(tenant_id, call_id, party_size=40), now=NOW)
    other = _other_call(session, tenant_id)
    with pytest.raises(Conflict) as exc:
        create_reservation(session, request(tenant_id, other, party_size=1), now=NOW)
    assert exc.value.say is not None
    assert "halb sieben ist leider nichts frei" in exc.value.say
    assert session.scalar(select(func.count()).select_from(Reservation)) == 1


def test_ruhetag_ist_conflict(session, tenant_id, call_id):
    montag = berlin(date(2026, 9, 21), 18, 30)
    with pytest.raises(Conflict):
        create_reservation(
            session, request(tenant_id, call_id, reserved_for=montag), now=NOW
        )


def test_vergangenheit_ist_invalid_input(session, tenant_id, call_id):
    with pytest.raises(InvalidInput):
        create_reservation(
            session,
            request(tenant_id, call_id, reserved_for=berlin(DIENSTAG, 7, 0)),
            now=NOW,
        )


def test_unbekannter_anruf_ist_not_found(session, tenant_id):
    with pytest.raises(NotFound) as exc:
        create_reservation(session, request(tenant_id, uuid.uuid4()), now=NOW)
    assert exc.value.say is not None
    assert session.scalar(select(func.count()).select_from(Reservation)) == 0


def test_anruf_eines_anderen_mandanten_ist_not_found(session, tenant_id, call_id):
    other = uuid.UUID(
        seed(session, tenant_name="Anderer", timezone="Europe/Berlin").tenant_id
    )
    with pytest.raises(NotFound):
        create_reservation(session, request(other, call_id), now=NOW)


def test_unbekannter_mandant_ist_not_found(session, call_id):
    with pytest.raises(NotFound):
        create_reservation(session, request(uuid.uuid4(), call_id), now=NOW)


def test_rufnummer_wird_normalisiert(session, tenant_id, call_id):
    draft = create_reservation(
        session, request(tenant_id, call_id, phone="0721 / 555-1234"), now=NOW
    )
    assert draft.phone == "+497215551234"
    assert session.get(Reservation, draft.reservation_id).phone == "+497215551234"


@pytest.mark.parametrize(
    ("caller_id", "own"),
    [("07221 5551234", True), ("+4972215559999", False), (None, False)],
    ids=["own line", "named number", "withheld"],
)
def test_the_draft_notes_whether_it_was_booked_on_its_own_line(
    session, tenant_id, call_id, caller_id, own
):
    """Read once, here: the caller ID of the call goes before the table's name
    and number do (#523)."""
    session.get(Call, call_id).caller_id = caller_id
    session.commit()
    draft = create_reservation(session, request(tenant_id, call_id), now=NOW)
    assert session.get(Reservation, draft.reservation_id).booked_on_own_line is own


def test_each_draft_of_a_call_notes_its_own_number(session, tenant_id, call_id):
    session.get(Call, call_id).caller_id = "+4972215551234"
    session.commit()
    drafts = [
        create_reservation(session, request(tenant_id, call_id), now=NOW),
        create_reservation(
            session,
            request(tenant_id, call_id, guest_name="Schmidt", phone="07221 5559999"),
            now=NOW,
        ),
    ]
    rows = [session.get(Reservation, draft.reservation_id) for draft in drafts]
    assert [row.booked_on_own_line for row in rows] == [True, False]


def test_ungueltige_rufnummer_ist_invalid_input(session, tenant_id, call_id):
    with pytest.raises(InvalidInput) as exc:
        create_reservation(session, request(tenant_id, call_id, phone="12345"), now=NOW)
    assert exc.value.say is not None


def test_leerer_name_ist_invalid_input(session, tenant_id, call_id):
    with pytest.raises(InvalidInput):
        create_reservation(
            session, request(tenant_id, call_id, guest_name="   "), now=NOW
        )


def test_readback_morgen_eine_person_mit_hinweis(session, tenant_id, call_id):
    draft = create_reservation(
        session,
        request(
            tenant_id,
            call_id,
            party_size=1,
            reserved_for=berlin(MITTWOCH, 12, 0),
            note="  Kinderstuhl ",
        ),
        now=NOW,
    )
    assert draft.note == "Kinderstuhl"
    assert draft.readback == (
        "Ein Tisch für eine Person morgen um zwölf Uhr mittags, auf den Namen Müller"
        ", mit Ihrem Hinweis: Kinderstuhl. Passt das so?"
    )


def test_readback_mit_wochentag_und_datum(session, tenant_id, call_id):
    lift_party_limits(session, tenant_id)
    draft = create_reservation(
        session,
        request(
            tenant_id,
            call_id,
            party_size=15,
            reserved_for=berlin(date(2026, 9, 25), 19, 15),
        ),
        now=NOW,
    )
    assert draft.readback == (
        "Ein Tisch für 15 Personen am Freitag, den 25. September um viertel nach sieben"
        " abends"
        ", auf den Namen Müller. Passt das so?"
    )


@pytest.mark.parametrize(
    ("hh", "said"),
    [
        (0, "in der Nacht von Dienstag auf Mittwoch um halb eins"),
        (12, "morgen um halb eins mittags"),
    ],
)
def test_the_readback_tells_half_past_midnight_from_half_past_noon(hh, said):
    """#681: with lunch until 14:30 and the evening until 01:00, "morgen um
    halb eins" was the same sentence for both, and the guest's yes confirms it."""
    table = Reservation(
        reserved_for=berlin(MITTWOCH, hh, 30),
        created_at=NOW,
        party_size=2,
        guest_name="Müller",
        note=None,
    )
    assert readback(table, BERLIN) == (
        f"Ein Tisch für zwei Personen {said}, auf den Namen Müller. Passt das so?"
    )


@pytest.mark.parametrize(
    ("created", "day", "hh", "said"),
    [
        # 00:30 of "morgen" is the night of today to tomorrow: it is still
        # tonight, and "morgen um halb eins nachts" sent the guest to 24 hours
        # later than the table (#742).
        (NOW, MITTWOCH, 0, "in der Nacht von Dienstag auf Mittwoch um halb eins"),
        (
            NOW,
            MITTWOCH + timedelta(days=1),
            0,
            "in der Nacht von Mittwoch auf Donnerstag",
        ),
        # More than a week out the date comes with it.
        (
            NOW,
            date(2026, 9, 26),
            0,
            "in der Nacht von Freitag auf Samstag, den 26. September",
        ),
        # Tonight's own after-midnight table is named by its two days as well.
        (NOW, MITTWOCH - timedelta(days=1), 0, "in der Nacht von Montag auf Dienstag"),
        (NOW, MITTWOCH, 4, "in der Nacht von Dienstag auf Mittwoch um halb fünf"),
        # 23:30 is still the evening of its own day, 05:30 the morning.
        (NOW, MITTWOCH, 23, "morgen um halb zwölf nachts"),
        (NOW, MITTWOCH, 5, "morgen um halb sechs morgens"),
    ],
)
def test_after_midnight_table_is_named_by_its_night(created, day, hh, said):
    table = Reservation(
        reserved_for=berlin(day, hh, 30),
        created_at=created,
        party_size=2,
        guest_name="Müller",
        note=None,
    )
    assert said in readback(table, BERLIN)


# Wednesday 00:15: the night of Tuesday to Wednesday, the evening may run on.
AFTER_MIDNIGHT = berlin(MITTWOCH, 0, 15)


@pytest.mark.parametrize(
    ("created", "day", "hh", "said"),
    [
        # At 00:15 most callers still live in Tuesday: "heute" would be
        # Tuesday's and "morgen" Wednesday to them. A guest who meant Wednesday
        # by "morgen" heard "morgen" back for Thursday, and the yes booked the
        # table 24 hours late (#750 follow-up).
        (
            AFTER_MIDNIGHT,
            MITTWOCH,
            19,
            "Ein Tisch für zwei Personen am Mittwoch, den 16. September um sieben "
            "Uhr abends",
        ),
        (
            AFTER_MIDNIGHT,
            MITTWOCH + timedelta(days=1),
            19,
            "Ein Tisch für zwei Personen am Donnerstag, den 17. September um "
            "sieben Uhr abends",
        ),
        # 04:59 is still the night.
        (
            berlin(MITTWOCH, 4, 59),
            MITTWOCH,
            12,
            "am Mittwoch, den 16. September um zwölf Uhr mittags",
        ),
        # The table's own night keeps its name by both days.
        (
            AFTER_MIDNIGHT,
            MITTWOCH,
            1,
            "in der Nacht von Dienstag auf Mittwoch um ein Uhr",
        ),
        # From five the calendar day holds again, and the evening before too.
        (berlin(MITTWOCH, 5, 0), MITTWOCH, 19, "heute um sieben Uhr abends"),
        (
            berlin(DIENSTAG, 20, 0),
            MITTWOCH,
            19,
            "morgen um sieben Uhr abends",
        ),
        (
            berlin(DIENSTAG, 23, 59),
            MITTWOCH,
            19,
            "morgen um sieben Uhr abends",
        ),
    ],
)
def test_a_draft_made_after_midnight_names_the_day_by_weekday_and_date(
    created, day, hh, said
):
    table = Reservation(
        reserved_for=berlin(day, hh),
        created_at=created,
        party_size=2,
        guest_name="Müller",
        note=None,
    )
    assert said in readback(table, BERLIN)


def test_a_call_after_midnight_reads_the_draft_back_by_weekday_and_date(
    session, tenant_id, call_id
):
    draft = create_reservation(
        session,
        request(tenant_id, call_id, reserved_for=berlin(MITTWOCH + timedelta(1), 19)),
        now=AFTER_MIDNIGHT,
    )
    assert draft.readback == (
        "Ein Tisch für vier Personen am Donnerstag, den 17. September um sieben Uhr"
        " abends, auf den Namen Müller. Passt das so?"
    )


def test_a_draft_made_after_midnight_never_says_heute_or_morgen():
    for hh in (0, 1, 4):
        for later in range(3):
            table = Reservation(
                reserved_for=berlin(MITTWOCH + timedelta(days=later), 19),
                created_at=berlin(MITTWOCH, hh, 15).astimezone(ZoneInfo("UTC")),
                party_size=2,
                guest_name="Müller",
                note=None,
            )
            said = readback(table, BERLIN)
            assert " heute " not in said and " morgen " not in said, said


def _at(y: int, m: int, d: int, hh: int, mm: int = 0, fold: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=BERLIN, fold=fold)


@pytest.mark.parametrize(
    ("created", "table", "said"),
    [
        # Month end: Wednesday 30 September to Thursday 1 October.
        (
            _at(2026, 9, 30, 20),
            _at(2026, 10, 1, 0, 30),
            "in der Nacht von Mittwoch auf Donnerstag um halb eins",
        ),
        (_at(2026, 9, 30, 20), _at(2026, 10, 1, 19), "morgen um sieben Uhr abends"),
        (
            _at(2026, 10, 1, 0, 15),
            _at(2026, 10, 1, 19),
            "am Donnerstag, den 1. Oktober um sieben Uhr abends",
        ),
        # Year end: Thursday 31 December 2026 to Friday 1 January 2027.
        (
            _at(2026, 12, 31, 20),
            _at(2027, 1, 1, 0, 30),
            "in der Nacht von Donnerstag auf Freitag um halb eins",
        ),
        (_at(2026, 12, 31, 20), _at(2027, 1, 1, 19), "morgen um sieben Uhr abends"),
        (
            _at(2027, 1, 1, 0, 15),
            _at(2027, 1, 1, 19),
            "am Freitag, den 1. Januar um sieben Uhr abends",
        ),
        (
            _at(2026, 12, 28, 20),
            _at(2027, 1, 2, 0, 30),
            "in der Nacht von Freitag auf Samstag",
        ),
        # Clocks go back in the night of Saturday 24 to Sunday 25 October 2026
        # (03:00 summer time is 02:00 winter time): the night has 25 hours.
        (
            _at(2026, 10, 24, 20),
            _at(2026, 10, 25, 1, 30),
            "in der Nacht von Samstag auf Sonntag um halb zwei",
        ),
        (
            _at(2026, 10, 24, 20),
            _at(2026, 10, 25, 2, 30, fold=1),
            "in der Nacht von Samstag auf Sonntag um halb drei",
        ),
        (_at(2026, 10, 24, 20), _at(2026, 10, 25, 19), "morgen um sieben Uhr abends"),
        (
            _at(2026, 10, 25, 2, 15, fold=1),
            _at(2026, 10, 25, 19),
            "am Sonntag, den 25. Oktober um sieben Uhr abends",
        ),
        # Clocks go forward in the night of Saturday 27 to Sunday 28 March 2027
        # (02:00 winter time is 03:00 summer time): the night has 23 hours.
        (
            _at(2027, 3, 27, 20),
            _at(2027, 3, 28, 3, 30),
            "in der Nacht von Samstag auf Sonntag um halb vier",
        ),
        (_at(2027, 3, 27, 20), _at(2027, 3, 28, 19), "morgen um sieben Uhr abends"),
        (_at(2027, 3, 28, 5), _at(2027, 3, 28, 19), "heute um sieben Uhr abends"),
    ],
)
def test_the_night_read_back_holds_across_month_year_and_clock_change(
    created, table, said
):
    """#862: the read-back counts local days with `ZoneInfo`, never hours from
    UTC; stored in UTC, as the database returns them."""
    utc = ZoneInfo("UTC")
    row = Reservation(
        reserved_for=table.astimezone(utc),
        created_at=created.astimezone(utc),
        party_size=2,
        guest_name="Müller",
        note=None,
    )
    assert f"Ein Tisch für zwei Personen {said}" in readback(row, BERLIN)


def test_entwurf_zaehlt_sofort_gegen_die_kapazitaet(session, tenant_id, call_id):
    # For every other call: a call's own drafts do not count against it (#207).
    lift_party_limits(session, tenant_id)
    create_reservation(session, request(tenant_id, call_id, party_size=39), now=NOW)
    second, third = _other_call(session, tenant_id), _other_call(session, tenant_id)
    create_reservation(session, request(tenant_id, second, party_size=1), now=NOW)
    with pytest.raises(Conflict):
        create_reservation(session, request(tenant_id, third, party_size=1), now=NOW)


# --- A draft holds its seats only while its call can still confirm it (#250) ---


def _other_call(session, tenant_id) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext-2",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return call.id


def _call_limit(session, tenant_id) -> timedelta:
    """From the database, like the code under test: no number of its own."""
    return timedelta(seconds=session.get(ServiceConfig, tenant_id).max_call_seconds)


def _hang_up(session, tenant_id, call_id, at: datetime) -> None:
    end_call(
        session,
        EndCallRequest(call_id=call_id, tenant_id=tenant_id, outcome="abandoned"),
        now=at,
    )


def test_a_draft_of_a_live_call_keeps_its_seats(session, tenant_id, call_id):
    """While the guest is on the line nobody else gets the table."""
    lift_party_limits(session, tenant_id)
    create_reservation(session, request(tenant_id, call_id, party_size=40), now=NOW)
    other = _other_call(session, tenant_id)

    with pytest.raises(Conflict):
        create_reservation(session, request(tenant_id, other, party_size=2), now=NOW)


def test_a_draft_of_an_ended_call_gives_its_seats_back(session, tenant_id, call_id):
    """Forty seats asked for, then hung up at the read-back: the evening must
    not stay full for everyone who calls after."""
    lift_party_limits(session, tenant_id)
    create_reservation(session, request(tenant_id, call_id, party_size=40), now=NOW)
    _hang_up(session, tenant_id, call_id, NOW + timedelta(minutes=1))
    other = _other_call(session, tenant_id)

    later = NOW + timedelta(minutes=2)
    draft = create_reservation(
        session, request(tenant_id, other, party_size=40), now=later
    )

    assert draft.status == "draft"


def test_a_draft_past_the_call_limit_gives_its_seats_back(session, tenant_id, call_id):
    """A call that never got its end event (process died, line dropped) must not
    hold seats for good either: no call lasts longer than the limit."""
    lift_party_limits(session, tenant_id)
    create_reservation(session, request(tenant_id, call_id, party_size=40), now=NOW)
    other = _other_call(session, tenant_id)
    limit = _call_limit(session, tenant_id)

    with pytest.raises(Conflict):
        create_reservation(
            session,
            request(tenant_id, other, party_size=2),
            now=NOW + limit - timedelta(seconds=1),
        )
    draft = create_reservation(
        session,
        request(tenant_id, other, party_size=2),
        now=NOW + limit + timedelta(seconds=1),
    )

    assert draft.status == "draft"


def test_a_confirmed_table_keeps_its_seats_after_the_call(session, tenant_id, call_id):
    """Only drafts expire. A yes is a booking, whatever the call did after."""
    lift_party_limits(session, tenant_id)
    draft = create_reservation(
        session, request(tenant_id, call_id, party_size=40), now=NOW
    )
    confirm(
        session,
        ConfirmRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            entity="reservation",
            entity_id=draft.reservation_id,
            idempotency_key=uuid.uuid4().hex,
        ),
        now=NOW,
    )
    _hang_up(session, tenant_id, call_id, NOW + timedelta(minutes=1))
    other = _other_call(session, tenant_id)
    much_later = NOW + _call_limit(session, tenant_id) * 3

    with pytest.raises(Conflict):
        create_reservation(
            session, request(tenant_id, other, party_size=2), now=much_later
        )


# --- A call's own drafts do not count against it (#207) ---

WEDNESDAY_EVENING = berlin(MITTWOCH, 19)


def _seat(session, tenant_id, call, guests: int, status: str = "confirmed") -> None:
    """What the window already holds, written directly."""
    session.add(
        Reservation(
            tenant_id=tenant_id,
            call_id=call,
            status=status,
            guest_name="Schmidt",
            phone="+4900000000",
            party_size=guests,
            reserved_for=WEDNESDAY_EVENING,
            idempotency_key=uuid.uuid4().hex,
            created_at=NOW,
        )
    )
    session.commit()


def _six(tenant_id, call_id, **overrides) -> CreateReservationRequest:
    wish = {"party_size": 6, "reserved_for": WEDNESDAY_EVENING, **overrides}
    return request(tenant_id, call_id, **wish)


def _confirm(session, tenant_id, call_id, reservation_id):
    return confirm(
        session,
        ConfirmRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            entity="reservation",
            entity_id=reservation_id,
            idempotency_key=uuid.uuid4().hex,
        ),
        now=NOW,
    )


@pytest.mark.parametrize(
    "correction",
    [{"reserved_for": berlin(MITTWOCH, 19, 30)}, {"party_size": 8}],
    ids=["new_time", "more_guests"],
)
def test_a_corrected_draft_does_not_count_against_its_own_call(
    session, tenant_id, call_id, correction
):
    """30 of 40 seats booked, the guest drafts six and then corrects the time or
    the party size. Ten seats are free: the corrected table is drafted, and the
    yes books it. Before, the first draft stood in the way (36 + 6 > 40)."""
    _seat(session, tenant_id, _other_call(session, tenant_id), 30)
    create_reservation(session, _six(tenant_id, call_id), now=NOW)

    corrected = create_reservation(
        session, _six(tenant_id, call_id, **correction), now=NOW
    )

    assert corrected.status == "draft"
    assert _confirm(session, tenant_id, call_id, corrected.reservation_id).status == (
        "confirmed"
    )


def test_the_draft_of_another_live_call_still_counts(session, tenant_id, call_id):
    other = _other_call(session, tenant_id)
    _seat(session, tenant_id, other, 30)
    _seat(session, tenant_id, other, 6, status="draft")

    with pytest.raises(Conflict):
        create_reservation(session, _six(tenant_id, call_id), now=NOW)


def test_a_confirmed_table_of_the_same_call_still_counts(session, tenant_id, call_id):
    """A second table in the same call: the first is booked, not replaced."""
    _seat(session, tenant_id, _other_call(session, tenant_id), 30)
    first = create_reservation(session, _six(tenant_id, call_id), now=NOW)
    _confirm(session, tenant_id, call_id, first.reservation_id)

    with pytest.raises(Conflict):
        create_reservation(
            session,
            _six(tenant_id, call_id, reserved_for=berlin(MITTWOCH, 19, 30)),
            now=NOW,
        )


def _three_corrections(session, tenant_id, call_id):
    """Six at seven, a quarter past and half past: three drafts of one call,
    the last one written last."""
    drafts = [
        create_reservation(
            session,
            _six(tenant_id, call_id, reserved_for=berlin(MITTWOCH, 19, mm)),
            now=NOW + timedelta(minutes=step),
        )
        for step, mm in enumerate((0, 15, 30))
    ]
    return drafts[-1]


def test_only_the_newest_draft_of_a_call_holds_seats(session, tenant_id, call_id):
    """#908: every correction wrote a draft and every draft held its seats, so
    one call could fill the evening for everybody else. 30 of 40 seats booked,
    call A corrects twice: only its newest six count, and call B still drafts
    four. A's yes to its newest draft books it."""
    _seat(session, tenant_id, _other_call(session, tenant_id), 30)
    newest = _three_corrections(session, tenant_id, call_id)

    other = create_reservation(
        session,
        _six(tenant_id, _other_call(session, tenant_id), party_size=4),
        now=NOW + timedelta(minutes=5),
    )

    assert other.status == "draft"
    assert _confirm(session, tenant_id, call_id, newest.reservation_id).status == (
        "confirmed"
    )


def test_a_replaced_draft_holds_nothing_after_the_yes(session, tenant_id, call_id):
    """The newest draft confirmed, the older ones of its call stay drafts: they
    must not count again now that a confirmed row is the newest."""
    _seat(session, tenant_id, _other_call(session, tenant_id), 30)
    newest = _three_corrections(session, tenant_id, call_id)
    _confirm(session, tenant_id, call_id, newest.reservation_id)

    other = create_reservation(
        session,
        _six(tenant_id, _other_call(session, tenant_id), party_size=4),
        now=NOW + timedelta(minutes=5),
    )

    assert other.status == "draft"


def test_the_newest_draft_of_another_call_still_counts(session, tenant_id, call_id):
    """One draft per call still holds its seats: 30 + 6 + 6 > 40."""
    _seat(session, tenant_id, _other_call(session, tenant_id), 30)
    _three_corrections(session, tenant_id, _other_call(session, tenant_id))

    with pytest.raises(Conflict):
        create_reservation(
            session, _six(tenant_id, call_id), now=NOW + timedelta(minutes=5)
        )


def test_two_drafts_of_one_call_never_book_past_the_window(session, tenant_id, call_id):
    """A call may hold two drafts now, but `confirm` counts every confirmed
    seat: only one of them can be booked."""
    _seat(session, tenant_id, _other_call(session, tenant_id), 30)
    first = create_reservation(session, _six(tenant_id, call_id), now=NOW)
    second = create_reservation(
        session,
        _six(tenant_id, call_id, reserved_for=berlin(MITTWOCH, 19, 30)),
        now=NOW,
    )

    assert _confirm(session, tenant_id, call_id, first.reservation_id).status == (
        "confirmed"
    )
    with pytest.raises(Conflict):
        _confirm(session, tenant_id, call_id, second.reservation_id)


def test_replay_behaelt_readback_ueber_mitternacht(session, tenant_id, call_id):
    """Gleicher Schlüssel, gleiche Antwort — auch wenn der Tag zwischen den Aufrufen wechselt."""
    kurz_vor_mitternacht = datetime(2026, 9, 15, 23, 50, tzinfo=BERLIN)
    nach_mitternacht = datetime(2026, 9, 16, 0, 30, tzinfo=BERLIN)
    req = request(
        tenant_id,
        call_id,
        idempotency_key="k-mitternacht",
        reserved_for=berlin(MITTWOCH, 18, 30),
    )

    first = create_reservation(session, req, now=kurz_vor_mitternacht)
    assert "morgen" in first.readback

    second = create_reservation(session, req, now=nach_mitternacht)
    assert second.readback == first.readback
    assert second == first


def _seven_then_eight(session, tenant_id, call_id):
    """A table at seven, corrected to eight: the request that goes back."""
    seven = request(
        tenant_id, call_id, idempotency_key="k-seven", reserved_for=berlin(DIENSTAG, 19)
    )
    first = create_reservation(session, seven, now=NOW)
    create_reservation(
        session,
        request(
            tenant_id,
            call_id,
            idempotency_key="k-eight",
            reserved_for=berlin(DIENSTAG, 20),
        ),
        now=NOW + timedelta(minutes=1),
    )
    return first, seven


def test_going_back_to_an_earlier_draft_checks_the_slot_again(
    session, tenant_id, call_id
):
    """#862: the earlier draft's key replayed it without `check_slot`, so the
    notice that has run out since (or hours closed since) was never read."""
    _first, seven = _seven_then_eight(session, tenant_id, call_id)
    session.execute(update(Capacity).values(notice_minutes=60))
    session.commit()

    with pytest.raises(Conflict):
        create_reservation(session, seven, now=berlin(DIENSTAG, 18, 30))


def test_going_back_replays_the_same_draft_and_holds_no_second(
    session, tenant_id, call_id
):
    """Checked again, the earlier draft is the one replayed: no second row
    holds the same seats against other calls."""
    first, seven = _seven_then_eight(session, tenant_id, call_id)

    back = create_reservation(session, seven, now=NOW + timedelta(minutes=2))
    again = create_reservation(session, seven, now=NOW + timedelta(minutes=3))

    assert back == first and again == first
    assert session.scalar(select(func.count()).select_from(Reservation)) == 2


def test_a_retry_is_checked_again_too(session, tenant_id, call_id):
    """The same request after the notice was raised: the slot no longer
    holds, and the draft is not read back again."""
    req = request(tenant_id, call_id, idempotency_key="k-same")
    create_reservation(session, req, now=NOW)
    session.execute(update(Capacity).values(notice_minutes=60 * 24))
    session.commit()

    with pytest.raises(Conflict):
        create_reservation(session, req, now=NOW + timedelta(minutes=5))
    assert session.scalar(select(func.count()).select_from(Reservation)) == 1


def test_a_draft_cancelled_elsewhere_is_replayed_as_cancelled(
    session, tenant_id, call_id
):
    """The call's session keeps its rows past a commit: read without a fresh
    look, the tablet's cancel was not seen and the draft was checked and read
    back as a draft again."""
    req = request(tenant_id, call_id, idempotency_key="k-tablet")
    first = create_reservation(session, req, now=NOW)
    with Session(session.get_bind()) as tablet:
        tablet.execute(
            update(Reservation)
            .where(Reservation.id == first.reservation_id)
            .values(status="cancelled")
        )
        tablet.commit()

    again = create_reservation(session, req, now=NOW + timedelta(minutes=1))

    assert again.reservation_id == first.reservation_id
    assert again.status == "cancelled"


def test_a_confirmed_table_is_replayed_as_it_stands(session, tenant_id, call_id):
    """Its yes was given: the same request after it books nothing again."""
    first, seven = _seven_then_eight(session, tenant_id, call_id)
    session.execute(
        update(Reservation)
        .where(Reservation.id == first.reservation_id)
        .values(status="confirmed")
    )
    session.commit()

    again = create_reservation(session, seven, now=NOW + timedelta(minutes=2))

    assert again.reservation_id == first.reservation_id
    assert again.status == "confirmed"


def test_parallele_anlagen_ueberbuchen_nicht(migrated_db_url):
    """Acht gleichzeitige Anfragen auf ein Fenster mit 40 Plätzen: nur vier dürfen durchkommen."""
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        lift_party_limits(s, tenant)
        # One call per request: a call's own drafts do not count against it (#207).
        calls = [
            Call(
                tenant_id=tenant,
                external_session_id=f"ext-{n}",
                started_at=NOW,
                delete_after=DIENSTAG,
            )
            for n in range(8)
        ]
        s.add_all(calls)
        s.commit()
        call_ids = [call.id for call in calls]

    start = threading.Barrier(8)
    results: list[object] = []
    lock = threading.Lock()

    def book(n: int) -> None:
        start.wait(timeout=10)
        with Session(engine) as own:
            try:
                create_reservation(
                    own,
                    request(
                        tenant,
                        call_ids[n],
                        idempotency_key=f"parallel-{n}",
                        party_size=10,
                    ),
                    now=NOW,
                )
                outcome: object = "ok"
            except Conflict as exc:
                outcome = exc
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=book, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with Session(engine) as s:
        booked = s.scalar(
            select(func.coalesce(func.sum(Reservation.party_size), 0)).where(
                Reservation.tenant_id == tenant
            )
        )
    engine.dispose()

    assert results.count("ok") == 4, f"erwartet 4 Buchungen, bekam {results}"
    assert booked == 40, f"Fenster mit 40 Plätzen ist mit {booked} Gästen überbucht"


def test_sperre_gilt_fuer_das_ganze_fenster_ueber_mitternacht(migrated_db_url):
    """Review PR #152: Montag 23:30 und Dienstag 00:30 liegen im selben Fenster
    18:00-01:00. Haelt ein Anruf die Sperre fuer Montag 23:30, muss ein zweiter fuer
    Dienstag 00:30 warten; mit einer Sperre je Kalendertag liefe er parallel und
    ueberbuchte das Fenster. Deterministisch statt mit Threads: B bekommt 300 ms."""
    engine = create_engine(migrated_db_url)
    montag = date(2026, 9, 14)
    with Session(engine) as s:
        tenant = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        s.add_all(
            [
                OpeningHours(
                    tenant_id=tenant,
                    weekday=0,
                    opens_at=time(18),
                    closes_at=time(1),
                    service="dinein",
                ),
                Capacity(
                    tenant_id=tenant,
                    weekday=0,
                    slot_start=time(18),
                    slot_end=time(1),
                    max_guests=20,
                ),
            ]
        )
        row = Call(
            tenant_id=tenant,
            external_session_id="ext",
            started_at=NOW,
            delete_after=DIENSTAG,
        )
        s.add(row)
        s.commit()
        call = row.id

    with Session(engine) as a, Session(engine) as b:
        # A prueft gerade Montag 23:30, die Transaktion ist offen.
        reservations_capacity.lock_capacity_days(a, tenant, montag)
        b.execute(text("SET LOCAL statement_timeout = 300"))
        with pytest.raises(OperationalError) as waited:
            create_reservation(
                b,
                request(
                    tenant,
                    call,
                    idempotency_key="nacht-b",
                    reserved_for=berlin(DIENSTAG, 0, 30),
                ),
                now=berlin(montag, 8),
            )
        # Genau das Warten auf die Sperre, nicht irgendein anderer DB-Fehler.
        assert isinstance(waited.value.orig, QueryCanceled)
        b.rollback()
        a.rollback()
    engine.dispose()


@pytest.mark.parametrize(
    ("day", "party_size", "heard"),
    [
        (DIENSTAG, 6, "sechs Personen noch heute"),  # same day, above 5
        (MITTWOCH, 9, "neun Personen kann ich"),  # any day, above 8
        (date(2026, 12, 15), 2, "höchstens 90 Tage im Voraus"),  # day 91
    ],
)
def test_beyond_the_limits_of_the_restaurant_nothing_is_drafted(
    session, tenant_id, call_id, day, party_size, heard
):
    wish = request(
        tenant_id, call_id, party_size=party_size, reserved_for=berlin(day, 18, 30)
    )
    with pytest.raises(Conflict) as exc:
        create_reservation(session, wish, now=NOW)
    assert exc.value.say is not None
    assert heard in exc.value.say
    # A table for the team is told apart from a wish nobody can book.
    assert isinstance(exc.value, TeamDecides) is ("Personen" in heard)
    assert session.scalar(select(func.count()).select_from(Reservation)) == 0


@pytest.mark.parametrize(("day", "party_size"), [(DIENSTAG, 5), (MITTWOCH, 8)])
def test_at_the_limits_the_agent_drafts_the_table(
    session, tenant_id, call_id, day, party_size
):
    wish = request(
        tenant_id, call_id, party_size=party_size, reserved_for=berlin(day, 18, 30)
    )
    assert create_reservation(session, wish, now=NOW).party_size == party_size
