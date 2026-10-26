"""agent/dispatch.py: Tool-Name -> domain-Funktion ohne HTTP-Umweg (docs/11 §agent).

Deckt ab: jedes der sechs Stufe-1-Tools läuft durch, ein unbekannter Toolname und
fehlende Pflichtfelder werden als invalid_input beantwortet statt zu crashen, die
Reservierungs-/confirm-Idempotenz wird ohne Zutun des Modells erzeugt, und jeder
Aufruf landet in calls.tool_calls (docs/04 §Gemeinsame Regeln) -- auch fehlgeschlagene.
"""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.agent.dispatch import dispatch
from api.models import Call, Reservation
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
DIENSTAG = date(2026, 9, 15)
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
def tenant_id(session):
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


@pytest.fixture
def call_id(session, tenant_id):
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return call.id


def tool_calls(session, call_id) -> list[dict]:
    session.expire_all()
    return session.get(Call, call_id).tool_calls


def test_get_service_status_laeuft_ohne_zusatzargumente(session, tenant_id, call_id):
    result = dispatch(session, call_id, tenant_id, "get_service_status", {}, now=NOW)

    assert result.ok
    assert "is_open" in result.data
    entries = tool_calls(session, call_id)
    assert entries[-1]["name"] == "get_service_status"
    assert entries[-1]["ok"] is True
    assert entries[-1]["duration_ms"] >= 0


def test_check_slot_frei(session, tenant_id, call_id):
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "check_slot",
        {"reserved_for": berlin(DIENSTAG, 18, 30), "party_size": 4},
        now=NOW,
    )

    assert result.ok
    assert result.data["available"] is True


@pytest.mark.parametrize(
    "tool",
    ["get_service_status", "check_slot"],
)
def test_call_and_tenant_named_by_the_model_are_ignored(
    session, tenant_id, call_id, tool
):
    """Seen in the first run on a real model: it passes `call_id` and
    `tenant_id` because the tool reference lists them. The code decides both,
    never the model: an invented value must neither crash the call nor point
    a tool at another call or tenant."""
    result = dispatch(
        session,
        call_id,
        tenant_id,
        tool,
        {
            "call_id": "call_123",
            "tenant_id": "tenant_456",
            "reserved_for": berlin(DIENSTAG, 18, 30),
            "party_size": 4,
        }
        if tool == "check_slot"
        else {"call_id": "call_123", "tenant_id": "tenant_456"},
        now=NOW,
    )

    assert result.ok


def test_invalid_arguments_tell_the_model_which_field_is_wrong(
    session, tenant_id, call_id
):
    """Seen in the first run on a real model: `create_reservation` without the
    phone number came back as a bare `invalid_input`, and the model sent the
    same call five times instead of asking the guest. The hint names the
    field and the problem, never the value."""
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {
            "guest_name": "Müller",
            "party_size": "viele",
            "reserved_for": berlin(DIENSTAG, 18, 30),
        },
        now=NOW,
    )

    assert result.ok is False
    assert result.error_code == "invalid_input"
    assert "phone" in result.hint
    assert "party_size" in result.hint
    assert "viele" not in result.hint
    assert "Müller" not in result.hint


def test_create_reservation_erzeugt_eigenen_idempotency_key(
    session, tenant_id, call_id
):
    """Das Modell liefert nie einen Schlüssel; Code entscheidet ihn (CLAUDE.md §2 Regel 1)."""
    args = {
        "guest_name": "Müller",
        "phone": "+4972215551234",
        "party_size": 4,
        "reserved_for": berlin(DIENSTAG, 18, 30),
    }

    first = dispatch(
        session, call_id, tenant_id, "create_reservation", dict(args), now=NOW
    )
    assert first.ok
    assert first.data["status"] == "draft"

    # Gleicher Anruf, gleiches Tool, gleiche Eingabe -> gleicher Schlüssel, kein zweiter Entwurf.
    second = dispatch(
        session, call_id, tenant_id, "create_reservation", dict(args), now=NOW
    )
    assert second.data["reservation_id"] == first.data["reservation_id"]


