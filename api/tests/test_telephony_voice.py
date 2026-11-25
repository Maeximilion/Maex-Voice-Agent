"""telephony/router.py and app.py: the service `voice` on its WebSocket (T-1.11).

The test client plays Asterisk: it connects to `/media` as the dialplan dials it,
sends the frames Asterisk recorded on the workbench (`fixtures/asterisk_media_*`)
and answers the marks the line sends. Then it asks `/calls/{id}/ending` as the
dialplan does when our leg is gone. No network, no Asterisk.
"""

import asyncio
import base64
import json
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from api.agent.llm import LLMError
from api.agent.recording import SAY_WHAT_CAN_I_DO
from api.config import settings
from api.core.errors import NotFound
from api.domain.calls import tenant_named
from api.models import Callback
from api.telephony import app as voice_app
from api.telephony import router as router_module
from api.telephony import watchdog as voice_watchdog
from api.telephony.adapters import speech_engines, tts_piper
from api.telephony.adapters.fake_speech import FakeEar, FakeMouth
from api.telephony.app import Voice, create_app, voice_from_settings
from api.telephony.handler import ANNOUNCEMENTS, GREETING
from api.telephony.router import _write
from api.telephony.session import Lines
from api.tests.test_compose_ports import _load
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
        self.stopped = True

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

    def on_talked_over(self, session_id: str, text: str) -> None:
        self.seen.append(("talked_over", text))

    def on_unheard(self, session_id: str, unheard: str) -> None:
        self.seen.append(("unheard", unheard))

    def on_call_ended(self, session_id: str, speech=None, **_) -> None:
        self.seen.append(("ended",))

    def on_recording_stopped(self, session_id: str) -> bool:
        self.seen.append(("recording_stopped", session_id))
        return self.stopped

    def on_team_missed(self, session_id: str, *, left: bool = False) -> bool:
        self.seen.append(("missed", session_id))
        self.left = left
        return self.card_written

    card_written = True
    team_phone = TEAM


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
        team_phone=lambda: core.team_phone,
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
        team_phone=lambda: TEAM,
    )
    assert not voice.authorized(basic("")["authorization"])


def test_health_answers_without_a_token(phone):
    client, _ = phone
    assert client.get("/health").text == "ok"


def test_media_ready_wants_the_media_token(phone):
    """The dialplan's question before it dials `/media` (#768): a `voice`
    whose token differs from Asterisk's would refuse the media connection."""
    client, _ = phone
    assert client.get("/media/ready").status_code == 401
    assert client.get("/media/ready", headers=basic("wrong")).status_code == 401
    assert client.get("/media/ready", headers=basic(TOKEN)).text == "ok"


def test_the_team_door_wants_the_token_and_answers_the_routed_number(phone):
    """The dialplan asks it when a call has come and before each leg it dials
    on its own (#835, #861): `voice` crashed, ended without a transfer, or a
    listed number nobody took."""
    client, _ = phone
    assert client.get("/team").status_code == 401
    assert client.get("/team", headers=basic("wrong")).status_code == 401
    response = client.get("/team", headers=basic(TOKEN))
    assert (response.status_code, response.text) == (200, TEAM)


def test_the_team_door_answers_nothing_when_the_number_is_unreadable(phone):
    """The dialplan keeps the number it had then, TEAM_PHONE of its
    environment at the call's start."""
    client, core = phone
    core.team_phone = ""
    response = client.get("/team", headers=basic(TOKEN))
    assert (response.status_code, response.text) == (200, "")


def test_the_dialplan_asks_for_the_team_number_after_the_probe():
    """After `/media/ready`, so a `voice` that is down costs the caller no
    second wait; before the Dial, so a `voice` that crashes in the call has
    answered already."""
    conf = DIALPLAN.read_text(encoding="utf-8")
    probe = conf.index("CURL(http://voice:8001/media/ready)")
    team = conf.index("Gosub(teamnow,1)")
    assert probe < team < conf.index("Dial(WebSocket/voice")


def _extension(name: str) -> list[str]:
    """The steps of one extension of [ai], from its first to the next one."""
    steps = [s.strip() for s in _steps()]
    start = next(i for i, s in enumerate(steps) if s.startswith(f"exten => {name},1,"))
    end = next(
        (i for i, s in enumerate(steps[start + 1 :], start + 1) if "exten =>" in s),
        len(steps),
    )
    return steps[start:end]


