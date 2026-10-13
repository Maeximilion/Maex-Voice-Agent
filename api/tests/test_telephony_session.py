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
from api.telephony.session import KEYPAD_MAX_DIGITS, CallSession, Lines
from api.telephony.session import PLAYED_GRACE_SECONDS as GRACE
from api.telephony.session import UTTERANCE_LIMIT_SECONDS as UTTERANCE_LIMIT
from api.telephony.session import UTTERANCE_SECONDS as UTTERANCE
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
    drain = session.drain

    def drain_and_check() -> None:
        """After every step of a test: a call that is still open waits for
        something with a deadline. Without one it would sit on the line for
        ever when that something never comes (Codex PR #286)."""
        drain()
        assert session.over or session._until_next_timer() is not None

    session.drain = drain_and_check
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


def test_a_yes_that_waited_in_the_queue_is_no_answer_to_the_read_back(call):
    """The guest says the order and at once "Ja", before the worker is through
    with the first sentence. What the core asks for in a turn is carried out
    before anything that is queued behind that turn, so the read-back protects
    itself before the "Ja" is looked at (Codex PR #286, hard rule 3)."""
    call.core.replies["Das wars"] = [("say", READBACK)]
    call.start(GREETING)
    call.played()
    call.clock.now += 1.0
    call.ear.hear(Heard("Das wars", ended_at=call.clock.now))
    call.ear.hear(Heard("Ja", ended_at=call.clock.now + 0.4))
    call.s.key("1")
    call.s.drain()
    assert call.core.turns() == ["Das wars"] and call.core.count("dtmf") == 0
    call.played(after=4.0)
    call.said("Ja", after=0.5)
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


def test_an_utterance_the_ear_never_finishes_is_a_failure(call):
    """The caller talks over a sentence, playback is dropped, and then the ear
    owes the finished utterance. If it never comes, nothing else would ever
    happen on the call: a failure, and the team (Codex PR #286, rule 5)."""
    call.core.on_failure = OUTAGE
    call.core.replies["Was gibt es"] = [("say", "Wir haben viele Gerichte.", True)]
    call.start(GREETING)
    call.played()
    call.said("Was gibt es", after=1.0)
    call.partial("die dreiundzwanzig")
    assert call.line.kinds()[-1] == "flush"
    call.wait(UTTERANCE - 0.5)
    assert call.core.count("voice_failure") == 0 and call.core.count("silence") == 0
    call.wait(1.0)
    assert call.core.count("voice_failure") == 1
    assert call.line.played()[-1] == "Technische Störung."


def test_an_utterance_begun_on_a_quiet_line_and_never_finished_is_a_failure(call):
    call.core.on_failure = OUTAGE
    call.start(GREETING)
    call.played()
    call.clock.now += 1.0
    call.partial("ich hätte")
    call.wait(UTTERANCE + 0.5)
    assert call.core.count("voice_failure") == 1 and call.core.count("silence") == 0


def test_an_utterance_may_take_as_long_as_the_caller_speaks(call):
    call.start(GREETING)
    call.played()
    for _ in range(UTTERANCE_TICKS := 30):  # thirty seconds, reported once a second
        call.clock.now += 1.0
        call.partial("")
    call.said("Eine lange Bestellung", after=0.5)
    assert call.core.count("voice_failure") == 0
    assert (
        call.core.turns() == ["Eine lange Bestellung"] and UTTERANCE_TICKS > UTTERANCE
    )


def test_no_silence_is_counted_while_the_ear_owes_an_utterance(call):
    """The caller began to answer while the question was still playing. The
    question ends, the ear is still working on the answer: that is no silent
    line, however long the ear may take (Codex PR #286)."""
    call.core.replies["Was gibt es"] = [("say", READBACK)]
    call.start(GREETING)
    call.played()
    call.said("Was gibt es", after=1.0)
    call.partial("ja das")
    call.played(after=1.0)
    call.wait(SILENCE + 0.5)
    assert call.core.count("silence") == 0
    call.said("Ja, das stimmt", after=0.5)
    assert call.core.turns()[-1] == "Ja, das stimmt" and call.core.count("silence") == 0


