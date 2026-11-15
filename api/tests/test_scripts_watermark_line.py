"""scripts/watermark_line.py: the stages of the line, the spread spectrum mark
and the table (#637). The neural watermarks are measured by running the
script; their packages are not in CI. Synthetic signals only."""

import pytest

np = pytest.importorskip("numpy")

from scripts import watermark_line as wl  # noqa: E402

RATE = wl.RATE


def voiced(seconds: float = 3.0, seed: int = 0):
    """A speech-like signal: harmonics of a wandering pitch under syllables of
    about 4 Hz, with pauses. No recording."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    pitch = 120 + 20 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / RATE
    x = sum(np.sin(k * phase) / k for k in range(1, 25))
    syllables = np.clip(np.sin(2 * np.pi * 4 * t + rng.uniform(0, 6)), 0, None)
    return (0.2 * x * syllables).astype(np.float32)


def test_the_mark_is_found_at_the_far_end_of_a_noisy_line():
    from api.telephony.speech_bench import on_the_line

    mark = wl.Spread()
    far = on_the_line(wl.pcm16(mark.embed(voiced())), RATE, before=0.37, seed=1)
    score, bits = mark.detect(far)
    assert score > wl.SPREAD_THRESHOLD and bits is None


def test_unmarked_speech_and_another_key_stay_under_the_threshold():
    from api.telephony.speech_bench import on_the_line

    x = voiced(seed=3)
    assert wl.Spread().detect(on_the_line(wl.pcm16(x), RATE, seed=2))[0] < 5.5
    marked = wl.Spread(key=1).embed(x)
    assert wl.Spread(key=2).detect(marked)[0] < wl.SPREAD_THRESHOLD


def test_the_mark_survives_lost_packets_and_a_fast_clock():
    mark = wl.Spread()
    far = wl.drift(wl.lose(mark.embed(voiced()), 0.03, seed=7), 100)
    assert mark.detect(far)[0] > wl.SPREAD_THRESHOLD


def test_a_sentence_shorter_than_one_period_scores_nothing():
    assert wl.Spread().detect(np.zeros(wl.PERIOD, dtype=np.float32))[0] == 0.0


def test_lost_packets_are_whole_frames_and_at_least_one():
    x = np.ones(RATE, dtype=np.float32)
    y = wl.lose(x, 0.0, seed=1)
    gone = (y == 0).reshape(-1, wl.FRAME).all(axis=1)
    assert gone.sum() == 1 and ((y == 0) | (y == 1)).all()
    assert (wl.lose(x, 1.0, seed=1) == 0).all()


def test_a_fast_clock_shortens_the_far_ends_audio():
    assert len(wl.drift(np.zeros(1_000_000), 100)) == 999_900


class Fake:
    name = "fake"
    threshold = 0.5

    def embed(self, x):
        return x + 1.0

    def detect(self, y):
        return (1.0, 0.75) if y.mean() > 0.5 else (0.0, None)


def test_the_table_counts_marks_found_false_positives_and_bits():
    speech = {"a": voiced(1.0), "b": voiced(1.0, seed=1)}
    result = wl.measure(Fake(), speech, codecs=False)
    text = wl.table(Fake(), result, codecs=False)
    assert "| clean | 2 of 2 | 1.00 | 0 of 2 | 0.00 | 75.0% |" in text
    assert "noisy + 3% packets lost" in text
    assert "GSM" not in text and "Codec stages left out" in text


def test_a_method_that_does_not_load_is_reported_and_the_rest_runs(
    tmp_path, capsys, monkeypatch
):
    import wave

    with wave.open(str(tmp_path / "s.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(wl.pcm16(voiced(1.0))[: 2 * 22050])

    def broken():
        raise OSError("no weights")

    monkeypatch.setitem(wl.METHODS, "broken", broken)
    monkeypatch.setitem(wl.METHODS, "fake", Fake)
    monkeypatch.setattr(wl.shutil, "which", lambda _: None)
    assert wl.main(["--speech", str(tmp_path), "--methods", "broken,fake"]) == 0
    out = capsys.readouterr().out
    assert "**broken**: did not load (OSError: no weights)" in out
    assert "**fake**" in out and "| clean | 1 of 1 |" in out


def test_no_speech_is_an_error(tmp_path):
    assert wl.main(["--speech", str(tmp_path)]) == 1


def test_written_marks_are_found_in_a_recording_and_plain_speech_is_not(
    tmp_path, capsys, monkeypatch
):
    from api.telephony.speech_bench import on_the_line

    speech, played = tmp_path / "speech", tmp_path / "played"
    speech.mkdir()
    wl.write_wav(speech / "s.wav", voiced(3.0))
    monkeypatch.setattr(wl.shutil, "which", lambda _: None)
    args = ["--speech", str(speech), "--methods", "spread", "--write", str(played)]
    assert wl.main(args) == 0
    marked = wl.read_wav(played / "s-spread.wav")
    # The recording of a test call: the far end of the modelled line.
    wl.write_wav(tmp_path / "call.wav", on_the_line(wl.pcm16(marked), RATE, 0.5))
    capsys.readouterr()
    files = [str(tmp_path / "call.wav"), str(speech / "s.wav")]
    assert wl.main(["--detect", *files, "--methods", "spread"]) == 0
    out = capsys.readouterr().out
    assert "- call.wav: found, score" in out
    assert "- s.wav: not found, score" in out
