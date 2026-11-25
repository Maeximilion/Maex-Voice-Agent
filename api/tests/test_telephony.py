"""telephony/: port, provider-neutral call handler and the fake adapter (T-1.13).

A call file is played through the fake adapter into the handler, which drives the
same agent core as `sim/` and the evals. Checked are what the line would be
told to do (say, transfer, hang up) and what lands in the database, never the
wording of the model (docs/08 §3) - except the sentences the caller must hear:
the AI disclosure at the start and the outage sentence before a transfer.
"""

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from api.agent.llm import FakeLLM, LLMError, LLMTurn, Usage
from api.agent.loop import (
    SAY_BOOKED_GOODBYE,
    SAY_CALLBACK_NUMBER,
    SAY_TABLE_BOOKED,
    SUMMARY_ALLERGENS,
    SUMMARY_READBACK_UNANSWERED,
    ConversationLoop,
)
from api.agent.state import ConversationState
from api.config import settings
from api.domain.callbacks.create import SAY_NOTED, SUMMARY_BOOKED
from api.domain.callbacks.transfer import (
    SUMMARY_HUNG_UP,
    SUMMARY_MISSED,
    SUMMARY_NOTE_TO_CLEAR,
)
from api.domain.menu.importer import apply
from api.domain.menu.items import active_count
from api.domain.menu.wishes import speaks_of_health
from api.models import (
    Call,
    Callback,
    CallLimitWindow,
    ComplaintRule,
    MenuItem,
    Order,
    Reservation,
    ServiceConfig,
)
from api.telephony import handler as handler_module
from api.telephony.adapters.fake import FakeTelephony, load_call
from api.telephony.handler import (
    FAREWELL,
    SAY_OUTAGE,
    SAY_OUTAGE_FIRST,
    STILL_THERE,
    CallHandler,
    call_cost_cents,
    key_terms,
)
from api.telephony.speech import SpeechUsage
from evals.runner import menu_plan
from scripts.seed import seed
from sim.scripted_llm import QUESTIONS, ScriptedLLM
from sim.session import menu_numbers, resolve_tenant

# The core says the booking after a confirm (agent/loop.py), not the stand-in.
SAY_CONFIRMED = f"{SAY_TABLE_BOOKED} {SAY_BOOKED_GOODBYE}"
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


def handler_for(
    session, tenant, port, *, llm_factory=None, session_factory=None, now=NOW, **more
):
    return CallHandler(
        port,
        tenant_id=tenant.id,
        session_factory=session_factory or (lambda: session),
        llm_factory=llm_factory or scripted(session, tenant),
        now=now,
        **more,
    )


def the_call(session, session_id: str) -> Call:
    return session.scalars(
        select(Call).where(Call.external_session_id == session_id)
    ).one()


class BrokenLLM:
    """A model, or anything behind it, that fails in the middle of a call."""

    def next_turn(
        self, system_prompt, state_json, input_text, *, timeout=None
    ) -> LLMTurn:
        raise RuntimeError("model endpoint down")


class NoLLM:
    """Fails the test if the agent is asked at all."""

    def next_turn(
        self, system_prompt, state_json, input_text, *, timeout=None
    ) -> LLMTurn:
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


@pytest.mark.parametrize("silent", [True, False])
def test_a_call_without_caller_audio_is_marked_in_the_call_log(session, tenant, silent):
    """#651 (Maxi, 11.11.2026): the call ends as a silent one, the team is not
    rung, and the call log carries the note."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")

    handler.on_call_ended(
        "s-1", SpeechUsage(no_caller_audio=silent), caller_hung_up=True
    )

    note = the_call(session, "s-1").line_note
    assert note == ("no_caller_audio" if silent else None)
    assert not port.transfers("s-1")


def test_a_model_outage_with_nobody_to_reach_and_a_withheld_number_leaves_a_card(
    session, tenant
):
    """#652 on the line: the model is down, no service is open and the caller
    id is withheld. The line asks for a number, played to its end so the
    digits behind it are that number, stays on the call for the answer, and
    hangs up once the card is written. Before, it said to call again later
    and hung up: nobody rang and nothing was on the tablet (rule 5)."""

    class Down:
        def next_turn(self, system_prompt, state_json, input_text, *, timeout=None):
            raise LLMError("model call failed: ConnectError")

    port = FakeTelephony()
    handler = handler_for(
        session, tenant, port, llm_factory=lambda s: Down(), now=CLOSED
    )

    sid = port.play(
        {
            "transcript": [
                {"role": "customer", "text": "Einen Tisch für vier bitte"},
                {"role": "customer", "text": "0721 5551234"},
            ]
        },
        handler,
    )

    asked = [a for a in port.actions if a.value == SAY_CALLBACK_NUMBER]
    assert len(asked) == 1 and asked[0].interruptible is False
    assert not port.transfers(sid) and port.hangups(sid)
    assert port.says(sid)[-2:] == [SAY_NOTED, FAREWELL]
    card = session.scalars(select(Callback)).one()
    assert card.phone == "+497215551234"
    assert "Einen Tisch für vier bitte" in card.summary
    assert the_call(session, sid).outcome == "callback"


def test_database_down_at_call_start_still_reaches_the_team(session, tenant):
    def no_database():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    port = FakeTelephony()
    handler = handler_for(session, tenant, port, session_factory=no_database)

    sid = port.play(RESERVATION, handler)

    # Without a database the number from the environment is the only one left.
    assert port.transfers(sid) == [settings.team_phone]
    # The first sentence of the call: the AI says it is one (docs/09 L1), and a
    # team that does not answer leaves it the sentence after the leg too.
    assert port.says(sid) == [SAY_OUTAGE_FIRST]


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


def test_a_call_above_the_limit_rings_the_team_until_a_place_is_free(session, tenant):
    """D16, T-10.9: with a limit of one, a second call while the first is open
    goes to the team at once and silently; once the first leg has ended, the
    agent takes the next call again."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")

    port.open("s-2", caller_id="+497215551234")
    handler.on_call_started("s-2")
    assert port.transfers("s-2") == [TEAM_PHONE] and port.says("s-2") == []
    assert port.says("s-1") and port.transfers("s-1") == []

    handler.on_call_ended("s-1")
    handler.on_call_ended("s-2")
    port.open("s-3", caller_id="+497215551234")
    handler.on_call_started("s-3")
    assert port.says("s-3") and port.transfers("s-3") == []


def test_a_failed_call_keeps_its_place_until_its_leg_has_ended(session, tenant):
    """Review of T-10.9: the outage sentence still plays on the AI leg after a
    failure, so the place is free only once the line reports the end."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_voice_failure("s-1")
    assert port.transfers("s-1") == [TEAM_PHONE]

    port.open("s-2", caller_id="+497215551234")
    handler.on_call_started("s-2")
    assert port.transfers("s-2") == [TEAM_PHONE] and port.says("s-2") == []

    handler.on_call_ended("s-1")
    handler.on_call_ended("s-2")
    port.open("s-3", caller_id="+497215551234")
    handler.on_call_started("s-3")
    assert port.says("s-3") and port.transfers("s-3") == []


def test_the_handler_reads_the_windows_at_its_own_time(session, tenant):
    """A window of two around the handler's fixed time (Tuesday 18:00) lets a
    second call reach the agent (review of T-10.9: the wall clock was read)."""
    session.add(
        CallLimitWindow(
            tenant_id=tenant.id,
            weekday=NOW.weekday(),
            starts_at=clock_time(17, 0),
            ends_at=clock_time(22, 0),
            max_calls=2,
        )
    )
    session.commit()
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    for sid in ("s-1", "s-2"):
        port.open(sid, caller_id="+497215551234")
        handler.on_call_started(sid)

    assert port.says("s-2") and port.transfers("s-2") == []


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


def test_the_greeting_names_where_the_privacy_notice_is(session, tenant):
    """docs/09 L4: in the call the AI notice and where the full notice is, the
    address from service_config, never from the code."""
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant.id)
        .values(privacy_notice_address="testbetrieb.example/datenschutz")
    )
    session.commit()
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")

    handler.on_call_started("s-1")

    assert port.says("s-1") == [
        "Guten Tag, hier ist der KI-Assistent von Testbetrieb. "
        "Hinweise zum Datenschutz finden Sie unter testbetrieb.example/datenschutz. "
        "Was kann ich für Sie tun?"
    ]


def test_spaces_around_the_address_are_not_read_out(session, tenant):
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant.id)
        .values(privacy_notice_address=" testbetrieb.example/datenschutz\n")
    )
    session.commit()
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")

    handler.on_call_started("s-1")

    assert "unter testbetrieb.example/datenschutz. Was" in port.says("s-1")[0]


def test_without_an_address_the_greeting_leaves_the_pointer_out(session, tenant):
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")

    handler.on_call_started("s-1")

    assert port.says("s-1") == [
        "Guten Tag, hier ist der KI-Assistent von Testbetrieb. Was kann ich für Sie tun?"
    ]


def test_a_changed_address_is_said_from_the_next_call(session, tenant):
    """Read with the routing at the start of every call, like the mode."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    with Session(session.get_bind()) as other:
        other.execute(
            update(ServiceConfig)
            .where(ServiceConfig.tenant_id == tenant.id)
            # Two calls at once: the limit of one would send the second to
            # the team (T-10.9).
            .values(
                privacy_notice_address="neu.example/datenschutz", max_concurrent_calls=2
            )
        )
        other.commit()

    port.open("s-2", caller_id="+497215551234")
    handler.on_call_started("s-2")

    assert "Datenschutz" not in port.says("s-1")[0]
    assert "neu.example/datenschutz" in port.says("s-2")[0]


def test_a_yes_to_are_you_still_there_does_not_take_the_offer_of_the_team(
    session, tenant
):
    """Codex PR #372, P1: the guest asks "Sind Sie ein Mensch?", hears the
    answer with the offer of the team, says nothing, and answers the line's
    "Sind Sie noch da?" with "Ja". That yes is no wish for the team."""
    port = FakeTelephony()
    handler = handler_for(
        session,
        tenant,
        port,
        llm_factory=lambda s: FakeLLM([LLMTurn(say="Wie kann ich helfen?")]),
    )
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")

    handler.on_user_turn("s-1", "Sind Sie ein Mensch?")
    handler.on_silence("s-1")
    handler.on_user_turn("s-1", "Ja")

    assert "Ich bin ein KI-Assistent, kein Mensch." in port.says("s-1")[1]
    assert port.says("s-1")[2] == STILL_THERE[0]
    assert port.says("s-1")[-1] == "Wie kann ich helfen?"
    assert not port.transfers("s-1")