def test_confirm_erzeugt_eigenen_idempotency_key(session, tenant_id, call_id):
    draft = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": "+4972215551234",
            "party_size": 4,
            "reserved_for": berlin(DIENSTAG, 18, 30),
        },
        now=NOW,
    )

    result = dispatch(
        session,
        call_id,
        tenant_id,
        "confirm",
        {"entity": "reservation", "entity_id": draft.data["reservation_id"]},
        now=NOW,
    )

    assert result.ok
    assert result.data["status"] == "confirmed"


def test_create_callback(session, tenant_id, call_id):
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "create_callback",
        {
            "phone": "+4972215551234",
            "reason": "human_requested",
            "summary": "Wunsch nach Mensch",
        },
        now=NOW,
    )
    assert result.ok
    assert result.data["status"] == "open"


def test_transfer_to_team(session, tenant_id, call_id):
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "transfer_to_team",
        {"reason": "complaint"},
        now=NOW,
    )
    assert result.ok
    assert "transfer_to" in result.data


def test_unbekanntes_tool_ist_invalid_input_statt_crash(session, tenant_id, call_id):
    result = dispatch(session, call_id, tenant_id, "does_not_exist", {}, now=NOW)

    assert result.ok is False
    assert result.error_code == "invalid_input"
    assert tool_calls(session, call_id)[-1]["error_code"] == "invalid_input"


def test_fehlendes_pflichtfeld_ist_invalid_input(session, tenant_id, call_id):
    result = dispatch(
        session, call_id, tenant_id, "check_slot", {"party_size": 4}, now=NOW
    )

    assert result.ok is False
    assert result.error_code == "invalid_input"


def test_domain_fehler_wird_geloggt_und_sitzung_bleibt_nutzbar(
    session, tenant_id, call_id
):
    """Ein AppError (Zeitpunkt in der Vergangenheit) darf die Sitzung nicht für den
    nächsten Tool-Aufruf verderben -- die Sitzung lebt hier über den ganzen Anruf,
    anders als im HTTP-Pfad mit einer frischen Sitzung je Request."""
    failed = dispatch(
        session,
        call_id,
        tenant_id,
        "check_slot",
        {"reserved_for": NOW - timedelta(hours=1), "party_size": 4},
        now=NOW,
    )
    assert failed.ok is False
    assert failed.error_code == "invalid_input"

    # Nach dem Fehler funktioniert ein unabhängiges Tool auf derselben Sitzung immer noch.
    status = dispatch(session, call_id, tenant_id, "get_service_status", {}, now=NOW)
    assert status.ok

    entries = tool_calls(session, call_id)
    assert [e["name"] for e in entries] == ["check_slot", "get_service_status"]
    assert entries[0]["ok"] is False
    assert entries[0]["error_code"] == "invalid_input"
    assert entries[1]["ok"] is True


def test_geaenderte_notiz_ist_ein_neuer_entwurf(session, tenant_id, call_id):
    """Offener Punkt aus PR #127 (T-4.10): aendert der Gast nach dem Vorlesen nur
    die Notiz ("mit Hochstuhl"), entsteht ein neuer Entwurf mit neuem readback -
    sonst kaeme der alte ohne Notiz zurueck."""
    args = {
        "guest_name": "Müller",
        "phone": "+4972215551234",
        "party_size": 4,
        "reserved_for": berlin(DIENSTAG, 18, 30),
    }
    first = dispatch(
        session, call_id, tenant_id, "create_reservation", dict(args), now=NOW
    )
    second = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {**args, "note": "mit Hochstuhl"},
        now=NOW,
    )
    assert second.ok
    assert second.data["reservation_id"] != first.data["reservation_id"]
    assert second.data["note"] == "mit Hochstuhl"


def test_schluessel_gleich_bei_anderer_schreibweise(session, tenant_id, call_id):
    """Review PR #139: dieselbe Reservierung mit anders geschriebener Nummer und
    anderer Zeitzone im Zeitstempel ist kein zweiter Entwurf."""
    from datetime import UTC

    wann = berlin(DIENSTAG, 18, 30)
    args = {"guest_name": "Müller", "party_size": 4}
    first = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {**args, "phone": "+4972215551234", "reserved_for": wann.isoformat()},
        now=NOW,
    )
    second = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {
            **args,
            "phone": "07221 5551234",
            "reserved_for": wann.astimezone(UTC).isoformat(),
        },
        now=NOW,
    )
    assert second.data["reservation_id"] == first.data["reservation_id"]


