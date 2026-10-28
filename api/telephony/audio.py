"""Test tones in raw call audio: make one, find one (docs/20 §2, spike T-10.1).

Audio is signed linear, 16 bit, little endian, mono, as the media channel carries
it. A tone of a known frequency is the cheapest proof that sound arrives, in the
right direction and on the right call; the probe and the spike kit both use this.
Standard library only, so the kit runs on a host without the API's packages.
"""

import math
import sys
from array import array
from collections.abc import Iterable

# Share of full scale. Loud enough to survive a telephone codec, far from clipping.
AMPLITUDE = 0.3
# A tone counts as present from this estimated amplitude on.
FLOOR = 0.1
# A tone counts as heard when it sounds at least this long, whatever surrounds it.
MIN_TONE_SECONDS = 0.1
FULL_SCALE = 32768
# A voice counts from this peak on (about -34 dBFS); the hiss of a quiet line stays below.
VOICE_FLOOR = 0.02


def tone(
    freq_hz: int, seconds: float, rate: int, amplitude: float = AMPLITUDE
) -> bytes:
    """A sine of `freq_hz` as raw samples."""
    step = 2 * math.pi * freq_hz / rate
    peak = amplitude * (FULL_SCALE - 1)
    samples = array(
        "h", (round(peak * math.sin(step * n)) for n in range(int(seconds * rate)))
    )
    return _to_bytes(samples)


def level(pcm: bytes, freq_hz: int, rate: int) -> float:
    """Estimated amplitude of `freq_hz` in `pcm`, as a share of full scale."""
    return _level(_samples(pcm), freq_hz, rate)


def dominant(pcm: bytes, candidates: Iterable[int], rate: int) -> int | None:
    """The candidate frequency that sounds longest in `pcm`, None when none of
    them sounds. How much other audio surrounds the tone does not matter."""
    seconds = {freq: tone_seconds(pcm, freq, rate) for freq in candidates}
    best = max(seconds, key=lambda freq: seconds[freq], default=None)
    if best is None or seconds[best] < MIN_TONE_SECONDS:
        return None
    return best


def tone_seconds(pcm: bytes, freq_hz: int, rate: int) -> float:
    """How long `freq_hz` sounds in `pcm` in total. A gap in the tone is not
    counted, so a dropout in the middle shortens the result.

    Looked at in windows of 20 ms, one frame of the phone line; a window the tone
    only partly fills may not count, so each edge is exact to about that.
    """
    return len(_sounding(pcm, freq_hz, rate)) * (rate // 50) / rate


def tone_span(pcm: bytes, freq_hz: int, rate: int) -> tuple[float, float] | None:
    """The seconds in `pcm` at which `freq_hz` first sounds and last stops, None
    when it never sounds. Where a tone is, which its length alone does not tell."""
    sounding = _sounding(pcm, freq_hz, rate)
    if not sounding:
        return None
    return sounding[0] / rate, (sounding[-1] + rate // 50) / rate


def loud_seconds(pcm: bytes, rate: int, floor: float = VOICE_FLOOR) -> float:
    """Seconds of 20 ms windows whose peak reaches `floor` of full scale: a voice
    on the line, whatever its pitch, where line noise stays below."""
    samples = _samples(pcm)
    window = rate // 50
    peak = floor * FULL_SCALE
    loud = sum(
        1
        for start in range(0, len(samples) - window + 1, window)
        if max(map(abs, samples[start : start + window])) >= peak
    )
    return round(loud * window / rate, 2)


def _sounding(pcm: bytes, freq_hz: int, rate: int) -> list[int]:
    """Where the 20 ms windows start, in samples, in which `freq_hz` sounds."""
    samples = _samples(pcm)
    window = rate // 50
    return [
        start
        for start in range(0, len(samples) - window + 1, window)
        if _level(samples[start : start + window], freq_hz, rate) >= FLOOR
    ]


def _samples(pcm: bytes) -> array:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _to_bytes(samples: array) -> bytes:
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def _level(samples: array, freq_hz: int, rate: int) -> float:
    """Goertzel: the strength of one frequency without a full transform."""
    if not samples:
        return 0.0
    coeff = 2 * math.cos(2 * math.pi * freq_hz / rate)
    prev = prev2 = 0.0
    for sample in samples:
        prev, prev2 = sample + coeff * prev - prev2, prev
    power = prev * prev + prev2 * prev2 - coeff * prev * prev2
    return 2 * math.sqrt(max(power, 0.0)) / len(samples) / FULL_SCALE