def test_the_answer_to_the_question_for_a_human_is_played_to_the_end(session, tenant):
    """Ultrareview of PR #372: the core counts the offer of the team as heard
    (`last_say`), so the guest's talk over it must not cut it off; a yes to an
    offer they never heard would hand the call over."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, llm_factory=lambda s: NoLLM())
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")

    handler.on_user_turn("s-1", "Sind Sie ein Mensch?")

    assert "kein Mensch" in port.says("s-1")[1]
    assert port.interruptible("s-1") == []


def test_repeated_start_event_greets_once_and_logs_one_call(session, tenant):
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")

    handler.on_call_started("s-1")
    handler.on_call_started("s-1")  # repeated start event

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

    assert port.says("lost-session") == [SAY_OUTAGE_FIRST]
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

    handler.on_user_turn("s-1", "Ach, noch was.")  # late event
    handler.on_call_ended("s-1")
    handler.on_call_ended("s-1")  # repeated end event

    assert len(port.says("s-1")) == said
    assert not port.transfers("s-1")
    assert the_call(session, "s-1").outcome == "completed"


def test_failed_transfer_is_raised_to_the_call_session(session, tenant):
    """If the line refuses the transfer, the handler cannot reach the team
    itself. It raises, so the call session ends as failed and the dialplan rings
    the team (docs/20 §5)."""

    class RefusingTransfer(FakeTelephony):
        def transfer(self, session_id: str, target: str) -> None:
            super().transfer(session_id, target)
            raise ConnectionError("the line refused the transfer")

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

        def on_silence(self, session_id):
            pass

        def on_talked_over(self, session_id, text):
            pass

        def on_voice_failure(self, session_id):
            pass

        def on_call_ended(self, session_id, speech=None, **_):
            self.events.append(("end", session_id))

        def on_team_missed(self, session_id):
            return False

        def on_recording_stopped(self, session_id):
            return False

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


def test_model_on_the_phone_line_gets_the_tool_reference(session, tenant):
    """The same prompt as in the text phone and the evals: a model that works
    there must not meet the phone line without the tools' arguments (own
    review of PR #235)."""
    prompts = []

    class Listening:
        def next_turn(
            self, system_prompt, state_json, input_text, *, timeout=None
        ) -> LLMTurn:
            prompts.append(system_prompt)
            return LLMTurn(say="Auf Wiederhören.")

    port = FakeTelephony()
    port.play(
        RESERVATION,
        handler_for(session, tenant, port, llm_factory=lambda s: Listening()),
    )

    assert prompts
    assert "## check_slot" in prompts[0]
    assert "## confirm" in prompts[0]


# -- the call time limit ---------------------------------------------------------
# The limit is the tenant's (`service_config.max_call_seconds`), read with the
# routing at the start of the call. The setting of the same name is only what
# the seed writes into a new tenant (audit 12.10.2026: the phone line ran every
# call on the setting).

REPLY = "Was darf es sein?"


class Talks:
    def next_turn(
        self, system_prompt, state_json, input_text, *, timeout=None
    ) -> LLMTurn:
        return LLMTurn(say=REPLY)