def test_the_team_number_is_asked_in_one_place_that_keeps_it_on_failure():
    """One subroutine asks `/team` (#861). Only a listed answer replaces
    TEAM; an empty one, a refused or frozen `voice` or a database that is
    down keep the number TEAM held, never nothing (rule 5)."""
    conf = DIALPLAN.read_text(encoding="utf-8")
    assert conf.count("CURL(http://voice:8001/team)") == 1
    ask = _extension("teamnow")
    assert [s for s in ask if "Set(TEAM=" in s] == [
        'same => n,ExecIf($["${ROUTED}" != "" & '
        "${FIELDNUM(LISTED,\\,,${ROUTED})} > 0]?Set(TEAM=${ROUTED}))"
    ]
    assert [s.strip() for s in _steps() if "Set(TEAM=" in s and "ExecIf" not in s] == [
        "same => n,Set(TEAM=${ENV(TEAM_PHONE)})"
    ]
    token = "same => n,Set(CURLOPT(userpwd)=asterisk:${ENV(VOICE_MEDIA_TOKEN)})"
    assert ask.index(token) < ask.index(
        "same => n,Set(ROUTED=${CURL(http://voice:8001/team)})"
    )


def test_an_empty_answer_is_the_last_question_of_the_call():
    """`voice` frozen or down, or its database down (2 s of connect timeout),
    would cost the caller that wait before every leg (#861 review): after an
    empty answer the call asks no more and keeps TEAM."""
    ask = _extension("teamnow")
    assert ask[0] == 'exten => teamnow,1,GotoIf($["${TEAMASK}" = "off"]?back)'
    off = ask.index('same => n,ExecIf($["${ROUTED}" = ""]?Set(TEAMASK=off))')
    assert ask[off - 1] == "same => n,Set(ROUTED=${FILTER(+0123456789,${ROUTED})})"
    assert ask[-1] == "same => n(back),Return()"


def test_a_voice_that_is_not_answering_is_not_asked_for_the_team_number():
    """An ending question that ran into its 2 s, or a `voice` the call never
    reached, turns the question off (#861 review): it would only cost the
    caller another wait before the team or the card."""
    steps = [s.strip() for s in _steps()]
    assert [s for s in steps if "TEAMASK=off" in s] == [
        "same => n,ExecIf($[${EPOCH} - ${ASKED} >= 2]?Set(TEAMASK=off))",
        'same => n,ExecIf($["${ROUTED}" = ""]?Set(TEAMASK=off))',
        "same => n,Set(TEAMASK=off)",
    ]
    ending = steps.index(
        "same => n,Set(ENDING=${CURL(http://voice:8001/calls/${UNIQUEID}/ending)})"
    )
    assert steps[ending - 1] == "same => n,Set(ASKED=${EPOCH})"
    # Right behind the filter, which stays right behind the question (#776).
    assert steps[ending + 2] == (
        "same => n,ExecIf($[${EPOCH} - ${ASKED} >= 2]?Set(TEAMASK=off))"
    )
    assert _extension("unreached")[1] == "same => n,Set(TEAMASK=off)"


def test_the_dialplan_asks_again_before_each_leg_it_dials_on_its_own():
    """A number changed on the tablet during the call reaches the legs the
    dialplan dials on its own (#861): the ending that is no transfer to a
    listed number, and every leg after one nobody took."""
    toteam = [s.strip() for s in _steps()]
    at = toteam.index("same => n(toteam),Gosub(teamnow,1)")
    assert toteam[at + 1 : at + 3] == [
        "same => n,Set(TARGET=${TEAM})",
        "same => n,Goto(team,1)",
    ]
    team = _extension("team")
    leg = team.index("same => n,Log(NOTICE,call ${UNIQUEID}: team leg ${DIALSTATUS})")
    assert team[leg + 1] == "same => n,Gosub(teamnow,1)"
    assert team[leg + 2].startswith("same => n,GotoIf($[${FIELDNUM(TRIED,\\,,${TEAM})}")


def test_every_subroutine_the_dialplan_calls_exists_and_returns():
    """A Gosub to an extension that is not there hangs the call up, the team
    never rings (rule 5)."""
    calls = re.findall(r"Gosub\((\w+),1\)", DIALPLAN.read_text(encoding="utf-8"))
    assert calls
    for name in set(calls):
        assert _extension(name)[-1].endswith(",Return()")


