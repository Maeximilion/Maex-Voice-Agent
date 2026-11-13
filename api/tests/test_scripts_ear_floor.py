"""scripts/ear_floor.py: the table and the decision rule of the floor (#678).
The measuring itself runs in the image of `voice`, with the engines."""

import pytest

np = pytest.importorskip("numpy")

from scripts.ear_floor import (  # noqa: E402
    clicks_and_breaths,
    from_log,
    log_table,
    summary,
    table,
    verdict,
)


def test_a_floor_between_the_loudest_noise_and_the_shortest_answer_is_named():
    assert "from 7 to 9 windows" in verdict(6, 9)
    assert "smallest: 7" in verdict(6, 9)


@pytest.mark.parametrize("noise,answer", [(9, 9), (10, 5)])
def test_overlapping_noise_and_answers_keep_the_floor(noise, answer):
    assert "no floor separates" in verdict(noise, answer)


def test_the_table_counts_runs_below_the_floor_per_word_and_per_voice():
    rows = [
        ("answer", "Ja. kerstin-low", 5, "Ja."),
        ("answer", "Ja. kerstin-low", 11, "Ja."),
        ("answer", "Nein. thorsten-medium", 12, None),
        ("noise", "click 10 ms", 0, None),
    ]
    out = table(rows, 8)
    assert "| answer | Ja. | 2 | 5 / 8 / 11 | 1 | Ja. |" in out
    assert "| kerstin-low | 2 | 5 / 8 / 11 | 1 |" in out
    assert "| noise | click 10 ms | 1 | 0 / 0 / 0 | 1 | - |" in out
    assert "smallest: 1" in out


def test_the_lines_of_a_test_call_become_a_table_in_the_order_said(tmp_path):
    log = tmp_path / "voice.log"
    log.write_text(
        "noise line\n"
        "INFO ear measure: windows=5 floor=8 below=True heard='Nein.'\n"
        "INFO ear measure: windows=12 floor=8 below=False heard=''\n"
    )
    out = log_table(from_log(log, "answer"), 8)
    assert "| answer 1 | 5 | True | Nein. |" in out
    assert "| answer 2 | 12 | False | - |" in out


def test_the_logs_of_two_test_calls_give_the_verdict(tmp_path):
    words, clicks = tmp_path / "words.log", tmp_path / "clicks.log"
    words.write_text(
        "ear measure: windows=6 floor=8 below=True heard='Nein.'\n"
        "ear measure: windows=11 floor=8 below=False heard='Ja.'\n"
    )
    clicks.write_text("ear measure: windows=3 floor=8 below=True heard=''\n")
    answers, noises = from_log(words, "answer"), from_log(clicks, "noise")
    assert "from 4 to 6 windows" in summary(answers, noises)
    assert "Both logs are needed" in summary(answers, [])


def test_the_synthetic_noises_are_16_bit_and_not_silent():
    for name, samples in clicks_and_breaths().items():
        assert samples.dtype == np.int16 and abs(samples).max() > 100, name
