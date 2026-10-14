"""telephony/router.py and app.py: the service `voice` on its WebSocket (T-1.11).

The test client plays Asterisk: it connects to `/media` as the dialplan dials it,
sends the frames Asterisk recorded on the workbench (`fixtures/asterisk_media_*`)
and answers the marks the line sends. Then it asks `/calls/{id}/ending` as the
dialplan does when our leg is gone. No network, no Asterisk.
"""

import base64
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from api.agent.llm import LLMError
from api.config import settings
from api.core.errors import NotFound
from api.domain.calls import tenant_named
from api.telephony import app as voice_app
from api.telephony.adapters.fake_speech import FakeEar, FakeMouth
from api.telephony.app import Voice, create_app, voice_from_settings, without_query
from api.telephony.handler import GREETING
from api.telephony.session import Lines
from api.tests.test_telephony import (  # noqa: F401 - fixtures
    TEAM_PHONE,
    handler_for,
    session,
    set_mode,
    tenant,
    the_call,
)
from api.tests.test_telephony_asterisk import recorded_event

TOKEN = "media-token-for-tests-only-0123456789"
CALL = "1791966129.0"
TEAM = "+4972215550900"


def basic(password: str, user: str = "asterisk") -> dict[str, str]:
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"authorization": f"Basic {credentials}"}


def media_start() -> str:
    return recorded_event("asterisk_media_marks.jsonl", "MEDIA_START")


def processed(correlation_id: str) -> str:
    return recorded_event(
        "asterisk_media_marks.jsonl",
        "MEDIA_MARK_PROCESSED",
        correlation_id=correlation_id,
    )


class Core:
    """Stands in for the call handler: greets, ends the call on keys, and does
    what a test tells it at the start."""

    def __init__(self, lines: Lines) -> None:
        self.lines = lines
        self.seen: list[tuple[str, ...]] = []
        self.at_start = "greet"

    def on_call_started(self, session_id: str) -> None:
        self.seen.append(("started", session_id))
        if self.at_start == "greet":
            self.lines.say(session_id, "Guten Tag")
        elif self.at_start == "transfer":
            self.lines.transfer(session_id, TEAM)
        elif self.at_start == "crash":
            raise RuntimeError("the core broke")

    def on_user_turn(self, session_id: str, text: str) -> None:
        self.seen.append(("turn", text))

    def on_dtmf(self, session_id: str, digits: str) -> None:
        self.seen.append(("keys", digits))
        self.lines.say(session_id, "Auf Wiederhoeren")
        self.lines.hangup(session_id)

    def on_silence(self, session_id: str) -> None:
        self.seen.append(("silence",))

    def on_voice_failure(self, session_id: str) -> None:
        self.seen.append(("failure",))

    def on_call_ended(self, session_id: str) -> None:
        self.seen.append(("ended",))


@pytest.fixture
def phone():
    lines = Lines()
    core = Core(lines)
    voice = Voice(
        events=core,
        lines=lines,
        token=TOKEN,
        ear=FakeEar,
        mouth=FakeMouth,
        silence_seconds=60.0,
    )
    with TestClient(create_app(voice)) as client:
        yield client, core


def connect(client: TestClient, call: str = CALL, headers: dict | None = None):
    return client.websocket_connect(
        f"/media?call={call}&caller=%2B4972215550701",
        subprotocols=["media"],
        headers=basic(TOKEN) if headers is None else headers,
    )


def receive_command(ws) -> dict:
    """The next control frame; audio in between is skipped."""
    while True:
        message = ws.receive()
        if message.get("text") is not None:
            return json.loads(message["text"])
        assert message.get("bytes") is not None, message


def sentence(ws) -> str:
    """A sentence went out: its audio, a chunk mark and its own mark. The
    chunk is acknowledged; the mark of the sentence is returned."""
    chunk = receive_command(ws)
    assert chunk["command"] == "MARK_MEDIA" and chunk["correlation_id"].startswith("c")
    ws.send_text(processed(chunk["correlation_id"]))
    mark = receive_command(ws)
    assert mark["command"] == "MARK_MEDIA" and mark["correlation_id"].startswith("s")
    return mark["correlation_id"]


def ending(client: TestClient, call: str = CALL) -> str:
    response = client.get(f"/calls/{call}/ending", headers=basic(TOKEN))
    assert response.status_code == 200
    return response.text


