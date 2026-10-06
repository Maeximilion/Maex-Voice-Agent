"""telephony/: port, provider-neutral call handler and the fake adapter (T-1.13).

A call file is played through the fake adapter into the handler, which drives the
same agent core as `sim/` and the evals. Checked are what the platform would be
told to do (say, transfer, hang up) and what lands in the database, never the
wording of the model (docs/08 §3) - except the sentences the caller must hear:
the AI disclosure at the start and the outage sentence before a transfer.
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.agent.llm import LLMTurn
from api.config import settings
from api.domain.callbacks.create import SAY_NOTED
from api.models import Call, Callback, Reservation, ServiceConfig
from api.telephony.adapters.fake import FakeTelephony, load_call
from api.telephony.handler import FAREWELL, SAY_OUTAGE, CallHandler
from scripts.seed import seed
from sim.scripted_llm import QUESTIONS, SAY_CONFIRMED, ScriptedLLM
from sim.session import menu_numbers, resolve_tenant

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 15, 18, 0, tzinfo=BERLIN)  # Tuesday, evening service open
CLOSED = datetime(2026, 9, 15, 15, 30, tzinfo=BERLIN)  # Tuesday, between the services
CASES = Path(__file__).resolve().parents[2] / "evals" / "cases"
TEAM_PHONE = "+497215550000"

RESERVATION = {
    "id": "telefon_reservierung",
    "caller_id": "0721 5551234",
    "transcript": [
        {
            "role": "customer",
            "text": "Guten Tag, ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr.",
        },
        {"role": "agent", "text": "Auf welchen Namen?"},
        {"role": "customer", "text": "Auf den Namen Mueller."},
        {"role": "customer", "text": "Ja, passt so."},
    ],
}


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant(session):
    """Plain values, not the ORM row: the handler closes its session at the end of
    every call, and in these tests that is the test's own session."""
    seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    row = resolve_tenant(session, "Testbetrieb")
    tenant = SimpleNamespace(id=row.id, timezone=row.timezone, name=row.name)
    set_mode(session, tenant.id, "primary")
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant.id)
        .values(team_phone=TEAM_PHONE)
    )
    session.commit()
    return tenant


def set_mode(session, tenant_id, mode: str) -> None:
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(call_mode=mode)
    )
    session.commit()


def scripted(session, tenant):
    return lambda s: ScriptedLLM(
        now=NOW,
        timezone=tenant.timezone,
        menu=lambda: menu_numbers(s, tenant.id),
    )


def handler_for(session, tenant, port, *, llm_factory=None, session_factory=None):
    return CallHandler(
        port,
        tenant_id=tenant.id,
        session_factory=session_factory or (lambda: session),
        llm_factory=llm_factory or scripted(session, tenant),
        now=NOW,
    )


def the_call(session, session_id: str) -> Call:
    return session.scalars(
        select(Call).where(Call.external_session_id == session_id)
    ).one()


class BrokenLLM:
    """A model, or anything behind it, that fails in the middle of a call."""

    def next_turn(self, system_prompt, state_json, input_text) -> LLMTurn:
        raise RuntimeError("model endpoint down")


class NoLLM:
    """Fails the test if the agent is asked at all."""

    def next_turn(self, system_prompt, state_json, input_text) -> LLMTurn:
        raise AssertionError("the agent must not answer this call")


# -- normal case ---------------------------------------------------------------


def test_reservation_by_phone_is_confirmed_and_the_agent_hangs_up(session, tenant):
    port = FakeTelephony()
    sid = port.play(RESERVATION, handler_for(session, tenant, port))

    says = port.says(sid)
    assert says[0].startswith("Guten Tag, hier ist der KI-Assistent von Testbetrieb.")
    # The disclosure comes from the code once, the model does not repeat it.
    assert sum("KI-Assistent" in line for line in says) == 1
    assert port.actions[-1].kind == "hangup"
    assert not port.transfers(sid)
    assert not port.recordings(sid)

    call = the_call(session, sid)
    assert call.caller_id == "+497215551234"
    assert call.outcome == "completed"
    assert call.intent == "reservation"
    assert call.ended_at is not None
    reservation = session.scalars(
        select(Reservation).where(Reservation.call_id == call.id)
    ).one()
    assert reservation.status == "confirmed"
    # The number came from caller identification, the agent never asked for it.
    assert QUESTIONS["phone"] not in " ".join(says)


