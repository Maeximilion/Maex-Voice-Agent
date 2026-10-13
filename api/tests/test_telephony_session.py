"""telephony/session.py: the call session rules of docs/20 §4, with fakes for ear,
mouth and line (T-10.4).

One test per row of the rules table. The session is driven on one thread with a
clock the test sets: `drain()` handles what is queued and then the timers. What
the core would do is scripted in `Core`, which answers a turn through the same
port the real handler uses.
"""

import threading
from types import SimpleNamespace

import pytest

from api.telephony.adapters.fake_speech import FakeEar, FakeLine, FakeMouth
from api.telephony.session import PLAYED_GRACE_SECONDS as GRACE
from api.telephony.session import CallSession, Lines
from api.telephony.speech import Heard

SILENCE = 6.0
GAP = 3.0
READBACK = "Zweimal Nummer 23. Macht 13 Euro 80. Stimmt das so?"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Core:
    """Stands in for `CallHandler`: records the events and answers as scripted.

    `on_start` and `replies[text]` are lists of calls on the port, as
    `(method, *arguments)`.
    """

    def __init__(self, port: Lines) -> None:
        self.port = port
        self.calls: list[tuple] = []
        self.on_start: list[tuple] = []
        self.replies: dict[str, list[tuple]] = {}
        self.on_silent: list[tuple] = []
        self.on_failure: list[tuple] = []
        self.crash_on: str | None = None

    def _do(self, session_id: str, actions: list[tuple]) -> None:
        for method, *arguments in actions:
            getattr(self.port, method)(session_id, *arguments)

    def on_call_started(self, session_id: str) -> None:
        self.calls.append(("started",))
        self._do(session_id, self.on_start)

    def on_user_turn(self, session_id: str, text: str) -> None:
        self.calls.append(("turn", text))
        if text == self.crash_on:
            raise RuntimeError("the core crashed")
        self._do(session_id, self.replies.get(text, []))

    def on_dtmf(self, session_id: str, digits: str) -> None:
        self.calls.append(("dtmf", digits))

    def on_silence(self, session_id: str) -> None:
        self.calls.append(("silence",))
        self._do(session_id, self.on_silent)

    def on_voice_failure(self, session_id: str) -> None:
        self.calls.append(("voice_failure",))
        self._do(session_id, self.on_failure)

    def on_call_ended(self, session_id: str) -> None:
        self.calls.append(("ended",))

    def turns(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "turn"]

    def count(self, kind: str) -> int:
        return sum(1 for call in self.calls if call[0] == kind)


@pytest.fixture
def call():
    clock, line, ear, mouth, lines = (
        Clock(),
        FakeLine(),
        FakeEar(),
        FakeMouth(),
        Lines(),
    )
    core = Core(lines)
    session = CallSession(
        "s1",
        line=line,
        ear=ear,
        mouth=mouth,
        events=core,
        caller_id="+497215551234",
        clock=clock,
        silence_seconds=SILENCE,
        keypad_gap_seconds=GAP,
    )
    lines.add(session)
    c = SimpleNamespace(
        clock=clock, line=line, ear=ear, mouth=mouth, lines=lines, core=core, s=session
    )

    def start(*on_start: tuple) -> None:
        core.on_start = list(on_start)
        session.start()
        session.drain()

    def said(text: str, *, after: float = 0.0, **more) -> None:
        """The caller finishes an utterance `after` seconds from now."""
        clock.now += after
        ear.hear(Heard(text, ended_at=clock.now, **more))
        session.drain()

    def partial(text: str) -> None:
        ear.hear(Heard(text, final=False))
        session.drain()

    def played(*, after: float = 0.0) -> None:
        """The line reports that everything queued was played."""
        clock.now += after
        session.played(line.mark)
        session.drain()

    def wait(seconds: float) -> None:
        clock.now += seconds
        session.drain()

    def key(digits: str) -> None:
        for digit in digits:
            session.key(digit)
        session.drain()

    c.start, c.said, c.partial, c.played, c.wait, c.key = (
        start,
        said,
        partial,
        played,
        wait,
        key,
    )
    return c