def test_an_utterance_has_an_end_however_long_the_ear_reports_speech(call):
    """A detector stuck in "speech" reports it for ever. Without a limit the
    call would stay open past everything, the tenant's call limit included,
    which is checked when a turn begins (Codex PR #286)."""
    call.core.on_failure = OUTAGE
    call.start(GREETING)
    call.played()
    for _ in range(int(UTTERANCE_LIMIT) - 1):
        call.clock.now += 1.0
        call.partial("")
    assert call.core.count("voice_failure") == 0
    for _ in range(3):
        call.clock.now += 1.0
        call.partial("")
    assert call.core.count("voice_failure") == 1


def test_a_dropped_utterance_starts_the_silence_anew(call):
    """Noise, or an utterance into a read-back that is reported late: nothing
    is taken, and the silent line is counted from there."""
    call.start(GREETING)
    call.played()
    call.clock.now += 1.0
    call.partial("")
    call.said("Ja", after=1.0, speech=False)
    call.wait(SILENCE - 0.5)
    assert call.core.count("silence") == 0
    call.wait(1.0)
    assert call.core.count("silence") == 1 and call.core.count("voice_failure") == 0


def test_a_hash_alone_that_stopped_a_sentence_leaves_the_call_waiting(call):
    call.core.replies["Was gibt es"] = [("say", "Wir haben viele Gerichte.", True)]
    call.start(GREETING)
    call.played()
    call.said("Was gibt es", after=1.0)
    call.key("#")
    assert call.line.kinds()[-1] == "flush"
    call.wait(SILENCE + 0.1)
    assert call.core.count("silence") == 1


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


def test_speech_that_began_before_the_answer_does_not_stop_it(call):
    """The guest was already saying the next sentence while the core worked on
    the first. That partial is older than the answer on the line: no barge-in,
    as for the finished utterance (Codex PR #286)."""

    class Slow(FakeMouth):
        def speak(self, text):
            call.clock.now += 2.0
            return super().speak(text)

    call.s._mouth = Slow()
    call.core.replies["Die 23"] = [("say", "Nummer 23.", True)]
    call.start()
    call.clock.now += 1.0
    call.ear.hear(Heard("Die 23", ended_at=call.clock.now))
    call.ear.hear(Heard("und die dreizehn", final=False))
    call.s.drain()
    assert call.line.played() == ["Nummer 23."] and "flush" not in call.line.kinds()
    call.partial("und die dreizehn bitte")  # still speaking, now over the answer
    assert call.line.kinds()[-1] == "flush"


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


def test_digits_that_waited_in_the_queue_stay_one_answer(call):
    """The worker was busy for longer than the gap. The keys were pressed a
    second apart, and that is what counts, not when they are looked at
    (Codex PR #286)."""
    call.start(GREETING)
    call.played()
    call.clock.now += 1.0
    call.s.key("4")
    call.clock.now += 1.0
    call.s.key("2")
    call.clock.now += 10.0  # the worker comes to them only now
    call.s.drain()
    assert [c for c in call.core.calls if c[0] == "dtmf"] == [("dtmf", "42")]


def test_keys_without_end_are_handed_over_at_a_length(call):
    """A key every two seconds never leaves the gap, so the entry would never
    end and no turn would begin, the tenant's call limit included
    (Codex PR #286). No number on a telephone is that long."""
    call.start(GREETING)
    call.played()
    for n in range(KEYPAD_MAX_DIGITS + 3):
        call.clock.now += GAP - 1.0
        call.key(str(n % 10))
    handed = [c[1] for c in call.core.calls if c[0] == "dtmf"]
    assert [len(digits) for digits in handed] == [KEYPAD_MAX_DIGITS]


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
    """From the first word of an utterance to its end no silence is counted."""
    call.start(GREETING)
    call.played()
    call.wait(SILENCE - 1)
    call.partial("ähm")
    call.wait(2.0)  # past the silence timeout, in the middle of the utterance
    assert call.core.count("silence") == 0
    call.said("Ähm, einen Tisch bitte", after=0.5)
    assert call.core.turns() == ["Ähm, einen Tisch bitte"]
    assert call.core.count("silence") == 0


