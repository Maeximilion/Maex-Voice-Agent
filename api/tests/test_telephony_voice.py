"""telephony/router.py and app.py: the service `voice` on its WebSocket (T-1.11).

The test client plays Asterisk: it connects to `/media` as the dialplan dials it,
sends the frames Asterisk recorded on the workbench (`fixtures/asterisk_media_*`)
and answers the marks the line sends. Then it asks `/calls/{id}/ending` as the
dialplan does when our leg is gone. No network, no Asterisk.
"""

import asyncio
import base64
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
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

    # The end of the leg is reported after our hangup too: the core closes its
    # call log then, not when it asked for the hangup (T-1.11 part 2).
    assert core.seen == [("started", CALL), ("keys", "1"), ("ended",)]
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
    # No watchdog thread is left behind: it would end the test process with
    # `os._exit` once a watched call took long.
    monkeypatch.setattr(voice_app.Watchdog, "start", lambda self: None)
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
def its_database(startable, migrated_db_url, bounded_db, tenant):  # noqa: F811
    startable.setattr(settings, "tenant_name", tenant.name)
    startable.setattr(settings, "team_phone", "+4972215550999")
    engine = create_engine(migrated_db_url)
    startable.setattr(voice_app, "SessionLocal", sessionmaker(bind=engine))
    startable.setattr(voice_app, "bounded_sessions", bounded_db)
    yield startable
    engine.dispose()


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


# -- the engines in the service and their watchdog (T-1.11 part 2) ----------------------


class _Exit:
    def __init__(self) -> None:
        self.codes: list[int] = []

    def __call__(self, code: int) -> None:
        self.codes.append(code)


def watchdog_at(clock: list[float]) -> tuple[voice_app.Watchdog, _Exit]:
    end = _Exit()
    return voice_app.Watchdog(limit=30.0, clock=lambda: clock[0], end=end), end


def test_an_engine_call_that_hangs_ends_the_process():
    """A thread cannot be cancelled, and the model is shared by every call: the
    process ends and Compose starts it again (docs/20 §5)."""
    clock = [100.0]
    watchdog, end = watchdog_at(clock)
    with watchdog.watch("ear"):
        clock[0] += 30.0
        watchdog.check()
        assert end.codes == []  # at the limit, not past it
        clock[0] += 0.5
        watchdog.check()
    assert end.codes == [voice_app.EXIT_ENGINE_HUNG]


def test_an_engine_call_that_returned_is_not_watched_any_more():
    clock = [100.0]
    watchdog, end = watchdog_at(clock)
    transcribe = watchdog.watched("ear", lambda audio, terms: "ja")
    assert transcribe(b"", []) == "ja"
    with pytest.raises(ZeroDivisionError):
        watchdog.watched("ear", lambda: 1 / 0)()
    clock[0] += 3600.0
    watchdog.check()
    assert end.codes == []


def test_the_mouth_is_watched_while_it_speaks():
    clock = [100.0]
    watchdog, end = watchdog_at(clock)

    class Slow:
        def speak(self, text):
            yield b"\x01\x00"
            clock[0] += 31.0  # the engine hangs between two pieces
            watchdog.check()
            yield b"\x02\x00"

    assert b"".join(voice_app.WatchedMouth(Slow(), watchdog).speak("Hallo")) == (
        b"\x01\x00\x02\x00"
    )
    assert end.codes == [voice_app.EXIT_ENGINE_HUNG]


def test_ear_and_mouth_that_cannot_be_loaded_keep_the_service_down(
    its_database, tmp_path
):
    """Without the engines' packages, as here in the API's image: refused at
    start, and the dialplan rings the team meanwhile. Without their model files
    in the image of `voice`: test_telephony_engine_loading.py."""
    its_database.setattr(voice_app, "MODELS", tmp_path)
    its_database.setattr(settings, "voice_ear", "whisper")
    with pytest.raises(RuntimeError, match="could not be loaded"):
        voice_from_settings()
    its_database.setattr(settings, "voice_ear", "fake")
    its_database.setattr(settings, "voice_mouth", "piper")
    with pytest.raises(RuntimeError, match="could not be loaded"):
        voice_from_settings()


def test_the_phone_line_waits_on_the_database_no_longer_than_its_bound(its_database):
    voice = voice_from_settings()
    with voice._events._session_factory() as db:  # type: ignore[attr-defined]
        assert db.scalar(text("show statement_timeout")) == (
            f"{voice_app.DB_TIMEOUT_MS // 1000}s"
        )