GREETING = ("say", "Guten Tag.")


# --- Call start ---------------------------------------------------------------------


def test_the_first_sentence_answers_the_line(call):
    call.start(GREETING)
    assert call.core.calls == [("started",)]
    assert call.line.actions == [("answer",), ("play", "Guten Tag.")]
    assert call.lines.caller_id("s1") == "+497215551234"


def test_a_transfer_before_any_sentence_leaves_the_call_unanswered(call):
    """Mode `paused` or `shadow`, the limit reached, the database down: the team
    telephone rings, and the caller never hears us."""
    call.start(("transfer", "+497215550000"))
    assert call.line.actions == [("transfer", "+497215550000")]
    assert (call.s.outcome, call.s.transfer_target) == ("transfer", "+497215550000")
    assert call.ear.closed


def test_the_line_is_answered_once(call):
    call.start(GREETING, ("say", "Was kann ich für Sie tun?"))
    assert call.line.kinds() == ["answer", "play", "play"]


# --- End of utterance ----------------------------------------------------------------


def test_a_finished_utterance_is_one_turn(call):
    call.start(GREETING)
    call.played()
    call.said("Einen Tisch für vier, bitte.", after=1.0)
    assert call.core.turns() == ["Einen Tisch für vier, bitte."]


def test_audio_goes_to_the_ear_with_its_arrival_time(call):
    call.start(GREETING)
    call.s.audio(b"\x01\x02")
    call.clock.now += 0.02
    call.s.audio(b"\x03\x04")
    call.s.drain()
    assert call.ear.frames == [(b"\x01\x02", 100.0), (b"\x03\x04", 100.02)]


# --- Draining -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ending", "action", "outcome"),
    [
        (("hangup",), ("hangup",), "hangup"),
        (("transfer", "+497215550000"), ("transfer", "+497215550000"), "transfer"),
    ],
)
def test_a_transfer_or_hangup_waits_for_the_end_of_the_last_sentence(
    call, ending, action, outcome
):
    call.core.replies["Tschüss"] = [("say", "Eins."), ("say", "Zwei."), ending]
    call.start(GREETING)
    call.played()
    call.said("Tschüss", after=1.0)
    assert call.line.played()[-2:] == ["Eins.", "Zwei."]
    assert action not in call.line.actions and call.s.outcome == "open"
    call.played(after=2.0)
    assert call.line.actions[-1] == action and call.s.outcome == outcome


def test_nothing_is_said_behind_a_transfer_or_hangup(call):
    call.core.replies["Tschüss"] = [
        ("say", "Auf Wiederhören."),
        ("hangup",),
        ("say", "x"),
    ]
    call.start(GREETING)
    call.played()
    call.said("Tschüss", after=1.0)
    assert call.line.played() == ["Guten Tag.", "Auf Wiederhören."]


def test_a_transfer_replaces_a_hangup_that_waits(call):
    """A failure while the farewell is playing: the team, not the dead line."""
    call.core.replies["Tschüss"] = [("say", "Auf Wiederhören."), ("hangup",)]
    call.start(GREETING)
    call.played()
    call.said("Tschüss", after=1.0)
    call.lines.transfer("s1", "+497215550000")
    call.lines.hangup("s1")
    call.played(after=1.0)
    assert call.line.actions[-1] == ("transfer", "+497215550000")


# --- Read-back (hard rule 3) -----------------------------------------------------------


def test_a_read_back_is_not_interrupted(call):
    call.core.replies["Das wars"] = [("say", READBACK)]
    call.start(GREETING)
    call.played()
    call.said("Das wars", after=1.0)
    call.partial("ja genau das stimmt")
    call.key("1")
    assert "flush" not in call.line.kinds()


