"""The process of the service `voice`: one tenant's telephone (docs/20 §2).

Asterisk connects to `/media` once per call (`telephony/router.py`). This module
builds what a call needs: the call handler of the process, behind it the same core
as the text phone and the evals; a call session per call with its own ear and
mouth; and the record of how each call ended, which the dialplan asks for when our
leg is gone.

    uvicorn --factory api.telephony.app:from_settings --host 0.0.0.0 --port 8001

One worker: the live calls are held in this process (`CallHandler`), and the
dialplan's question has to reach the process that took the call.

It does not start rather than take calls it cannot serve; while `voice` is down
the dialplan rings the team (docs/20 §5). So it refuses to start without a media
token, without its own number, when the team number is that number, without a
model, with the drill engines outside dev and test, or without its tenant.
"""

import base64
import binascii
import hmac
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import NoReturn

from fastapi import FastAPI

from api.agent.llm import ChatCompletionsLLM
from api.config import LOCAL_ENVS, TOKEN_MIN_LENGTH, settings
from api.core.logging import configure_logging
from api.db import SessionLocal, engine, refuse_a_superuser_role
from api.domain.calls import call_routing, tenant_named
from api.domain.customers.phone import E164
from api.telephony.adapters.fake_speech import FakeEar, FakeMouth
from api.telephony.handler import CallHandler
from api.telephony.port import CallEvents
from api.telephony.router import router
from api.telephony.session import CallSession, Lines
from api.telephony.speech import Ear, Line, Mouth

# Ear and mouth by their name in the settings. The engines of the test phase
# of T-10.5 come with T-1.11 part 2.
EARS: dict[str, Callable[[], Ear]] = {"fake": FakeEar}
MOUTHS: dict[str, Callable[[], Mouth]] = {"fake": FakeMouth}
# They hear nothing and speak noise: for drills of the workbench only.
DRILL_ENGINES = frozenset({"fake"})
# How many endings are kept for the dialplan. It asks within a second of the
# end of our leg; an ending nobody asked for goes after this many newer ones.
ENDINGS_KEPT = 1000


class Voice:
    """What the router needs of the process."""

    def __init__(
        self,
        *,
        events: CallEvents,
        lines: Lines,
        token: str,
        ear: Callable[[], Ear],
        mouth: Callable[[], Mouth],
        silence_seconds: float,
    ) -> None:
        self.lines = lines
        self._events = events
        self._token = token
        self._ear = ear
        self._mouth = mouth
        self._silence_seconds = silence_seconds
        self._lock = threading.Lock()
        self._endings: OrderedDict[str, str] = OrderedDict()

    def authorized(self, header: str | None) -> bool:
        """Basic credentials whose password is the media token
        (websocket_client.conf and the dialplan send them). The user name is
        not checked."""
        if not self._token or not header:
            return False
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(value.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        _, _, password = decoded.partition(b":")
        return hmac.compare_digest(password, self._token.encode())

    def open(self, call_id: str, *, line: Line, caller_id: str | None) -> CallSession:
        """A session for a call that just connected."""
        session = CallSession(
            call_id,
            line=line,
            ear=self._ear(),
            mouth=self._mouth(),
            events=self._events,
            silence_seconds=self._silence_seconds,
            caller_id=caller_id,
        )
        self.lines.add(session)
        return session

    def ended(self, call_id: str, ending: str) -> None:
        """How our leg ends: "hangup" or "transfer <number>"."""
        with self._lock:
            self._endings[call_id] = ending
            while len(self._endings) > ENDINGS_KEPT:
                self._endings.popitem(last=False)

    def ending(self, call_id: str) -> str:
        """Asked once by the dialplan. Empty for a call that did not end by our
        decision: it crashed, the session failed, or it is unknown. The dialplan
        rings the team then."""
        with self._lock:
            return self._endings.pop(call_id, "")


def create_app(voice: Voice) -> FastAPI:
    app = FastAPI(title="voice", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.voice = voice
    app.include_router(router)
    return app


def from_settings() -> FastAPI:
    """The app of the service, from `.env`; the factory uvicorn starts."""
    configure_logging(settings.log_level)
    return create_app(voice_from_settings())


def voice_from_settings() -> Voice:
    token = settings.voice_media_token
    if not token:
        _refuse("VOICE_MEDIA_TOKEN is empty")
    local = settings.env in LOCAL_ENVS
    if not local and len(token) < TOKEN_MIN_LENGTH:
        _refuse(f"VOICE_MEDIA_TOKEN is shorter than {TOKEN_MIN_LENGTH} characters")
    if not E164.match(settings.voice_number):
        _refuse("VOICE_NUMBER is not a number in E.164 format")
    ear = EARS.get(settings.voice_ear)
    mouth = MOUTHS.get(settings.voice_mouth)
    if ear is None or mouth is None:
        _refuse(
            f"no ear {settings.voice_ear!r} or no mouth {settings.voice_mouth!r}; "
            f"ears: {sorted(EARS)}, mouths: {sorted(MOUTHS)}"
        )
    if not local and {settings.voice_ear, settings.voice_mouth} & DRILL_ENGINES:
        _refuse("the drill engines hear nothing and are for dev and test only")
    # Raises without a model: every call would go to the team at its first turn.
    ChatCompletionsLLM.from_settings().close()
    refuse_a_superuser_role(settings.env, engine)
    with SessionLocal() as db:
        tenant_id = tenant_named(db, settings.tenant_name)
        team_phone = call_routing(db, tenant_id).team_phone
    if settings.voice_number in {team_phone, settings.team_phone}:
        _refuse("the team number is the AI telephone's own number")

    lines = Lines()
    handler = CallHandler(
        lines,
        tenant_id=tenant_id,
        session_factory=SessionLocal,
        llm_factory=lambda _db: ChatCompletionsLLM.from_settings(),
    )
    return Voice(
        events=handler,
        lines=lines,
        token=token,
        ear=ear,
        mouth=mouth,
        silence_seconds=settings.voice_silence_seconds,
    )


def _refuse(why: str) -> NoReturn:
    raise RuntimeError(f"voice does not start: {why}")
