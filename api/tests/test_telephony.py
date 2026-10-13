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
from api.telephony.handler import FAREWELL, SAY_OUTAGE, STILL_THERE, CallHandler
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
    other = resolve_tenant(session, "Zweitbetrieb")
    other = SimpleNamespace(id=other.id, timezone=other.timezone, name=other.name)
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

    def __init__(self, session, tenant):
        from api.telephony.adapters.fake_speech import FakeEar, FakeLine, FakeMouth
        from api.telephony.session import CallSession, Lines

        self.now = 50.0
        self.line, self.ear, self.lines = FakeLine(), FakeEar(), Lines()
        self.call = CallSession(
            "voice-1",
            line=self.line,
            ear=self.ear,
            mouth=FakeMouth(),
            events=handler_for(session, tenant, self.lines),
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