def test_a_call_file_from_the_eval_suite_plays_as_a_phone_call(session, tenant):
    port = FakeTelephony()
    case = load_call(CASES / "eskalation_0001_beschwerde.json")

    sid = port.play(case, handler_for(session, tenant, port))

    assert port.transfers(sid) == [TEAM_PHONE]
    assert the_call(session, sid).outcome == "transferred"


# -- edge cases ------------------------------------------------------------------


def test_withheld_number_is_asked_for_and_logged_as_empty(session, tenant):
    case = {
        "transcript": [
            {
                "role": "customer",
                "text": "Ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr.",
            },
            {"role": "customer", "text": "Auf den Namen Mueller."},
            {"role": "customer", "text": "Meine Nummer ist 0721 5551234."},
            {"role": "customer", "text": "Ja, passt so."},
        ]
    }
    port = FakeTelephony()
    sid = port.play(case, handler_for(session, tenant, port))

    assert QUESTIONS["phone"] in " ".join(port.says(sid))
    call = the_call(session, sid)
    assert call.caller_id is None
    assert call.outcome == "completed"


def test_complaint_is_transferred_to_the_team_extension(session, tenant):
    case = {
        "caller_id": "+497215551234",
        "transcript": [
            {
                "role": "customer",
                "text": "Ich will mich beschweren, das Essen war kalt.",
            },
            {"role": "customer", "text": "Hallo? Sind Sie noch da?"},
        ],
    }
    port = FakeTelephony()
    sid = port.play(case, handler_for(session, tenant, port))

    assert port.transfers(sid) == [TEAM_PHONE]
    # Once transferred, the line belongs to the team: no hangup, no further turn.
    assert not port.hangups(sid)
    assert len(port.says(sid)) == 2  # greeting, then the transfer sentence
    assert the_call(session, sid).outcome == "transferred"


def test_failure_in_the_agent_ends_with_the_outage_sentence_and_the_team(
    session, tenant
):
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, llm_factory=lambda s: BrokenLLM())

    sid = port.play(RESERVATION, handler)

    assert port.says(sid)[-1] == SAY_OUTAGE
    assert port.transfers(sid) == [TEAM_PHONE]
    call = the_call(session, sid)
    assert call.outcome == "error"
    assert call.ended_at is not None


def test_database_down_at_call_start_still_reaches_the_team(session, tenant):
    def no_database():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    port = FakeTelephony()
    handler = handler_for(session, tenant, port, session_factory=no_database)

    sid = port.play(RESERVATION, handler)

    # Without a database the number from the environment is the only one left.
    assert port.transfers(sid) == [settings.team_phone]
    assert port.says(sid) == [SAY_OUTAGE]


@pytest.mark.parametrize("mode", ["paused", "shadow"])
def test_ai_switched_off_sends_the_call_straight_to_the_team(session, tenant, mode):
    set_mode(session, tenant.id, mode)
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, llm_factory=lambda s: NoLLM())

    sid = port.play(RESERVATION, handler)

    assert port.transfers(sid) == [TEAM_PHONE]
    # The AI never speaks: no greeting, no transfer sentence.
    assert port.says(sid) == []
    assert the_call(session, sid).outcome == "transferred"