def test_speech_without_words_yet_is_no_silence(call):
    """An ear without partial transcripts still says that the caller is
    speaking: a long order must not be answered with "Sind Sie noch da?" in the
    middle of it (Codex PR #286). Speech alone stops no sentence."""
    call.core.replies["Was gibt es"] = [("say", "Wir haben viele Gerichte.", True)]
    call.start(GREETING)
    call.played()
    for _ in range(20):  # twenty seconds of speech, reported once a second
        call.clock.now += 1.0
        call.partial("")
    assert call.core.count("silence") == 0
    call.said("Was gibt es", after=0.5)
    call.partial("")
    assert "flush" not in call.line.kinds()
    call.said("hm", after=0.5, speech=False)  # it was noise: the utterance ends
    call.played()
    call.wait(SILENCE + 0.1)
    assert call.core.count("silence") == 1


def test_an_answer_that_waited_in_the_queue_is_no_silence(call):
    """The guest answered three seconds after the question, the worker comes to
    it ten seconds later: an answer, not a silent line."""
    call.start(GREETING)
    call.played()
    call.clock.now += 3.0
    call.ear.hear(Heard("Einen Tisch bitte", ended_at=call.clock.now))
    call.clock.now += 10.0
    call.s.drain()
    assert call.core.turns() == ["Einen Tisch bitte"]
    assert call.core.count("silence") == 0


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


def test_a_farewell_that_cannot_be_spoken_does_not_end_in_a_hangup(call):
    """The mouth fails on the last sentence and the hangup is asked for right
    behind it. A call whose voice failed is not hung up by us: the core's
    transfer, or the dialplan, takes it to the team (Codex PR #286, rule 5)."""
    call.core.replies["Tschüss"] = [("say", "Auf Wiederhören."), ("hangup",)]
    call.core.on_failure = [("transfer", "+497215550000")]
    call.start(GREETING)
    call.played()
    call.mouth.broken = True
    call.said("Tschüss", after=1.0)
    assert "hangup" not in call.line.kinds()
    assert call.line.actions[-1] == ("transfer", "+497215550000")


def test_a_failed_call_the_core_has_closed_already_goes_to_the_dialplan(call):
    """The handler had finished the call when it asked for the hangup, so it
    does not answer the failure. No hangup either way: ended as failed."""
    call.core.replies["Tschüss"] = [("say", "Auf Wiederhören."), ("hangup",)]
    call.start(GREETING)
    call.played()
    call.mouth.broken = True
    call.said("Tschüss", after=1.0)
    assert "hangup" not in call.line.kinds() and call.s.outcome == "failed"


def test_the_core_is_told_of_a_failure_even_if_the_line_refuses_the_flush(call):
    """Two things break at once: the ear, and the line when asked to drop what
    is queued. The core must still close its call (Codex PR #286)."""
    call.core.replies["Was gibt es"] = [("say", "Eine lange Liste.", True)]
    call.start(GREETING)
    call.played()
    call.said("Was gibt es", after=1.0)
    call.line.refuses_flush = True
    call.ear.broken = True
    call.s.audio(b"\x00")
    call.s.drain()
    assert call.core.count("voice_failure") == 1 and call.s.outcome == "failed"


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


def test_a_failure_the_core_does_not_answer_ends_the_session_for_the_dialplan(call):
    """After a failure nothing listens any more. If the core neither transfers
    nor hangs up (it does not know the call), the session must not sit on the
    line: it ends as failed, and the dialplan rings the team (rule 5)."""
    call.start(GREETING)
    call.played()
    call.ear.broken = True
    call.s.audio(b"\x00")
    call.s.drain()
    assert call.core.count("voice_failure") == 1
    assert call.s.outcome == "failed" and "hangup" not in call.line.kinds()


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


def test_a_line_that_does_not_answer_does_not_hold_the_call(call):
    """The telephone system stalls in the middle of a transfer. The one worker
    of the call must not wait in there for ever: after a deadline the session
    ends as failed, so the leg can be closed and the dialplan rings the team
    (Codex PR #286, rule 5)."""
    call.line.stalls = threading.Event()
    call.s._line_seconds = 0.05
    call.start(("transfer", "+497215550000"))
    call.line.stalls.set()
    assert call.s.outcome == "failed" and call.s.transfer_target is None
    assert call.core.count("voice_failure") == 1


def test_a_transfer_the_telephone_system_refuses_fails_the_session(call):
    call.line.refuses_transfer = True
    call.start(("transfer", "+497215550000"))
    assert call.s.outcome == "failed" and call.s.transfer_target is None


# --- A line that does not report the end of playback ------------------------------------