class Clock:
    """The seconds a call has run, set by the test: no test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def set_limit(session, tenant_id, seconds: int) -> None:
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(max_call_seconds=seconds)
    )
    session.commit()


def speak_after(handler, port, clock, seconds: float, sid: str = "s-1") -> None:
    """A call in which the guest says something `seconds` after it began."""
    port.open(sid, caller_id="+497215551234")
    handler.on_call_started(sid)
    clock.now += seconds
    handler.on_user_turn(sid, "Hallo")


def limited_call(
    session, tenant, seconds: float, *, llm=Talks, now=NOW, sid: str = "s-1"
):
    port, clock = FakeTelephony(), Clock()
    handler = handler_for(
        session, tenant, port, llm_factory=lambda s: llm(), clock=clock, now=now
    )
    speak_after(handler, port, clock, seconds, sid=sid)
    handler.on_call_ended(sid)  # the line reports the end, as any adapter does
    return port


def test_call_past_the_tenants_limit_goes_to_the_team(session, tenant):
    set_limit(session, tenant.id, 30)
    assert settings.max_call_seconds > 31  # the setting would let it go on

    port = limited_call(session, tenant, 31, llm=NoLLM)

    assert port.transfers("s-1") == [TEAM_PHONE]
    assert not port.hangups("s-1")
    assert the_call(session, "s-1").outcome == "transferred"


def test_call_within_the_tenants_limit_goes_on(session, tenant):
    set_limit(session, tenant.id, 30)

    port = limited_call(session, tenant, 29)

    assert port.says("s-1")[-1] == REPLY
    assert not port.transfers("s-1")


def test_a_tenant_may_allow_longer_calls_than_the_setting(session, tenant):
    set_limit(session, tenant.id, settings.max_call_seconds + 300)

    port = limited_call(session, tenant, settings.max_call_seconds + 60)

    assert port.says("s-1")[-1] == REPLY
    assert not port.transfers("s-1")


def test_two_tenants_keep_their_own_limits(session, tenant):
    seed(session, tenant_name="Zweitbetrieb", timezone="Europe/Berlin")
    second = resolve_tenant(session, "Zweitbetrieb")
    other = SimpleNamespace(id=second.id, timezone=second.timezone, name=second.name)
    set_mode(session, other.id, "primary")
    set_limit(session, tenant.id, 30)
    set_limit(session, other.id, 600)

    short = limited_call(session, tenant, 100, llm=NoLLM)
    long = limited_call(session, other, 100, sid="s-2")

    # Handed over at the limit, not by the outage path of a model that failed.
    assert the_call(session, "s-1").outcome == "transferred"
    assert short.transfers("s-1") == [TEAM_PHONE]
    assert long.says("s-2")[-1] == REPLY
    assert not long.transfers("s-2")


def test_a_changed_limit_takes_effect_on_the_next_call(session, tenant):
    """Read once with the routing, like the mode: the call that is running
    keeps the limit it began with."""
    set_limit(session, tenant.id, 600)
    port, clock = FakeTelephony(), Clock()
    handler = handler_for(
        session, tenant, port, llm_factory=lambda s: Talks(), clock=clock
    )
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    set_limit(session, tenant.id, 30)

    clock.now += 100
    handler.on_user_turn("s-1", "Hallo")

    assert port.says("s-1")[-1] == REPLY
    assert not port.transfers("s-1")
    handler.on_call_ended("s-1")

    speak_after(handler, port, clock, 31, sid="s-2")

    assert port.transfers("s-2") == [TEAM_PHONE]
    assert REPLY not in port.says("s-2")


def test_past_the_limit_with_nobody_there_a_callback_is_noted(session, tenant):
    """Between the services nobody takes the transfer: the guest gets a
    callback under the number they called from, and the line is closed."""
    set_limit(session, tenant.id, 30)

    port = limited_call(session, tenant, 31, llm=NoLLM, now=CLOSED)

    assert not port.transfers("s-1")
    assert port.actions[-1].kind == "hangup"
    callback = session.scalars(select(Callback)).one()
    assert callback.phone == "+497215551234"


def test_an_answer_that_comes_after_the_tenants_limit_is_not_spoken(session, tenant):
    """The limit is read again once the model has answered (Codex PR #208)."""
    set_limit(session, tenant.id, 30)
    port, clock = FakeTelephony(), Clock()

    class Slow:
        def next_turn(
            self, system_prompt, state_json, input_text, *, timeout=None
        ) -> LLMTurn:
            clock.now += 60
            return LLMTurn(say=REPLY)

    handler = handler_for(
        session, tenant, port, llm_factory=lambda s: Slow(), clock=clock
    )
    speak_after(handler, port, clock, 10)

    assert REPLY not in port.says("s-1")
    assert port.transfers("s-1") == [TEAM_PHONE]


# -- the own voice layer: what may be talked over, silence, a failing ear (T-10.4) --


def _reservation_until_the_read_back(*after: dict) -> dict:
    return {**RESERVATION, "transcript": [*RESERVATION["transcript"][:3], *after]}


SILENCE = {"role": "customer", "silence": True}
YES = {"role": "customer", "text": "Ja, passt so."}


def test_a_question_may_be_talked_over_the_disclosure_and_the_read_back_may_not(
    session, tenant
):
    port = FakeTelephony()
    sid = port.play(RESERVATION, handler_for(session, tenant, port))

    says, open_to_talk = port.says(sid), port.interruptible(sid)
    assert says[0] not in open_to_talk  # the AI disclosure
    assert QUESTIONS["guest_name"] in " ".join(open_to_talk)
    read_back = next(line for line in says if line.endswith("Passt das so?"))
    assert read_back not in open_to_talk
    assert says[-1] not in open_to_talk  # the confirmation before the hangup


def test_a_sentence_the_core_counts_as_heard_is_not_talked_over(session, tenant):
    """Codex PR #286: the core takes the sentence that ends a turn as heard. If
    it asked for the guest's number, the digits of the next sentence are that
    number; if it named a dish, that dish may enter the order. A guest who
    talked over such a sentence would be measured against words they never
    heard, so these sentences are played to the end."""
    port = FakeTelephony()
    case = {
        "transcript": [
            {
                "role": "customer",
                "text": "Ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr.",
            },
            {"role": "customer", "text": "Auf den Namen Mueller."},
        ]
    }
    sid = port.play(case, handler_for(session, tenant, port))
    says, open_to_talk = port.says(sid), port.interruptible(sid)
    assert QUESTIONS["phone"] in says[-1]  # the number is withheld
    assert says[-1] not in open_to_talk
    assert QUESTIONS["guest_name"] in " ".join(open_to_talk)


def _a_rule(session, tenant) -> str:
    """Invented for these tests, no rule of any restaurant. The offer it adds.
    The line 0721 5551234 ordered an hour before: a rule is offered only for
    such an order (#888 S8)."""
    offer = "Testangebot, stimmt das so für Sie?"
    earlier = Call(
        tenant_id=tenant.id,
        external_session_id=uuid.uuid4().hex,
        started_at=NOW - timedelta(hours=1),
        delete_after=NOW.date(),
    )
    session.add(earlier)
    session.flush()
    session.add(
        Order(
            tenant_id=tenant.id,
            call_id=earlier.id,
            type="pickup",
            status="confirmed",
            phone="+497215551234",
            booked_on_own_line=True,
            customer_name="Mueller",
            items_total_cents=1000,
            total_cents=1000,
            idempotency_key=uuid.uuid4().hex,
            created_at=NOW - timedelta(hours=1),
        )
    )
    session.add(
        ComplaintRule(
            tenant_id=tenant.id,
            name="Testregel",
            phrases=["kalt"],
            remedy_cents=345,
            offer_say=offer,
            granted_say="Testzusage.",
            active=True,
        )
    )
    session.commit()
    return offer


COLD_FOOD = {"role": "customer", "text": "Ich bin unzufrieden, das Essen war kalt."}


def test_a_remedy_offer_is_not_talked_over_and_its_yes_ends_the_call(session, tenant):
    """T-10.11: a yes books money, so it counts only for an offer heard to the
    end, like a read-back."""
    offer = _a_rule(session, tenant)
    port = FakeTelephony()
    case = {
        "caller_id": "0721 5551234",
        "transcript": [COLD_FOOD, {"role": "customer", "text": "Ja."}],
    }
    sid = port.play(case, handler_for(session, tenant, port))

    assert offer in port.says(sid)
    assert offer not in port.interruptible(sid)
    assert "Testzusage." in port.says(sid)
    assert not port.transfers(sid)
    assert port.actions[-1].kind == "hangup"
    assert the_call(session, sid).outcome == "callback"


def test_a_guest_who_hangs_up_on_a_remedy_offer_stays_with_the_team(session, tenant):
    """Rule 5: the complaint no longer goes to the team at once, so the end of
    the line must not lose it. Nothing is booked."""
    _a_rule(session, tenant)
    port = FakeTelephony()
    case = {"caller_id": "0721 5551234", "transcript": [COLD_FOOD]}
    sid = port.play(case, handler_for(session, tenant, port))

    call = the_call(session, sid)
    assert call.outcome == "callback"
    card = session.scalars(select(Callback).where(Callback.call_id == call.id)).one()
    assert card.reason == "complaint"
    assert "Essen war kalt" in card.summary


def test_a_yes_to_are_you_still_there_is_no_yes_to_a_remedy_offer(session, tenant):
    _a_rule(session, tenant)
    port = FakeTelephony()
    case = {"caller_id": "0721 5551234", "transcript": [COLD_FOOD, SILENCE, YES]}
    sid = port.play(case, handler_for(session, tenant, port))

    assert port.transfers(sid) == [TEAM_PHONE]
    assert "Testzusage." not in port.says(sid)


def test_what_is_said_before_a_transfer_is_not_talked_over(session, tenant):
    port = FakeTelephony()
    case = load_call(CASES / "eskalation_0001_beschwerde.json")
    sid = port.play(case, handler_for(session, tenant, port))
    assert port.transfers(sid) == [TEAM_PHONE]
    assert port.interruptible(sid) == []


def test_a_silent_line_is_asked_twice_then_the_agent_says_goodbye(session, tenant):
    port = FakeTelephony()
    case = {"transcript": [SILENCE, SILENCE, SILENCE, YES]}
    sid = port.play(case, handler_for(session, tenant, port))

    assert port.says(sid)[1:] == [*STILL_THERE, FAREWELL]
    assert port.interruptible(sid) == list(STILL_THERE)
    assert port.actions[-1].kind == "hangup" and not port.transfers(sid)
    assert the_call(session, sid).ended_at is not None


def test_a_caller_who_answers_is_asked_from_the_start_the_next_time(session, tenant):
    port = FakeTelephony()
    case = {
        "transcript": [
            SILENCE,
            SILENCE,
            {"role": "customer", "text": "Ich haette gern einen Tisch."},
            SILENCE,
            SILENCE,
            SILENCE,
        ]
    }
    sid = port.play(case, handler_for(session, tenant, port))
    asked = [line for line in port.says(sid) if line in STILL_THERE]
    assert asked == [*STILL_THERE, *STILL_THERE]
    assert port.says(sid)[-1] == FAREWELL


def test_a_yes_to_are_you_still_there_confirms_nothing(session, tenant):
    """Hard rule 3: after the read-back the guest is silent, the code asks "Sind
    Sie noch da?", and the "Ja" to that is no yes to the reservation. The draft
    is read out again, and only the yes to it books."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    sid = port.play(_reservation_until_the_read_back(SILENCE, YES), handler)

    call = the_call(session, sid)
    reservation = session.scalars(
        select(Reservation).where(Reservation.call_id == call.id)
    ).one()
    assert reservation.status == "draft"
    says = port.says(sid)
    asked_at = says.index(STILL_THERE[0])
    read_back = says[asked_at - 1]
    assert says[asked_at + 1 :] == [read_back]  # read out again, nothing booked
    assert SAY_CONFIRMED not in says

    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    sid = port.play(_reservation_until_the_read_back(SILENCE, YES, YES), handler)
    booked = session.scalars(
        select(Reservation).where(Reservation.call_id == the_call(session, sid).id)
    ).one()
    assert booked.status == "confirmed" and port.says(sid)[-1] == SAY_CONFIRMED


def test_a_guest_who_stays_silent_after_the_read_back_books_nothing(session, tenant):
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    case = _reservation_until_the_read_back(SILENCE, SILENCE, SILENCE)
    sid = port.play(case, handler)
    call = the_call(session, sid)
    reservation = session.scalars(
        select(Reservation).where(Reservation.call_id == call.id)
    ).one()
    assert reservation.status == "draft" and port.actions[-1].kind == "hangup"
    assert port.says(sid)[-1] == FAREWELL


def test_a_failing_ear_or_mouth_ends_with_the_outage_sentence_and_the_team(
    session, tenant
):
    port = FakeTelephony()
    case = {
        "transcript": [
            {"role": "customer", "text": "Ich haette gern einen Tisch."},
            {"role": "customer", "voice_failure": True},
            YES,
        ]
    }
    sid = port.play(case, handler_for(session, tenant, port))

    assert port.says(sid)[-1] == SAY_OUTAGE
    assert port.transfers(sid) == [TEAM_PHONE]
    assert the_call(session, sid).outcome == "error"


def test_silence_or_a_failure_on_an_unknown_call_goes_to_the_team(session, tenant):
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    handler.on_silence("lost-1")
    handler.on_voice_failure("lost-2")
    # The tenant's number is not known for a call this process never saw.
    assert port.transfers("lost-1") == port.transfers("lost-2") == [settings.team_phone]


# -- a whole call through the session: ear, core and mouth (T-10.4) -----------------


# The first sentence of RESERVATION.
WISH = "Guten Tag, ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr."


class _Wire:
    """A call on the own voice layer with fakes for ear, mouth and line: the
    handler and the core are the real ones."""

    def __init__(
        self, session, tenant, *, refuses_transfer: bool = False, llm_factory=None
    ):
        from api.telephony.adapters.fake_speech import FakeEar, FakeLine, FakeMouth
        from api.telephony.session import CallSession, Lines

        self.now = 50.0
        self.line, self.ear, self.lines = FakeLine(), FakeEar(), Lines()
        self.line.refuses_transfer = refuses_transfer
        self.call = CallSession(
            "voice-1",
            line=self.line,
            ear=self.ear,
            mouth=FakeMouth(),
            events=handler_for(session, tenant, self.lines, llm_factory=llm_factory),
            caller_id="0721 5551234",
            clock=lambda: self.now,
            silence_seconds=6.0,
        )
        self.lines.add(self.call)
        self.call.start()
        self.call.drain()

    def played(self) -> None:
        """The line has played everything that was queued."""
        self.now += 3.0
        self.call.played(self.line.mark)
        self.call.drain()

    def says(self, text: str, **more) -> None:
        from api.telephony.speech import Heard

        self.now += 1.0
        self.ear.hear(Heard(text, ended_at=self.now, **more))
        self.call.drain()

    def until_the_read_back(self) -> str:
        self.played()
        self.says(WISH)
        self.played()
        self.says("Auf den Namen Mueller.")
        return self.line.played()[-1]


def _reservation_of(session, session_id: str) -> Reservation:
    call = the_call(session, session_id)
    return session.scalars(
        select(Reservation).where(Reservation.call_id == call.id)
    ).one()


def test_a_reservation_on_the_wire_is_booked_by_the_yes_after_the_read_back(
    session, tenant
):
    wire = _Wire(session, tenant)
    assert wire.line.kinds() == ["answer", "play"]  # the greeting answers the line
    read_back = wire.until_the_read_back()
    assert read_back.endswith("Passt das so?")

    wire.played()
    wire.says("Ja, passt so.")
    assert wire.line.played()[-1] == SAY_CONFIRMED
    assert _reservation_of(session, "voice-1").status == "confirmed"
    # The hangup waits until the caller has heard the confirmation.
    assert "hangup" not in wire.line.kinds() and wire.call.outcome == "open"
    wire.played()
    assert wire.line.kinds()[-1] == "hangup" and wire.call.outcome == "hangup"
    assert the_call(session, "voice-1").outcome == "completed"


def test_a_yes_spoken_into_the_read_back_books_nothing(session, tenant):
    """Hard rule 3 on the wire: the guest says "Ja" while the read-back is still
    playing. It is no answer; the yes after the read-back is."""
    wire = _Wire(session, tenant)
    read_back = wire.until_the_read_back()
    wire.says("Ja, passt so.")  # the read-back has not been played to the end
    assert wire.line.played()[-1] == read_back
    assert "flush" not in wire.line.kinds()
    assert _reservation_of(session, "voice-1").status == "draft"

    wire.played()
    wire.says("Ja, passt so.")
    assert _reservation_of(session, "voice-1").status == "confirmed"


def test_an_allergy_spoken_into_the_read_back_is_read_back_on_the_wire(session, tenant):
    """#1003 on the wire: the allergy said into the read-back makes the
    table be read back again with the note, behind the first read-back; a
    yes said into that one still books nothing, the yes after it books the
    table with the note (rule 3, L13)."""
    wire = _Wire(session, tenant)
    read_back = wire.until_the_read_back()
    wire.says("Ich habe eine Nussallergie.")  # still playing
    again = wire.line.played()[-1]
    assert again != read_back and again.endswith("Passt das so?")
    assert "flush" not in wire.line.kinds()

    wire.says("Ja, passt so.")  # into the second read-back
    assert {table.status for table in _tables_of(session, "voice-1")} == {"draft"}
    wire.played()
    wire.says("Ja, passt so.")

    # The first draft stays one: a draft the guest moved on from is never booked.
    [table] = [t for t in _tables_of(session, "voice-1") if t.status != "draft"]
    assert table.status == "confirmed"
    assert speaks_of_health(table.note or "")


def _tables_of(session, session_id: str) -> list[Reservation]:
    call = the_call(session, session_id)
    return list(
        session.scalars(select(Reservation).where(Reservation.call_id == call.id))
    )


def test_a_yes_that_was_queued_before_the_read_back_books_nothing(session, tenant):
    """Hard rule 3 on the wire (Codex PR #286): the guest gives the name and
    says "Ja" at once, before the worker has handled the name. The core has
    "said" the read-back by then, but the guest has not heard a word of it."""
    from api.telephony.speech import Heard

    wire = _Wire(session, tenant)
    wire.played()
    wire.says(WISH)
    wire.played()
    wire.now += 1.0
    wire.ear.hear(Heard("Auf den Namen Mueller.", ended_at=wire.now))
    wire.ear.hear(Heard("Ja, passt so.", ended_at=wire.now + 0.5))
    wire.call.drain()
    assert wire.line.played()[-1].endswith("Passt das so?")
    assert _reservation_of(session, "voice-1").status == "draft"

    wire.played()
    wire.says("Ja, passt so.")
    assert _reservation_of(session, "voice-1").status == "confirmed"


def test_line_noise_after_the_read_back_is_no_yes(session, tenant):
    wire = _Wire(session, tenant)
    read_back = wire.until_the_read_back()
    wire.played()
    wire.says("Ja", speech=False)  # words an engine made of noise
    assert wire.line.played()[-1] == read_back
    assert _reservation_of(session, "voice-1").status == "draft"


def test_a_silent_caller_on_the_wire_is_asked_twice_and_then_let_go(session, tenant):
    wire = _Wire(session, tenant)
    wire.until_the_read_back()
    for _ in range(3):
        wire.played()
        wire.now += 6.5
        wire.call.drain()
    assert wire.line.played()[-3:] == [*STILL_THERE, FAREWELL]
    wire.played()
    assert wire.call.outcome == "hangup"
    assert _reservation_of(session, "voice-1").status == "draft"


def test_an_ear_that_breaks_on_the_wire_sends_the_call_to_the_team(session, tenant):
    wire = _Wire(session, tenant)
    wire.played()
    wire.ear.broken = True
    wire.call.audio(b"\x00\x00")
    wire.call.drain()
    assert wire.line.played()[-1] == SAY_OUTAGE
    wire.played()
    assert wire.line.actions[-1] == ("transfer", TEAM_PHONE)
    assert wire.call.outcome == "transfer"
    assert the_call(session, "voice-1").outcome == "error"


def test_a_call_the_agent_does_not_take_rings_the_team_unanswered(session, tenant):
    """Mode `paused`: the transfer comes before any sentence, so the line is
    never answered by us (docs/20 §4, call start)."""
    set_mode(session, tenant.id, "paused")
    wire = _Wire(session, tenant)
    assert wire.line.actions == [("transfer", TEAM_PHONE)]
    assert wire.call.outcome == "transfer"


# -- the call log closes on the end of the leg (T-1.11 part 2) ------------------------


def _confirmed_and_saying_goodbye(session, tenant) -> _Wire:
    """A reservation on the wire, confirmed: the agent's last sentence is
    queued, and the hangup waits for it."""
    wire = _Wire(session, tenant)
    wire.until_the_read_back()
    wire.played()
    wire.says("Ja, passt so.")
    assert wire.call.outcome == "open"
    return wire


def _logged(session, session_id: str) -> Call:
    session.expire_all()
    return the_call(session, session_id)


def test_the_call_log_closes_once_the_last_sentence_was_played(session, tenant):
    """Not when the core asks for the hangup: the guest is still hearing the
    confirmation then (Codex on PR #286)."""
    wire = _confirmed_and_saying_goodbye(session, tenant)
    assert _logged(session, "voice-1").ended_at is None

    wire.played()
    call = _logged(session, "voice-1")
    assert call.ended_at is not None and call.outcome == "completed"


def test_a_caller_who_hangs_up_during_the_goodbye_closes_the_log_then(session, tenant):
    wire = _confirmed_and_saying_goodbye(session, tenant)
    wire.call.closed()
    wire.call.drain()
    assert wire.call.outcome == "closed"
    call = _logged(session, "voice-1")
    assert call.ended_at is not None and call.outcome == "completed"


def test_a_goodbye_the_line_never_reports_goes_to_the_team_as_an_error(session, tenant):
    """Whether the guest heard the last sentence is not known: the call is not
    hung up but goes to the team, with the outage sentence, and the log says
    so. The booking stands (rule 5)."""
    wire = _confirmed_and_saying_goodbye(session, tenant)
    wire.now += 60.0  # past the sentence and the grace for its report
    wire.call.drain()
    assert wire.line.played()[-1] == SAY_OUTAGE
    wire.now += 60.0  # the outage sentence is not reported either
    wire.call.drain()

    assert wire.line.actions[-1] == ("transfer", TEAM_PHONE)
    assert "hangup" not in wire.line.kinds()
    assert _logged(session, "voice-1").outcome == "error"
    assert _reservation_of(session, "voice-1").status == "confirmed"


def test_a_transfer_the_line_refuses_is_logged_as_an_error(session, tenant):
    """The session ends as failed and the dialplan rings the team; the log
    must not say `transferred` (Codex on PR #286)."""
    set_mode(session, tenant.id, "paused")
    wire = _Wire(session, tenant, refuses_transfer=True)
    assert wire.call.outcome == "failed"
    call = _logged(session, "voice-1")
    assert call.ended_at is not None and call.outcome == "error"


def test_the_handler_keeps_a_finished_call_open_until_its_end_is_reported(
    session, tenant
):
    """Any adapter: after the core's hangup the log waits for `on_call_ended`,
    and nothing the caller says in between reaches the agent."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    for line in (WISH, "Auf den Namen Mueller.", "Ja, passt so."):
        handler.on_user_turn("s-1", line)
    assert port.hangups("s-1")
    said = len(port.says("s-1"))
    assert _logged(session, "s-1").ended_at is None

    handler.on_user_turn("s-1", "Ach, noch was.")
    handler.on_silence("s-1")
    handler.on_call_ended("s-1")

    assert len(port.says("s-1")) == said and not port.transfers("s-1")
    call = _logged(session, "s-1")
    assert call.ended_at is not None and call.outcome == "completed"


# -- key terms for the speech engine (T-10.5) ------------------------------------------


def test_key_terms_are_the_dishes_in_question_never_the_menu(session, tenant):
    """After each turn the engine gets the names of the dishes the agent just
    named, which the guest answers next, and then those of the order
    (docs/20 §6); a turn that names none and has no order gives none."""
    apply(session, tenant.id, menu_plan(), now=NOW)
    port = FakeTelephony()
    case = load_call(
        CASES / "abholung_0060_duck_is_ambiguous_the_chosen_one_is_sold_out.json"
    )
    port.play(case, handler_for(session, tenant, port))

    ducks = ["Ente knusprig", "Ente süß-sauer", "Ente mit Mango"]
    assert port.terms == [
        [],  # "Was möchten Sie bestellen?"
        ducks,  # "Meinen Sie Nummer 47 Ente knusprig oder ...?"
        ducks,  # the same question again, the one chosen is sold out
        [],
        [],
        ["Ente mit Mango"],  # the read-back
        [],
    ]


def test_key_terms_are_names_as_a_guest_says_them(session, tenant):
    """The number of pieces is part of a name on the card, not of what a guest
    says ("Frühlingsrollen (4 Stück)")."""
    apply(session, tenant.id, menu_plan(), now=NOW)
    port = FakeTelephony()
    case = load_call(CASES / "abholung_0001_zwei_positionen_mit_option.json")
    port.play(case, handler_for(session, tenant, port))
    assert ["Frühlingsrollen", "Ente knusprig"] in port.terms


def _seen(*dishes: tuple[str, str]) -> ConversationState:
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    for number, name in dishes:
        state.seen_items[number] = {"number": number, "name": name}
    return state


def test_a_dish_named_without_its_pieces_is_named():
    """The agent says a name as a guest does, without "(4 Stück)" (Codex PR
    #292, P2)."""
    state = _seen(("23", "Frühlingsrollen (4 Stück)"), ("24", "Wan Tan"))
    said = "Meinen Sie die Frühlingsrollen oder die Wan Tan?"
    assert key_terms(state, said, menu_size=50) == ["Frühlingsrollen", "Wan Tan"]


def test_key_terms_that_would_be_the_whole_menu_are_not_handed_over():
    """On a menu of one dish, or of two both in question, the terms would be
    the menu (CLAUDE.md §2 rule 6, Codex PR #292, P1)."""
    state = _seen(("1", "Pho Bo"), ("2", "Pho Ga"))
    said = "Meinen Sie Nummer 1 Pho Bo oder Nummer 2 Pho Ga?"
    assert key_terms(state, said, menu_size=2) == []
    assert key_terms(state, said, menu_size=3) == ["Pho Bo", "Pho Ga"]
    one = _seen(("1", "Pho Bo"))
    assert key_terms(one, "Einmal Nummer 1 Pho Bo?", menu_size=1) == []


def test_the_menu_size_counts_the_active_dishes(session, tenant):
    apply(session, tenant.id, menu_plan(), now=NOW)
    size = active_count(session, tenant.id)
    dish = session.scalars(
        select(MenuItem).where(MenuItem.tenant_id == tenant.id)
    ).first()
    dish.active = False
    session.commit()
    assert size > 1
    assert active_count(session, tenant.id) == size - 1
    assert active_count(session, uuid.uuid4()) == 0


def test_a_question_into_the_silence_takes_the_key_terms_of_the_question(
    session, tenant
):
    """After "Sind Sie noch da?" the guest says "Ja"; the names of the dishes
    asked about before would bend that yes into a dish (Codex PR #292, P2).
    The terms follow what was said last, as after every turn."""
    apply(session, tenant.id, menu_plan(), now=NOW)
    port = FakeTelephony()
    case = {
        "transcript": [
            {
                "role": "customer",
                "text": "Hallo, ich moechte etwas zum Abholen bestellen.",
            },
            {"role": "customer", "text": "Einmal Ente."},
            {"role": "customer", "silence": True},
        ]
    }
    port.play(case, handler_for(session, tenant, port))
    assert port.terms[-2] == ["Ente knusprig", "Ente süß-sauer", "Ente mit Mango"]
    assert port.terms[-1] == []


@pytest.fixture
def bounded(bounded_db):
    """The phone line's sessions (`db.bounded_sessions`) at 300 ms."""
    return bounded_db(300)


def test_a_turn_that_waits_on_a_lock_goes_to_the_team_at_the_bound(
    session, tenant, migrated_db_url, bounded
):
    """The phone line's sessions end a statement at their bound (`voice` uses
    `db.bounded_sessions`): a tool that waits on a lock sends the guest to the
    team instead of keeping them on the line (Codex on PR #287)."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, session_factory=bounded)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", WISH)

    holder = create_engine(migrated_db_url)
    with holder.connect() as conn:
        # The draft of the next turn is written into this table.
        conn.execute(text("lock table reservations in access exclusive mode"))
        handler.on_user_turn("s-1", "Auf den Namen Mueller.")
        assert port.says("s-1")[-1] == SAY_OUTAGE
        assert port.transfers("s-1") == [TEAM_PHONE]
    holder.dispose()
    handler.on_call_ended("s-1")
    assert _logged(session, "s-1").outcome == "error"


class _LateModel:
    """The scripted model, taking `late` seconds of the call's clock per
    answer: what the guest has waited when a tool starts."""

    def __init__(self, inner, clock: Clock) -> None:
        self.inner, self.clock, self.late = inner, clock, 0.0

    def next_turn(self, *args, **kwargs) -> LLMTurn:
        self.clock.now += self.late
        return self.inner.next_turn(*args, **kwargs)


def test_a_tool_waits_on_a_lock_no_longer_than_the_turn_has_left(
    session, tenant, migrated_db_url, bounded_db
):
    """With 9.75 s of a 10 s turn spent on the model, a tool that waits on a
    lock gives up within the quarter second left, not at the line's 2 s
    bound, and the guest goes to the team (Codex on PR #298, docs/24)."""
    clock = Clock()
    model = _LateModel(scripted(session, tenant)(session), clock)
    port = FakeTelephony()
    handler = handler_for(
        session,
        tenant,
        port,
        session_factory=bounded_db(2000),
        llm_factory=lambda _s: model,
        clock=clock,
    )
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", WISH)
    deadline = session.scalar(select(ServiceConfig.turn_deadline_seconds))
    model.late = deadline - 0.25

    holder = create_engine(migrated_db_url)
    with holder.connect() as conn:
        conn.execute(text("lock table reservations in access exclusive mode"))
        started = time.monotonic()
        handler.on_user_turn("s-1", "Auf den Namen Mueller.")
        took = time.monotonic() - started
        assert port.says("s-1")[-1] == SAY_OUTAGE
        assert port.transfers("s-1") == [TEAM_PHONE]
    holder.dispose()
    assert took < 1.0
    handler.on_call_ended("s-1")
    assert _logged(session, "s-1").outcome == "error"
    assert session.scalars(select(Reservation)).all() == []


def _ended_under_a_lock(
    session,
    tenant,
    migrated_db_url,
    bounded,
    seconds: float,
    *,
    port: FakeTelephony | None = None,
    end: Callable[[CallHandler], None] = lambda handler: handler.on_call_ended("s-1"),
):
    """A call on the phone line's sessions that ends, by default with the end
    of its leg, while another transaction holds its row for `seconds`. How
    long the end took."""
    port = port or FakeTelephony()
    handler = handler_for(session, tenant, port, session_factory=bounded)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", WISH)
    call_id = the_call(session, "s-1").id
    session.rollback()

    holder = create_engine(migrated_db_url)
    conn = holder.connect()
    conn.execute(text("select 1 from calls where id = :id for update"), {"id": call_id})
    release = threading.Timer(seconds, conn.rollback)
    release.start()
    started = time.monotonic()
    end(handler)
    took = time.monotonic() - started
    release.join()
    conn.close()
    holder.dispose()
    return took


def test_the_end_of_the_call_waits_out_a_lock_longer_than_the_bound(
    session, tenant, migrated_db_url, bounded
):
    """The bound is for a guest waiting on a turn. The log of a call that is
    over is written past it, or a lock held for a moment would leave the call
    without an end and an outcome for good (Codex on PR #298)."""
    _ended_under_a_lock(session, tenant, migrated_db_url, bounded, 1.0)

    call = _logged(session, "s-1")
    assert call.ended_at is not None
    assert call.outcome == "abandoned"


def test_the_end_of_the_call_waits_on_a_lock_no_longer_than_its_own_bound(
    session, tenant, migrated_db_url, bounded, monkeypatch
):
    """Past it, the call's thread and its connection are given back: a lock
    that is not coming free must not hold them for ever (ultra review on PR
    #298). The call stays without an end then, as after a crash."""
    monkeypatch.setattr(handler_module, "END_CALL_TIMEOUT_MS", 300)

    took = _ended_under_a_lock(session, tenant, migrated_db_url, bounded, 3.0)

    assert took < 2.0
    assert _logged(session, "s-1").ended_at is None


def test_a_session_in_a_failed_transaction_still_closes_the_call_log(session, tenant):
    """A statement that failed and was caught leaves the transaction aborted;
    the call log is written all the same (ultra review on PR #298)."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", WISH)
    with pytest.raises(DBAPIError):
        session.execute(text("select 1 / 0"))

    handler.on_call_ended("s-1")

    call = _logged(session, "s-1")
    assert call.ended_at is not None
    assert call.outcome == "abandoned"


def test_a_failure_reported_while_the_call_ends_closes_its_log_once(
    session, tenant, monkeypatch, caplog
):
    """An adapter may report events on more than one thread: the end of the
    leg is handled while a failure of the same call is still on its way. The
    call is logged once, and nothing of it stays behind (ultra review on PR
    #298)."""
    usage = Usage(model="test-model", requests=1, prompt_tokens=10, completion_tokens=1)

    def metered(s):
        llm = scripted(session, tenant)(s)
        llm.usage = usage
        return llm

    class Gone(FakeTelephony):
        def transfer(self, session_id, target):
            raise RuntimeError("the leg is gone")

    port = Gone()
    handler = handler_for(session, tenant, port, llm_factory=metered)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", WISH)
    live = handler._live["s-1"]

    with caplog.at_level(logging.INFO, logger="api.telephony.handler"):
        handler.on_call_ended("s-1")
        # The failure, on the other thread, with the call it had looked up.
        with pytest.raises(RuntimeError, match="the leg is gone"):
            handler._fail("s-1", live)

    assert "s-1" not in handler._ending
    assert len([r for r in caplog.records if r.getMessage() == "model usage"]) == 1
    assert _logged(session, "s-1").outcome == "abandoned"


def _ending_during_start(monkeypatch, handler, *, fail: bool = False):
    """`start_call` that is told about the end of the leg while it writes the
    call row, the way an adapter on another thread would."""
    import api.telephony.handler as module
    from api.domain.calls import start_call as real

    def start_call(session, req, **kwargs):
        handler.on_call_ended(req.external_session_id)
        if fail:
            raise OperationalError("INSERT", {}, Exception("connection lost"))
        return real(session, req, **kwargs)

    monkeypatch.setattr(module, "start_call", start_call)


def test_a_hangup_while_the_call_opens_closes_its_log(session, tenant, monkeypatch):
    """The end of the leg arrives while the call row is written: the call is
    closed once it is open, not left live with no end (#207)."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, llm_factory=lambda s: NoLLM())
    _ending_during_start(monkeypatch, handler)
    port.open("s-1", caller_id="+497215551234")

    handler.on_call_started("s-1")

    assert not port.says("s-1")  # no greeting into a dead line
    call = _logged(session, "s-1")
    assert call.ended_at is not None
    assert call.outcome == "abandoned"
    # A late event of that call is ignored, not handed to the team.
    handler.on_user_turn("s-1", WISH)
    handler.on_call_ended("s-1")
    assert not port.transfers("s-1")


def test_a_hangup_while_the_call_fails_to_open_rings_nobody(
    session, tenant, monkeypatch
):
    """Nobody is left on the line to hand to the team."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    _ending_during_start(monkeypatch, handler, fail=True)
    port.open("s-1", caller_id="+497215551234")

    handler.on_call_started("s-1")

    assert not port.says("s-1")
    assert not port.transfers("s-1")
    handler.on_user_turn("s-1", WISH)
    assert not port.transfers("s-1")


def test_a_phone_call_logs_its_model_cost(session, tenant, monkeypatch, caplog):
    """Like the text telephone (`sim/session.py`): model and cost on the call,
    the exact tokens in the line "model usage" (#207)."""
    monkeypatch.setattr(settings, "llm_input_cents_per_mtok", 300)
    monkeypatch.setattr(settings, "llm_output_cents_per_mtok", 1500)
    # The local engines of the workbench cost nothing (T-10.6).
    monkeypatch.setattr(settings, "voice_ear_cents_per_hour", 0)
    monkeypatch.setattr(settings, "voice_mouth_cents_per_mchar", 0)
    usage = Usage(
        model="test-model", requests=2, prompt_tokens=18000, completion_tokens=800
    )

    def metered(s):
        llm = scripted(session, tenant)(s)
        llm.usage = usage
        return llm

    port = FakeTelephony()
    with caplog.at_level(logging.INFO, logger="api.telephony.handler"):
        sid = port.play(
            RESERVATION, handler_for(session, tenant, port, llm_factory=metered)
        )

    call = _logged(session, sid)
    assert call.model == "test-model"
    # 18000 x 300 + 800 x 1500 = 6.6 million, per million tokens: 6.6 cents, up.
    assert call.cost_cents == 7
    lines = [r.extra for r in caplog.records if r.getMessage() == "model usage"]
    assert lines == [{"call_id": str(call.id), **asdict(usage), "cost_cents": 7}]


PRICED = Usage(model="m", requests=1, prompt_tokens=10_000, completion_tokens=0)


@pytest.mark.parametrize(
    ("ear", "mouth", "outbound", "speech", "transferred", "cents"),
    [
        # 3 cents of model, 60 s of audio at 36 cents an hour (0.6), 2,000
        # characters at 1,600 cents a million (3.2): 3 + ceil(3.8) = 7.
        (36, 1600, 0, SpeechUsage(60.0, 2000), False, 7),
        (0, 0, 0, SpeechUsage(60.0, 2000), True, 3),
        # Unknown is never 0: a price or the measurement missing, or a priced
        # transfer leg whose length the line does not report.
        (None, 0, 0, SpeechUsage(60.0, 2000), False, None),
        (0, None, 0, SpeechUsage(60.0, 2000), False, None),
        (0, 0, 0, None, False, None),
        (0, 0, 90, SpeechUsage(60.0, 2000), True, None),
        (0, 0, None, SpeechUsage(60.0, 2000), True, None),
        (0, 0, 90, SpeechUsage(60.0, 2000), False, 3),
        # 0.1 s at 36,000 cents an hour is exactly one cent, not two.
        (36000, 0, 0, SpeechUsage(0.1, 0), False, 4),
    ],
)
def test_the_cost_of_a_phone_call(
    monkeypatch, ear, mouth, outbound, speech, transferred, cents
):
    """T-10.6: model, ear and mouth on the call log, in integer cents."""
    monkeypatch.setattr(settings, "llm_input_cents_per_mtok", 300)
    monkeypatch.setattr(settings, "llm_output_cents_per_mtok", 0)
    monkeypatch.setattr(settings, "voice_ear_cents_per_hour", ear)
    monkeypatch.setattr(settings, "voice_mouth_cents_per_mchar", mouth)
    monkeypatch.setattr(settings, "voice_outbound_cents_per_hour", outbound)

    assert call_cost_cents(PRICED, speech, transferred=transferred) == cents


@pytest.mark.parametrize(
    ("state", "outcome", "cents"),
    [
        ({"transferred": False, "stage": "collecting"}, None, 3),
        # The outage path hands the call to the team without the tool's flag.
        ({"transferred": False, "stage": "collecting"}, "error", None),
        ({"transferred": False, "stage": "transferred"}, None, None),
    ],
)
def test_a_team_leg_of_any_kind_is_priced(monkeypatch, state, outcome, cents):
    """Review of T-10.6: the outage transfer is a team leg too."""
    monkeypatch.setattr(settings, "llm_input_cents_per_mtok", 300)
    monkeypatch.setattr(settings, "llm_output_cents_per_mtok", 0)
    monkeypatch.setattr(settings, "voice_ear_cents_per_hour", 0)
    monkeypatch.setattr(settings, "voice_mouth_cents_per_mchar", 0)
    monkeypatch.setattr(settings, "voice_outbound_cents_per_hour", 90)
    # A stand-in with only the part `_cost` reads.
    live = cast(
        handler_module._LiveCall, SimpleNamespace(state=SimpleNamespace(**state))
    )

    assert handler_module._cost(live, PRICED, SpeechUsage(), outcome) == cents


def test_a_cost_that_cannot_be_computed_leaves_it_unknown(monkeypatch):
    monkeypatch.setattr(settings, "llm_input_cents_per_mtok", 300)
    monkeypatch.setattr(settings, "llm_output_cents_per_mtok", 0)
    monkeypatch.setattr(settings, "voice_ear_cents_per_hour", 36)
    monkeypatch.setattr(settings, "voice_mouth_cents_per_mchar", 0)
    live = cast(
        handler_module._LiveCall,
        SimpleNamespace(state=SimpleNamespace(transferred=False, stage="done")),
    )

    assert (
        handler_module._cost(live, PRICED, SpeechUsage(float("nan"), 0), None) is None
    )


def test_a_phone_call_on_the_stand_in_logs_no_model(session, tenant, caplog):
    port = FakeTelephony()
    with caplog.at_level(logging.INFO, logger="api.telephony.handler"):
        sid = port.play(RESERVATION, handler_for(session, tenant, port))

    call = _logged(session, sid)
    assert call.model is None
    assert call.cost_cents is None
    assert not [r for r in caplog.records if r.getMessage() == "model usage"]


def test_a_failure_in_the_gap_before_the_log_is_written_leaves_nothing_behind(
    session, tenant, monkeypatch
):
    """The end of the leg takes the call; a failure of the same call, on
    another thread, comes before its log is written. The call is not parked
    again for an end of the leg that was already used up (ultra review on PR
    #310)."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", WISH)
    finish = handler._finish

    def failure_in_the_gap(live, outcome=None, **kwargs):
        handler._fail("s-1", live)  # the other thread
        finish(live, outcome, **kwargs)

    monkeypatch.setattr(handler, "_finish", failure_in_the_gap)
    handler.on_call_ended("s-1")

    assert "s-1" not in handler._ending
    assert _logged(session, "s-1").outcome == "abandoned"


def test_after_a_failed_transfer_the_log_does_not_keep_the_caller_from_the_team(
    session, tenant, migrated_db_url, bounded, monkeypatch
):
    """No hangup went out: the dialplan rings the team once the session has
    ended, after the log is written. Then the log waits no longer than the
    line's bound for a turn (ultra review on PR #310)."""
    monkeypatch.setattr(handler_module, "END_CALL_TIMEOUT_MS", 5000)

    class Gone(FakeTelephony):
        def transfer(self, session_id, target):
            raise RuntimeError("the leg is gone")

    def failure(handler: CallHandler) -> None:
        with pytest.raises(RuntimeError, match="the leg is gone"):
            handler.on_voice_failure("s-1")

    took = _ended_under_a_lock(
        session, tenant, migrated_db_url, bounded, 3.0, port=Gone(), end=failure
    )

    assert took < 2.0


def test_after_an_outage_transfer_the_log_waits_out_a_lock_like_any_end(
    session, tenant, migrated_db_url, bounded
):
    """The transfer went out, as a hangup to the line: nobody waits on the log,
    and an error call is the one the team reviews (own review on PR #310)."""

    def outage(handler: CallHandler) -> None:
        handler.on_voice_failure("s-1")
        handler.on_call_ended("s-1")

    _ended_under_a_lock(session, tenant, migrated_db_url, bounded, 1.0, end=outage)

    call = _logged(session, "s-1")
    assert call.ended_at is not None
    assert call.outcome == "error"


# -- the team leg nobody answered (T-10.8) ---------------------------------------

COMPLAINT_TEXT = "Ich will mich beschweren, das Essen war kalt."
COMPLAINT = {
    "caller_id": "+497215551234",
    "transcript": [{"role": "customer", "text": COMPLAINT_TEXT}],
}


def test_a_team_leg_nobody_answered_becomes_a_callback_card(session, tenant):
    """The dialplan reports it after our leg and its log are closed: the card
    is the team's only notice of the call then (docs/20 §5)."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    sid = port.play(COMPLAINT, handler)
    assert port.transfers(sid) == [TEAM_PHONE]

    assert handler.on_team_missed(sid) is True

    [card] = session.scalars(select(Callback)).all()
    assert (card.phone, card.reason) == ("+497215551234", "complaint")
    assert the_call(session, sid).outcome == "callback"


def test_an_allergen_question_keeps_its_card_when_nobody_answers(session, tenant):
    """Codex PR #369, P1: the core's card for an allergen question beyond the
    database (L17) reaches the card of a team leg nobody answered, not the
    generic one, and still names no allergy of the guest."""
    case = {
        "caller_id": "+497215551234",
        "transcript": [
            {"role": "customer", "text": "Sind da Spuren von Erdnuessen drin?"},
        ],
    }
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    sid = port.play(case, handler)
    assert port.transfers(sid) == [TEAM_PHONE]

    assert handler.on_team_missed(sid) is True

    [card] = session.scalars(select(Callback)).all()
    assert card.summary.startswith(SUMMARY_MISSED)
    assert SUMMARY_ALLERGENS in card.summary
    assert "Erdnuss" not in card.summary


@pytest.mark.parametrize(
    ("said", "marked"),
    [
        ("Ich haette gern einen Tisch fuer zwei, ich habe eine Nussallergie.", True),
        ("Ich haette gern einen Tisch fuer zwei.", False),
    ],
)
def test_a_voice_failure_keeps_the_kitchen_marker_for_the_missed_card(
    session, tenant, said, marked
):
    """#956: the voice layer fails mid-call, so the core wrote no card for the
    handover; the guest spoke of an allergy, and the card of the team leg
    nobody answered still leads with the kitchen marker, never the allergy
    (L13)."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", said)
    live = handler._live["s-1"]
    assert bool(live.state.allergy_said) is marked
    assert live.state.handoff_summary is None

    handler.on_voice_failure("s-1")
    handler.on_call_ended("s-1")
    assert handler.on_team_missed("s-1") is True

    [card] = session.scalars(select(Callback)).all()
    assert card.summary.startswith(SUMMARY_MISSED)
    assert (SUMMARY_NOTE_TO_CLEAR in card.summary) is marked
    assert "Nuss" not in card.summary


@pytest.mark.parametrize(
    ("over", "marked"),
    [("Ich habe eine Nussallergie.", True), ("Ja, genau.", False)],
)
def test_an_allergy_said_into_a_protected_sentence_marks_the_missed_card(
    session, tenant, over, marked
):
    """#970: words said into a read-back make no turn, so the core never hears
    an allergy among them; the line does, and the card of the team leg nobody
    answered after a failure leads with the kitchen marker (L13)."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", "Ich haette gern einen Tisch fuer zwei.")
    handler.on_talked_over("s-1", over)

    handler.on_voice_failure("s-1")
    handler.on_call_ended("s-1")
    assert handler.on_team_missed("s-1") is True

    [card] = session.scalars(select(Callback)).all()
    assert (SUMMARY_NOTE_TO_CLEAR in card.summary) is marked
    assert "Nuss" not in card.summary


def test_a_core_card_with_the_marker_keeps_it_once_when_nobody_answers(session, tenant):
    """The core's own card leads with the marker already (#940): the line's
    card carries it once."""
    case = {
        "caller_id": "+497215551234",
        "transcript": [
            {
                "role": "customer",
                "text": "Ich haette gern einen Tisch fuer zwei, "
                "ich habe eine Nussallergie.",
            },
            {"role": "customer", "text": "Sind da Spuren von Erdnuessen drin?"},
        ],
    }
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    sid = port.play(case, handler)
    assert port.transfers(sid) == [TEAM_PHONE]

    assert handler.on_team_missed(sid) is True

    [card] = session.scalars(select(Callback)).all()
    assert card.summary.count(SUMMARY_NOTE_TO_CLEAR) == 1
    assert "Nuss" not in card.summary and "Erdnu" not in card.summary


def test_a_number_the_guest_named_goes_on_the_card(session, tenant):
    """Withheld caller id: only the number said in the call is left, and only
    this process knows it once the call is closed."""
    case = {
        "transcript": [
            {
                "role": "customer",
                "text": "Ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr.",
            },
            {"role": "customer", "text": "Auf den Namen Mueller."},
            {"role": "customer", "text": "Meine Nummer ist 0721 5551234."},
            {"role": "customer", "text": "Ich will mich beschweren."},
        ]
    }
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    sid = port.play(case, handler)
    assert port.transfers(sid) == [TEAM_PHONE]

    assert handler.on_team_missed(sid) is True

    [card] = session.scalars(select(Callback)).all()
    assert card.phone == "+497215551234"
    assert the_call(session, sid).caller_id is None


def test_a_named_number_is_forgotten_after_the_handover_window(session, tenant):
    """A team that answered sends no report, and the number would stay for the
    next call that comes with the same id (Codex on PR #345): only a report
    within minutes of the handover reads it."""
    clock = Clock()
    case = {
        "transcript": [
            {
                "role": "customer",
                "text": "Ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr.",
            },
            {"role": "customer", "text": "Auf den Namen Mueller."},
            {"role": "customer", "text": "Meine Nummer ist 0721 5551234."},
            {"role": "customer", "text": "Ich will mich beschweren."},
        ]
    }
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, clock=clock)
    sid = port.play(case, handler)

    clock.now += handler_module.NAMED_PHONE_SECONDS + 1
    assert handler.on_team_missed(sid) is False

    assert session.scalars(select(Callback)).all() == []


def test_a_named_number_is_dropped_when_a_later_call_ends(session, tenant):
    """A team that answered sends no report: the number and the card stayed
    in memory until the next handover or report, overnight on a quiet line
    (#861). The end of any later call drops them; the call's own end keeps
    them for the report of its team leg."""
    clock = Clock()
    case = {
        "transcript": [
            {"role": "customer", "text": "Meine Nummer ist 0721 5551234."},
            {"role": "customer", "text": "Ich will mich beschweren."},
        ]
    }
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, clock=clock)
    sid = port.play(case, handler)
    assert sid in handler._named_phones  # its own end keeps it

    clock.now += handler_module.NAMED_PHONE_SECONDS + 1
    port.play({"transcript": []}, handler)  # a caller who hangs up at once

    assert sid not in handler._named_phones


def test_without_any_number_the_report_has_no_card(session, tenant):
    case = {"transcript": COMPLAINT["transcript"]}
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    sid = port.play(case, handler)

    assert handler.on_team_missed(sid) is False

    assert session.scalars(select(Callback)).all() == []


@pytest.mark.parametrize("mode", ["paused", "shadow"])
def test_a_call_the_ai_never_answered_becomes_a_card_too(session, tenant, mode):
    """The team was rung at once and nobody took it: the card under the caller
    id, without a concern the AI never heard."""
    set_mode(session, tenant.id, mode)
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, llm_factory=lambda s: NoLLM())
    sid = port.play(RESERVATION, handler)

    assert handler.on_team_missed(sid) is True

    [card] = session.scalars(select(Callback)).all()
    assert (card.phone, card.reason) == ("+497215551234", "out_of_scope")


def test_a_report_the_database_cannot_take_is_false_and_names_no_number(
    session, tenant, caplog
):
    sessions = iter([session])

    def once_then_down() -> Session:
        try:
            return next(sessions)
        except StopIteration:
            raise OperationalError(
                "SELECT 1", {"phone": "+497215551234"}, Exception("refused")
            ) from None

    port = FakeTelephony()
    handler = handler_for(session, tenant, port, session_factory=once_then_down)
    sid = port.play(COMPLAINT, handler)

    with caplog.at_level(logging.INFO):
        assert handler.on_team_missed(sid) is False

    assert "5551234" not in caplog.text
    assert any(sid in str(r.__dict__) for r in caplog.records)


def test_a_report_for_a_call_this_process_never_saw_is_false(session, tenant):
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)

    assert handler.on_team_missed("1760000000.9") is False


# -- failure paths that must still end with the right team (#674) ----------------


def test_a_call_that_fails_to_open_goes_to_the_routed_team_phone(
    session, tenant, monkeypatch
):
    """The routing was read and the call row was not written: the call goes
    to today's team number, not to the one of the environment, which may be
    the phone nobody staffs today."""

    def start_call(session, req, **kwargs):
        raise OperationalError("INSERT", {}, Exception("statement timeout"))

    monkeypatch.setattr(handler_module, "start_call", start_call)
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, fallback_team_phone="+4930000000")
    port.open("s-1", caller_id="+497215551234")

    handler.on_call_started("s-1")

    assert port.says("s-1") == [SAY_OUTAGE_FIRST]
    assert port.transfers("s-1") == [TEAM_PHONE]


# -- a team number that changes during a call (#710) -----------------------------


CHANGED_PHONE = "+4972155599999"


def _call_that_fails_after_the_staff_changed_the_number(session, tenant):
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, llm_factory=lambda s: BrokenLLM())
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant.id)
        .values(team_phone=CHANGED_PHONE)
    )
    session.commit()
    handler.on_user_turn("s-1", "Einen Tisch fuer vier bitte")
    return port


def test_an_outage_during_a_call_rings_the_number_that_holds_now(session, tenant):
    port = _call_that_fails_after_the_staff_changed_the_number(session, tenant)

    assert port.says("s-1")[-1] == SAY_OUTAGE
    assert port.transfers("s-1") == [CHANGED_PHONE]


def test_an_outage_with_the_number_unreadable_rings_the_one_read_at_the_start(
    session, tenant, monkeypatch
):
    """The database is the outage: the read of the number fails, the call goes
    to the number it started with, and the sentence and transfer still happen."""

    def unreadable(session, tenant_id, now=None):
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    monkeypatch.setattr(handler_module, "current_team_phone", unreadable)

    port = _call_that_fails_after_the_staff_changed_the_number(session, tenant)

    assert port.says("s-1")[-1] == SAY_OUTAGE
    assert port.transfers("s-1") == [TEAM_PHONE]


def _outage_on_the_wire(
    session, tenant, monkeypatch, *, unreadable=False, llm_factory=None
):
    """A call on the own voice layer whose turn fails after the staff changed
    the team number. Each read of the number is marked on the line, so the
    test sees it between what the caller hears."""
    wire = _Wire(session, tenant, llm_factory=llm_factory or (lambda s: BrokenLLM()))
    wire.played()
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant.id)
        .values(team_phone=CHANGED_PHONE)
    )
    session.commit()
    from api.domain.calls import current_team_phone as real

    def marked(*args, **kwargs):
        wire.line.actions.append(("team_number_read",))
        if unreadable:
            raise OperationalError("SELECT", {}, Exception("connection refused"))
        return real(*args, **kwargs)

    monkeypatch.setattr(handler_module, "current_team_phone", marked)
    return wire


def test_on_the_wire_the_outage_sentence_plays_before_the_team_number_is_read(
    session, tenant, monkeypatch
):
    """#754 follow-up: inside a turn the sentence would wait for the end of the
    turn, behind the read. It goes out first, the read runs while it plays,
    and the transfer still waits for its end and dials the number of now."""
    wire = _outage_on_the_wire(session, tenant, monkeypatch)

    wire.says("Einen Tisch fuer vier bitte")

    actions = wire.line.actions
    assert actions.index(("play", SAY_OUTAGE)) < actions.index(("team_number_read",))
    assert "transfer" not in wire.line.kinds()
    wire.played()
    assert actions[-1] == ("transfer", CHANGED_PHONE)
    assert actions.count(("team_number_read",)) == 1


def test_on_the_wire_an_unreadable_team_number_comes_after_the_sentence_too(
    session, tenant, monkeypatch
):
    """The database is the outage: the sentence is on the line before the read
    that fails, and the call goes to the number it started with."""
    wire = _outage_on_the_wire(session, tenant, monkeypatch, unreadable=True)

    wire.says("Einen Tisch fuer vier bitte")
    wire.played()

    actions = wire.line.actions
    assert actions.index(("play", SAY_OUTAGE)) < actions.index(("team_number_read",))
    assert actions[-1] == ("transfer", TEAM_PHONE)
    assert the_call(session, "voice-1").outcome == "error"


def test_on_the_wire_a_mouth_that_fails_on_the_outage_sentence_rings_the_team_once(
    session, tenant, monkeypatch
):
    """The sentence that now goes out first fails in the mouth: that failure
    takes the call to the team, and the failed turn behind it neither reads
    the number again nor asks for a second transfer."""
    wire = _outage_on_the_wire(session, tenant, monkeypatch)
    wire.call._mouth.broken = True

    wire.says("Einen Tisch fuer vier bitte")

    actions = wire.line.actions
    assert ("play", SAY_OUTAGE) not in actions
    assert [a for a in actions if a[0] == "transfer"] == [("transfer", CHANGED_PHONE)]
    assert actions.count(("team_number_read",)) == 1
    assert wire.call.outcome == "transfer"
    assert the_call(session, "voice-1").outcome == "error"


class DatabaseLostLLM:
    """A turn that fails on the database, the way a lost connection does."""

    def next_turn(
        self, system_prompt, state_json, input_text, *, timeout=None
    ) -> LLMTurn:
        raise OperationalError("SELECT", {}, Exception("connection lost"))


def test_on_the_wire_a_mouth_failing_after_a_database_failure_reads_no_number(
    session, tenant, monkeypatch
):
    """#926: the turn failed on the database, so its outage path reads no
    number; then the mouth fails on the outage sentence. That failure hands the
    call over once more, and must not read the number from the dead database
    either: the call's own number, rung once."""
    wire = _outage_on_the_wire(
        session,
        tenant,
        monkeypatch,
        unreadable=True,
        llm_factory=lambda s: DatabaseLostLLM(),
    )
    wire.call._mouth.broken = True

    wire.says("Einen Tisch fuer vier bitte")

    actions = wire.line.actions
    assert ("team_number_read",) not in actions
    assert [a for a in actions if a[0] == "transfer"] == [("transfer", TEAM_PHONE)]
    assert wire.call.outcome == "transfer"
    assert the_call(session, "voice-1").outcome == "error"


def test_a_failing_voice_after_an_unreadable_team_number_reads_it_no_more(
    session, tenant, monkeypatch
):
    """#926: a handover read the number and the database refused it; then the
    voice fails. The outage path dials the call's own number without a second
    read that would only wait out another connect timeout."""
    reads = []

    def unreadable(session, tenant_id, now=None):
        reads.append(tenant_id)
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    monkeypatch.setattr(handler_module, "current_team_phone", unreadable)

    assert handler._team_phone(handler._live["s-1"]) == TEAM_PHONE
    handler.on_voice_failure("s-1")

    assert len(reads) == 1
    assert port.says("s-1")[-1] == SAY_OUTAGE
    assert port.transfers("s-1") == [TEAM_PHONE]


def test_a_database_failure_does_not_wait_on_a_second_read_of_the_number(
    session, tenant, monkeypatch
):
    """Inside a turn the outage sentence is queued until the failure is
    handled, so a read of the number after a database error held the sentence
    for another connect timeout of silence (ledger #764). The database is the
    failure: the call's own number, unread."""
    reads = []

    def counted(session, tenant_id, now=None):
        reads.append(tenant_id)
        return CHANGED_PHONE

    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    monkeypatch.setattr(handler_module, "current_team_phone", counted)

    def lost(live, text):
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(handler, "_turn", lost)
    handler.on_user_turn("s-1", "Einen Tisch fuer vier bitte")

    assert reads == []
    assert port.says("s-1")[-1] == SAY_OUTAGE
    assert port.transfers("s-1") == [TEAM_PHONE]


def test_a_database_lost_behind_the_key_terms_is_not_read_again_for_the_team(
    session, tenant, monkeypatch
):
    """#926 review: the read of the menu size for the ear's key terms meets a
    database that does not answer and goes on without names; the outage that
    follows dials the call's own number without waiting on the database again."""
    reads = []

    def counted(session, tenant_id, now=None):
        reads.append(tenant_id)
        return CHANGED_PHONE

    def unreachable(session, tenant_id):
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    monkeypatch.setattr(handler_module, "current_team_phone", counted)
    monkeypatch.setattr(handler_module, "active_count", unreachable)

    assert handler._menu_size(handler._live["s-1"]) == 0
    handler.on_voice_failure("s-1")

    assert reads == []
    assert port.transfers("s-1") == [TEAM_PHONE]


def test_a_constraint_error_reads_the_number_that_holds_now(
    session, tenant, monkeypatch
):
    """#926: a constraint or data error comes from a database that answers, so
    the outage path still reads the number of now (#710) instead of the one of
    the call's start."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    monkeypatch.setattr(
        handler_module, "current_team_phone", lambda *a, **k: CHANGED_PHONE
    )

    def refused(live, text):
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    monkeypatch.setattr(handler, "_turn", refused)
    handler.on_user_turn("s-1", "Einen Tisch fuer vier bitte")

    assert port.says("s-1")[-1] == SAY_OUTAGE
    assert port.transfers("s-1") == [CHANGED_PHONE]


def test_a_handover_without_a_named_target_dials_the_number_that_holds_now(
    session, tenant
):
    """The shadow leg and a transfer without a target of the core take it."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    live = handler._live["s-1"]
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant.id)
        .values(team_phone=CHANGED_PHONE)
    )
    session.commit()

    assert handler._team_phone(live) == CHANGED_PHONE
    assert live.team_phone == CHANGED_PHONE


def _today(session, tenant, number: str | None, until: datetime | None) -> None:
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant.id)
        .values(team_phone_today=number, team_phone_today_until=until)
    )
    session.commit()


def test_the_dialplan_is_told_the_number_that_holds_now(session, tenant):
    """The legs the dialplan dials on its own (#835): the standard number,
    today's while it holds, the standard again after it."""
    handler = handler_for(session, tenant, FakeTelephony())
    assert handler.team_phone() == TEAM_PHONE

    _today(session, tenant, CHANGED_PHONE, NOW + timedelta(hours=1))
    assert handler.team_phone() == CHANGED_PHONE

    _today(session, tenant, CHANGED_PHONE, NOW - timedelta(seconds=1))
    assert handler.team_phone() == TEAM_PHONE


def test_the_dialplan_is_told_no_number_without_a_database(session, tenant):
    """Empty, never an exception: the dialplan keeps the number it had,
    TEAM_PHONE of its environment at the call's start (rule 5)."""

    def no_database():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    handler = handler_for(session, tenant, FakeTelephony(), session_factory=no_database)
    assert handler.team_phone() == ""


def test_the_dialplan_is_told_no_number_for_a_tenant_without_its_config(
    session, tenant
):
    handler = handler_for(session, tenant, FakeTelephony())
    handler._tenant_id = uuid.uuid4()
    assert handler.team_phone() == ""


@pytest.mark.parametrize(("length", "kept"), [(40, True), (41, False), (200, False)])
def test_an_overlong_caller_id_never_refuses_the_call(session, tenant, length, kept):
    """The dialplan hands the caller id over unbounded: one longer than the
    call log takes is no number, and the call goes on as one with a withheld
    number, never down the outage path."""
    caller_id = "x" * length
    port = FakeTelephony()
    port.open("s-1", caller_id=caller_id)

    handler_for(session, tenant, port).on_call_started("s-1")

    assert port.says("s-1")[0].startswith("Guten Tag")
    assert not port.transfers("s-1")
    assert the_call(session, "s-1").caller_id == (caller_id if kept else None)


def test_a_failing_key_term_lookup_never_reaches_the_caller(
    session, tenant, monkeypatch
):
    """The key terms only help the ear: a database error while counting the
    menu leaves the booking, its confirmation and the next turn alone, and no
    names are handed over then."""

    def broken(s, tenant_id):
        s.execute(text("select 1 / 0"))

    monkeypatch.setattr(handler_module, "active_count", broken)
    port = FakeTelephony()

    sid = port.play(RESERVATION, handler_for(session, tenant, port))

    assert SAY_OUTAGE not in port.says(sid)
    assert port.actions[-1].kind == "hangup"
    assert set(map(tuple, port.terms)) == {()}
    call = the_call(session, sid)
    assert call.outcome == "completed"
    assert _reservation_of(session, sid).status == "confirmed"


def test_a_failed_session_closes_its_log_as_one_the_caller_waits_on(
    session, tenant, migrated_db_url, bounded, monkeypatch
):
    """The session broke: its outage sentence and transfer reach nobody, and
    the dialplan rings the team only after this write. So it waits no longer
    than the line's bound, like a transfer the line refused."""
    monkeypatch.setattr(handler_module, "END_CALL_TIMEOUT_MS", 5000)

    def failed(handler: CallHandler) -> None:
        handler.on_voice_failure("s-1")
        handler.on_call_ended("s-1", line_failed=True)

    took = _ended_under_a_lock(
        session, tenant, migrated_db_url, bounded, 3.0, end=failed
    )

    assert took < 2.0


def test_a_guest_who_hangs_up_on_the_handover_sentence_gets_a_card(session, tenant):
    """The transfer waits for its sentence; a guest gone before it reaches the
    line was never with the team: a card from the core's handover, and the
    call is not counted as handed over."""
    wire = _Wire(session, tenant)
    wire.played()
    wire.says(COMPLAINT_TEXT)
    assert wire.call.outcome == "open"

    wire.call.closed()
    wire.call.drain()

    assert wire.line.kinds().count("transfer") == 0
    [card] = session.scalars(select(Callback)).all()
    assert (card.phone, card.reason) == ("+497215551234", "complaint")
    assert card.summary.startswith(SUMMARY_HUNG_UP)
    assert _logged(session, "voice-1").outcome == "callback"


def test_a_withheld_guest_who_hangs_up_on_the_handover_is_abandoned(session, tenant):
    """Nobody can call back: no card, and the call reached nobody."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id=None)
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", COMPLAINT_TEXT)
    assert port.transfers("s-1") == [TEAM_PHONE]

    handler.on_call_ended("s-1", caller_hung_up=True)

    assert session.scalars(select(Callback)).all() == []
    assert the_call(session, "s-1").outcome == "abandoned"


def test_a_guest_who_hangs_up_on_the_outage_sentence_gets_a_card(session, tenant):
    """#741: a turn failed, the outage sentence is queued and the transfer waits
    behind it. A guest gone before it reaches the line was never with the team:
    a card from the kept summary, and the call stays logged as an error."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, llm_factory=lambda s: BrokenLLM())
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", "Einen Tisch für vier bitte")
    assert port.says("s-1")[-1] == SAY_OUTAGE

    handler.on_call_ended("s-1", caller_hung_up=True)

    [card] = session.scalars(select(Callback)).all()
    assert card.phone == "+497215551234"
    assert the_call(session, "s-1").outcome == "error"


def test_a_guest_who_hangs_up_on_the_outage_sentence_keeps_the_kitchen_marker(
    session, tenant
):
    """#956: the guest named an allergy, the voice failed and the guest hung up
    on the outage sentence: the card the hang-up writes leads with the kitchen
    marker as well, never the allergy (L13)."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn(
        "s-1", "Ich haette gern einen Tisch fuer zwei, ich habe eine Nussallergie."
    )
    handler.on_voice_failure("s-1")

    handler.on_call_ended("s-1", caller_hung_up=True)

    [card] = session.scalars(select(Callback)).all()
    assert card.summary.startswith(f"{SUMMARY_HUNG_UP} {SUMMARY_NOTE_TO_CLEAR}.")
    assert "Nuss" not in card.summary


def _booked_then_broken(session, tenant) -> "_Wire":
    """On the wire: a table is booked, and the line breaks while the guest
    hears the confirmation, so the outage sentence follows it."""
    wire = _Wire(session, tenant)
    wire.until_the_read_back()
    wire.played()
    wire.says("Ja, passt so.")
    assert _reservation_of(session, "voice-1").status == "confirmed"
    wire.call.broken()
    wire.call.drain()
    assert wire.line.played()[-1] == SAY_OUTAGE
    return wire


def _table_on_the_card(session, tenant) -> str:
    reservation = _reservation_of(session, "voice-1")
    when = reservation.reserved_for.astimezone(ZoneInfo(tenant.timezone))
    return (
        f"{SUMMARY_BOOKED} Reservierung, {reservation.party_size} Personen, "
        f"{when:%d.%m.%Y %H:%M} Uhr, Name {reservation.guest_name}."
    )


def test_a_guest_who_hangs_up_on_the_outage_sentence_after_a_booking_gets_it_named(
    session, tenant
):
    """#743 follow-up: the outage path keeps no card of the core, so the card
    named an unknown concern while the table stood; the team could book it a
    second time. It names the booking of the call from the database."""
    wire = _booked_then_broken(session, tenant)

    wire.call.closed()
    wire.call.drain()

    [card] = session.scalars(select(Callback)).all()
    assert card.summary == f"{SUMMARY_HUNG_UP} {_table_on_the_card(session, tenant)}"
    assert _logged(session, "voice-1").outcome == "error"


def test_a_missed_team_leg_of_an_outage_after_a_booking_names_it(session, tenant):
    """The guest stays for the outage sentence, nobody takes the team leg: the
    card the dialplan asks for names the booking too."""
    wire = _booked_then_broken(session, tenant)
    wire.played()
    assert wire.line.kinds()[-1] == "transfer"

    assert wire.call._events.on_team_missed("voice-1")

    [card] = session.scalars(select(Callback)).all()
    assert card.summary == f"{SUMMARY_MISSED} {_table_on_the_card(session, tenant)}"


def test_a_withheld_guest_who_hangs_up_on_the_outage_sentence_gets_no_card(
    session, tenant
):
    """Nobody can call back, and the end stays an error."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, llm_factory=lambda s: BrokenLLM())
    port.open("s-1", caller_id=None)
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", "Einen Tisch für vier bitte")

    handler.on_call_ended("s-1", caller_hung_up=True)

    assert session.scalars(select(Callback)).all() == []
    assert the_call(session, "s-1").outcome == "error"


def test_an_outage_transfer_that_reached_the_line_leaves_no_card(session, tenant):
    """The guest stayed for the sentence: the line had the transfer, and the
    team's missed leg is what writes a card then."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, llm_factory=lambda s: BrokenLLM())
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", "Einen Tisch für vier bitte")

    handler.on_call_ended("s-1")

    assert session.scalars(select(Callback)).all() == []
    assert the_call(session, "s-1").outcome == "error"


# -- #953: the call ends while an order waits for its yes ---------------------------

ORDER_READ_BACK = [
    {"role": "customer", "text": "Guten Tag, ich moechte etwas zum Abholen bestellen."},
    {"role": "customer", "text": "Zweimal die 23 und einmal die knusprige Ente."},
    {"role": "customer", "text": "Huhn bitte."},
    {"role": "customer", "text": "Nein, das wars."},
    {"role": "customer", "text": "Auf den Namen Mueller."},
    {"role": "customer", "text": "Meine Nummer ist 0721 5551234."},
]


def _order_cards(session, sid) -> list[Callback]:
    call = the_call(session, sid)
    return list(session.scalars(select(Callback).where(Callback.call_id == call.id)))


def test_a_hang_up_on_an_order_read_back_leaves_a_card(session, tenant):
    """Maxi, 24.11.2026: the caller hangs up while the order waits for its
    yes; a "Danke" alone is none. The team calls back, nothing is booked."""
    apply(session, tenant.id, menu_plan(), now=NOW)
    port = FakeTelephony()
    case = {"transcript": [*ORDER_READ_BACK, {"role": "customer", "text": "Danke."}]}
    sid = port.play(case, handler_for(session, tenant, port))

    call = the_call(session, sid)
    assert call.outcome == "callback"
    cards = _order_cards(session, sid)
    assert len(cards) == 1 and SUMMARY_READBACK_UNANSWERED in cards[0].summary
    assert (
        session.scalars(select(Order).where(Order.call_id == call.id)).one().status
        == "draft"
    )


def test_an_outage_on_an_order_read_back_leaves_no_card_of_its_own(session, tenant):
    """The outage takes the call to the team, whose own card waits if nobody
    answers: the order card is not written for it."""
    apply(session, tenant.id, menu_plan(), now=NOW)
    port = FakeTelephony()
    case = {
        "transcript": [*ORDER_READ_BACK, {"role": "customer", "voice_failure": True}]
    }
    sid = port.play(case, handler_for(session, tenant, port))

    assert port.transfers(sid) == [TEAM_PHONE]
    assert the_call(session, sid).outcome == "error"
    assert all(
        SUMMARY_READBACK_UNANSWERED not in card.summary
        for card in _order_cards(session, sid)
    )


def test_a_failed_order_card_still_closes_the_call_log(session, tenant, monkeypatch):
    """A database error on the card costs the card, never the log: without it
    the call would count as an outage later."""
    apply(session, tenant.id, menu_plan(), now=NOW)

    def broken(self, state, *, with_the_team=False):
        raise OperationalError("insert", {}, Exception("lock timeout"))

    monkeypatch.setattr(ConversationLoop, "offer_unanswered", broken)
    port = FakeTelephony()
    sid = port.play({"transcript": ORDER_READ_BACK}, handler_for(session, tenant, port))

    call = the_call(session, sid)
    assert call.ended_at is not None and call.outcome == "abandoned"


# -- #1003: an allergy said into a protected sentence reaches the core ------------


def _order_read_back(session, tenant, port) -> CallHandler:
    """An order call up to its read-back, which waits for the guest's yes."""
    apply(session, tenant.id, menu_plan(), now=NOW)
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    for line in ORDER_READ_BACK[:-1]:
        handler.on_user_turn("s-1", line["text"])
    assert handler._live["s-1"].state.stage == "readback_pending"
    return handler


def test_an_allergy_said_into_the_read_back_is_read_back_before_the_yes(
    session, tenant
):
    """#1003, rules 2 and 3, L13: the guest names an allergy while the order is
    read back. The draft without it is set aside and read out again with the
    note, so only the yes after that books the order the kitchen can cook
    safely."""
    port = FakeTelephony()
    handler = _order_read_back(session, tenant, port)
    first = port.says("s-1")[-1]

    handler.on_talked_over("s-1", "Ich habe eine Erdnussallergie.")

    state = handler._live["s-1"].state
    assert state.allergy_said == ["Ich habe eine Erdnussallergie."]
    assert state.stage == "readback_pending" and state.readback_heard
    assert port.says("s-1")[-1] != first
    handler.on_user_turn("s-1", "Ja, passt so.")
    handler.on_call_ended("s-1")

    orders = session.scalars(
        select(Order).where(Order.call_id == the_call(session, "s-1").id)
    ).all()
    placed = [order for order in orders if order.status != "draft"]
    assert placed, [order.status for order in orders]
    assert all(speaks_of_health(order.note or "") for order in placed)


def test_a_yes_and_an_allergy_in_one_breath_into_the_read_back_book_nothing(
    session, tenant
):
    """#1003, rule 3: the yes said in the same utterance as the allergy, into
    the read-back, is no yes to it. Whatever the model makes of the sentence,
    the order without the note is not placed."""
    port = FakeTelephony()
    handler = _order_read_back(session, tenant, port)
    said = len(port.says("s-1"))

    handler.on_talked_over("s-1", "Ja, passt, ich habe eine Erdnussallergie.")
    # The words made a turn: the guest hears what became of them.
    assert len(port.says("s-1")) > said
    handler.on_call_ended("s-1")

    orders = session.scalars(
        select(Order).where(Order.call_id == the_call(session, "s-1").id)
    ).all()
    assert orders and {order.status for order in orders} == {"draft"}


def test_an_allergy_said_into_the_farewell_marks_the_open_order_card(session, tenant):
    """#1003: the guest stays silent on the read-back, the call ends, and the
    guest names an allergy into the farewell (heard on a recorded call). No
    turn, but the core keeps it: the card of the unanswered order leads with
    the kitchen marker, never the allergy (L13)."""
    port = FakeTelephony()
    handler = _order_read_back(session, tenant, port)
    for _ in range(len(STILL_THERE) + 1):
        handler.on_silence("s-1")
    assert port.hangups("s-1")

    handler.on_talked_over("s-1", "Ich habe eine Erdnussallergie.")
    handler.on_call_ended("s-1")

    [card] = _order_cards(session, "s-1")
    assert SUMMARY_READBACK_UNANSWERED in card.summary
    assert card.summary.startswith(SUMMARY_NOTE_TO_CLEAR)
    assert "Erdnuss" not in card.summary


def test_a_yes_into_the_read_back_with_no_allergy_makes_no_turn(session, tenant):
    """Only an allergy makes a turn of words said into the read-back: a yes
    said there still books nothing (rule 3)."""
    port = FakeTelephony()
    handler = _order_read_back(session, tenant, port)
    said = len(port.says("s-1"))

    handler.on_talked_over("s-1", "Ja, passt so.")

    state = handler._live["s-1"].state
    assert len(port.says("s-1")) == said
    assert state.stage == "readback_pending" and not state.allergy_said


def test_an_allergy_said_into_another_protected_sentence_is_kept_without_a_turn(
    session, tenant
):
    """#1003: into a protected sentence while no draft waits for its yes (the
    session decides which sentence is protected; the handler takes what it
    hands over as such) the words still make no turn, and the guest hears
    nothing new; the allergy is kept for the safety net of every later draft."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port)
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    handler.on_user_turn("s-1", "Ich haette gern einen Tisch fuer zwei.")
    said = len(port.says("s-1"))

    handler.on_talked_over("s-1", "Ich habe eine Nussallergie.")

    state = handler._live["s-1"].state
    assert len(port.says("s-1")) == said
    assert state.allergy_said == ["Ich habe eine Nussallergie."]
    assert state.allergy_talk
