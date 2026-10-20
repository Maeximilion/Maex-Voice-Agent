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
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn, TypeVar

from fastapi import FastAPI

from api.agent.llm import ChatCompletionsLLM
from api.config import LOCAL_ENVS, TOKEN_MIN_LENGTH, settings
from api.core.logging import configure_logging, get_logger
from api.db import SessionLocal, bounded_sessions, engine, refuse_a_superuser_role
from api.domain.calls import call_routing, tenant_named
from api.domain.customers.phone import E164
from api.telephony.adapters.fake_speech import FakeEar, FakeMouth
from api.telephony.handler import ANNOUNCEMENTS, CallHandler
from api.telephony.port import CallEvents
from api.telephony.router import router
from api.telephony.session import CallSession, Lines
from api.telephony.speech import Ear, Line, Mouth

# Where the model files are inside the container: the folder of the speech kit,
# mounted read-only from deploy/speech/run/models (docker-compose.yml).
MODELS = Path("/models")
# The ear for orders by the run of 13.10.2026 (docs/20 §11).
EAR_MODEL = "large-v3-turbo"
# They hear nothing and speak noise: for drills of the workbench only.
DRILL_ENGINES = frozenset({"fake"})
# How many endings are kept for the dialplan. It asks within a second of the
# end of our leg; an ending nobody asked for goes after this many newer ones.
ENDINGS_KEPT = 1000
# A statement of the phone line that takes longer fails, and the call goes to
# the team (`db.bounded_sessions`). Its tools answer in milliseconds (budget
# 300 ms, docs/04 §1), so a wait this long is a lock that is not coming free.
DB_TIMEOUT_MS = 2000
# An engine call that works longer hangs. The session's own deadlines
# (`session.SPEAK_SECONDS`, `session.UTTERANCE_SECONDS`) have sent its call to
# the team by then, and a sentence of a minute takes the ear about a second
# on the graphics card.
ENGINE_HANG_SECONDS = 30.0
# Loading an engine: as long as Compose gives the service to start
# (`start_period`, docker-compose.yml). A load that hangs, a graphics card in
# a bad state, would otherwise keep the process from ever listening. Also the
# ear's limit on the processor: a run there took 4 to 5 s for 25 s of audio
# (ultra review on PR #310), but an utterance may last a minute and the engine
# runs it up to six times when it is unsure of the words. Ending the process
# drops every call on it and keeps the line down for a restart, which takes up
# to this long: it only pays for a run that would last longer (own review on
# PR #310).
ENGINE_LOAD_SECONDS = 120.0
# The exit code of a process its watchdog ended (EX_SOFTWARE).
EXIT_ENGINE_HUNG = 70
# Where the sentences after a team leg nobody answered go (`announce`): a
# volume Asterisk reads (docker-compose.yml). Half a second of silence behind
# each, so its last word is not cut off by the hangup.
ANNOUNCEMENTS_FOLDER = Path("/announcements")
ANNOUNCEMENT_TAIL_BYTES = 16000

logger = get_logger(__name__)
_T = TypeVar("_T")


class Watchdog:
    """Ends the process when an engine call hangs.

    The engines run on threads of the ears and of the session, and a thread
    in Python cannot be cancelled: an inference that never returns keeps the
    model it runs on, and every later call of the process would stall behind
    it (Codex on PR #292). So the process ends, Compose starts it again
    (`restart`), and the calls still on it ring the team (docs/20 §5).

    ponytail: a thread in this process; an engine that freezes it whole,
    holding the interpreter lock, is beyond it. Asterisk's pings send such a
    call to the team (docs/20 §5); the restart for it would need a check from
    outside the process.
    """

    def __init__(
        self,
        *,
        limit: float = ENGINE_HANG_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        end: Callable[[int], object] = os._exit,
    ) -> None:
        self._limit = limit
        self._clock = clock
        self._end = end
        self._lock = threading.Lock()
        self._running: dict[object, tuple[str, float, float]] = {}

    @contextmanager
    def watch(self, what: str, limit: float | None = None) -> Iterator[None]:
        """Watched until the block is left; hung past `limit`, by default the
        watchdog's own."""
        key = object()
        with self._lock:
            self._running[key] = (
                what,
                self._clock(),
                self._limit if limit is None else limit,
            )
        try:
            yield
        finally:
            with self._lock:
                del self._running[key]

    def watched(
        self, what: str, call: Callable[..., _T], limit: float | None = None
    ) -> Callable[..., _T]:
        """`call`, one run at a time and watched while it runs. One model
        serves every call of the process: a call that waits for it is in line,
        not stuck, and its wait does not count (ultra review on PR #298)."""
        turn = threading.Lock()

        def watched_call(*args: object) -> _T:
            with turn, self.watch(what, limit):
                return call(*args)

        return watched_call

    def check(self) -> None:
        now = self._clock()
        with self._lock:
            hung = sorted(
                {
                    what
                    for what, since, limit in self._running.values()
                    if now - since > limit
                }
            )
        if hung:
            logger.critical(
                "an engine hangs, voice ends and is started again",
                extra={"extra": {"engines": hung}},
            )
            self._end(EXIT_ENGINE_HUNG)

    def start(self) -> None:
        def run() -> None:
            while True:
                time.sleep(1.0)
                self.check()

        threading.Thread(target=run, name="watchdog", daemon=True).start()