def test_a_yes_inside_the_read_back_is_no_answer(call):
    """An utterance counts only if it ends after the read-back was played to the
    end - also when the ear reports it late."""
    call.core.replies["Das wars"] = [("say", READBACK)]
    call.start(GREETING)
    call.played()
    call.said("Das wars", after=1.0)
    call.said("Ja", after=2.0)  # while it plays
    assert call.core.turns() == ["Das wars"]
    call.clock.now += 3.0
    spoken_before_the_end = call.clock.now - 0.1
    call.played()
    call.ear.hear(Heard("Ja genau", ended_at=spoken_before_the_end))
    call.s.drain()
    assert call.core.turns() == ["Das wars"]
    call.said("Ja", after=0.8)
    assert call.core.turns() == ["Das wars", "Ja"]


def test_keys_pressed_inside_the_read_back_are_dropped(call):
    call.core.replies["Das wars"] = [("say", READBACK)]
    call.start(GREETING)
    call.played()
    call.said("Das wars", after=1.0)
    call.key("1#")
    call.played(after=4.0)
    call.wait(GAP + 1)
    assert call.core.count("dtmf") == 0


def test_a_transcript_without_speech_energy_is_dropped(call):
    """Line noise that an engine turned into words is no yes."""
    call.start(GREETING)
    call.played()
    call.said("Ja", after=1.0, speech=False)
    assert call.core.turns() == []


def test_a_read_back_behind_an_interruptible_sentence_protects_both(call):
    call.core.replies["Das wars"] = [("say", "Gern.", True), ("say", READBACK)]
    call.start(GREETING)
    call.played()
    call.said("Das wars", after=1.0)
    call.partial("ja das stimmt so")
    call.said("Ja", after=0.5)
    assert "flush" not in call.line.kinds() and call.core.turns() == ["Das wars"]


# --- Barge-in --------------------------------------------------------------------------


def test_two_words_stop_a_sentence_that_may_be_interrupted(call):
    call.core.replies["Was gibt es"] = [("say", "Wir haben viele Gerichte.", True)]
    call.start(GREETING)
    call.played()
    call.said("Was gibt es", after=1.0)
    call.partial("die")
    assert "flush" not in call.line.kinds()
    call.partial("die dreiundzwanzig")
    assert call.line.kinds()[-1] == "flush"
    call.said("Die dreiundzwanzig bitte", after=1.0)
    assert call.core.turns()[-1] == "Die dreiundzwanzig bitte"
    assert call.line.kinds().count("flush") == 1


def test_a_key_stops_a_sentence_that_may_be_interrupted(call):
    call.core.replies["Was gibt es"] = [("say", "Wir haben viele Gerichte.", True)]
    call.start(GREETING)
    call.played()
    call.said("Was gibt es", after=1.0)
    call.key("2")
    assert call.line.kinds()[-1] == "flush"


def test_a_finished_word_stops_a_sentence_that_may_be_interrupted(call):
    """One word is no barge-in while it is being said, but a finished "Nein" is
    an answer: the agent stops and takes it, instead of talking over it."""
    call.core.replies["Was gibt es"] = [("say", "Wir haben viele Gerichte.", True)]
    call.start(GREETING)
    call.played()
    call.said("Was gibt es", after=1.0)
    call.said("Nein", after=1.0)
    assert call.line.kinds()[-1] == "flush" and call.core.turns()[-1] == "Nein"


def test_what_was_said_before_the_answer_began_does_not_stop_it(call):
    """The guest added a sentence while the core was still working. It is the
    next turn, and the answer to the first one is played to its end first."""
    call.core.replies["Die 23"] = [("say", "Nummer 23.", True)]
    call.core.replies["Und die 13"] = [("say", "Nummer 13.", True)]
    call.start(GREETING)
    call.played()
    call.clock.now += 1.0
    first, second = call.clock.now, call.clock.now + 0.5
    call.ear.hear(Heard("Die 23", ended_at=first))
    call.clock.now += 2.0  # the core took its time
    call.ear.hear(Heard("Und die 13", ended_at=second))
    call.s.drain()
    assert call.core.turns() == ["Die 23", "Und die 13"]
    assert "flush" not in call.line.kinds()
    assert call.line.played()[-2:] == ["Nummer 23.", "Nummer 13."]


