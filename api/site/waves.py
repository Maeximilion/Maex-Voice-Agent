"""The sound waves of the hero as SVG path data.

Each wave is two periods of 1440 units, so a shift by half its width loops
without a seam. Drawn on the server, so the page needs no script for them.
"""

WIDTH = 2880
MID = 120

SWINGS = {
    "neon": (
        60,
        (
            20,
            36,
            60,
            96,
            140,
            180,
            200,
            176,
            132,
            88,
            52,
            28,
            16,
            28,
            56,
            100,
            152,
            192,
            168,
            120,
            76,
            44,
            24,
            16,
        ),
    ),
    "violet": (
        80,
        (
            30,
            70,
            120,
            170,
            150,
            100,
            60,
            30,
            20,
            40,
            90,
            150,
            190,
            160,
            110,
            70,
            40,
            20,
        ),
    ),
    "light": (120, (40, 90, 150, 110, 60, 30, 50, 120, 170, 130, 80, 40)),
}


def wave_path(segment: int, swings: tuple[int, ...]) -> str:
    """Half waves of `segment` units, alternating up and down, each swinging as
    far as the next number of `swings` says."""
    parts = [f"M0 {MID}"]
    for i in range(WIDTH // segment):
        swing = swings[i % len(swings)] * (1 if i % 2 else -1)
        parts.append(
            f"Q{i * segment + segment // 2} {MID + swing} {(i + 1) * segment} {MID}"
        )
    return " ".join(parts)


def paths() -> dict[str, str]:
    return {name: wave_path(seg, swings) for name, (seg, swings) in SWINGS.items()}