def test_switching_the_ai_off_takes_effect_on_the_next_call(session, tenant):
    """The emergency stop in the tablet header changes `call_mode` in another
    session; the next call must read it fresh, not from a cached row."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    # Held, so the identity map keeps the row (it holds rows only weakly).
    cached = session.get(ServiceConfig, tenant.id)
    assert cached.call_mode == "primary"

    other = Session(session.get_bind())
    set_mode(other, tenant.id, "paused")
    other.close()

    sid = port.play({"transcript": []}, handler)
    assert port.transfers(sid) == [TEAM_PHONE]


def test_caller_hanging_up_mid_call_is_logged_as_abandoned(session, tenant):
    case = {
        "caller_id": "+497215551234",
        "transcript": [
            {
                "role": "customer",
                "text": "Ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr.",
            },
            {"role": "customer", "hangup": True},
            {"role": "customer", "text": "Auf den Namen Mueller."},
        ],
    }
    port = FakeTelephony()
    sid = port.play(case, handler_for(session, tenant, port))

    assert not port.hangups(sid)
    assert len(port.says(sid)) == 2  # greeting and the question after the first turn
    call = the_call(session, sid)
    assert call.outcome == "abandoned"
    assert call.ended_at is not None


def test_keypad_digits_answer_the_open_question(session, tenant):
    """Withheld number, asked for, typed on the keypad (docs/05 §2, step 4)."""
    case = {
        "transcript": [
            {
                "role": "customer",
                "text": "Ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr.",
            },
            {"role": "customer", "text": "Auf den Namen Mueller."},
            {"role": "customer", "dtmf": "0721 5551234#"},
            {"role": "customer", "text": "Ja, passt so."},
        ]
    }
    port = FakeTelephony()
    sid = port.play(case, handler_for(session, tenant, port))

    assert QUESTIONS["phone"] in port.says(sid)[2]
    call = the_call(session, sid)
    assert call.outcome == "completed"
    reservation = session.scalars(
        select(Reservation).where(Reservation.call_id == call.id)
    ).one()
    assert reservation.phone == "+497215551234"


def test_keypad_without_digits_is_ignored(session, tenant):
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")

    handler.on_dtmf("s-1", "#*")

    assert len(port.says("s-1")) == 1  # only the greeting


def test_repeated_start_event_greets_once_and_logs_one_call(session, tenant):
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")

    handler.on_call_started("s-1")
    handler.on_call_started("s-1")  # platform retry

    assert len(port.says("s-1")) == 1
    assert (
        len(
            session.scalars(select(Call).where(Call.external_session_id == "s-1")).all()
        )
        == 1
    )


def test_turn_for_an_unknown_call_goes_to_the_team(session, tenant):
    """After a restart the handler has no state for a running call: rather the
    team than a caller who waits in silence (CLAUDE.md §2 rule 5)."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)

    handler.on_user_turn("lost-session", "Hallo?")

    assert port.says("lost-session") == [SAY_OUTAGE]
    assert port.transfers("lost-session") == [settings.team_phone]


def test_turn_after_the_agent_hung_up_is_ignored(session, tenant):
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    for line in (
        "Ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr.",
        "Auf den Namen Mueller.",
        "Ja, passt so.",
    ):
        handler.on_user_turn("s-1", line)
    assert port.hangups("s-1")
    said = len(port.says("s-1"))

    handler.on_user_turn("s-1", "Ach, noch was.")  # late webhook
    handler.on_call_ended("s-1")
    handler.on_call_ended("s-1")  # repeated end event

    assert len(port.says("s-1")) == said
    assert not port.transfers("s-1")
    assert the_call(session, "s-1").outcome == "completed"


def test_failed_transfer_is_raised_to_the_adapter(session, tenant):
    """If the platform refuses the transfer, the handler cannot reach the team
    itself. It raises, so the adapter answers the webhook with an error and the
    platform's own fallback (docs/02 §5) takes the call."""

    class RefusingTransfer(FakeTelephony):
        def transfer(self, session_id: str, target: str) -> None:
            super().transfer(session_id, target)
            raise ConnectionError("platform refused the transfer")

    set_mode(session, tenant.id, "paused")
    port = RefusingTransfer()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")

    with pytest.raises(ConnectionError):
        handler.on_call_started("s-1")

    call = the_call(session, "s-1")
    assert call.outcome == "error"


