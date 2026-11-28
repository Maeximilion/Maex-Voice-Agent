"""The attribution of the latency measurement (#550, evals/latency.py)."""

import math

import pytest

from evals.latency import STAGES, attribute, quantiles, summarize

# One turn: the caller's speech ends at 10.0, the first audio is heard at 12.0.
TURN = [
    ("speech_end", 10.0, 10.0, "Zweimal die 23."),
    ("stt_req", 10.75, 10.75, ""),
    ("stt_start", 10.8, 10.8, ""),
    ("stt_end", 10.95, 10.95, ""),
    ("core_start", 11.0, 11.0, "zweimal die 23"),
    ("llm", 11.0, 11.4, ""),
    ("tool", 11.4, 11.45, "search_menu"),
    ("llm", 11.45, 11.6, ""),
    ("tts_req", 11.65, 11.65, ""),
    ("tts_first", 11.7, 11.7, ""),
    ("tts_end", 11.85, 11.85, ""),
    ("play", 11.86, 11.86, ""),
    ("audible", 12.0, 12.0, ""),
    ("core_end", 11.9, 11.9, "0.05"),
]


def test_the_stages_add_up_to_the_total_without_double_counting():
    turn = attribute(TURN, 10.0, math.inf)

    assert turn["total"] == pytest.approx(2.0)
    assert sum(turn[s] for s in STAGES) == pytest.approx(turn["total"])
    assert turn["turn_end"] == pytest.approx(0.75)
    assert turn["stt_wait"] == pytest.approx(0.05)
    assert turn["core"] == pytest.approx(0.65)
    assert turn["core_llm"] == pytest.approx(0.55)
    assert turn["core_llm_hops"] == 2
    assert turn["core_tools"] == pytest.approx(0.05)
    assert turn["core_rest"] == pytest.approx(0.05)
    assert turn["core_cpu"] == pytest.approx(0.05)
    assert turn["tts_first_piece"] == pytest.approx(0.05)


def test_a_model_hop_after_the_first_sentence_is_not_on_the_critical_path():
    """The core spoke first (speak_now) and asked the model again while the
    sentence played: that hop is outside the turn's latency."""
    late = [*TURN, ("llm", 11.9, 13.0, "")]

    turn = attribute(late, 10.0, math.inf)

    assert turn["core_llm"] == pytest.approx(0.55)
    assert turn["core_llm_hops"] == 2


def test_marks_of_the_turn_before_are_not_taken():
    """Audio of the previous answer still playing when the caller spoke is no
    answer to this turn."""
    earlier = [("play", 9.0, 9.0, ""), ("audible", 9.5, 9.5, ""), *TURN]

    assert attribute(earlier, 10.0, math.inf)["line"] == pytest.approx(0.14)


def test_a_turn_the_ear_never_handed_over_fails_at_that_mark():
    unheard = [e for e in TURN if e[0] != "stt_req"]

    assert attribute(unheard, 10.0, math.inf) == {"failed": "stt_req"}


def test_an_answer_after_the_next_sentence_belongs_to_that_one():
    assert attribute(TURN, 10.0, 11.9) == {"failed": "audible"}


def test_quantiles_of_one_value_and_of_none():
    assert quantiles([1.2]) == {"n": 1, "p50": 1.2, "p95": 1.2, "max": 1.2}
    assert quantiles([]) == {"n": 0, "p50": None, "p95": None, "max": None}


def test_the_summary_counts_failures_apart():
    turns = [
        attribute(TURN, 10.0, math.inf),
        {"failed": "stt_req"},
        {"failed": "stt_req"},
    ]

    summary = summarize(turns)

    assert summary["turns"] == 3
    assert summary["failed"] == {"stt_req": 2}
    assert summary["total"]["n"] == 1
    assert summary["total"]["p50"] == pytest.approx(2.0)