def test_a_lost_end_of_playback_is_a_failure_and_never_a_read_back_heard(call):
    """The line owes a mark for what it played. If none comes, nobody knows
    whether the guest heard the read-back: the call goes to the team, and no
    "Ja" is taken as its answer (Codex PR #286, hard rules 3 and 5)."""
    call.core.on_failure = OUTAGE
    call.core.replies["Das wars"] = [("say", READBACK)]
    call.start(GREETING)
    call.played()
    call.said("Das wars", after=1.0)
    call.wait(GRACE - 1)  # may still be playing
    assert call.core.count("voice_failure") == 0
    call.wait(1.5)  # the grace is over, no mark came
    assert call.core.count("voice_failure") == 1
    call.said("Ja", after=0.5)
    assert call.core.turns() == ["Das wars"]
    # The outage sentence gets no mark either: the transfer does not wait for it.
    call.wait(GRACE + 0.5)
    assert call.line.actions[-1] == ("transfer", "+497215550000")
    assert call.s.outcome == "transfer"


def test_a_lost_end_of_playback_of_a_plain_sentence_is_a_failure_too(call):
    call.core.on_failure = OUTAGE
    call.start(("say", "Was darf es sein?", True))
    call.wait(GRACE + 0.5)
    assert call.core.count("voice_failure") == 1 and call.core.count("silence") == 0


def test_a_farewell_nobody_may_have_heard_does_not_end_in_a_hangup(call):
    """The call was ending, the farewell was queued, and the line never said
    that it played. Hanging up would tell the dialplan that all is well; the
    caller may have heard nothing. Ended as failed, the team is rung
    (Codex PR #286, rule 5)."""
    call.core.replies["Tschüss"] = [("say", "Auf Wiederhören."), ("hangup",)]
    call.start(GREETING)
    call.played()
    call.said("Tschüss", after=1.0)
    call.wait(GRACE + 0.5)
    assert "hangup" not in call.line.kinds() and call.s.outcome == "failed"
    assert call.core.count("voice_failure") == 1


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
    assert call.core.count("voice_failure") == 0
    call.wait(1.5)
    assert call.core.count("voice_failure") == 1


def test_a_report_that_waited_in_the_queue_is_no_lost_end_of_playback(call):
    call.core.replies["Das wars"] = [("say", READBACK)]
    call.start(GREETING)
    call.played()
    call.said("Das wars", after=1.0)
    call.clock.now += 1.0
    call.s.played(call.line.mark)  # reported in time
    call.clock.now += GRACE * 4  # looked at late
    call.s.drain()
    assert call.core.count("voice_failure") == 0
    call.said("Ja", after=0.5)
    assert call.core.turns() == ["Das wars", "Ja"]


# --- A mouth that takes its time, or never finishes --------------------------------------


def test_a_second_sentence_that_took_long_to_make_is_no_lost_end_of_playback(call):
    """Two sentences in one turn, the second took six seconds to synthesize:
    its audio cannot be through before it was queued (Codex PR #286)."""

    class SlowOnTheSecond(FakeMouth):
        def speak(self, text):
            if text == "Zwei.":
                call.clock.now += 6.0
            return super().speak(text)

    call.s._mouth = SlowOnTheSecond()
    call.core.replies["Hallo"] = [("say", "Eins.", True), ("say", "Zwei.", True)]
    call.start()
    call.said("Hallo", after=1.0)
    assert call.core.count("voice_failure") == 0
    call.played(after=1.0)
    call.wait(SILENCE + 0.1)
    assert call.core.count("voice_failure") == 0 and call.core.count("silence") == 1


def test_what_was_said_while_the_mouth_was_working_does_not_stop_the_answer(call):
    """Playback begins when the audio is on the line, not when the sentence was
    asked for: an utterance that ended in between is the next turn and no
    barge-in (Codex PR #286)."""

    class SlowMouth(FakeMouth):
        def speak(self, text):
            call.clock.now += 2.0  # synthesis takes two seconds
            return super().speak(text)

    call.s._mouth = SlowMouth()
    call.core.replies["Die 23"] = [("say", "Nummer 23.", True)]
    call.core.replies["Und die 13"] = [("say", "Nummer 13.", True)]
    call.start()
    asked = call.clock.now + 1.0
    call.said("Die 23", after=1.0)
    call.ear.hear(Heard("Und die 13", ended_at=asked + 1.0))  # while it was made
    call.s.drain()
    assert "flush" not in call.line.kinds()
    assert call.line.played() == ["Nummer 23.", "Nummer 13."]


