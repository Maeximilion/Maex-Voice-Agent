"""How fast side sessions burn the 5-hour window, read from Claude Code transcripts.

Usage: python scripts/window_burn.py START END [PROJECTS_DIR]
  START, END: UTC, e.g. 2026-11-23T18:00 2026-11-23T23:00.

Window readings are the `get_usage` tool results ("5-hour limit", percentUsed)
found in the transcripts. Requests come from `wake_cost.rows` and are priced with
`wake_cost.cost`. Cost units weight tokens by type (cache read 0.1, writes 1.25 or
2.0, output 5), NOT by model price, so a per-model fit of percent points per cost
unit absorbs the price difference; read the weights as points per million units.
Findings: the PR that added this script, `.claude/commands/chain.md` section 5.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_SPEC = importlib.util.spec_from_file_location(
    "wake_cost", Path(__file__).resolve().parent / "wake_cost.py"
)
assert _SPEC is not None and _SPEC.loader is not None
wake_cost = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wake_cost)

FAMILIES = ("opus", "sonnet", "haiku", "fable")
BUCKET = timedelta(minutes=10)
BUILDING = 6  # requests in 10 minutes from which a session counts as building
BERLIN = ZoneInfo("Europe/Berlin")


def family(model: str) -> str | None:
    return next((f for f in FAMILIES if f in model.lower()), None)


def session_of(path: Path) -> str:
    return path.parent.parent.name if path.parent.name == "subagents" else path.stem


def transcripts(root: Path) -> list[Path]:
    return [
        p
        for d in root.iterdir()
        if d.is_dir() and "Maex-Voice-Agent" in d.name
        for p in d.rglob("*.jsonl")
    ]


def readings(
    paths: list[Path], start: datetime, end: datetime
) -> list[tuple[datetime, int]]:
    """Every 5-hour percentUsed in the window, sorted, one per timestamp."""
    found: dict[datetime, int] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "5-hour limit" not in line:
                continue
            try:
                row = json.loads(line)
                when = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                for part in row["message"]["content"]:
                    for text in part.get("content", []):
                        for w in json.loads(text["text"])["plan"]["windows"]:
                            if w["label"] == "5-hour limit" and start <= when <= end:
                                found[when] = w["percentUsed"]
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    return sorted(found.items())


def load(
    paths: list[Path], start: datetime, end: datetime
) -> list[tuple[datetime, float, str, str]]:
    """Requests in the window: time, cost units, model family, session."""
    out = []
    for path in paths:
        for when, usage, model in wake_cost.rows(path):
            fam = family(model)
            if fam and start <= when <= end:
                out.append((when, wake_cost.cost(usage)[0], fam, session_of(path)))
    return sorted(out)


def at(points: list[tuple[datetime, int]], when: datetime) -> float | None:
    """Percent at a time, linear between readings; None outside them."""
    for (t0, p0), (t1, p1) in pairwise(points):
        if t0 <= when <= t1:
            return p0 + (p1 - p0) * (when - t0).total_seconds() / max(
                (t1 - t0).total_seconds(), 1
            )
    return None


def solve(xs: list[list[float]], ys: list[float]) -> list[float]:
    """Least squares without intercept, coefficients clipped at 0 (refit without them)."""
    n = len(xs[0])
    active = list(range(n))
    while True:
        k = len(active)
        a = [
            [sum(r[i] * r[j] for r in xs) for j in active]
            + [sum(r[i] * y for r, y in zip(xs, ys, strict=True))]
            for i in active
        ]
        for c in range(k):
            piv = max(range(c, k), key=lambda r: abs(a[r][c]))
            a[c], a[piv] = a[piv], a[c]
            if abs(a[c][c]) < 1e-12:
                a[c][c] = 1e-12
            for r in range(k):
                if r != c:
                    f = a[r][c] / a[c][c]
                    a[r] = [v - f * w for v, w in zip(a[r], a[c], strict=True)]
        sol = [a[i][k] / a[i][i] for i in range(k)]
        if all(v >= 0 for v in sol) or k == 1:
            coef = [0.0] * n
            for i, v in zip(active, sol, strict=True):
                coef[i] = max(v, 0.0)
            return coef
        active.pop(min(range(k), key=lambda i: sol[i]))


def fit(
    points: list[tuple[datetime, int]], reqs: list[tuple[datetime, float, str, str]]
) -> tuple[dict[str, float], float, int]:
    """Points per million cost units per family, R squared, intervals used."""
    fams = [f for f in FAMILIES if any(r[2] == f for r in reqs)]
    xs, ys = [], []
    for (t0, p0), (t1, p1) in pairwise(points):
        row = [0.0] * len(fams)
        for when, units, fam, _ in reqs:
            if t0 < when <= t1:
                row[fams.index(fam)] += units / 1e6
        xs.append(row)
        ys.append(float(p1 - p0))
    if not xs or not fams:
        return {}, 0.0, 0
    coef = solve(xs, ys)
    pred = [sum(c * v for c, v in zip(coef, r, strict=True)) for r in xs]
    mean = sum(ys) / len(ys)
    ss_tot = sum((y - mean) ** 2 for y in ys)
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, pred, strict=True))
    return (
        dict(zip(fams, coef, strict=True)),
        (1 - ss_res / ss_tot if ss_tot else 0.0),
        len(xs),
    )


def buckets(
    start: datetime, end: datetime, reqs: list[tuple[datetime, float, str, str]]
) -> dict[datetime, dict[str, Any]]:
    """Per 10-minute bucket: units per family, requests per session."""
    table: dict[datetime, dict[str, Any]] = {}
    t = start
    while t < end:
        table[t] = {
            "units": defaultdict(float),
            "sessions": defaultdict(int),
            "by": defaultdict(lambda: defaultdict(float)),
        }
        t += BUCKET
    for when, units, fam, session in reqs:
        key = start + ((when - start) // BUCKET) * BUCKET
        if key in table:
            table[key]["units"][fam] += units
            table[key]["sessions"][session] += 1
            table[key]["by"][session][fam] += units
    return table


def lane_rates(
    table: dict[datetime, dict[str, Any]], coef: dict[str, float]
) -> dict[tuple[str, str], float]:
    """Points per hour of one session-bucket, split building (many requests) and waiting."""
    acc: dict[tuple[str, str], list[float]] = defaultdict(list)
    for b in table.values():
        for session, n in b["sessions"].items():
            kind = "building" if n >= BUILDING else "waiting"
            for fam, units in b["by"][session].items():
                acc[(fam, kind)].append(units / 1e6 * coef.get(fam, 0.0) * 6)
    return {k: sum(v) / len(v) for k, v in acc.items()}


def local(t: datetime) -> str:
    return t.astimezone(BERLIN).strftime("%H:%M")


def report(start: datetime, end: datetime, root: Path) -> str:
    paths = transcripts(root)
    pts = readings(paths, start, end)
    reqs = load(paths, start, end)
    out = [
        f"window {local(start)}-{local(end)} Berlin, {len(paths)} transcripts, {len(reqs)} requests, {len(pts)} readings",
        "",
        "readings (Berlin, percent, ahead = percent + minutes to reset / 3 - 100):",
    ]
    out += [
        f"  {local(t)}  {p:3}  {p + (end - t).total_seconds() / 180 - 100:+6.1f}"
        for t, p in pts
    ]
    table = buckets(start, end, reqs)
    out += [
        "",
        "10-minute buckets: units (millions) per model, active lanes, percent rise:",
    ]
    out.append(
        f"  {'time':5} " + " ".join(f"{f:>7}" for f in FAMILIES) + "  lanes  rise"
    )
    for t, b in table.items():
        p0, p1 = at(pts, t), at(pts, t + BUCKET)
        rise = f"{p1 - p0:5.1f}" if p0 is not None and p1 is not None else "    -"
        out.append(
            f"  {local(t)} "
            + " ".join(f"{b['units'][f] / 1e6:7.2f}" for f in FAMILIES)
            + f"  {len(b['sessions']):5}  {rise}"
        )
    coef, r2, n = fit(pts, reqs)
    out += [
        "",
        f"fit: points per million cost units, {n} intervals, R2 {r2:.2f}",
        "  (cost units weight tokens by type, not by model price; the weights absorb price)",
    ]
    out += [f"  {f:7} {c:6.2f}" for f, c in coef.items()]
    rates = lane_rates(table, coef)
    out += ["", "one session, points per hour (measured units x fit):"]
    for (fam, kind), v in sorted(rates.items()):
        out.append(f"  {fam:7} {kind:9} {v:6.1f}")
    for fam in FAMILIES:
        if (fam, "building") in rates:
            rate = rates[(fam, "building")]
            out.append(
                f"  {fam} lanes building: "
                + ", ".join(f"{k} = {k * rate:.0f}/h" for k in (2, 3, 5))
            )
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    start, end = (datetime.fromisoformat(a).replace(tzinfo=UTC) for a in argv[:2])
    root = Path(argv[2]) if len(argv) > 2 else Path.home() / ".claude" / "projects"
    print(report(start, end, root))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
