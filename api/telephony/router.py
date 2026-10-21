"""The doors of the service `voice` (docs/20 §2 and §5).

- `/media`: the media channel of Asterisk, one WebSocket connection per call.
  Frames in go to the call's `MediaLine` and on to its session; frames out come
  from the line through one writer per connection.
- `/calls/{call_id}/ending`: once `Dial` returns, the dialplan asks how our leg
  ended. "hangup" ends the call, "transfer <number>" is dialled if the number
  is TEAM_PHONE or on TRANSFER_TARGETS of the dialplan, else TEAM_PHONE, and
  anything else, an empty answer or none at all, rings the team (CLAUDE.md §2
  rule 5, docs/20 §5).
- `/calls/{call_id}/missed`: nobody answered the team leg at TEAM_PHONE. A
  callback card is written; "noted" once it is, "none" otherwise, and the
  dialplan tells the caller which (docs/20 §5, T-10.8).

All three want the media token as the password of Basic credentials. A call is named
by the id of the caller's channel (`${UNIQUEID}`): the dialplan hands it to the
media channel as an inherited variable, which `MEDIA_START` reports
(`adapters.asterisk.call_of`), and asks by it.
"""

import asyncio
import contextlib
import threading
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import PlainTextResponse
from starlette.types import ASGIApp

from api.core.logging import get_logger
from api.telephony.adapters.asterisk import (
    MEDIA_START,
    MediaLine,
    ProtocolError,
    call_of,
    parse_event,
)

if TYPE_CHECKING:
    from api.telephony.app import Voice
    from api.telephony.session import CallSession

# A frame Asterisk does not take within this time: the connection stalls, and a
# leg we cannot reach is closed (`speech.Line`). The dialplan rings the team.
WRITE_SECONDS = 2.0

logger = get_logger(__name__)
router = APIRouter()


@router.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


@router.get("/calls/{call_id}/ending", response_class=PlainTextResponse)
def ending(call_id: str, request: Request) -> str:
    voice = _voice(request.app)
    if not voice.authorized(request.headers.get("authorization")):
        raise HTTPException(status_code=401)
    return voice.ending(call_id)


@router.post("/calls/{call_id}/missed", response_class=PlainTextResponse)
def missed(call_id: str, request: Request) -> str:
    voice = _voice(request.app)
    if not voice.authorized(request.headers.get("authorization")):
        raise HTTPException(status_code=401)
    return "noted" if voice.missed(call_id) else "none"


@router.websocket("/media")
async def media(websocket: WebSocket) -> None:
    voice = _voice(websocket.app)
    if not voice.authorized(websocket.headers.get("authorization")):
        # Closed before it is accepted: Asterisk sees a refused connection, Dial
        # fails, and the dialplan rings the team.
        logger.warning("media connection refused: no valid token")
        await websocket.close(code=1008)
        return
    await websocket.accept(subprotocol="media")
    # Which call this is comes with the first frame, MEDIA_START. A connection
    # that does not say, or names a call that is already live, is closed
    # without a session: the dialplan finds no ending and rings the team.
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        return  # gone before it said which call: nothing to clean up
    first = message.get("text") or ""
    try:
        start = parse_event(first)
        if start.name != MEDIA_START:
            raise ProtocolError(f"first frame is {start.name}, not {MEDIA_START}")
        name = call_of(start)
    except ProtocolError:
        logger.exception("media connection without a call it can be told by")
        with contextlib.suppress(Exception):
            await websocket.close(code=1008)
        return
    call_id = name.call_id
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()

    def send(frame: str | bytes | None) -> None:
        loop.call_soon_threadsafe(outbox.put_nowait, frame)

    line = MediaLine(send, ended=lambda how: voice.ended(call_id, how))
    try:
        session = voice.open(call_id, line=line, caller_id=name.caller_id)
    except ValueError:
        # Two connections for one call: the first keeps it, this one is refused.
        logger.exception("a second media connection for a live call")
        with contextlib.suppress(Exception):
            await websocket.close(code=1008)
        return
    line.attach(session)

    def work() -> None:
        try:
            session.run()
        finally:
            voice.lines.remove(call_id)
            # The writer closes the connection; after our HANGUP Asterisk has
            # done so already. The loop may be gone with the server.
            with contextlib.suppress(RuntimeError):
                send(None)

    threading.Thread(target=work, name=f"call {call_id}", daemon=True).start()
    writer = asyncio.create_task(_write(websocket, outbox, session))
    try:
        # MEDIA_START once more, now to the line: it takes the frame size and
        # starts the session. Inside the try, so a start the line cannot take
        # is a failure like any other frame (own review of PR #297).
        line.on_text(first)
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("text") is not None:
                line.on_text(message["text"])
            elif message.get("bytes") is not None:
                line.on_audio(message["bytes"])
    except Exception:
        # Not a caller who hung up: the voice layer failed on this call, and
        # the call log says so. The dialplan rings the team.
        logger.exception(
            "frame of the media channel not understood, the team takes the call",
            extra={"extra": {"session_id": call_id}},
        )
        session.broken()
    finally:
        line.closed()
        await writer


async def _write(
    websocket: WebSocket,
    outbox: "asyncio.Queue[str | bytes | None]",
    session: "CallSession",
) -> None:
    """Reported to the session at once, as `speech.Line` asks, not when the
    close handshake with a stalled peer gives up. Without our ending the
    dialplan rings the team either way."""
    try:
        while (frame := await outbox.get()) is not None:
            if isinstance(frame, bytes):
                await asyncio.wait_for(websocket.send_bytes(frame), WRITE_SECONDS)
            else:
                await asyncio.wait_for(websocket.send_text(frame), WRITE_SECONDS)
    except TimeoutError:
        # Asterisk is there and takes nothing: a failure of the voice layer,
        # and the call log says so (Codex on PR #297).
        logger.warning("the media connection stalled, closing it")
        session.broken()
    except Exception:  # noqa: BLE001 - the leg is gone, whatever the cause
        # A send into a connection Asterisk closed: the caller hung up.
        logger.warning("the media connection is gone")
        session.closed()
    with contextlib.suppress(Exception):
        await websocket.close()


def _voice(app: ASGIApp) -> "Voice":
    return app.state.voice  # type: ignore[attr-defined]