def wait_for(condition, seconds: float = 3.0) -> None:
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_a_call_is_answered_spoken_to_and_ended_by_our_hangup(phone):
    client, core = phone
    with connect(client) as ws:
        assert ws.accepted_subprotocol == "media"
        ws.send_text(media_start())
        assert receive_command(ws) == {"command": "ANSWER"}
        ws.send_text(processed(sentence(ws)))  # the greeting was played
        for key in ("1", "#"):
            ws.send_text(
                recorded_event("asterisk_media_two_way.jsonl", "DTMF_END", digit=key)
            )
        ws.send_text(processed(sentence(ws)))  # the farewell was played
        assert receive_command(ws) == {"command": "HANGUP"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()  # the service closes its side as well

    assert core.seen == [("started", CALL), ("keys", "1")]
    assert ending(client) == "hangup"
    # Asked once: a second question gets nothing, which rings the team.
    assert ending(client) == ""


def test_the_hangup_waits_until_the_farewell_was_played(phone):
    client, _ = phone
    with connect(client) as ws:
        ws.send_text(media_start())
        receive_command(ws)  # ANSWER
        ws.send_text(processed(sentence(ws)))
        ws.send_text(
            recorded_event("asterisk_media_two_way.jsonl", "DTMF_END", digit="#")
        )
        ws.send_text(
            recorded_event("asterisk_media_two_way.jsonl", "DTMF_END", digit="7")
        )
        ws.send_text(
            recorded_event("asterisk_media_two_way.jsonl", "DTMF_END", digit="#")
        )
        sentence(ws)  # the farewell is on the line, its mark not reported
        time.sleep(0.2)
        assert ending(client) == ""
        ws.close()


def test_a_transfer_before_any_sentence_leaves_the_call_unanswered(phone):
    """Mode `paused` and the like: the team takes the call, which we never
    picked up (docs/20 §4, recorded in asterisk_media_unanswered)."""
    client, core = phone
    core.at_start = "transfer"
    with connect(client) as ws:
        ws.send_text(media_start())
        assert receive_command(ws) == {"command": "HANGUP"}

    assert ending(client) == f"transfer {TEAM}"


def test_a_session_that_breaks_closes_the_leg_and_leaves_no_ending(phone):
    """The dialplan finds no ending and rings the team (recorded in
    asterisk_media_closed: a connection closed without HANGUP)."""
    client, core = phone
    core.at_start = "crash"
    with connect(client) as ws:
        ws.send_text(media_start())
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()

    assert ("failure",) in core.seen
    assert ending(client) == ""


def test_a_caller_who_hangs_up_ends_the_call_in_the_core(phone):
    client, core = phone
    with connect(client) as ws:
        ws.send_text(media_start())
        receive_command(ws)  # ANSWER
    wait_for(lambda: ("ended",) in core.seen)
    assert ending(client) == ""


def test_a_frame_that_is_not_the_format_ends_our_leg_without_an_ending(phone):
    client, core = phone
    with connect(client) as ws:
        ws.send_text("MEDIA_START connection_id:voice format:ulaw")  # plain-text format
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()

    assert ending(client) == ""
    wait_for(lambda: ("ended",) in core.seen)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        basic("wrong-token"),
        {"authorization": f"Bearer {TOKEN}"},
        {"authorization": "Basic not-base64!"},
    ],
)
def test_the_media_door_wants_the_token(phone, headers):
    client, core = phone
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        connect(client, headers=headers),
    ):
        pass
    assert refused.value.code == 1008
    assert core.seen == []


@pytest.mark.parametrize("call", ["", "abc", "1.2;3", "1791966129.0/../x"])
def test_the_media_door_wants_a_call_id_the_dialplan_can_ask_by(phone, call):
    client, core = phone
    with pytest.raises(WebSocketDisconnect), connect(client, call=call):
        pass
    assert core.seen == []


def test_the_ending_door_wants_the_token(phone):
    client, _ = phone
    assert client.get(f"/calls/{CALL}/ending").status_code == 401
    assert client.get(f"/calls/{CALL}/ending", headers=basic("x")).status_code == 401


def test_the_user_name_of_the_credentials_is_not_what_counts(phone):
    client, _ = phone
    response = client.get(f"/calls/{CALL}/ending", headers=basic(TOKEN, user="any"))
    assert response.status_code == 200


def test_without_a_token_nothing_is_let_in():
    lines = Lines()
    voice = Voice(
        events=Core(lines),
        lines=lines,
        token="",
        ear=FakeEar,
        mouth=FakeMouth,
        silence_seconds=60.0,
    )
    assert not voice.authorized(basic("")["authorization"])


def test_the_callers_number_in_the_url_stays_out_of_the_log():
    """uvicorn logs each WebSocket with its query string, where the dialplan
    puts the caller's number (security review of T-1.11)."""
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        1,
        '%s - "WebSocket %s" [accepted]',
        ("172.19.0.5:43656", "/media?call=1791968728.0&caller=%2B4972215550101"),
        None,
    )

    assert without_query(record)
    assert record.getMessage() == '172.19.0.5:43656 - "WebSocket /media" [accepted]'


def test_health_answers_without_a_token(phone):
    client, _ = phone
    assert client.get("/health").text == "ok"