class WatchedMouth:
    """A mouth whose sentences the watchdog watches until the last piece.

    The sentences of several calls are made side by side, not in line as the
    ear's: the voice takes them so, and in line they waited past the session's
    `SPEAK_SECONDS` without anything hanging (ultra review on PR #310: three
    read-backs at once were done after 7.8 s in line, 5.8 s side by side)."""

    def __init__(self, mouth: Mouth, watchdog: Watchdog) -> None:
        self._mouth = mouth
        self._watchdog = watchdog

    def speak(self, text: str) -> Iterable[bytes]:
        with self._watchdog.watch("mouth"):
            yield from self._mouth.speak(text)


# Ear and mouth by their name in the settings. Each loads its engine once per
# process, watched, runs it once, and returns what makes the ear or mouth of
# one call. An engine that loads may still fail on its first run, when the
# graphics card's libraries are set up: refused at start then, not in a
# caller's first sentence. The first run also takes longer (the ear 0.4 s
# instead of 0.1 on the workbench, 15.10.2026). Ultra review on PR #298.
# The engines' packages are imported here: they are in the image of `voice`
# only (api/Dockerfile, target `voice`).


def _whisper(watchdog: Watchdog) -> Callable[[], Ear]:
    from api.telephony.adapters.stt_whisper import (
        SAMPLE_RATE,
        WINDOW,
        SegmentingEar,
        Whisper,
        silero,
    )

    gpu = settings.voice_ear_device == "cuda"
    with watchdog.watch("ear", ENGINE_LOAD_SECONDS):
        whisper = Whisper(
            EAR_MODEL,
            MODELS / "whisper",
            device=settings.voice_ear_device,
            compute_type="float16" if gpu else "int8",
        )
    if gpu:
        # The first run loads the graphics card's libraries from disk, which
        # takes longer than a run: watched as a load (own review on PR #310). On the
        # processor there are none, and the run takes 4.5 s of every start
        # (ultra review on PR #310).
        with watchdog.watch("ear", ENGINE_LOAD_SECONDS):
            whisper.transcribe(bytes(2 * SAMPLE_RATE), [])  # a second of silence
    transcribe = watchdog.watched(
        "ear", whisper.transcribe, None if gpu else ENGINE_LOAD_SECONDS
    )
    # The detector too, on one of its own: its model is loaded here, not in
    # the first call, and watched like the engines' loads. Each call gets a
    # fresh one, it keeps state from window to window (Codex on PR #310).
    with watchdog.watch("detector", ENGINE_LOAD_SECONDS):
        silero()(bytes(2 * WINDOW))
    return lambda: SegmentingEar(transcribe, watchdog.watched("detector", silero()))


def _piper(watchdog: Watchdog) -> Callable[[], Mouth]:
    from api.telephony.adapters.tts_piper import VOICE, piper

    voice = settings.voice_mouth_voice or VOICE
    with watchdog.watch("mouth", ENGINE_LOAD_SECONDS):
        loaded = piper(MODELS / "piper" / f"{voice}.onnx")
    mouth = WatchedMouth(loaded, watchdog)
    # Empty without an error is a failure too, as in the session
    # (`CallSession._synthesize`) (Codex on PR #310).
    if not b"".join(mouth.speak("Guten Tag.")):
        raise RuntimeError("the mouth made no audio")
    # One for every call: it keeps nothing from one sentence to the next.
    return lambda: mouth


EARS: dict[str, Callable[[Watchdog], Callable[[], Ear]]] = {
    "fake": lambda _: FakeEar,
    "whisper": _whisper,
}
MOUTHS: dict[str, Callable[[Watchdog], Callable[[], Mouth]]] = {
    "fake": lambda _: FakeMouth,
    "piper": _piper,
}


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

    def missed(self, call_id: str) -> bool:
        """Nobody answered the team leg: True once the callback card is written."""
        return self._events.on_team_missed(call_id)


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
    # Raises without a model: every call would go to the team at its first turn.
    ChatCompletionsLLM.from_settings().close()
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
        llm_factory=lambda _db: ChatCompletionsLLM.from_settings(),
    )
    return Voice(
        events=handler,
        lines=lines,
        token=token,
        ear=make_ear,
        mouth=make_mouth,
        silence_seconds=settings.voice_silence_seconds,
    )


def _refuse(why: str) -> NoReturn:
    raise RuntimeError(f"voice does not start: {why}")