OFF_THE_CALENDAR = [
    "0001-01-01T00:00:00+14:00",
    "9999-12-31T23:59:00-12:00",
    # In range itself, but the window of the next day is not.
    "9999-12-31T12:00:00+00:00",
]


@pytest.mark.parametrize("tool", ["check_slot", "create_reservation"])
@pytest.mark.parametrize("when", OFF_THE_CALENDAR)
def test_a_date_no_calendar_holds_is_invalid_input(
    session, tenant_id, call_id, tool, when
):
    """A model that returns the year 9999 (invented, or the guest said so) must
    get a failed call it can act on. The date arithmetic of the slot check
    overflowed on it, the error was none the core knows, and the call ended as
    an outage with the team (#252)."""
    args = {"reserved_for": when, "party_size": 2}
    if tool == "create_reservation":
        args |= {"guest_name": "Müller", "phone": "+4972215551234"}

    result = dispatch(session, call_id, tenant_id, tool, args, now=NOW)

    assert result.ok is False
    assert result.error_code == "invalid_input"
    assert "reserved_for" in result.hint


@pytest.mark.parametrize(
    "when", ["9999-12-30T00:00:00+00:00", "0001-01-03T00:00:00+00:00"]
)
def test_the_last_days_that_do_work_are_not_refused_as_off_the_calendar(
    session, tenant_id, call_id, when
):
    """No booking horizon by the back door: a moment the arithmetic can handle
    gets the answer of the slot check, whatever that is (Codex, PR #262)."""
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "check_slot",
        {"reserved_for": when, "party_size": 2},
        now=NOW,
    )

    assert "reserved_for" not in (result.hint or "")


def test_a_table_for_the_team_carries_the_signal(session, tenant_id, call_id):
    """`team_callback` is what the conversation loop hands the wish over on
    (#252)."""
    wish = {"reserved_for": berlin(DIENSTAG, 18, 30).isoformat()}
    team = dispatch(
        session, call_id, tenant_id, "check_slot", {**wish, "party_size": 6}, now=NOW
    )
    assert team.ok and team.data["team_callback"] is True
    assert team.say and "sechs Personen noch heute" in team.say

    free = dispatch(
        session, call_id, tenant_id, "check_slot", {**wish, "party_size": 5}, now=NOW
    )
    assert free.data["available"] is True and free.data["team_callback"] is False


def test_a_table_for_the_team_cannot_be_drafted(session, tenant_id, call_id):
    """A model that skips `check_slot` gets no draft either."""
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": "+4972215551234",
            "party_size": 6,
            "reserved_for": berlin(DIENSTAG, 18, 30).isoformat(),
        },
        now=NOW,
    )
    assert not result.ok and result.error_code == "conflict"
    assert result.say and "nicht selbst zusagen" in result.say
    # The same signal as on `check_slot`: the core hands the wish to the team.
    assert result.data == {"team_callback": True}


def test_a_full_evening_is_a_conflict_without_the_signal(session, tenant_id, call_id):
    monday = berlin(DIENSTAG + timedelta(days=6), 18, 30)
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": "+4972215551234",
            "party_size": 2,
            "reserved_for": monday.isoformat(),
        },
        now=NOW,
    )
    assert not result.ok and result.error_code == "conflict"
    assert result.data == {} and result.hint is None


def test_check_slot_leaves_out_the_drafts_of_its_own_call(session, tenant_id, call_id):
    """#207: after a draft the guest moves the time. The draft of this call
    must not fill the evening for the same guest (35 + 5 + 5 > 40)."""
    other = Call(
        tenant_id=tenant_id,
        external_session_id="ext-2",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(other)
    session.flush()
    session.add(
        Reservation(
            tenant_id=tenant_id,
            call_id=other.id,
            status="confirmed",
            guest_name="Schmidt",
            phone="+4900000000",
            party_size=35,
            reserved_for=berlin(DIENSTAG, 18),
            idempotency_key=uuid.uuid4().hex,
        )
    )
    session.commit()
    wish = {"party_size": 5, "reserved_for": berlin(DIENSTAG, 18, 30).isoformat()}
    draft = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {"guest_name": "Müller", "phone": "+4972215551234", **wish},
        now=NOW,
    )
    assert draft.ok

    moved = dispatch(
        session,
        call_id,
        tenant_id,
        "check_slot",
        {**wish, "reserved_for": berlin(DIENSTAG, 19, 30).isoformat()},
        now=NOW,
    )

    assert moved.data["available"] is True