def test_recording_is_never_started(session, tenant):
    """No recording before the legal check in docs/09 is ticked (CLAUDE.md §9)."""
    port = FakeTelephony()
    for case in (RESERVATION, load_call(CASES / "eskalation_0001_beschwerde.json")):
        sid = port.play(case, handler_for(session, tenant, port))
        assert not port.recordings(sid)


# -- the fake adapter itself -------------------------------------------------------


def test_load_call_reads_the_eval_case_format(tmp_path):
    path = tmp_path / "anruf.json"
    path.write_text(json.dumps(RESERVATION), encoding="utf-8")

    assert load_call(path)["caller_id"] == "0721 5551234"


def test_load_call_rejects_a_file_without_caller_events(tmp_path):
    path = tmp_path / "leer.json"
    path.write_text('{"transcript": [{"role": "agent", "text": "Hallo"}]}')

    with pytest.raises(ValueError):
        load_call(path)


def test_fake_stops_feeding_turns_once_the_line_is_gone():
    class Recorder:
        def __init__(self, port):
            self.port = port
            self.events: list[tuple[str, str]] = []

        def on_call_started(self, session_id):
            self.events.append(("start", session_id))

        def on_user_turn(self, session_id, text):
            self.events.append(("turn", text))
            self.port.hangup(session_id)

        def on_dtmf(self, session_id, digits):
            self.events.append(("dtmf", digits))

        def on_call_ended(self, session_id):
            self.events.append(("end", session_id))

    port = FakeTelephony()
    recorder = Recorder(port)
    sid = port.play(
        {
            "session_id": f"fake-{uuid.uuid4().hex[:6]}",
            "transcript": [
                {"role": "customer", "text": "eins"},
                {"role": "customer", "text": "zwei"},
            ],
        },
        recorder,
    )

    assert recorder.events == [("start", sid), ("turn", "eins"), ("end", sid)]


def test_complaint_outside_opening_hours_becomes_a_callback(session, tenant):
    """Nobody to transfer to: the known number gets a callback, then the agent
    hangs up (docs/05 §3, "niemand frei")."""
    port = FakeTelephony()
    handler = CallHandler(
        port,
        tenant_id=tenant.id,
        session_factory=lambda: session,
        llm_factory=scripted(session, tenant),
        now=CLOSED,
    )
    sid = port.play(
        {
            "caller_id": "+497215551234",
            "transcript": [
                {"role": "customer", "text": "Ich will mich beschweren."},
            ],
        },
        handler,
    )

    assert not port.transfers(sid)
    assert port.hangups(sid)
    # The callback sentence says nothing to part with: the code adds the goodbye.
    assert port.says(sid)[-2:] == [SAY_NOTED, FAREWELL]
    assert port.actions[-1].kind == "hangup"
    call = the_call(session, sid)
    assert call.outcome == "callback"
    callback = session.scalars(
        select(Callback).where(Callback.call_id == call.id)
    ).one()
    assert callback.phone == "+497215551234"


def test_hangup_comes_after_a_goodbye_said_once(session, tenant):
    """The agent's own closing sentence already says goodbye ("bis dann"):
    the code hangs up after it and does not add a second goodbye."""
    port = FakeTelephony()
    sid = port.play(RESERVATION, handler_for(session, tenant, port))

    assert port.says(sid)[-1].endswith(SAY_CONFIRMED)
    assert FAREWELL not in port.says(sid)
    assert [a.kind for a in port.actions[-2:]] == ["say", "hangup"]


def test_every_hangup_in_the_eval_suite_follows_a_goodbye(session, tenant):
    """Whatever the case, the last thing the caller hears before the line goes
    dead is a goodbye."""
    goodbyes = ("Auf Wiederhören", "bis dann", "bis gleich")
    hung_up = 0
    for path in sorted(CASES.glob("*.json")):
        port = FakeTelephony()
        sid = port.play(load_call(path), handler_for(session, tenant, port))
        if not port.hangups(sid):
            continue
        hung_up += 1
        last = port.actions[-2]
        assert last.kind == "say", path.name
        assert any(word in (last.value or "") for word in goodbyes), path.name
    assert hung_up > 0