def test_nothing_is_interrupted_once_a_transfer_or_hangup_waits(call):
    call.core.replies["Tschüss"] = [("say", "Auf Wiederhören.", True), ("hangup",)]
    call.start(GREETING)
    call.played()
    call.said("Tschüss", after=1.0)
    call.partial("halt warten Sie")
    call.key("1")
    call.said("Halt, noch etwas", after=0.5)
    assert "flush" not in call.line.kinds() and call.core.turns() == ["Tschüss"]
    call.played(after=1.0)
    assert call.s.outcome == "hangup"


# --- Keypad ----------------------------------------------------------------------------


def test_digits_are_handed_over_as_one_answer_at_the_hash(call):
    call.start(GREETING)
    call.played()
    call.clock.now += 1.0
    call.key("42#")
    assert call.core.calls[-1] == ("dtmf", "42")


def test_digits_are_handed_over_after_a_gap(call):
    call.start(GREETING)
    call.played()
    call.clock.now += 1.0
    call.key("4")
    call.wait(GAP - 0.5)
    call.key("2")
    call.wait(GAP - 0.5)
    assert call.core.count("dtmf") == 0
    call.wait(1.0)
    assert call.core.calls[-1] == ("dtmf", "42")


def test_a_hash_alone_hands_nothing_over(call):
    call.start(GREETING)
    call.played()
    call.clock.now += 1.0
    call.key("#")
    assert call.core.count("dtmf") == 0


# --- Silence ---------------------------------------------------------------------------


def test_silence_is_counted_from_the_end_of_playback(call):
    call.start(GREETING)
    call.wait(GRACE - 1)  # still playing: no silence yet
    assert call.core.count("silence") == 0
    call.played()
    call.wait(SILENCE - 0.5)
    assert call.core.count("silence") == 0
    call.wait(1.0)
    assert call.core.count("silence") == 1
    call.wait(SILENCE - 0.5)  # reported once per silence
    assert call.core.count("silence") == 1
    call.wait(1.0)  # the core said nothing: the next silence counts from the report
    assert call.core.count("silence") == 2


def test_a_re_prompt_starts_the_silence_anew(call):
    call.core.on_silent = [("say", "Sind Sie noch da?", True)]
    call.start(GREETING)
    call.played()
    call.wait(SILENCE + 0.1)
    call.wait(GRACE - 1)  # the re-prompt is still playing
    assert call.core.count("silence") == 1
    call.played()
    call.wait(SILENCE + 0.1)
    assert call.core.count("silence") == 2


def test_a_caller_who_speaks_is_not_silent(call):
    call.start(GREETING)
    call.played()
    call.wait(SILENCE - 1)
    call.partial("ähm")
    call.wait(SILENCE - 1)
    assert call.core.count("silence") == 0
    call.wait(1.5)
    assert call.core.count("silence") == 1


def test_a_turn_without_an_answer_starts_the_silence_too(call):
    call.start(GREETING)
    call.played()
    call.said("Hm", after=1.0)  # the core says nothing
    call.wait(SILENCE + 0.1)
    assert call.core.count("silence") == 1


def test_no_silence_once_the_call_is_ending(call):
    call.core.replies["Tschüss"] = [("say", "Auf Wiederhören."), ("hangup",)]
    call.start(GREETING)
    call.played()
    call.said("Tschüss", after=1.0)
    call.wait(SILENCE * 2)
    assert call.core.count("silence") == 0


# --- Ear or mouth fails ----------------------------------------------------------------

OUTAGE = [("say", "Technische Störung."), ("transfer", "+497215550000")]


def test_an_ear_that_fails_hands_the_call_to_the_team(call):
    call.core.on_failure = OUTAGE
    call.start(GREETING)
    call.played()
    call.ear.broken = True
    call.s.audio(b"\x00")
    call.s.audio(b"\x00")
    call.s.drain()
    assert call.core.count("voice_failure") == 1
    assert call.line.played()[-1] == "Technische Störung."
    call.played(after=1.0)
    assert call.s.outcome == "transfer"