def test_a_tool_log_that_fails_leaves_the_session_usable(
    session, tenant_id, call_id, monkeypatch
):
    """The entry in `calls.tool_calls` is best effort. When its statement fails
    (on the phone line: a lock past the line's bound), the transaction is
    rolled back, or the next statement of the turn would fail and send a guest
    whose booking stands to the team (ultra review on PR #298)."""
    import sys

    from sqlalchemy import text

    # The module, not the function of the same name that `api.agent` exports.
    dispatch_module = sys.modules["api.agent.dispatch"]

    def failing(session, call_id, tenant_id, entry):
        session.execute(text("select 1 / 0"))

    monkeypatch.setattr(dispatch_module, "append_tool_call", failing)
    result = dispatch(session, call_id, tenant_id, "get_service_status", {}, now=NOW)

    assert result.ok
    assert session.execute(text("select 1")).scalar_one() == 1


def test_a_tool_log_whose_rollback_fails_too_still_answers(
    session, tenant_id, call_id, monkeypatch
):
    """A lost connection fails the rollback as well; the tool's answer still
    goes out (Codex on PR #310)."""
    import sys

    from sqlalchemy.exc import OperationalError

    dispatch_module = sys.modules["api.agent.dispatch"]

    def lost(*args, **kwargs):
        raise OperationalError("UPDATE", {}, Exception("connection lost"))

    monkeypatch.setattr(dispatch_module, "append_tool_call", lost)
    monkeypatch.setattr(session, "rollback", lost)
    result = dispatch(session, call_id, tenant_id, "get_service_status", {}, now=NOW)

    assert result.ok


@pytest.mark.parametrize("tool", ["check_slot", "create_reservation"])
def test_a_time_without_an_offset_is_the_restaurants_local_time(
    session, tenant_id, call_id, tool
):
    """A real model sent "2026-09-15T20:00:00": the tool refused it for the
    missing time zone, the model asked the guest for one, and a table the team
    decides on never reached the team (eval reservierung_0035, 22.10.2026).
    The state gives the model the restaurant's local time, so a time without
    an offset means that one, as core/time.to_utc reads one from the GUI."""
    local = berlin(DIENSTAG, 18, 30)
    args = {"reserved_for": local.replace(tzinfo=None).isoformat(), "party_size": 4}
    if tool == "create_reservation":
        args |= {"guest_name": "Müller", "phone": "+4972215551234"}

    result = dispatch(session, call_id, tenant_id, tool, args, now=NOW)

    assert result.ok, result.hint
    if tool == "check_slot":
        assert result.data["available"] is True
    else:
        reservation = session.get(Reservation, uuid.UUID(result.data["reservation_id"]))
        assert reservation.reserved_for == local


@pytest.mark.parametrize(
    "when",
    [
        "9999-12-31T23:59:00",
        "morgen um sieben",
        # The local zone moves it before the first day: no crash (Codex PR #347).
        "0001-01-01T00:00:00",
        # A date without a time is no time: never midnight (Codex PR #347).
        "2026-09-15",
        # No such wall time in Berlin (spring forward): never another hour.
        "2026-03-29T02:30:00",
        # Twice in Berlin (fall back): which one is not ours to guess.
        "2026-10-25T02:30:00",
    ],
)
def test_a_time_without_an_offset_that_is_no_time_stays_invalid_input(
    session, tenant_id, call_id, when
):
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "check_slot",
        {"reserved_for": when, "party_size": 2},
        now=NOW,
    )

    assert result.ok is False
    assert result.error_code == "invalid_input"
    assert "reserved_for" in result.hint