def test_a_call_that_waits_for_the_engine_is_not_taken_for_a_hang():
    """One model serves every call of the process. A call queued behind
    another is not stuck: only the time the engine works on it counts (ultra
    review on PR #298)."""
    clock = [100.0]
    watchdog, end = watchdog_at(clock)
    runs = [(threading.Event(), threading.Event()) for _ in range(2)]

    def engine(running: threading.Event, done: threading.Event) -> str:
        running.set()
        done.wait(5)
        return ""

    transcribe = watchdog.watched("ear", engine)
    first = threading.Thread(target=transcribe, args=runs[0])
    first.start()
    assert runs[0][0].wait(5)
    clock[0] += 1.0
    second = threading.Thread(target=transcribe, args=runs[1])
    second.start()
    # On the code under test the result does not hang on the scheduler: the
    # second call cannot be stamped before the first returns. The pause only
    # lets code without the line stamp it early and fail reliably.
    time.sleep(0.2)
    clock[0] += 28.0  # the first worked 29 s
    runs[0][1].set()
    assert runs[1][0].wait(5)
    clock[0] += 3.0  # the second waited 28 s and worked 3
    watchdog.check()
    runs[1][1].set()
    first.join(5)
    second.join(5)
    assert end.codes == []


@pytest.fixture
def stand_in_engines(monkeypatch):
    """The engines' classes stand in for the packages, which only the image of
    `voice` has; what the loaders do with them is checked here."""
    from api.telephony.adapters import stt_whisper, tts_piper

    runs: list[tuple[str, object]] = []

    class Engine:
        def __init__(self, model, models, **kwargs):
            pass

        def transcribe(self, pcm: bytes, terms: list[str]) -> str:
            runs.append(("ear", len(pcm)))
            return ""

    class Mouth:
        def speak(self, text: str):
            runs.append(("mouth", text))
            yield b"\x01\x00"

    def piper(voice):
        runs.append(("voice", voice.name))
        return Mouth()

    monkeypatch.setattr(stt_whisper, "Whisper", Engine)
    monkeypatch.setattr(settings, "voice_ear_device", "cuda")

    def silero():
        def probability(window: bytes) -> float:
            runs.append(("detector", len(window)))
            return 0.0

        return probability

    monkeypatch.setattr(stt_whisper, "silero", silero)
    monkeypatch.setattr(tts_piper, "piper", piper)
    return runs


def test_each_engine_has_run_once_before_the_service_answers(stand_in_engines):
    """An engine that loads may still fail on its first run, when the
    graphics card's libraries are set up: at start, where the service is
    refused, not in a caller's first sentence (ultra review on PR #298)."""
    watchdog = voice_app.Watchdog()
    voice_app._whisper(watchdog)
    voice_app._piper(watchdog)
    assert [kind for kind, _ in stand_in_engines] == [
        "ear",
        "detector",
        "voice",
        "mouth",
    ]
    assert stand_in_engines[0][1] > 0  # audio, not an empty buffer


def test_the_mouth_speaks_the_engines_own_voice_unless_one_is_set(
    stand_in_engines, monkeypatch
):
    from api.telephony.adapters import tts_piper

    monkeypatch.setattr(settings, "voice_mouth_voice", "")
    voice_app._piper(voice_app.Watchdog())
    monkeypatch.setattr(settings, "voice_mouth_voice", "de_DE-other-high")
    voice_app._piper(voice_app.Watchdog())
    voices = [name for kind, name in stand_in_engines if kind == "voice"]
    assert voices == [f"{tts_piper.VOICE}.onnx", "de_DE-other-high.onnx"]


def test_an_engine_that_loads_but_cannot_run_keeps_the_service_down(
    its_database, stand_in_engines, monkeypatch
):
    from api.telephony.adapters import stt_whisper

    def cannot_run(self, pcm, terms):
        raise RuntimeError("Unable to load libcudnn_ops.so")

    monkeypatch.setattr(stt_whisper.Whisper, "transcribe", cannot_run)
    its_database.setattr(settings, "voice_ear", "whisper")
    with pytest.raises(RuntimeError, match="could not be loaded"):
        voice_from_settings()


def test_the_watchdog_runs_before_the_engines_first_runs(
    its_database, stand_in_engines, monkeypatch
):
    """A first run that hangs, rather than raising, ends the process and
    Compose starts it again; without the watchdog it would never start
    (Codex on PR #310)."""
    from api.telephony.adapters import stt_whisper

    order = []
    monkeypatch.setattr(
        voice_app.Watchdog, "start", lambda self: order.append("watchdog")
    )
    monkeypatch.setattr(
        stt_whisper.Whisper,
        "transcribe",
        lambda self, pcm, terms: order.append("ear") or "",
    )
    its_database.setattr(settings, "voice_ear", "whisper")
    voice_from_settings()
    assert order == ["watchdog", "ear"]