def test_the_routed_number_is_dialled_only_when_it_is_on_the_list():
    """A number `voice` names is never dialled off the list (#252 finding 3):
    TEAM is TEAM_PHONE of the environment unless the routed one is listed."""
    steps = [s.strip() for s in _steps()]
    default = steps.index("same => n,Set(TEAM=${ENV(TEAM_PHONE)})")
    listed = steps.index(
        'same => n,ExecIf($["${ROUTED}" != "" & '
        "${FIELDNUM(LISTED,\\,,${ROUTED})} > 0]?Set(TEAM=${ROUTED}))"
    )
    assert default < listed
    assert (
        steps.index("same => n,Set(LISTED=${ENV(TEAM_PHONE)},${ENV(TRANSFER_TARGETS)})")
        < listed
    )


def test_every_fallback_rings_the_routed_number_before_team_phone():
    """#835: `voice` not reached, an ending that is no transfer to a listed
    number, and a listed number nobody took all ring TEAM, the number the
    restaurant set; TEAM_PHONE of the environment only after it, once."""
    steps = [s.strip() for s in _steps()]
    targets = [s for s in steps if "Set(TARGET=" in s]
    assert targets == [
        "same => n,Set(TARGET=${FILTER(+0123456789,${ENDING:9})})",
        "same => n,Set(TARGET=${TEAM})",
        "same => n,Set(TARGET=${ENV(TEAM_PHONE)})",
        "same => n(routed),Set(TARGET=${TEAM})",
        "same => n,Set(TARGET=${TEAM})",
    ]
    team = steps.index("exten => team,1,Log(NOTICE,call ${UNIQUEID}: to the team)")
    chain = steps[team:]
    assert chain[1] == "same => n,Set(TRIED=${TRIED}${TARGET},)"
    routed = chain.index(
        "same => n,GotoIf($[${FIELDNUM(TRIED,\\,,${TEAM})} = 0]?routed)"
    )
    tried = chain.index(
        "same => n,GotoIf($[${FIELDNUM(TRIED,\\,,${ENV(TEAM_PHONE)})} > 0]?missed,1)"
    )
    assert (
        chain.index("same => n,Log(NOTICE,call ${UNIQUEID}: team leg ${DIALSTATUS})")
        < routed
        < tried
    )
    assert not [s for s in steps if "${ENV(TEAM_PHONE)}" in s and "Dial(" in s]


def test_the_dialplan_asks_voice_with_the_token_before_it_dials_it():
    """A `voice` that is down, frozen or holds another token rings the team
    without a media connection tried (#768, kept with 22.12.0 in #775): the
    dialplan asks `/media/ready` with the media token first, and anything but
    "ok" rings the team."""
    conf = DIALPLAN.read_text()
    dial = conf.index("Dial(WebSocket/voice")
    token = conf.index("CURLOPT(userpwd)=asterisk:${ENV(VOICE_MEDIA_TOKEN)}")
    probe = conf.index("CURL(http://voice:8001/media/ready)")
    assert token < probe < dial
    line = conf[conf.rindex("\n", 0, probe) : conf.index("\n", probe)]
    assert '!= "ok"]?unreached,1)' in line


def test_asterisk_says_it_is_alive_with_the_media_token(phone):
    """Its healthcheck posts while it is registered (T-8.3); the monitor reads
    how long ago, without a token, like /health."""
    client, _ = phone
    assert client.get("/asterisk/seen").json() == {"seconds": None}

    assert client.post("/asterisk/alive").status_code == 401
    assert client.post("/asterisk/alive", headers=basic("wrong")).status_code == 401
    assert client.get("/asterisk/seen").json() == {"seconds": None}

    assert client.post("/asterisk/alive", headers=basic(TOKEN)).status_code == 200
    seconds = client.get("/asterisk/seen").json()["seconds"]
    assert 0 <= seconds < 5


# -- the team leg nobody answered (T-10.8) ---------------------------------------

DIALPLAN = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "asterisk"
    / "conf"
    / "extensions.conf"
)


def test_the_missed_door_wants_the_token(phone):
    client, core = phone
    assert client.post(f"/calls/{CALL}/missed").status_code == 401
    assert client.post(f"/calls/{CALL}/missed", headers=basic("x")).status_code == 401
    assert ("missed", CALL) not in core.seen


@pytest.mark.parametrize(("written", "answer"), [(True, "noted"), (False, "none")])
def test_the_missed_door_says_noted_only_for_a_written_card(phone, written, answer):
    """The dialplan promises a callback on "noted" alone; anything else, an
    error or no answer at all, is "call again later"."""
    client, core = phone
    core.card_written = written

    response = client.post(f"/calls/{CALL}/missed", headers=basic(TOKEN))

    assert (response.status_code, response.text) == (200, answer)
    assert ("missed", CALL) in core.seen
    assert core.left is False