def test_a_mouth_that_fails_still_hands_the_call_to_the_team(call):
    """The outage sentence cannot be spoken either. The transfer is what counts."""
    call.core.on_failure = OUTAGE
    call.core.replies["Hallo"] = [("say", "Guten Tag noch einmal.")]
    call.start(GREETING)
    call.played()
    call.mouth.broken = True
    call.said("Hallo", after=1.0)
    assert call.core.count("voice_failure") == 1
    assert call.line.played() == ["Guten Tag."]
    assert call.line.actions[-1] == ("transfer", "+497215550000")


def test_a_failure_drops_what_is_still_queued(call):
    call.core.on_failure = OUTAGE
    call.core.replies["Was gibt es"] = [("say", "Eine lange Liste.", True)]
    call.start(GREETING)
    call.played()
    call.said("Was gibt es", after=1.0)
    call.ear.broken = True
    call.s.audio(b"\x00")
    call.s.drain()
    assert call.line.kinds()[-2:] == ["flush", "play"]


def test_speech_without_a_transcript_twice_is_a_failure_of_the_ear(call):
    call.core.on_failure = OUTAGE
    call.start(GREETING)
    call.played()
    call.said("", after=1.0)
    assert call.core.count("voice_failure") == 0
    call.said("Die 23", after=1.0)
    call.said("", after=1.0)
    assert call.core.count("voice_failure") == 0  # not twice in a row
    call.said("  ", after=1.0)
    assert call.core.count("voice_failure") == 1


def test_nothing_is_heard_after_a_failure(call):
    call.core.on_failure = OUTAGE
    call.start(GREETING)
    call.played()
    call.ear.broken = True
    call.s.audio(b"\x00")
    call.s.drain()
    call.said("Ja", after=0.5)
    call.key("1#")
    assert call.core.turns() == [] and call.core.count("dtmf") == 0


# --- The line goes, the core crashes -----------------------------------------------------


def test_a_caller_who_hangs_up_ends_the_call(call):
    call.start(GREETING)
    call.s.closed()
    call.s.drain()
    assert call.core.calls[-1] == ("ended",) and call.s.outcome == "closed"
    assert call.ear.closed
    call.said("Hallo", after=1.0)
    assert call.core.turns() == []


def test_a_crash_in_the_core_ends_the_session_without_a_hangup(call):
    """Only an explicit hangup lets the dialplan end the call; after anything
    else it rings the team (docs/20 §5)."""
    call.core.crash_on = "Hallo"
    call.start(GREETING)
    call.played()
    call.said("Hallo", after=1.0)
    assert call.s.outcome == "failed"
    assert "hangup" not in call.line.kinds() and "transfer" not in call.line.kinds()
    # The core closes its call log as a failure, not as a caller who hung up.
    assert call.core.calls[-1] == ("voice_failure",)


def test_a_transfer_the_telephone_system_refuses_fails_the_session(call):
    call.line.refuses_transfer = True
    call.start(("transfer", "+497215550000"))
    assert call.s.outcome == "failed" and call.s.transfer_target is None


# --- A line that does not report the end of playback ------------------------------------


def test_a_lost_end_of_playback_does_not_leave_the_caller_unheard(call):
    """The line owes a mark for what it played. If it never comes, the session
    goes on by the length of the audio plus a grace: a read-back that stayed
    "playing" for ever would drop every answer."""
    call.core.replies["Das wars"] = [("say", READBACK)]
    call.start(GREETING)
    call.played()
    call.said("Das wars", after=1.0)
    call.said("Ja", after=GRACE - 1)  # may still be playing
    assert call.core.turns() == ["Das wars"]
    call.wait(1.5)  # the grace is over, no mark came
    call.said("Ja", after=0.5)
    assert call.core.turns() == ["Das wars", "Ja"]


