"""evals/voice.py: the audio evals (T-10.7) without engines.

The real ear and mouth run only in the speech image (`make eval-voice`). Here a
stand-in ear segments audio by its energy as the segmenting ear does, and a
stand-in mouth makes audio as long as its sentence, so the hook into the runner,
the noise rule and the read-back check in a real call session run in CI.
"""

import json
from pathlib import Path

from api.telephony.speech import Heard
from evals import voice
from evals.runner import run
from evals.voice import (
    SAMPLE_RATE,
    answer_to_readback,
    hear_with_noise,
    noise_cases,
    recognition,
)

CASE = Path("evals/cases/abholung_0001_zwei_positionen_mit_option.json")
READBACK = "Zweimal Nummer 23, einmal Nummer 47 mit Huhn. Macht 25 Euro 70. Stimmt das?"


def _case() -> dict:
    return json.loads(CASE.read_text(encoding="utf-8"))


class LoudMouth:
    """A tenth of a second of audio per character, never silent."""

    def speak(self, text: str):
        yield b"\x00\x10" * int(SAMPLE_RATE * 0.1 * len(text))


class EnergyEar:
    """Reports `text` as one utterance: speech is any frame that is not silent,
    and 0.7 s of silence ends it, stamped with when its last speech ended."""

    def __init__(self, text: str = "Ja") -> None:
        self.text = text
        self.sink = lambda heard: None
        self.spoken_until: float | None = None
        self.silent = 0.0

    def open(self, sink) -> None:
        self.sink = sink

    def feed(self, frame: bytes, at: float) -> None:
        if any(frame):
            self.spoken_until, self.silent = at, 0.0
            return
        if self.spoken_until is None:
            return
        self.silent += len(frame) / 2 / SAMPLE_RATE
        if self.silent >= 0.7:
            self.sink(Heard(self.text, ended_at=self.spoken_until))
            self.spoken_until = None

    def expect(self, terms) -> None: ...

    def close(self) -> None: ...


YES = b"\x00\x10" * int(SAMPLE_RATE * 0.5)


def test_a_yes_inside_the_read_back_never_reaches_the_core():
    assert answer_to_readback(READBACK, YES, -0.4, EnergyEar(), LoudMouth()) == []


def test_the_same_yes_after_the_read_back_is_the_callers_answer():
    """The control: without it the check above would pass on a deaf session."""
    assert answer_to_readback(READBACK, YES, 1.0, EnergyEar(), LoudMouth()) == ["Ja"]


def test_the_case_is_played_as_the_ear_heard_it(migrated_db_url, tmp_path):
    """A 23 heard as 24 is a wrong dish in the database: red by the same judge."""

    def misheard(line: str) -> str:
        return line.replace("die 23", "die 24")

    report = run(
        cases=[_case()], db_url=migrated_db_url, report_dir=tmp_path, hear=misheard
    )
    [result] = report.cases
    assert not result.passed
    assert any("items" in d for d in result.diffs), result.diffs


def test_a_line_without_speech_is_no_turn(migrated_db_url, tmp_path):
    """No speech heard in the yes: the order stays a draft, nothing confirmed."""
    noisy = hear_with_noise(lambda line: line, None)
    report = run(
        cases=noise_cases([_case()]),
        db_url=migrated_db_url,
        report_dir=tmp_path,
        hear=noisy,
    )
    [result] = report.cases
    assert result.id == "abholung_0001+noise"
    assert result.passed, result.diffs
    assert result.readback  # the agent got to the read-back, the yes was missing


def test_noise_heard_as_a_yes_is_a_confirm_after_noise(migrated_db_url, tmp_path):
    """An ear that makes "Ja" of line noise: the case ends confirmed, which the
    summary counts as hard."""
    noisy = hear_with_noise(lambda line: line, "Ja.")
    report = run(
        cases=noise_cases([_case()]),
        db_url=migrated_db_url,
        report_dir=tmp_path,
        hear=noisy,
    )
    [result] = report.cases
    assert not result.passed
    summary = voice.summarize({}, report, report, report, "Ja.", [], None)
    assert summary["hard"]["confirm_after_noise"] == 1
    assert not summary["passed"]


def test_a_deaf_ear_never_passes(migrated_db_url, tmp_path):
    """Nothing heard: no read-back to check and no noise variant that got to
    one, so both hard checks would be 0 for having checked nothing."""
    report = run(
        cases=noise_cases([_case()]),
        db_url=migrated_db_url,
        report_dir=tmp_path,
        hear=lambda line: None,
    )
    summary = voice.summarize({}, report, report, report, None, [], None)
    assert not any(summary["hard"].values())
    assert summary["noise_reached_readback"] == "0/1"
    assert not summary["passed"]


def test_a_read_back_without_its_control_is_unchecked_not_passed(
    migrated_db_url, tmp_path
):
    """One read-back whose control the ear missed: reported as unchecked; the
    run passes on the one that was checked."""
    report = run(
        cases=noise_cases([_case()]),
        db_url=migrated_db_url,
        report_dir=tmp_path,
        hear=hear_with_noise(lambda line: line, None),
    )
    rows = [
        {"readback": "A", "inside": [], "after": ["Ja"], "mouth_first_seconds": 0.1},
        {"readback": "B", "inside": [], "after": [], "mouth_first_seconds": 0.1},
    ]
    summary = voice.summarize({}, report, report, report, None, rows, None)
    assert summary["readback"]["unchecked_control_missing"] == ["B"]
    assert summary["passed"]
    rows[0]["after"] = []
    assert not voice.summarize({}, report, report, report, None, rows, None)["passed"]


def test_only_the_confirming_yes_becomes_noise():
    """An earlier yes (a consent) stays, or the call never gets to the
    read-back whose answer the noise stands for."""
    case = _case()
    case["transcript"].insert(1, {"role": "customer", "text": "Ja, genau."})
    [variant] = noise_cases([case])
    texts = [t["text"] for t in variant["transcript"]]
    assert texts.count(voice.NOISE) == 1 and texts[-1] == voice.NOISE
    assert "Ja, genau." in texts


def test_a_noise_variant_sends_no_second_confirm():
    """Without a first confirm the runner would call a missing second one red,
    and the summary would count a confirm after noise that never was."""
    repeated = {**_case(), "repeat_confirm": True}
    [variant] = noise_cases([repeated])
    assert "repeat_confirm" not in variant


def test_only_cases_that_end_confirmed_get_a_noise_variant():
    unconfirmed = {**_case(), "id": "x", "expected": {"confirmed": False}}
    assert [c["id"] for c in noise_cases([_case(), unconfirmed])] == [
        "abholung_0001+noise"
    ]


def test_numbers_and_dish_names_of_a_line_are_judged():
    assert recognition("Die 23 bitte.", "Die 23 bitte.") == {"number": "right"}
    assert recognition("Die 23 bitte.", "Die 24 bitte.") == {"number": "wrong"}
    assert recognition("Die 23 bitte.", None) == {"number": "asked"}
    ente = "Einmal die Ente knusprig."
    assert recognition(ente, "Einmal die Ente knusprig.")["dishes"] == ["right"]
    assert recognition(ente, "Einmal die Ente.")["dishes"] == ["asked"]
    # Two positions in one sentence: the case judges them, not the number rule.
    assert "number" not in recognition("Zweimal die 23 und die 24.", "")
    assert recognition("Huhn bitte.", "Huhn bitte.") == {}