def test_the_missed_door_passes_on_a_caller_who_left(phone):
    """`h` reports with `left=1` (#888 round 3): the card says the caller hung
    up before the team answered."""
    client, core = phone

    response = client.post(f"/calls/{CALL}/missed?left=1", headers=basic(TOKEN))

    assert (response.status_code, response.text) == (200, "noted")
    assert core.left is True


def test_the_stop_door_wants_the_token(phone):
    client, core = phone
    assert client.post(f"/calls/{CALL}/recording/stop").status_code == 401
    assert ("recording_stopped", CALL) not in core.seen


@pytest.mark.parametrize(("stopped", "answer"), [(True, "stopped"), (False, "none")])
def test_the_stop_door_says_what_became_of_it(phone, stopped, answer):
    client, core = phone
    core.stopped = stopped

    response = client.post(f"/calls/{CALL}/recording/stop", headers=basic(TOKEN))

    assert (response.status_code, response.text) == (200, answer)
    assert ("recording_stopped", CALL) in core.seen


def test_the_dialplan_records_a_team_leg_only_on_the_word_record():
    """Only "record <number>" sets it, it is dialled like a transfer, and the
    recording begins when the team answers (`b`), never while it rings."""
    dialplan = DIALPLAN.read_text(encoding="utf-8")
    assert '$["${ENDING:0:7}" = "record "]' in dialplan
    assert "MixMonitor(/recordings/${UNIQUEID}.wav,b)" in dialplan
    start = dialplan.index("exten => team,1")
    assert '$["${RECORD}" != "yes"]' in dialplan[start:]
    assert dialplan.index("MixMonitor(") > start


def _steps() -> list[str]:
    return [
        line
        for line in DIALPLAN.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]


@pytest.mark.parametrize(
    ("name", "alphabet"),
    [("ENDING", "a-z +0-9"), ("CARD", "a-z"), ("ROUTED", "+0123456789")],
)
def test_an_answer_of_voice_is_filtered_before_it_is_compared(name, alphabet):
    """22.11.0 counts a `$[...]` that a quote broke as true: a JSON error from
    `voice` would record a leg, hang up or promise a callback (#776). Checked
    on the pinned image: `{"detail":"Unauthorized"}` gave `record` and
    `hangup` both true, filtered it matches neither."""
    steps = _steps()
    fetch = next(i for i, s in enumerate(steps) if f"Set({name}=${{CURL(" in s)
    assert steps[fetch + 1].strip() == (
        f"same => n,Set({name}=${{FILTER({alphabet},${{{name}}})}})"
    )


def test_no_compare_reads_the_raw_caller_number():
    """The caller's number comes from outside; a quote in it would drop the
    call as a loop (#776)."""
    compares = [s for s in _steps() if "$[" in s]
    assert compares
    assert not [s for s in compares if "CALLERID(" in s]
    assert any("FILTER(+0-9,${CALLERID(num)})" in s for s in _steps())


def test_the_stop_key_stops_deletes_and_tells_voice():
    dialplan = DIALPLAN.read_text(encoding="utf-8")
    stop = dialplan[dialplan.index("[stop-recording]") :]
    assert "StopMixMonitor()" in stop
    assert "TrySystem(rm -f /recordings/${VOICE_CALL}.wav)" in stop
    assert "/calls/${VOICE_CALL}/recording/stop" in stop
    features = DIALPLAN.with_name("features.conf").read_text(encoding="utf-8")
    assert "stop_recording => *7,peer,Gosub(stop-recording,s,1)" in features


def test_no_shell_step_can_hang_up_a_call():
    """`System` hangs up the channel it runs on when the shell cannot run, and
    the stop key runs on the live team leg; `TrySystem` only sets a status
    (rule 5, audit of 02.11.2026 no. 6). Comments may name it."""
    steps = [
        line
        for line in DIALPLAN.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith(";")
    ]
    assert not [step for step in steps if re.search(r"(?<!Try)System\(", step)]


def _services(*parts: str) -> dict:
    return _load(Path(__file__).resolve().parents[2].joinpath(*parts))["services"]


def test_the_recordings_folder_belongs_to_the_user_of_voice_and_the_dispatcher():
    """render.sh gives the folder to RECORDINGS_UID, and nobody else may write
    or delete there: any other uid than the one of `voice` and the dispatcher
    breaks every deletion of the job (audit of 02.11.2026 no. 8), so .env has
    no say in it."""
    base = _services("docker-compose.yml")
    prod = _services("deploy", "docker-compose.prod.yml")
    owner = base["asterisk"]["environment"]["RECORDINGS_UID"]
    assert owner == "10001"
    users = {
        f["user"].split(":")[0]
        for f in (base["voice"], base["dispatcher"], prod["dispatcher"])
    }
    assert users == {owner}
    assert "user" not in prod["voice"]