def test_a_lost_end_of_playback_does_not_hold_a_transfer_back(call):
    call.core.replies["Hilfe"] = [
        ("say", "Ich verbinde."),
        ("transfer", "+497215550000"),
    ]
    call.start(GREETING)
    call.played()
    call.said("Hilfe", after=1.0)
    call.wait(GRACE - 1)
    assert call.s.outcome == "open"
    call.wait(1.5)
    assert call.line.actions[-1] == ("transfer", "+497215550000")


def test_the_grace_counts_from_the_end_of_the_audio_not_from_its_start(call):
    """Ten seconds of audio are not given up on after five."""
    call.mouth.seconds = 10.0
    call.core.replies["Das wars"] = [("say", READBACK)]
    call.start()
    call.said("Das wars", after=1.0)
    call.wait(10.0 + GRACE - 1)
    call.said("Ja", after=0.0)
    assert call.core.turns() == ["Das wars"]
    call.wait(1.5)
    call.said("Ja", after=0.5)
    assert call.core.turns() == ["Das wars", "Ja"]


# --- The port over all sessions ---------------------------------------------------------


def test_the_port_reaches_a_call_by_its_session_id(call):
    call.start()
    call.lines.say("s1", "Guten Tag.")
    call.lines.say("other", "Niemand hört das.")
    call.lines.hangup("other")
    call.s.drain()
    assert call.line.played() == ["Guten Tag."]
    assert call.lines.caller_id("other") is None
    with pytest.raises(LookupError):
        call.lines.transfer("other", "+497215550000")
    call.lines.remove("s1")
    assert call.lines.caller_id("s1") is None


def test_nothing_is_recorded(call):
    """Nothing calls it before the legal check in docs/09 (CLAUDE.md §9)."""
    with pytest.raises(RuntimeError):
        call.lines.start_recording("s1")


# --- One worker per call -----------------------------------------------------------------


def test_the_worker_handles_the_events_of_a_call_in_order_and_ends_with_it():
    line, ear, mouth, lines = FakeLine(), FakeEar(), FakeMouth(), Lines()
    core = Core(lines)
    core.on_start = [GREETING]
    core.replies = {f"Satz {n}": [("say", f"Antwort {n}", True)] for n in range(20)}
    session = CallSession(
        "s1", line=line, ear=ear, mouth=mouth, events=core, silence_seconds=SILENCE
    )
    lines.add(session)
    # A daemon: a failure below must not leave the test run waiting for it.
    worker = threading.Thread(target=session.run, daemon=True)
    worker.start()

    def spoken(count: int) -> None:
        """Wait until the worker has queued that many sentences on the line."""
        for _ in range(500):
            if len(line.played()) >= count:
                return
            threading.Event().wait(0.01)
        raise AssertionError(f"the worker spoke {line.played()}")

    session.start()
    spoken(1)
    for n in range(20):
        session.played(line.mark)
        # Audio and keys from the line arrive on other threads meanwhile.
        session.audio(b"\x00")
        ear.hear(Heard(f"Satz {n}", ended_at=float("inf")))
        spoken(n + 2)
    session.closed()
    worker.join(timeout=5)
    assert not worker.is_alive() and session.outcome == "closed"
    assert core.turns() == [f"Satz {n}" for n in range(20)]
    assert line.played() == ["Guten Tag.", *(f"Antwort {n}" for n in range(20))]
    assert core.calls[-1] == ("ended",) and len(ear.frames) == 20


def test_the_worker_wakes_for_a_timer_without_an_event():
    """Nothing arrives on a silent line: the silence is reported all the same."""
    line, ear, mouth, lines = FakeLine(), FakeEar(), FakeMouth(), Lines()
    core = Core(lines)
    core.on_silent = [("hangup",)]
    session = CallSession(
        "s1", line=line, ear=ear, mouth=mouth, events=core, silence_seconds=0.05
    )
    lines.add(session)
    worker = threading.Thread(target=session.run, daemon=True)
    worker.start()
    session.start()
    worker.join(timeout=5)
    assert not worker.is_alive() and session.outcome == "hangup"
    assert core.count("silence") == 1
