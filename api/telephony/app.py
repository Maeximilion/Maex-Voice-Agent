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
model, with the drill engines outside dev and test, without its tenant, or with
an ear or mouth it cannot load. An engine that hangs later ends the process
(`Watchdog`), and Compose starts it again.
"""

import base64
import binascii
import contextlib
import hmac
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from fastapi import FastAPI

from api.agent.messages_llm import CLAUDE_PREFIX, client_from_settings
from api.config import LOCAL_ENVS, TOKEN_MIN_LENGTH, settings
from api.core.database_role import refuse_a_superuser_role
from api.core.logging import configure_logging, get_logger
from api.db import SessionLocal, bounded_sessions, engine
from api.domain.calls import call_routing, tenant_named
from api.domain.customers.phone import E164
from api.telephony.adapters.speech_engines import EARS, MOUTHS
from api.telephony.handler import ANNOUNCEMENTS, CallHandler
from api.telephony.port import CallEvents
from api.telephony.router import router
from api.telephony.session import CallSession, Lines
from api.telephony.speech import Ear, Line, Mouth
from api.telephony.watchdog import Watchdog

# They hear nothing, "Ja" in a tone, or the load test's sentences, and speak
# noise or a tone: for drills and the load test (#841) of the workbench only.
DRILL_ENGINES = frozenset({"fake", "drill", "load"})
# How many endings are kept for the dialplan. It asks within a second of the
# end of our leg; an ending nobody asked for goes after this many newer ones.
ENDINGS_KEPT = 1000
# A statement of the phone line that takes longer fails, and the call goes to
# the team (`db.bounded_sessions`). Its tools answer in milliseconds (budget
# 300 ms, docs/04 §1), so a wait this long is a lock that is not coming free.
# A tool the model calls waits less when its turn has less left (`db.bounded_by`).
DB_TIMEOUT_MS = 2000
# Where the sentences after a team leg nobody answered go (`announce`): a
# volume Asterisk reads (docker-compose.yml). Half a second of silence behind
# each, so its last word is not cut off by the hangup.
ANNOUNCEMENTS_FOLDER = Path("/announcements")
ANNOUNCEMENT_TAIL_BYTES = 16000

logger = get_logger(__name__)


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
        team_phone: Callable[[], str],
    ) -> None:
        self.lines = lines
        # The number the team is at now, "" when it cannot be read (#835).
        self.team_phone = team_phone
        self._events = events
        self._token = token
        self._ear = ear
        self._mouth = mouth
        self._silence_seconds = silence_seconds
        self._lock = threading.Lock()
        self._endings: OrderedDict[str, str] = OrderedDict()
        self._asterisk_at: float | None = None

    def asterisk_alive(self) -> None:
        self._asterisk_at = time.monotonic()

    def asterisk_seen(self) -> float | None:
        at = self._asterisk_at
        return None if at is None else time.monotonic() - at

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

    def missed(self, call_id: str, *, left: bool = False) -> bool:
        """Nobody answered the team legs, or (`left`) the caller hung up before
        one did: True once the callback card is written."""
        return self._events.on_team_missed(call_id, left=left)

    def recording_stopped(self, call_id: str) -> bool:
        """The team stopped the recording of a team leg: True once it is logged."""
        return self._events.on_recording_stopped(call_id)


def announce(mouth: Mouth, folder: Path) -> None:
    """The sentences the dialplan plays after a team leg nobody answered, in
    this process's voice, as files Asterisk plays (raw 16 kHz, `.sln16`), so
    they are heard with `voice` or the database down. Rendered at every start,
    so they follow the code; each is renamed into place whole. A failure costs
    the sentences, never the start: the file of an older start goes too, and
    without a file the dialplan plays nothing (`STAT`)."""
    for name, sentence in ANNOUNCEMENTS.items():
        target = folder / f"{name}.sln16"
        try:
            audio = b"".join(mouth.speak(sentence))
            if not audio:
                raise RuntimeError("the mouth made no audio")
            # Whole samples: an odd byte would shift every sample behind it.
            audio += b"\x00" * (len(audio) % 2 + ANNOUNCEMENT_TAIL_BYTES)
            part = folder / f".{name}.part"
            part.write_bytes(audio)
            part.replace(target)
        except Exception as exc:  # noqa: BLE001 - never the start
            logger.warning(
                "announcement not rendered",
                extra={"extra": {"name": name, "error": type(exc).__name__}},
            )
            with contextlib.suppress(OSError):
                target.unlink(missing_ok=True)


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
    if not local and settings.llm_model.startswith(CLAUDE_PREFIX):
        # Real callers' words would leave the EU without a contract for it.
        _refuse(
            "a Claude model is for evals until an EU path and a DPA exist (docs/18 §3)"
        )
    if not local and settings.voice_ear_measure:
        # One log line per utterance with its text, kept 14 days in journald.
        _refuse(
            "VOICE_EAR_MEASURE logs what every caller says and is for the team's "
            "test calls under ENV=dev only (#678)"
        )
    # Raises without a model: every call would go to the team at its first turn.
    client_from_settings().close()
    refuse_a_superuser_role(settings.env, engine)
    with SessionLocal() as db:
        tenant_id = tenant_named(db, settings.tenant_name)
        team_phone = call_routing(db, tenant_id).team_phone
    if settings.voice_number in {team_phone, settings.team_phone}:
        _refuse("the team number is the AI telephone's own number")
    # Last: loading takes seconds, and on the graphics card its memory. The
    # watchdog runs first: an engine whose load or first run hangs ends the
    # process instead of keeping it from ever starting (Codex on PR #310).
    watchdog = Watchdog()
    watchdog.start()
    try:
        make_ear, make_mouth = ear(watchdog), mouth(watchdog)
    except Exception as exc:
        raise RuntimeError(
            f"voice does not start: ear {settings.voice_ear!r} or mouth "
            f"{settings.voice_mouth!r} could not be loaded ({exc})"
        ) from exc
    announce(make_mouth(), ANNOUNCEMENTS_FOLDER)

    lines = Lines()
    handler = CallHandler(
        lines,
        tenant_id=tenant_id,
        session_factory=bounded_sessions(DB_TIMEOUT_MS),
        llm_factory=lambda _db: client_from_settings(),
    )
    return Voice(
        events=handler,
        lines=lines,
        token=token,
        ear=make_ear,
        mouth=make_mouth,
        silence_seconds=settings.voice_silence_seconds,
        team_phone=handler.team_phone,
    )


def _refuse(why: str) -> NoReturn:
    raise RuntimeError(f"voice does not start: {why}")