def test_the_drill_gives_the_recordings_folder_to_its_own_voice():
    drill = _services("deploy", "asterisk", "docker-compose.drill.yml")
    owner = drill["asterisk"]["environment"]["RECORDINGS_UID"]
    assert drill["voice"]["user"].split(":")[0] == owner == "${DRILL_UID}"


def test_no_number_of_a_recorded_leg_reaches_the_log():
    """The word goes before the line that logs the first eight characters:
    "record +" and a digit behind it would be the start of a number."""
    dialplan = DIALPLAN.read_text(encoding="utf-8")
    assert dialplan.index("Set(ENDING=transfer ${ENDING:7})") < dialplan.index(
        "ending [${ENDING:0:8}]"
    )


def test_the_announcements_are_rendered_with_the_mouth_of_the_process(tmp_path):
    voice_app.announce(FakeMouth(), tmp_path)

    for name, sentence in ANNOUNCEMENTS.items():
        audio = (tmp_path / f"{name}.sln16").read_bytes()
        # The fake mouth's "audio" is the sentence: this one, and whole samples
        # with half a second of silence behind it.
        assert audio.startswith(sentence.encode())
        assert len(audio) % 2 == 0
        assert audio.endswith(b"\x00" * voice_app.ANNOUNCEMENT_TAIL_BYTES)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        f"{name}.sln16" for name in ANNOUNCEMENTS
    )


def test_a_mouth_or_folder_that_fails_does_not_stop_the_process(tmp_path, caplog):
    """Without the files the dialplan plays nothing (`STAT`); calls are still
    taken."""
    broken = FakeMouth()
    broken.broken = True

    voice_app.announce(broken, tmp_path)
    voice_app.announce(FakeMouth(), tmp_path / "missing")

    assert list(tmp_path.iterdir()) == []
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warned == ["announcement not rendered"] * 2 * len(ANNOUNCEMENTS)