def test_a_mouth_that_never_finishes_does_not_hold_the_call(call):
    """A hung engine must not block the one worker of the call: after the
    deadline it is a failure, and the transfer still happens (Codex PR #286)."""
    hung = threading.Event()

    class HungMouth(FakeMouth):
        def speak(self, text):
            hung.wait(5)
            return super().speak(text)

    call.s._mouth = HungMouth()
    call.s._speak_seconds = 0.05
    call.core.on_failure = [("transfer", "+497215550000")]
    call.start(GREETING)
    hung.set()
    assert call.core.count("voice_failure") == 1
    assert call.line.actions == [("transfer", "+497215550000")]


def test_a_mouth_that_makes_no_audio_has_not_spoken(call):
    """An engine that returns an empty stream without an error. Queued as it
    is, the line would report the read-back as played, and the "Ja" behind it
    would answer a sentence nobody heard (Codex PR #286, hard rule 3)."""

    class Mute(FakeMouth):
        def speak(self, text):
            return iter(())

    call.core.on_failure = [("transfer", "+497215550000")]
    call.core.replies["Das wars"] = [("say", READBACK)]
    call.start(GREETING)
    call.played()
    call.s._mouth = Mute()
    call.said("Das wars", after=1.0)
    assert call.core.count("voice_failure") == 1
    assert call.line.played() == ["Guten Tag."]
    call.said("Ja", after=1.0)
    assert call.core.turns() == ["Das wars"]
    assert call.line.actions[-1] == ("transfer", "+497215550000")


def test_a_sentence_without_words_is_not_spoken_and_no_failure(call):
    """The core hands over an empty line now and then. Nothing to say is no
    broken mouth, and it protects nothing."""
    call.core.replies["Hm"] = [("say", "  "), ("say", "Wie bitte?", True)]
    call.start(GREETING)
    call.played()
    call.said("Hm", after=1.0)
    assert call.core.count("voice_failure") == 0
    assert call.line.played() == ["Guten Tag.", "Wie bitte?"]
    assert call.mouth.spoken == ["Guten Tag.", "Wie bitte?"]


def test_a_mouth_that_failed_is_not_asked_for_the_outage_sentence(call):
    """The engine hangs. Asking it again for the outage sentence would cost the
    caller a second deadline of silence before the team is rung
    (Codex PR #286)."""
    hung = threading.Event()
    asked: list[str] = []

    class HungMouth(FakeMouth):
        def speak(self, text):
            asked.append(text)
            hung.wait(5)
            return super().speak(text)

    call.s._mouth = HungMouth()
    call.s._speak_seconds = 0.05
    call.core.on_failure = OUTAGE
    call.start(GREETING)
    hung.set()
    assert asked == ["Guten Tag."]
    assert call.line.actions == [("transfer", "+497215550000")]


def test_a_mouth_that_raises_late_is_a_failure(call):
    class BrokenMidway(FakeMouth):
        def speak(self, text):
            yield b"half"
            raise RuntimeError("the stream broke")

    call.s._mouth = BrokenMidway()
    call.core.on_failure = [("transfer", "+497215550000")]
    call.start(GREETING)
    assert call.core.count("voice_failure") == 1 and call.line.played() == []


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


def test_events_from_several_threads_are_queued_in_the_order_of_their_time():
    """The time of an event and its place in the queue are taken together.
    Otherwise a late event can overtake a timely one whose thread paused
    between the clock and the queue, and a timer that runs on the time of the
    events fires for something that was in time (Codex PR #286)."""
    import time

    def clock() -> float:
        now = time.monotonic()
        time.sleep(0)  # let another thread in between the clock and the queue
        return now

    lines = Lines()
    session = CallSession(
        "s1",
        line=FakeLine(),
        ear=FakeEar(),
        mouth=FakeMouth(),
        events=Core(lines),
        silence_seconds=SILENCE,
        clock=clock,
    )

    def post() -> None:
        for _ in range(2000):
            session.audio(b"")

    threads = [threading.Thread(target=post) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    times = []
    while not session._inbox.empty():
        times.append(session._inbox.get_nowait()[1])
    assert len(times) == 8000 and times == sorted(times)
