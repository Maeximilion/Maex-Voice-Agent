"""telephony/router.py and app.py: the service `voice` on its WebSocket (T-1.11).

The test client plays Asterisk: it connects to `/media` as the dialplan dials it,
sends the frames Asterisk recorded on the workbench (`fixtures/asterisk_media_*`)
and answers the marks the line sends. Then it asks `/calls/{id}/ending` as the
dialplan does when our leg is gone. No network, no Asterisk.
"""

import asyncio
import base64
import json
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
from api.telephony import router as router_module
from api.telephony.adapters.fake_speech import FakeEar, FakeMouth
from api.telephony.app import Voice, create_app, voice_from_settings
from api.telephony.handler import GREETING
from api.telephony.router import _write
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
# The call the recorded MEDIA_START names (`__VOICE_CALL`, `__VOICE_CALLER`).
CALL = "1791981978.0"
TEAM = "+4972215550900"


def basic(password: str, user: str = "asterisk") -> dict[str, str]:
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"authorization": f"Basic {credentials}"}


def media_start(**variables: str | None) -> str:
    """The recorded MEDIA_START, its channel variables changed as given (None
    takes one out)."""
    frame = json.loads(recorded_event("asterisk_media_marks.jsonl", "MEDIA_START"))
    for name, value in variables.items():
        if value is None:
            frame["channel_variables"].pop(name, None)
        else:
            frame["channel_variables"][name] = value
    return json.dumps(frame)


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


def connect(client: TestClient, headers: dict | None = None):
    """As Asterisk connects since T-1.11: no query, the call is named in
    MEDIA_START."""
    return client.websocket_connect(
        "/media",
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


def test_a_frame_that_is_not_the_format_is_a_failure_and_no_hangup(phone):
    """The core hears of a failure of the voice layer, not of a caller who hung
    up, and the dialplan finds no ending (code review of T-1.11)."""
    client, core = phone
    with connect(client) as ws:
        ws.send_text(media_start())
        assert receive_command(ws) == {"command": "ANSWER"}
        ws.send_text("MEDIA_START connection_id:voice format:ulaw")  # plain-text format
        # The greeting's frames, then the service closes its side.
        while ws.receive()["type"] != "websocket.close":
            pass

    wait_for(lambda: ("failure",) in core.seen)
    assert ending(client) == ""


class _Socket:
    """Asterisk's end of the connection: it stalls, or it is gone."""

    def __init__(self, gone: bool) -> None:
        self.gone = gone
        self.closed = False

    async def send_text(self, text: str) -> None:
        if self.gone:
            raise RuntimeError("Cannot call send once a close message has been sent")
        await asyncio.sleep(60)

    async def close(self) -> None:
        self.closed = True


class _Told:
    """What the writer tells the session."""

    def __init__(self) -> None:
        self.told: list[str] = []

    def broken(self) -> None:
        self.told.append("broken")

    def closed(self) -> None:
        self.told.append("closed")


@pytest.mark.parametrize(("gone", "told"), [(False, "broken"), (True, "closed")])
def test_the_writer_tells_a_stalled_leg_from_one_that_is_gone(gone, told):
    """A stall is a failure of the voice layer, a send into a closed connection
    a caller who hung up; either way the session hears it at once (Codex on
    PR #297)."""

    async def run() -> tuple[_Socket, _Told]:
        websocket, session = _Socket(gone), _Told()
        outbox: asyncio.Queue = asyncio.Queue()
        outbox.put_nowait('{"command": "ANSWER"}')
        await _write(websocket, outbox, session)  # type: ignore[arg-type]
        return websocket, session

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(router_module, "WRITE_SECONDS", 0.05)
        websocket, session = asyncio.run(run())
    assert session.told == [told] and websocket.closed


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


@pytest.mark.parametrize("call", [None, "", "abc", "1.2;3", "1791966129.0/../x"])
def test_a_connection_whose_media_start_names_no_call_is_closed(phone, call):
    """No session, no core: the dialplan finds no ending and rings the team."""
    client, core = phone
    with connect(client) as ws:
        ws.send_text(media_start(__VOICE_CALL=call))
        assert ws.receive()["type"] == "websocket.close"
    assert core.seen == []


def test_a_first_frame_that_is_no_media_start_is_closed(phone):
    client, core = phone
    with connect(client) as ws:
        ws.send_bytes(bytes(640))
        assert ws.receive()["type"] == "websocket.close"
    assert core.seen == []


def test_the_call_and_the_caller_come_from_media_start_not_the_url(phone):
    """The URL carries nothing (Asterisk #2194 hands v() parameters to other
    calls, and logs the URL); a query is ignored."""
    client, core = phone
    with client.websocket_connect(
        "/media?call=1.1&caller=%2B4900", subprotocols=["media"], headers=basic(TOKEN)
    ) as ws:
        ws.send_text(media_start(__VOICE_CALL="1791999999.7"))
        assert receive_command(ws) == {"command": "ANSWER"}
    assert core.seen[0] == ("started", "1791999999.7")


def test_a_second_connection_for_a_live_call_is_refused(phone):
    """The first keeps the call: a second session would take its place
    (Codex on PR #297)."""
    client, core = phone
    with connect(client) as first:
        first.send_text(media_start())
        assert receive_command(first) == {"command": "ANSWER"}
        with connect(client) as second:
            second.send_text(media_start())
            assert second.receive()["type"] == "websocket.close"
        # The first call is still the one the core reaches, and it goes on:
        # its greeting is still playing.
        assert client.app.state.voice.lines.caller_id(CALL) == "+4972215550701"
        first.send_text(processed(sentence(first)))
    assert core.seen.count(("started", CALL)) == 1


def test_a_media_start_the_line_cannot_take_is_a_failure(phone):
    """It names a call, so a session is made; its format is not ours, so the
    line refuses it. The core hears of a failure and the session goes (own
    review of PR #297)."""
    client, core = phone
    with connect(client) as ws:
        ws.send_text(json.dumps({**json.loads(media_start()), "format": "ulaw"}))
        while ws.receive()["type"] != "websocket.close":
            pass
    wait_for(lambda: ("failure",) in core.seen)
    wait_for(lambda: client.app.state.voice.lines.caller_id(CALL) is None)
    assert ending(client) != "hangup"


def test_a_connection_gone_before_media_start_leaves_nothing(phone, caplog):
    client, core = phone
    with connect(client) as ws:
        ws.close()
    time.sleep(0.1)
    assert core.seen == []
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


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
    with pytest.raises(NotFound) as missing:
        tenant_named(session, "Nobody")
    # The restaurant's name stays out of the message, which ends in a log
    # (Codex on PR #297).
    assert "Nobody" not in str(missing.value)
    assert "0 tenants" in str(missing.value)