def test_a_failed_render_leaves_no_sentence_of_an_older_start(tmp_path):
    """The volume outlives `voice`: a file from before a deploy would still
    be played with the old text (ultra review on PR #345)."""
    voice_app.announce(FakeMouth(), tmp_path)
    broken = FakeMouth()
    broken.broken = True

    voice_app.announce(broken, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_a_mouth_that_makes_no_audio_leaves_no_file(tmp_path):
    """Half a second of silence would pass `STAT` and be logged as told."""

    class Silent(FakeMouth):
        def speak(self, text):
            yield b""

    voice_app.announce(Silent(), tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_the_dialplan_plays_only_announcements_the_process_renders():
    conf = DIALPLAN.read_text()
    played = set(re.findall(r"\bmissed_[a-z]+\b", conf))
    assert played == set(ANNOUNCEMENTS)
    # Playback of a missing file answers the call first and then fails: the
    # caller would sit in silence (checked on 22.11.0).
    playback = conf.index("Playback(")
    assert "STAT(e," in conf[conf.rindex("exten =>", 0, playback) : playback]


def test_the_dialplan_tells_only_a_caller_the_ai_answered():
    """A caller in paused mode or with `voice` unreached never heard the AI
    say who it is: the card is written, nothing is played to them."""
    conf = DIALPLAN.read_text()
    missed = conf[conf.index("exten => missed,1") :]
    missed = missed[: missed.index("\n\n")]
    assert '"${VOICE_LEG}" != "ANSWER"' in missed
    assert missed.index('"${VOICE_LEG}" != "ANSWER"') < missed.index("Playback(")
    for line in re.findall(r"Log\([^\n]*", missed):
        assert "CALLERID" not in line and "TARGET" not in line, line


def test_the_dialplan_reports_every_missed_leg_and_waits_out_the_row():
    """Also after a leg `voice` took and lost before a word (CHANUNAVAIL), whose
    row is there; and long enough for the 2 s the write may wait on the row
    (ultra review on PR #345)."""
    conf = DIALPLAN.read_text()
    missed = conf[conf.index("exten => missed,1") :]
    missed = missed[: missed.index("\n\n")]
    report = missed.index("/missed")
    assert "GotoIf" not in missed[:report]
    assert "CURLOPT(httptimeout)=4" in missed[:report]


def _context_of(name: str) -> str:
    """The context an extension of the dialplan stands in."""
    context = ""
    for line in DIALPLAN.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            context = line[1 : line.index("]")]
        elif line.startswith(f"exten => {name},1,"):
            return context
    raise AssertionError(f"no extension {name}")


def test_a_caller_who_leaves_before_the_team_answers_gets_a_card():
    """T-10.8 open point (rule 5), #888 round 3: Asterisk runs `h` of the
    channel's context once the dialplan ends. A call handed to the team whose
    caller left before a leg answered gets the report of `missed`, marked
    `left`, once, with nothing played. No report for a call never handed to
    the team, after an answered leg, or after `missed` reported."""
    steps = [s.strip() for s in _steps()]
    # Every path into `team` marks the call handed: right behind the AI's own
    # hangup, where the AI leg's ANSWER is dropped too, and when `voice` was
    # not reached. Nothing before that mark leads to `team`.
    hangup = steps.index('same => n,GotoIf($["${ENDING}" = "hangup"]?done,1)')
    assert steps[hangup + 1 : hangup + 3] == [
        "same => n,Set(HANDED=yes)",
        "same => n,Set(DIALSTATUS=)",
    ]
    first = _extension("s")
    assert not [
        s
        for s in first[: first.index(steps[hangup])]
        if re.search(r"\bteam,1|toteam", s)
    ]
    unreached = _extension("unreached")
    assert unreached.index("same => n,Set(HANDED=yes)") < unreached.index(
        "same => n,Goto(team,1)"
    )
    missed = _extension("missed")
    assert missed[0] == "exten => missed,1,Set(HANDED=asked)"
    gone = _extension("h")
    assert _context_of("h") == _context_of("team") == _context_of("s") == "ai"
    assert gone[0] == (
        'exten => h,1,GotoIf($["${HANDED}" != "yes" | "${DIALSTATUS}" = "ANSWER"]?end)'
    )
    # The same credentials, timeouts, POST and filter as `missed`.
    report = next(i for i, s in enumerate(gone) if "/missed" in s)
    reported = next(i for i, s in enumerate(missed) if "/missed" in s)
    assert gone[1:report] == missed[1:reported]
    assert gone[report] == missed[reported].replace("/missed,", "/missed?left=1,")
    assert (
        gone[report + 1]
        == missed[reported + 1]
        == ("same => n,Set(CARD=${FILTER(a-z,${CARD})})")
    )
    assert not [s for s in gone if "Playback(" in s or "Dial(" in s]
    for line in gone:
        if "Log(" in line:
            assert "CALLERID" not in line and "TARGET" not in line, line


# -- with the real call handler and a database ----------------------------------------


def voice_for(session, tenant) -> Voice:  # noqa: F811 - fixtures
    lines = Lines()
    handler = handler_for(session, tenant, lines)
    return Voice(
        events=handler,
        lines=lines,
        token=TOKEN,
        ear=FakeEar,
        mouth=FakeMouth,
        silence_seconds=60.0,
        team_phone=handler.team_phone,
    )


def test_the_team_door_reads_the_restaurants_number(session, tenant):  # noqa: F811
    """With the real handler: the number of `service_config`, not `.env`."""
    with TestClient(create_app(voice_for(session, tenant))) as client:
        assert client.get("/team", headers=basic(TOKEN)).text == TEAM_PHONE


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


def test_a_team_that_does_not_answer_a_paused_line_gets_a_card(session, tenant):  # noqa: F811
    set_mode(session, tenant.id, "paused")
    voice = voice_for(session, tenant)
    with TestClient(create_app(voice)) as client:
        with connect(client) as ws:
            ws.send_text(media_start())
            assert receive_command(ws) == {"command": "HANGUP"}
        assert ending(client) == f"transfer {TEAM_PHONE}"
        wait_for(lambda: voice.lines.caller_id(CALL) is None)

        missed = client.post(f"/calls/{CALL}/missed", headers=basic(TOKEN))

    assert missed.text == "noted"
    call = the_call(session, CALL)
    assert call.outcome == "callback"
    [card] = session.scalars(select(Callback).where(Callback.call_id == call.id))
    assert card.phone == "+4972215550701"


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
    assert audio.rstrip(b"\x00").decode() == GREETING.format(
        name=tenant.name, privacy="", ask=SAY_WHAT_CAN_I_DO
    )
    call = the_call(session, CALL)
    assert call.ended_at is not None and call.outcome == "abandoned"


# -- what the service needs to start ----------------------------------------------------


@pytest.fixture
def startable(monkeypatch, tmp_path):
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
    monkeypatch.setattr(voice_watchdog.Watchdog, "start", lambda self: None)
    # Never the volume a running `voice` plays from.
    monkeypatch.setattr(voice_app, "ANNOUNCEMENTS_FOLDER", tmp_path)
    return monkeypatch


@pytest.mark.parametrize(
    ("name", "value", "why"),
    [
        ("voice_media_token", "", "VOICE_MEDIA_TOKEN is empty"),
        ("voice_number", "", "VOICE_NUMBER"),
        ("voice_number", "07221 5550100", "VOICE_NUMBER"),
        ("voice_ear", "", "no ear"),
        ("voice_mouth", "whisper", "no mouth"),
        ("voice_ear", "Whisper", "no ear"),  # names are exact
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
    # The load test's stand-ins (#841) hear sentences nobody said.
    startable.setattr(settings, "voice_ear", "load")
    startable.setattr(settings, "voice_mouth", "load")
    with pytest.raises(RuntimeError, match="drill engines"):
        voice_from_settings()


@pytest.mark.parametrize("env", ["prod", "staging"])
def test_outside_dev_and_test_the_ear_measure_is_refused(startable, env):
    """`VOICE_EAR_MEASURE` logs what every caller said, one line per utterance
    for the journal's 14 days: the team's own test calls only (#678, #888 S13)."""
    startable.setattr(settings, "env", env)
    startable.setattr(settings, "voice_media_token", "x" * 32)
    startable.setattr(settings, "voice_ear", "whisper")
    startable.setattr(settings, "voice_mouth", "piper")
    startable.setattr(settings, "voice_ear_measure", True)
    with pytest.raises(RuntimeError, match="VOICE_EAR_MEASURE"):
        voice_from_settings()


def test_in_dev_the_ear_measure_passes_the_start_check(startable):
    """The workbench runs `ENV=dev`: Maxi's measurement calls of #678 start."""
    startable.setattr(settings, "env", "dev")
    startable.setattr(settings, "voice_ear_measure", True)
    startable.setattr(settings, "llm_base_url", "")
    # Past the check of the flag: the next refusal is the missing model.
    with pytest.raises(LLMError):
        voice_from_settings()


def test_outside_dev_and_test_a_claude_model_is_refused(startable):
    """No EU path and no DPA yet: a Claude model answers evals, never a real
    caller (docs/18 §3, T-5.3)."""
    startable.setattr(settings, "env", "prod")
    startable.setattr(settings, "voice_media_token", "x" * 32)
    startable.setattr(settings, "voice_ear", "whisper")
    startable.setattr(settings, "voice_mouth", "piper")
    startable.setattr(settings, "llm_model", "claude-haiku-4-5")
    with pytest.raises(RuntimeError, match="Claude model is for evals"):
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


def watchdog_at(clock: list[float]) -> tuple[voice_watchdog.Watchdog, _Exit]:
    end = _Exit()
    return voice_watchdog.Watchdog(limit=30.0, clock=lambda: clock[0], end=end), end


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
    assert end.codes == [voice_watchdog.EXIT_ENGINE_HUNG]


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

    assert b"".join(voice_watchdog.WatchedMouth(Slow(), watchdog).speak("Hallo")) == (
        b"\x01\x00\x02\x00"
    )
    assert end.codes == [voice_watchdog.EXIT_ENGINE_HUNG]


def model_files(folder: Path, *voices: str) -> Path:
    """What the loaders look for themselves (`speech_engines.model_file`),
    empty: the engines' own files are theirs to read."""
    (folder / "whisper").mkdir(exist_ok=True)
    (folder / "piper").mkdir(exist_ok=True)
    for voice in voices or (tts_piper.VOICE,):
        (folder / "piper" / f"{voice}.onnx").touch()
    return folder


def test_ear_and_mouth_that_cannot_be_loaded_keep_the_service_down(
    its_database, tmp_path
):
    """Without the engines' packages, as here in the API's image: refused at
    start, and the dialplan rings the team meanwhile. Without their model files
    in the image of `voice`: test_telephony_engine_loading.py."""
    its_database.setattr(speech_engines, "MODELS", model_files(tmp_path))
    its_database.setattr(settings, "voice_mouth_voice", "")
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
def stand_in_engines(monkeypatch, tmp_path):
    """The engines' classes stand in for the packages, which only the image of
    `voice` has; what the loaders do with them is checked here."""
    from api.telephony.adapters import stt_whisper

    monkeypatch.setattr(
        speech_engines,
        "MODELS",
        model_files(tmp_path, tts_piper.VOICE, "de_DE-other-high"),
    )
    monkeypatch.setattr(settings, "voice_mouth_voice", "")
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
    watchdog = voice_watchdog.Watchdog()
    speech_engines._whisper(watchdog)
    speech_engines._piper(watchdog)
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
    speech_engines._piper(voice_watchdog.Watchdog())
    monkeypatch.setattr(settings, "voice_mouth_voice", "de_DE-other-high")
    speech_engines._piper(voice_watchdog.Watchdog())
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
        voice_watchdog.Watchdog, "start", lambda self: order.append("watchdog")
    )

    def transcribe(self, pcm, terms) -> str:
        order.append("ear")
        return ""

    monkeypatch.setattr(stt_whisper.Whisper, "transcribe", transcribe)
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
        speech_engines._piper(voice_watchdog.Watchdog())


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

    mouth = voice_watchdog.WatchedMouth(Mouth(), watchdog)
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
        clock[0] += voice_watchdog.ENGINE_LOAD_SECONDS - 1
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
        (speech_engines._whisper if ear else speech_engines._piper)(watchdog)
    assert end.codes == [voice_watchdog.EXIT_ENGINE_HUNG]


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
    ear = speech_engines._whisper(watchdog)()
    assert isinstance(ear, stt_whisper.SegmentingEar)
    transcribe = ear._transcribe
    assert end.codes == []
    transcribe(b"\x00\x00", [])
    if device == "cuda":
        assert end.codes == [voice_watchdog.EXIT_ENGINE_HUNG]
        return
    assert end.codes == []
    takes[0] = voice_watchdog.ENGINE_LOAD_SECONDS + 1
    transcribe(b"\x00\x00", [])
    assert end.codes == [voice_watchdog.EXIT_ENGINE_HUNG]


def test_a_limit_of_zero_is_no_default():
    clock = [100.0]
    watchdog, end = watchdog_at(clock)
    with watchdog.watch("ear", 0.0):
        clock[0] += 0.5
        watchdog.check()
    assert end.codes == [voice_watchdog.EXIT_ENGINE_HUNG]


def test_the_factory_makes_ear_and_mouth_by_their_name(stand_in_engines):
    """`app.py` knows the engines by name only; the factory in `adapters/`
    loads them (Codex on PR #298)."""
    from api.telephony.adapters import stt_whisper

    watchdog = voice_watchdog.Watchdog()
    assert isinstance(
        speech_engines.EARS["whisper"](watchdog)(), stt_whisper.SegmentingEar
    )
    assert b"".join(speech_engines.MOUTHS["piper"](watchdog)().speak("Hallo"))
    assert isinstance(speech_engines.EARS["fake"](watchdog)(), FakeEar)
    assert isinstance(speech_engines.MOUTHS["fake"](watchdog)(), FakeMouth)


@pytest.mark.parametrize("kind", ["whisper", "piper", "voice"])
def test_an_engine_without_its_model_file_is_not_loaded(
    stand_in_engines, monkeypatch, tmp_path, kind
):
    """Checked before the engine's package is imported, so the missing file
    is named in every image, and nothing is loaded."""
    empty = tmp_path / "empty"
    empty.mkdir()
    if kind == "voice":  # a voice set in `.env` that the kit does not hold
        monkeypatch.setattr(settings, "voice_mouth_voice", "de_DE-missing-low")
    else:
        monkeypatch.setattr(speech_engines, "MODELS", empty)
    load = (
        speech_engines.EARS["whisper"]
        if kind == "whisper"
        else (speech_engines.MOUTHS["piper"])
    )
    missing = {
        "whisper": empty / "whisper",
        "piper": empty / "piper" / f"{tts_piper.VOICE}.onnx",
        "voice": speech_engines.MODELS / "piper" / "de_DE-missing-low.onnx",
    }[kind]
    with pytest.raises(FileNotFoundError, match=re.escape(str(missing))):
        load(voice_watchdog.Watchdog())
    assert stand_in_engines == []


def test_a_missing_model_file_keeps_the_service_down(
    its_database, stand_in_engines, monkeypatch, tmp_path
):
    monkeypatch.setattr(speech_engines, "MODELS", tmp_path / "nothing")
    its_database.setattr(settings, "voice_mouth", "piper")
    with pytest.raises(RuntimeError, match=r"could not be loaded.*no model file"):
        voice_from_settings()


def test_on_the_processor_the_ear_is_not_run_at_start(stand_in_engines, monkeypatch):
    """No graphics card libraries to set up there, and the run took 4.5 s of
    every start (ultra review on PR #310). The detector still runs."""
    monkeypatch.setattr(settings, "voice_ear_device", "cpu")
    speech_engines._whisper(voice_watchdog.Watchdog())
    assert [kind for kind, _ in stand_in_engines] == ["detector"]