# -- with the real call handler and a database ----------------------------------------


def voice_for(session, tenant) -> Voice:  # noqa: F811 - fixtures
    lines = Lines()
    return Voice(
        events=handler_for(session, tenant, lines),
        lines=lines,
        token=TOKEN,
        ear=FakeEar,
        mouth=FakeMouth,
        silence_seconds=60.0,
    )


def test_a_paused_line_hands_the_call_to_the_team_unanswered(session, tenant):  # noqa: F811
    """The team number comes from the database, and we never pick up."""
    set_mode(session, tenant.id, "paused")
    voice = voice_for(session, tenant)
    with TestClient(create_app(voice)) as client:
        with connect(client) as ws:
            ws.send_text(media_start())
            assert receive_command(ws) == {"command": "HANGUP"}
        assert ending(client) == f"transfer {TEAM_PHONE}"
    wait_for(lambda: voice.lines.caller_id(CALL) is None)
    assert the_call(session, CALL).caller_id == "+4972215550701"


def test_a_call_is_greeted_and_logged_when_the_caller_hangs_up(session, tenant):  # noqa: F811
    voice = voice_for(session, tenant)
    with TestClient(create_app(voice)) as client:
        with connect(client) as ws:
            ws.send_text(media_start())
            assert receive_command(ws) == {"command": "ANSWER"}
            audio = ws.receive_bytes()
            sentence(ws)
        wait_for(lambda: voice.lines.caller_id(CALL) is None)
        assert ending(client) == ""

    # The fake mouth's audio is the sentence itself, padded to whole frames.
    assert audio.rstrip(b"\x00").decode() == GREETING.format(name=tenant.name)
    call = the_call(session, CALL)
    assert call.ended_at is not None and call.outcome == "abandoned"


# -- what the service needs to start ----------------------------------------------------


@pytest.fixture
def startable(monkeypatch):
    """Settings the service starts with, but for the database."""
    for name, value in {
        "voice_media_token": TOKEN,
        "voice_number": "+4972215550100",
        "voice_ear": "fake",
        "voice_mouth": "fake",
        "llm_base_url": "http://model.invalid",
        "llm_model": "test",
        "env": "test",
    }.items():
        monkeypatch.setattr(settings, name, value)
    return monkeypatch


@pytest.mark.parametrize(
    ("name", "value", "why"),
    [
        ("voice_media_token", "", "VOICE_MEDIA_TOKEN is empty"),
        ("voice_number", "", "VOICE_NUMBER"),
        ("voice_number", "07221 5550100", "VOICE_NUMBER"),
        ("voice_ear", "", "no ear"),
        ("voice_mouth", "whisper", "no mouth"),
    ],
)
def test_the_service_does_not_start_without_what_a_call_needs(
    startable, name, value, why
):
    startable.setattr(settings, name, value)
    with pytest.raises(RuntimeError, match=why):
        voice_from_settings()


def test_outside_dev_and_test_the_drill_engines_and_a_short_token_are_refused(
    startable,
):
    startable.setattr(settings, "env", "prod")
    startable.setattr(settings, "voice_media_token", "x" * 31)
    with pytest.raises(RuntimeError, match="shorter than"):
        voice_from_settings()
    startable.setattr(settings, "voice_media_token", "x" * 32)
    with pytest.raises(RuntimeError, match="drill engines"):
        voice_from_settings()


def test_the_service_does_not_start_without_a_model(startable):
    startable.setattr(settings, "llm_base_url", "")
    with pytest.raises(LLMError):
        voice_from_settings()


@pytest.fixture
def its_database(startable, migrated_db_url, tenant):  # noqa: F811
    startable.setattr(settings, "tenant_name", tenant.name)
    startable.setattr(settings, "team_phone", "+4972215550999")
    startable.setattr(
        voice_app, "SessionLocal", sessionmaker(bind=create_engine(migrated_db_url))
    )
    return startable


def test_the_service_starts_for_its_tenant(its_database):
    voice = voice_from_settings()
    assert voice.authorized(basic(TOKEN)["authorization"])


@pytest.mark.parametrize("team", ["database", "settings"])
def test_the_service_does_not_start_when_the_team_is_its_own_number(its_database, team):
    """The team would ring us, and we the team (docs/20 §5)."""
    own = TEAM_PHONE if team == "database" else "+4972215550999"
    its_database.setattr(settings, "voice_number", own)
    with pytest.raises(RuntimeError, match="own number"):
        voice_from_settings()


def test_a_tenant_is_found_by_its_name_and_only_when_it_is_the_one(session, tenant):  # noqa: F811
    assert tenant_named(session, tenant.name) == tenant.id
    with pytest.raises(NotFound):
        tenant_named(session, "Nobody")
