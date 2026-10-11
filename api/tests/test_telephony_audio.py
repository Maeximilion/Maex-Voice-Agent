"""telephony/audio.py: test tones and their detection in raw call audio (T-10.1)."""

import pytest

from api.telephony.audio import dominant, level, tone, tone_seconds

RATE = 16000


def test_tone_has_the_length_of_its_duration():
    assert len(tone(440, 1.5, RATE)) == int(1.5 * RATE) * 2


def test_level_finds_the_tone_at_its_own_frequency_only():
    pcm = tone(440, 1.0, RATE, amplitude=0.3)

    assert level(pcm, 440, RATE) == pytest.approx(0.3, abs=0.01)
    assert level(pcm, 1000, RATE) < 0.01


def test_dominant_names_the_frequency_that_is_there():
    pcm = tone(1200, 0.5, 8000)

    assert dominant(pcm, [1000, 1200, 1400], 8000) == 1200


def test_dominant_is_none_for_silence_and_for_no_audio():
    assert dominant(bytes(RATE * 2), [440, 554], RATE) is None
    assert dominant(b"", [440, 554], RATE) is None


def test_dominant_is_none_for_a_tone_that_is_no_candidate():
    assert dominant(tone(300, 0.5, RATE), [1000, 1200], RATE) is None


def test_tone_seconds_is_how_long_the_tone_sounds_inside_silence():
    pcm = bytes(RATE * 2) + tone(440, 2.0, RATE) + bytes(RATE)

    assert tone_seconds(pcm, 440, RATE) == pytest.approx(2.0, abs=0.03)


def test_tone_seconds_does_not_count_a_gap_in_the_tone():
    pcm = tone(440, 0.4, RATE) + bytes(int(3.2 * RATE) * 2) + tone(440, 0.4, RATE)

    assert tone_seconds(pcm, 440, RATE) == pytest.approx(0.8, abs=0.03)


def test_tone_seconds_ignores_another_tone():
    pcm = tone(554, 1.0, RATE) + tone(440, 1.0, RATE)

    assert tone_seconds(pcm, 440, RATE) == pytest.approx(1.0, abs=0.03)


def test_tone_seconds_is_zero_without_the_tone():
    assert tone_seconds(bytes(RATE), 440, RATE) == 0.0


def test_an_odd_trailing_byte_is_dropped_not_an_error():
    pcm = tone(440, 0.5, RATE) + b"\x01"

    assert level(pcm, 440, RATE) == pytest.approx(0.3, abs=0.01)
