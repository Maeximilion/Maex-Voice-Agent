"""The watchdog of the service `voice`: an engine call that hangs ends the process.

Used by the process (`telephony/app.py`), which starts it before the engines
load, and by the engines' loaders (`adapters/speech_engines.py`), which watch
every load and run with it.
"""

import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from api.core.logging import get_logger
from api.telephony.speech import Mouth

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