def test_a_mouth_that_makes_no_audio_is_not_loaded(stand_in_engines, monkeypatch):
    """The session takes an empty stream for a failure of the mouth; at start
    it keeps the service down (Codex on PR #310)."""
    from api.telephony.adapters import tts_piper

    class Silent:
        def speak(self, text):
            return iter(())

    monkeypatch.setattr(tts_piper, "piper", lambda voice: Silent())
    with pytest.raises(RuntimeError, match="no audio"):
        voice_app._piper(voice_app.Watchdog())


def test_the_mouth_makes_the_sentences_of_several_calls_side_by_side():
    """In line they would wait past the session's `SPEAK_SECONDS` without
    anything hanging (ultra review on PR #310)."""
    clock = [100.0]
    watchdog, _ = watchdog_at(clock)
    speaking = {text: threading.Event() for text in ("Eins.", "Zwei.")}
    met = []

    class Mouth:
        def speak(self, text):
            speaking[text].set()
            other = "Zwei." if text == "Eins." else "Eins."
            met.append(speaking[other].wait(2))
            yield b"\x01\x00"

    mouth = voice_app.WatchedMouth(Mouth(), watchdog)
    calls = [
        threading.Thread(target=lambda text=text: b"".join(mouth.speak(text)))
        for text in ("Eins.", "Zwei.")
    ]
    for call in calls:
        call.start()
    for call in calls:
        call.join(5)
    assert met == [True, True]


@pytest.mark.parametrize("ear", [True, False, "detector"])
def test_an_engine_that_hangs_while_it_loads_ends_the_process(
    stand_in_engines, monkeypatch, ear
):
    """A load gets as long as Compose gives the service to start; one that
    hangs past it, a graphics card in a bad state, ends the process instead
    of keeping it from ever listening (ultra review on PR #310)."""
    from api.telephony.adapters import stt_whisper, tts_piper

    clock = [100.0]
    watchdog, end = watchdog_at(clock)

    def loading(*args, **kwargs):
        clock[0] += voice_app.ENGINE_LOAD_SECONDS - 1
        watchdog.check()
        assert end.codes == []  # a slow load is no hang
        clock[0] += 2.0
        watchdog.check()
        raise RuntimeError("stopped here: the process would have ended")

    if ear == "detector":
        monkeypatch.setattr(stt_whisper, "silero", loading)
    elif ear:
        monkeypatch.setattr(stt_whisper, "Whisper", loading)
    else:
        monkeypatch.setattr(tts_piper, "piper", loading)
    with pytest.raises(RuntimeError, match="stopped here"):
        (voice_app._whisper if ear else voice_app._piper)(watchdog)
    assert end.codes == [voice_app.EXIT_ENGINE_HUNG]


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_a_slow_run_of_the_ear_is_a_hang_only_past_its_devices_limit(
    stand_in_engines, monkeypatch, device
):
    """On the graphics card 30 s is a hang. On the processor a minute of
    unsure speech may take longer, and ending the process costs every call a
    restart: a hang there only past the time of a restart. The first run on
    the graphics card loads its libraries and is watched as a load (own
    review on PR #310)."""
    from api.telephony.adapters import stt_whisper

    clock = [100.0]
    watchdog, end = watchdog_at(clock)
    takes = [31.0]

    def slow(self, pcm, terms):
        clock[0] += takes[0]
        watchdog.check()
        return ""

    monkeypatch.setattr(settings, "voice_ear_device", device)
    monkeypatch.setattr(stt_whisper.Whisper, "transcribe", slow)
    transcribe = voice_app._whisper(watchdog)()._transcribe
    assert end.codes == []
    transcribe(b"\x00\x00", [])
    if device == "cuda":
        assert end.codes == [voice_app.EXIT_ENGINE_HUNG]
        return
    assert end.codes == []
    takes[0] = voice_app.ENGINE_LOAD_SECONDS + 1
    transcribe(b"\x00\x00", [])
    assert end.codes == [voice_app.EXIT_ENGINE_HUNG]


def test_a_limit_of_zero_is_no_default():
    clock = [100.0]
    watchdog, end = watchdog_at(clock)
    with watchdog.watch("ear", 0.0):
        clock[0] += 0.5
        watchdog.check()
    assert end.codes == [voice_app.EXIT_ENGINE_HUNG]


def test_on_the_processor_the_ear_is_not_run_at_start(stand_in_engines, monkeypatch):
    """No graphics card libraries to set up there, and the run took 4.5 s of
    every start (ultra review on PR #310). The detector still runs."""
    monkeypatch.setattr(settings, "voice_ear_device", "cpu")
    voice_app._whisper(voice_app.Watchdog())
    assert [kind for kind, _ in stand_in_engines] == ["detector"]
