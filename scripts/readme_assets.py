#!/usr/bin/env python3
"""The README's images: logo, banner, measured charts and the animated call flow.

Every asset in `.github/assets/` is written by this script from data in the
repository, so the start page cannot claim more than the docs do:

- `docs/metrics/eval_history.csv`: one dated row per eval run on a real model
- `docs/metrics/voice_history.csv`: one dated row per voice layer measurement
- `docs/07_WORKPACKAGES.md`: task status, read for progress and the call flow
- `docs/01_STATUS.md`: stage and gates; `README.md`: version; `api/tests`: test count
- `docs/metrics/capabilities.toml`: the README's "What works" and "Not yet working"

`render` also writes the README's text regions (`<!-- readme:status -->` and
`<!-- readme:capabilities -->`); everything outside them is written by hand.

Usage:
    python scripts/readme_assets.py render            write all SVG assets
    python scripts/readme_assets.py render --check    fail if a committed asset is stale (advisory in CI)
    python scripts/readme_assets.py render --png      also the PNGs (needs Chromium)
    python scripts/readme_assets.py add <report.json> [--suite cases|targets]
    python scripts/readme_assets.py add-voice <date> <package> <metric> <value> ...
    python scripts/readme_assets.py pending [--record]   runs not yet in docs/metrics/
    python scripts/readme_assets.py findings <out.json>  what the maintenance bot reports
    python scripts/readme_assets.py mark-uploaded        social preview uploaded in Settings

`make eval`, `make eval-targets` and the voice kits record on their own (`auto-record`,
`record_kit_run`), on a branch only: on `main` a result waits in evals/reports/.

Rendering is deterministic: every date comes from the data, never from today, so
`--check` in CI only fails when the data moved and the images did not.
Design rules and tokens: docs/15_README_STRATEGY.md section "Assets and design".
"""

from __future__ import annotations

import argparse
import csv
import functools
import hashlib
import io
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / ".github" / "assets"
EVAL_HISTORY = ROOT / "docs" / "metrics" / "eval_history.csv"
VOICE_HISTORY = ROOT / "docs" / "metrics" / "voice_history.csv"
WORKPACKAGES = ROOT / "docs" / "07_WORKPACKAGES.md"
STATUS = ROOT / "docs" / "01_STATUS.md"
README = ROOT / "README.md"
CAPABILITIES = ROOT / "docs" / "metrics" / "capabilities.toml"
TESTS = ROOT / "api" / "tests"
BERLIN = ZoneInfo("Europe/Berlin")

# --------------------------------------------------------------------------- tokens
# "Night Service": the evening line after dark (Midnight Purple) and the lit sign of
# a restaurant that picks up (Neon Yellow). Single source for every asset; the
# palette was checked with the dataviz validator against SURFACE (CVD and contrast
# pass; yellow sits above the lightness band on purpose and is always labelled).
TOKENS = {
    "bg": "#0D0819",
    "surface": "#17102B",
    "surface_hi": "#21183D",
    "grid": "#2B2148",
    "ink": "#F1ECFF",
    "muted": "#A89CCB",
    "neon": "#E8FF2E",
    "neon_core": "#FAFFD6",
    "violet": "#9D7BFF",
    "violet_core": "#E9E1FF",
    "coral": "#FF5277",
    "plum": "#76699F",
}
FONT = "ui-monospace, 'JetBrains Mono', 'SF Mono', Menlo, Consolas, 'DejaVu Sans Mono', monospace"
# Which logo concept the assets use; the others stay drawable for the concept sheet.
LOGO_STYLE = "split"
LOGO_STYLES = ("neon", "wave", "split", "stamp")
REPO_TITLE = "MÆX"

# The call path as the code runs it (api/telephony, api/agent/loop.py, api/domain):
# Asterisk, our own exchange, hands the audio to the service `voice`; its ear turns
# speech into text; the conversation core asks the LLM, which answers with one
# sentence or one tool call; the core checks the call and runs the domain function,
# which reads Postgres; the result goes back to the LLM, at most six hops; the
# sentence goes out through the mouth. Nothing is confirmed before the caller's yes.
#
# One node per component, with the tasks that build it. A node is "built" when all
# of its tasks are done, "in progress" when any has started, and "planned"
# otherwise. A new component is one new entry here, its links and its place in
# ARCH_AT; the call flow's station rail names nodes from here as well.
#
# `covers` names what a node stands for: tools of `api/agent/dispatch.TOOLS`,
# services of docker-compose.yml and adapters under `api/telephony/adapters/`
# (patterns allowed). A test fails when one of them is neither covered nor in
# NOT_DRAWN, so a new component cannot be forgotten here.
FLOW_NODES: dict[str, dict] = {
    "caller": {"label": ["Caller"], "tasks": [], "kind": "person"},
    "access": {"label": ["Access", "router, SIP"], "tasks": ["T-10.1", "T-10.2"]},
    "asterisk": {
        "label": ["Asterisk", "our exchange"],
        "tasks": ["T-1.11"],
        "covers": ["service:asterisk", "adapter:asterisk"],
    },
    "ear": {
        "label": ["Ear", "speech to text"],
        "tasks": ["T-10.5", "T-1.11"],
        "covers": ["service:voice", "adapter:stt_*"],
    },
    "mouth": {
        "label": ["Mouth", "text to speech"],
        "tasks": ["T-10.5", "T-1.11"],
        "covers": ["service:voice", "adapter:tts_*"],
    },
    "core": {
        "label": ["Conversation", "core, guards"],
        "chip": "Core",
        "tasks": ["T-2.1", "T-2.2"],
        "covers": ["service:api"],
    },
    "llm": {"label": ["LLM", "understands"], "tasks": ["T-2.4"]},
    "status": {
        "label": ["Open now?", "get_service_status"],
        "tasks": ["T-1.3"],
        "covers": ["tool:get_service_status"],
        "code": True,
    },
    "menu": {
        "label": ["Menu", "search_menu"],
        "tasks": ["T-4.3", "T-4.4"],
        "covers": ["tool:search_menu"],
        "code": True,
    },
    "draft": {
        "label": ["Order draft", "draft_order"],
        "tasks": ["T-4.5"],
        "covers": ["tool:draft_order"],
        "code": True,
    },
    "confirm": {
        "label": ["Confirm", "confirm"],
        "tasks": ["T-1.6"],
        "covers": ["tool:confirm"],
        "code": True,
    },
    "postgres": {
        "label": ["Postgres", "the facts"],
        "tasks": ["T-1.1", "T-4.1"],
        "covers": ["service:db"],
        "kind": "db",
    },
    "slip": {"label": ["Order slip", "print bridge"], "tasks": ["T-4.6"]},
    "tablet": {
        "label": ["Tablet", "approval"],
        "tasks": ["T-3.1", "T-3.4", "T-4.7"],
        "covers": ["tool:create_callback"],
    },
    "push": {
        "label": ["Push", "outbox, n8n"],
        "tasks": ["T-1.12"],
        "covers": ["service:push", "service:dispatcher", "service:n8n"],
    },
    "teamphone": {
        # It stands in the cold band of the architecture but is rung while the
        # caller is on the line: a transfer, or the exchange when `voice` fails.
        "label": ["Team phone", "rung during the call"],
        "tasks": ["T-1.8", "T-1.11"],
        "covers": ["tool:transfer_to_team"],
    },
    "team": {"label": ["Team"], "tasks": [], "kind": "person"},
}
# What the pictures leave out on purpose, each with its reason.
NOT_DRAWN: dict[str, str] = {
    "tool:check_delivery": "a delivery takes the same hop through the core; the picture follows a pickup call",
    "tool:find_customer": "a delivery takes the same hop through the core; the picture follows a pickup call",
    "tool:check_slot": "a reservation takes the same hop through the core; the picture follows a pickup call",
    "tool:create_reservation": "a reservation takes the same hop through the core; the picture follows a pickup call",
    "tool:get_item_details": "a question about a dish, the same hop and the same menu as search_menu",
    "adapter:fake": "plays calls from files for the tests and the text phone, never on a call",
    "adapter:fake_speech": "the ear and mouth of the drills and the tests, never on a real call",
}
# The wires of the architecture, one per pair of nodes.
FLOW_LINKS: list[tuple[str, str]] = [
    ("caller", "access"),
    ("access", "asterisk"),
    ("asterisk", "ear"),
    ("ear", "core"),
    ("core", "mouth"),
    ("mouth", "asterisk"),
    # The LLM talks to the core and to nothing else: it never reads Postgres.
    ("core", "llm"),
    ("core", "status"),
    ("core", "menu"),
    ("core", "draft"),
    ("core", "confirm"),
    ("status", "postgres"),
    ("menu", "postgres"),
    ("draft", "postgres"),
    ("confirm", "postgres"),
    ("postgres", "slip"),
    ("postgres", "tablet"),
    ("postgres", "push"),
    ("slip", "team"),
    ("tablet", "team"),
    ("push", "team"),
    ("teamphone", "team"),
]

BLOCK_NAMES = {
    "0": "Scaffold",
    "1": "Reservations",
    "1b": "Conversation core",
    "1c": "Own voice layer",
    "2": "Tablet GUI",
    "3": "Pickup",
    "4": "Delivery",
    "5": "Learning",
    "5b": "Deployment",
    "6": "Operations",
}

HARD_RULES = [
    (
        "AI understands, code decides",
        "Prices, zones, hours, availability,",
        "allergens: from the database only.",
    ),
    ("Never guess", "No item enters an order without", "a definite menu_item_id."),
    (
        "Nothing without confirmation",
        "Only the caller's explicit yes",
        "turns a draft into an order.",
    ),
    (
        "Errors are measured",
        "Evals run before every merge.",
        "No gate without numbers.",
    ),
    (
        "Every outage ends with the team",
        "If anything breaks, the team",
        "phone rings. No call is lost.",
    ),
    (
        "Token-efficient by design",
        "Smallest context, smallest model",
        "that passes the evals.",
    ),
]

# ----------------------------------------------------------------------------- data

EVAL_FIELDS = [
    "date",
    "run_at",
    "report",
    "model",
    "suite",
    "total",
    "passed",
    "crashed",
    "pending",
    "guessed",
    "unconfirmed",
    "missed_esc",
    "false_esc_rate",
    "refused",
    "tokens_per_case",
    "source",
    "note",
]
VOICE_FIELDS = [
    "date",
    "package",
    "metric",
    "value",
    "unit",
    "limit",
    "verdict",
    "note",
    # When a kit's run ended; empty for a row recorded by hand. Two runs of one day
    # with the same result stay two rows.
    "run_at",
]
# What makes two voice rows the same measurement, next to run_at.
VOICE_KEY = ("date", "package", "metric", "value", "unit")
STAND_IN = "scripted-stand-in"
# What `evals/runner.py` writes as model for the rule-based stand-in (its default).
STAND_IN_NAMES = {"", "scripted", STAND_IN}
SUITE_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
FULL_SUITES = ("cases", "targets")


class RecordError(Exception):
    """A report that must not go into the history as asked."""


def _int(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    return int(raw) if raw else None


def _float(raw: str | None) -> float | None:
    raw = (raw or "").strip()
    return float(raw) if raw else None


@dataclass
class EvalRun:
    date: date
    model: str
    suite: str
    total: int
    passed: int
    crashed: int | None = None
    pending: int | None = None
    guessed: int | None = None
    unconfirmed: int | None = None
    missed_esc: int | None = None
    false_esc_rate: float | None = None
    refused: int | None = None
    tokens_per_case: int | None = None
    source: str = ""
    note: str = ""
    run_at: str = ""

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def hard(self) -> int | None:
        """Sum of the three hard metrics, or None if any of them was not recorded."""
        parts = [self.guessed, self.unconfirmed, self.missed_esc]
        if any(p is None for p in parts):
            return None
        return sum(p for p in parts if p is not None)


@dataclass
class VoiceRow:
    date: date
    package: str
    metric: str
    value: float
    unit: str
    limit: str
    verdict: str
    note: str = ""
    # As written in the CSV, so "2.0 %" stays "2.0 %" next to a limit of "2 %".
    shown: str = ""
    run_at: str = ""


def unique(rows: list[dict[str, str]], key: Callable[[dict[str, str]], object]):
    """Rows in file order without repeats: `merge=union` in .gitattributes keeps a
    row that both sides of a merge appended twice."""
    seen: set[object] = set()
    out = []
    for row in rows:
        if (k := key(row)) not in seen:
            seen.add(k)
            out.append(row)
    return out


def _eval_identity(row: dict[str, str]) -> object:
    # A backfilled row has no report name: only an identical line is a repeat.
    return eval_key(row) if row.get("report") else tuple(sorted(row.items()))


def _voice_identity(row: dict[str, str]) -> object:
    # Values as numbers: "2.0" in the backfill is 2 from the command line.
    measured = tuple(float(row[k]) if k == "value" else row[k] for k in VOICE_KEY)
    return (row.get("run_at") or "", *measured)


def _voice_key(row: dict[str, str]) -> object | None:
    """The identity of a history row, None for one that does not parse."""
    try:
        return _voice_identity(row)
    except (KeyError, TypeError, ValueError):
        return None


def _waiting_key(row: dict[str, str]) -> object | None:
    """The identity of a waiting row, None for one that is not whole: a line cut off
    when a kit was killed (too few fields, or too many where the next row was
    appended to it), or a hand edit with a date `render` cannot read.
    ponytail: a line cut inside its last field still has every field; one write
    of one short line makes that rare, so no end-of-line marker for it."""
    if None in row or None in row.values():
        return None
    try:
        date.fromisoformat(row["date"])
    except (KeyError, ValueError):
        return None
    return _voice_key(row)


def voice_identities(text: str | None) -> set[object]:
    return {_voice_key(r) for r in csv.DictReader(io.StringIO(text or ""))} - {None}


def read_eval_history(path: Path = EVAL_HISTORY) -> list[EvalRun]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        rows = unique(list(csv.DictReader(fh)), _eval_identity)
    runs = [
        EvalRun(
            date=date.fromisoformat(r["date"]),
            model=r["model"],
            suite=r["suite"],
            total=int(r["total"]),
            passed=int(r["passed"]),
            crashed=_int(r.get("crashed")),
            pending=_int(r.get("pending")),
            guessed=_int(r.get("guessed")),
            unconfirmed=_int(r.get("unconfirmed")),
            missed_esc=_int(r.get("missed_esc")),
            false_esc_rate=_float(r.get("false_esc_rate")),
            refused=_int(r.get("refused")),
            tokens_per_case=_int(r.get("tokens_per_case")),
            source=r.get("source", ""),
            note=r.get("note", ""),
            run_at=r.get("run_at", ""),
        )
        for r in rows
    ]
    # By day, then by the report's own timestamp, so a run recorded late still
    # sits in its place. Backfilled rows have none and keep their file order,
    # ahead of the timed runs of the same day (sorted() is stable).
    return sorted(runs, key=lambda run: (run.date, run.run_at))


def read_voice_history(path: Path = VOICE_HISTORY) -> list[VoiceRow]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        rows = unique(list(csv.DictReader(fh)), _voice_identity)
    out = [
        VoiceRow(
            date=date.fromisoformat(r["date"]),
            package=r["package"],
            metric=r["metric"],
            value=float(r["value"]),
            unit=r["unit"],
            limit=r.get("limit", ""),
            verdict=r["verdict"],
            note=r.get("note", ""),
            shown=r["value"].strip(),
            run_at=r.get("run_at") or "",
        )
        for r in rows
    ]
    return sorted(out, key=lambda row: (row.date, row.run_at))


def run_from_report(report: dict, suite: str) -> dict[str, str]:
    """One history row from a report JSON of `evals/report.py`."""
    run_at = datetime.fromisoformat(report["run_at"])
    if run_at.tzinfo is not None:
        run_at = run_at.astimezone(BERLIN)
    cases = report.get("cases", [])
    hard = report.get("hard") or {}
    rate = report.get("false_escalation_rate")

    def shown(value: object) -> str:
        return "" if value is None else str(value)

    model = report.get("model") or ""
    return {
        "date": run_at.date().isoformat(),
        # The report's own timestamp: the key that makes recording it twice a no-op.
        "run_at": report["run_at"],
        "model": STAND_IN if model in STAND_IN_NAMES else model,
        "suite": suite,
        "total": str(report["total"]),
        "passed": str(report["passed"]),
        "crashed": str(sum(1 for c in cases if c.get("error"))),
        "pending": str(
            sum(
                1
                for c in cases
                if c.get("pending") and not c.get("error") and not c.get("passed")
            )
        ),
        "guessed": shown(hard.get("guessed_items")),
        "unconfirmed": shown(hard.get("unconfirmed")),
        "missed_esc": shown(hard.get("missed_escalations")),
        "false_esc_rate": "" if rate is None else f"{rate:.4f}",
        "refused": shown(report.get("refused_attempts")),
        "tokens_per_case": shown(report.get("tokens_per_case")),
        "source": "report",
        "note": "",
    }


def append_row(path: Path, fields: list[str], row: dict[str, str]) -> None:
    # An empty file is one whose writer was killed before the header.
    new = not path.exists() or path.stat().st_size == 0
    path.parent.mkdir(parents=True, exist_ok=True)
    cut = False
    if not new:
        # A line a killed writer cut off stays a line of its own.
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            cut = fh.read(1) != b"\n"
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        if new:
            writer.writeheader()
        elif cut:
            fh.write("\n")
        writer.writerow({k: row.get(k, "") for k in fields})


def checked_row(
    report: dict,
    suite: str | None,
    reference: bool = False,
    note: str = "",
    report_name: str = "",
) -> dict[str, str]:
    """The history row of one report, or RecordError for what would corrupt the charts.

    A run with tags is partial and needs its own suite name, so it never poses as
    the full suite; the stand-in goes in only as the explicit reference row; a run
    in which no request reached the model measures nothing.
    """
    tags = report.get("tags") or []
    if suite is None:
        if tags:
            raise RecordError(
                f"the run has tags {', '.join(tags)}, so it is partial: name its suite, "
                "for example --suite reservations"
            )
        suite = "cases"
    if not SUITE_RE.fullmatch(suite):
        raise RecordError(f"suite {suite!r}: lowercase letters, digits and dashes only")
    if tags and suite in FULL_SUITES:
        raise RecordError(
            f"the run has tags {', '.join(tags)}: it cannot stand for the full suite {suite!r}"
        )
    row = run_from_report(report, suite)
    row["note"] = note
    row["report"] = report_name
    if row["model"] == STAND_IN and not reference:
        raise RecordError(
            "this is the scripted stand-in, not a real model; pass --reference to record "
            "it as the reference row"
        )
    cases = report.get("cases", [])
    metered = sum(c.get("requests", 0) - c.get("unmetered", 0) for c in cases)
    # No metered request at all: every request failed, or every case crashed
    # before its first one. Either way the cases show handovers, not a model.
    if row["model"] != STAND_IN and metered <= 0:
        raise RecordError(
            f"no request to model {row['model']!r} succeeded (mistyped name or the "
            "server down): nothing of this run measures the model"
        )
    return row


def eval_key(row: dict[str, str]) -> tuple[str, ...]:
    """The identity of a run in the history. The report file carries microseconds,
    run_at only seconds: the file name is the identity, run_at and model the
    fallback for a report given without one."""
    if row.get("report"):
        return (row["report"], row["suite"])
    return (row.get("run_at", ""), row["model"], row["suite"])


def is_recorded(row: dict[str, str], path: Path = EVAL_HISTORY) -> bool:
    if not path.exists():
        return False
    with path.open(encoding="utf-8", newline="") as fh:
        # A row of the file is compared under the key of the new one, so a report
        # given by name never matches a backfilled row without a name.
        keys = {
            eval_key(
                {**old, "report": old.get("report", "") if row.get("report") else ""}
            )
            for old in csv.DictReader(fh)
        }
    return eval_key(row) in keys


def record_report(
    report: dict,
    suite: str | None,
    reference: bool = False,
    note: str = "",
    path: Path = EVAL_HISTORY,
    report_name: str = "",
) -> str:
    """Append one report to the history; the same report recorded twice stays one row."""
    row = checked_row(report, suite, reference, note, report_name)
    if is_recorded(row, path):
        return f"already recorded: {row['suite']} run of {row['run_at']}"
    append_row(path, EVAL_FIELDS, row)
    return f"recorded {row['suite']} {row['passed']}/{row['total']} on {row['date']}"


def is_voice_recorded(row: dict[str, str], path: Path = VOICE_HISTORY) -> bool:
    if not path.exists():
        return False
    return _voice_identity(row) in voice_identities(path.read_text(encoding="utf-8"))


def record_voice(row: dict[str, str], path: Path = VOICE_HISTORY) -> str:
    """Append one voice measurement; the same one twice stays one row."""
    if is_voice_recorded(row, path):
        return f"already recorded: {row['metric']} on {row['date']}"
    append_row(path, VOICE_FIELDS, row)
    return f"recorded {row['metric']} on {row['date']}"


# Status words of docs/07, German and English: the column is free text.
DONE_WORDS = ("fertig", "done", "erledigt", "b fertig")
PROGRESS_WORDS = ("in arbeit", "in progress", "part ")
BLOCKED_WORDS = ("blockiert", "blocked")
DROPPED_WORDS = ("dropped", "entfallen")


def classify(status: str) -> str:
    """done, progress, blocked, open or dropped for one status cell."""
    text = status.strip().lower().lstrip("*")
    if text.startswith(DROPPED_WORDS):
        return "dropped"
    if text.startswith(PROGRESS_WORDS) or "part 1 done" in text:
        return "progress"
    if text.startswith(DONE_WORDS):
        return "done"
    if text.startswith(BLOCKED_WORDS):
        return "blocked"
    return "open"


@dataclass
class Block:
    key: str
    name: str
    tasks: dict[str, str] = field(default_factory=dict)

    def count(self, state: str) -> int:
        return sum(1 for s in self.tasks.values() if s == state)

    @property
    def total(self) -> int:
        return sum(1 for s in self.tasks.values() if s != "dropped")


BLOCK_RE = re.compile(r"^## Block (\w+)\b")
ROW_RE = re.compile(r"^\| (T-\d+\.\d+) \|")
DONE_DATE_RE = re.compile(
    r"^(?:fertig|done|erledigt|b fertig)\D{0,12}(\d{2})\.(\d{2})\.(\d{4})", re.I
)


def read_blocks(text: str) -> tuple[list[Block], date | None]:
    """Blocks with task states, and the date of the latest finished task."""
    blocks: list[Block] = []
    latest: date | None = None
    for line in text.splitlines():
        if m := BLOCK_RE.match(line):
            key = m.group(1)
            blocks.append(Block(key=key, name=BLOCK_NAMES.get(key, f"Block {key}")))
            continue
        if line.startswith("## ") and not line.startswith("## Block"):
            # Sections after the blocks (order, definition of done) carry no tasks.
            if blocks:
                blocks.append(Block(key="", name=""))
            continue
        m = ROW_RE.match(line)
        if not m or not blocks or not blocks[-1].key:
            continue
        status = line.rstrip().rstrip("|").rsplit("|", 1)[-1].strip()
        blocks[-1].tasks[m.group(1)] = classify(status)
        if d := DONE_DATE_RE.match(status.lower()):
            day = date(int(d.group(3)), int(d.group(2)), int(d.group(1)))
            latest = day if latest is None or day > latest else latest
    return [b for b in blocks if b.key], latest


def node_state(tasks: list[str], states: dict[str, str]) -> str:
    known = [states[t] for t in tasks if t in states]
    if not tasks or not known:
        return "built" if not tasks else "planned"
    if all(s == "done" for s in known):
        return "built"
    if any(s in ("done", "progress") for s in known):
        return "progress"
    return "planned"


@dataclass
class Gate:
    key: str
    passed: bool
    content: str = ""


def read_status(text: str) -> tuple[str, list[Gate]]:
    """Stage line and the gates from docs/01_STATUS.md."""
    stage = "Stage ?"
    if m := re.search(r"·\s*(Stage [^·]+?)\s*·", text):
        stage = m.group(1).strip()
    gates: list[Gate] = []
    part = text.split("## Gates in Detail", 1)[-1]
    for line in part.splitlines()[:20]:
        if re.match(r"^\| (P|G\d) \|.*\|\s*$", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            passed = cells[-1].lower().startswith("passed")
            gates.append(Gate(cells[0], passed, cells[1] if len(cells) > 2 else ""))
    return stage, gates


def read_version(text: str) -> str:
    m = re.search(r"^\| Version \| ([^|]+) \|", text, re.M)
    return m.group(1).strip() if m else "0.0.0"


def count_tests(folder: Path = TESTS) -> int:
    pattern = re.compile(r"^\s*(?:async\s+)?def test_", re.M)
    return sum(
        len(pattern.findall(p.read_text(encoding="utf-8")))
        for p in folder.glob("test_*.py")
    )


@dataclass
class Facts:
    version: str
    stage: str
    gates: list[Gate]
    blocks: list[Block]
    progress_date: date | None
    tests: int
    runs: list[EvalRun]
    voice: list[VoiceRow]

    @property
    def states(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for block in self.blocks:
            out.update(block.tasks)
        return out

    def latest(self, suite: str, model: str | None = None) -> EvalRun | None:
        real = [
            r
            for r in self.runs
            if r.suite == suite and r.model != STAND_IN and model in (None, r.model)
        ]
        return real[-1] if real else None

    def stand_in(self, suite: str) -> EvalRun | None:
        ref = [r for r in self.runs if r.suite == suite and r.model == STAND_IN]
        return ref[-1] if ref else None


def gather() -> Facts:
    blocks, progress_date = read_blocks(WORKPACKAGES.read_text(encoding="utf-8"))
    stage, gates = read_status(STATUS.read_text(encoding="utf-8"))
    return Facts(
        version=read_version(README.read_text(encoding="utf-8")),
        stage=stage,
        gates=gates,
        blocks=blocks,
        progress_date=progress_date,
        tests=count_tests(),
        runs=read_eval_history(),
        voice=read_voice_history(),
    )


# -------------------------------------------------------------------------- drawing


def f(value: float) -> str:
    """Coordinates with at most one decimal, so the output stays stable."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def de(day: date | None) -> str:
    return day.isoformat() if day else "no date"


def pct(rate: float) -> str:
    return f"{rate * 100:.0f} %"


def thousands(value: int) -> str:
    return f"{value:,}"


def text(
    x: float,
    y: float,
    content: str,
    size: float = 16,
    fill: str = "ink",
    anchor: str = "start",
    weight: int = 400,
    spacing: float = 0,
    extra: str = "",
) -> str:
    color = TOKENS.get(fill, fill)
    attrs = f'x="{f(x)}" y="{f(y)}" font-size="{f(size)}" fill="{color}"'
    if anchor != "start":
        attrs += f' text-anchor="{anchor}"'
    if weight != 400:
        attrs += f' font-weight="{weight}"'
    if spacing:
        attrs += f' letter-spacing="{f(spacing)}"'
    if extra:
        attrs += " " + extra
    return f"<text {attrs}>{escape(content)}</text>"


def document(
    width: int, height: int, title: str, desc: str, body: str, css: str = ""
) -> str:
    style = f"text{{font-family:{FONT}}}" + css
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d">\n'
        f'<title id="t">{escape(title)}</title>\n<desc id="d">{escape(desc)}</desc>\n'
        f"<style>{style}</style>\n"
        "<defs>"
        # User space regions: a flat stroke (a crossbar) has a bounding box of almost
        # no height, and a region in percent of it would clip the glow.
        f'<filter id="glow" filterUnits="userSpaceOnUse" x="0" y="0" width="{width}" height="{height}">'
        '<feGaussianBlur stdDeviation="6"/></filter>'
        f'<filter id="soft" filterUnits="userSpaceOnUse" x="0" y="0" width="{width}" height="{height}">'
        '<feGaussianBlur stdDeviation="2.2"/></filter>'
        "</defs>\n"
        f'<rect width="{width}" height="{height}" rx="24" fill="{TOKENS["bg"]}"/>\n'
        f"{body}\n</svg>\n"
    )


def dot_grid(width: int, height: int, step: int = 28) -> str:
    """Faint dots behind the content, like a cutting mat for the drawing."""
    return (
        f'<pattern id="dots" width="{step}" height="{step}" patternUnits="userSpaceOnUse">'
        f'<circle cx="2" cy="2" r="1.2" fill="{TOKENS["grid"]}"/></pattern>'
        f'<rect width="{width}" height="{height}" rx="24" fill="url(#dots)" opacity="0.8"/>'
    )


def slip(x: float, y: float, w: float, h: float, header: str, date_label: str) -> str:
    """An order slip: the frame of every stat card. Perforated top edge."""
    parts = [
        f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="10" '
        f'fill="{TOKENS["surface"]}" stroke="{TOKENS["grid"]}"/>'
    ]
    holes = int((w - 24) // 18)
    for i in range(holes + 1):
        cx = x + 12 + i * (w - 24) / holes
        parts.append(
            f'<circle cx="{f(cx)}" cy="{f(y)}" r="4.5" fill="{TOKENS["bg"]}"/>'
        )
    parts.append(
        text(x + 24, y + 38, header.upper(), 14, "muted", spacing=2.4, weight=700)
    )
    parts.append(text(x + w - 24, y + 38, date_label, 14, "muted", anchor="end"))
    parts.append(
        f'<line x1="{f(x + 24)}" y1="{f(y + 54)}" x2="{f(x + w - 24)}" y2="{f(y + 54)}" '
        f'stroke="{TOKENS["grid"]}" stroke-dasharray="4 6"/>'
    )
    return "\n".join(parts)


def wave_points(
    x0: float, x1: float, y: float, amp: float, periods: float, steps: int = 48
) -> list[tuple[float, float]]:
    pts = []
    for i in range(steps + 1):
        t = i / steps
        # Envelope: quiet at both ends, loud in the middle, like a spoken word.
        env = math.sin(math.pi * t)
        pts.append(
            (x0 + (x1 - x0) * t, y - amp * env * math.sin(2 * math.pi * periods * t))
        )
    return pts


def path_of(points: list[tuple[float, float]]) -> str:
    head, *rest = points
    return f"M{f(head[0])} {f(head[1])}" + "".join(
        f" L{f(px)} {f(py)}" for px, py in rest
    )


# ----------------------------------------------------------------------------- logo
# Glyphs on a 100 unit cap height, as stroke paths: the logo is drawn, not typed,
# so it looks the same on every machine. The Æ is the centre: A and E share one
# stroke, the way the AI and the code share one call.


def glyph_m(
    x: float, y: float, s: float
) -> list[tuple[str, list[tuple[float, float]]]]:
    pts = [(0, 100), (0, 0), (36, 60), (72, 0), (72, 100)]
    return [("a", [(x + px * s, y + py * s) for px, py in pts])]


def glyph_ae(
    x: float, y: float, s: float, wave: bool
) -> list[tuple[str, list[tuple[float, float]]]]:
    def p(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x + px * s, y + py * s) for px, py in points]

    bar: list[tuple[float, float]] = (
        wave_points(20, 86, 54, 7, 1.5, 36) if wave else [(21, 52), (86, 52)]
    )
    return [
        ("a", p([(0, 100), (44, 0)])),
        ("e", p([(44, 0), (98, 0)])),
        ("e", p([(44, 0), (44, 100), (98, 100)])),
        ("bar", p(bar)),
    ]


def glyph_x(
    x: float, y: float, s: float
) -> list[tuple[str, list[tuple[float, float]]]]:
    return [
        ("a", [(x, y), (x + 72 * s, y + 100 * s)]),
        ("b", [(x + 72 * s, y), (x, y + 100 * s)]),
    ]


def neon(points: list[tuple[float, float]], width: float, color: str = "neon") -> str:
    """A neon tube: blurred glow, the tube, and a pale core."""
    d = path_of(points)
    base = TOKENS[color]
    core = TOKENS["neon_core" if color == "neon" else "violet_core"]
    common = 'fill="none" stroke-linecap="round" stroke-linejoin="round"'
    return (
        f'<path d="{d}" {common} stroke="{base}" stroke-width="{f(width * 2.2)}" opacity="0.55" filter="url(#glow)"/>'
        f'<path d="{d}" {common} stroke="{base}" stroke-width="{f(width)}"/>'
        f'<path d="{d}" {common} stroke="{core}" stroke-width="{f(width * 0.32)}" opacity="0.9"/>'
    )


def wordmark(
    x: float, y: float, height: float, style: str = LOGO_STYLE
) -> tuple[str, float]:
    """MÆX at (x, y) top left with a cap height; returns the SVG and the width."""
    s = height / 100
    width = max(4.0, 11 * s)
    gap = 34 * s
    wave = style in ("wave", "split", "stamp")
    strokes: list[tuple[str, list[tuple[float, float]]]] = []
    strokes += glyph_m(x, y, s)
    ae_x = x + 72 * s + gap
    strokes += glyph_ae(ae_x, y, s, wave)
    x_x = ae_x + 98 * s + gap
    strokes += glyph_x(x_x, y, s)
    total = x_x + 72 * s - x
    parts = []
    for part, pts in strokes:
        color = "neon"
        if style == "split" and part in ("e",):
            color = "violet"
        parts.append(neon(pts, width, color))
    body = "".join(parts)
    if style == "stamp":
        pad = 26 * s
        frame = (
            f'<rect x="{f(x - pad)}" y="{f(y - pad)}" width="{f(total + 2 * pad)}" '
            f'height="{f(100 * s + 2 * pad)}" rx="{f(14 * s)}" fill="none" '
            f'stroke="{TOKENS["neon"]}" stroke-width="{f(width * 0.5)}" stroke-dasharray="{f(10 * s)} {f(7 * s)}"/>'
        )
        body = frame + body
    return body, total


def monogram(x: float, y: float, size: float, style: str = LOGO_STYLE) -> str:
    """The Æ alone in a rounded square: the icon and avatar."""
    s = size * 0.0058
    gw, gh = 98 * s, 100 * s
    gx, gy = x + (size - gw) / 2, y + (size - gh) / 2
    width = max(4.0, 11 * s)
    parts = [
        f'<rect x="{f(x)}" y="{f(y)}" width="{f(size)}" height="{f(size)}" rx="{f(size * 0.22)}" '
        f'fill="{TOKENS["surface"]}" stroke="{TOKENS["grid"]}" stroke-width="{f(size * 0.008)}"/>'
    ]
    for part, pts in glyph_ae(gx, gy, s, wave=style != "neon"):
        color = "violet" if style == "split" and part == "e" else "neon"
        parts.append(neon(pts, width, color))
    return "".join(parts)


def asset_logo_mark(style: str = LOGO_STYLE) -> str:
    size = 512
    return document(
        size,
        size,
        "MÆX logo mark",
        "The ligature Æ as a neon tube in a rounded square: the A and the E share one stroke, "
        "the crossbar is a voice waveform.",
        monogram(36, 36, size - 72, style),
    ).replace(f'rx="24" fill="{TOKENS["bg"]}"', f'rx="112" fill="{TOKENS["bg"]}"', 1)


def asset_logo_wordmark(style: str = LOGO_STYLE) -> str:
    _, w = wordmark(0, 0, 120, style)
    width, height = int(w + 160), 280
    body, _ = wordmark(80, 80, 120, style)
    return document(
        width,
        height,
        "MÆX wordmark",
        "The letters M, Æ and X drawn as neon tubes in Neon Yellow and Electric Violet on midnight purple.",
        body,
    )


def concept_sheet() -> str:
    """All logo concepts side by side, for choosing one. Not a README asset."""
    width, height = 1200, 900
    parts = [dot_grid(width, height)]
    for i, style in enumerate(LOGO_STYLES):
        col, row = i % 2, i // 2
        ox, oy = 40 + col * 580, 40 + row * 430
        parts.append(
            f'<rect x="{ox}" y="{oy}" width="540" height="400" rx="16" fill="{TOKENS["surface"]}" '
            f'stroke="{TOKENS["neon"] if style == LOGO_STYLE else TOKENS["grid"]}"/>'
        )
        parts.append(
            text(
                ox + 24,
                oy + 40,
                f"{i + 1}  {style.upper()}",
                15,
                "muted",
                spacing=2,
                weight=700,
            )
        )
        mark, _ = wordmark(ox + 60, oy + 90, 70, style)
        parts.append(mark)
        parts.append(monogram(ox + 210, oy + 210, 160, style))
    return document(
        width, height, "MÆX logo concepts", "Four logo concepts.", "\n".join(parts)
    )


# --------------------------------------------------------------------------- banner


# One example call, the same in the banner, the call flow and the demo: the
# recorded replay of scripts/demo/pickup_call.json against the test menu
# (evals/menu/). Every sentence is quoted as the replay has it, only wrapped into
# lines or cut with an ellipsis, never reworded; the caller's lines are ASCII as
# in the call file, the agent's carry the product's umlauts.
EXAMPLE_SAID = [
    "„Guten Tag, ich moechte etwas",
    "zum Abholen bestellen.“",
    "„Zweimal die 25.“",
]
EXAMPLE_SLIP = ("Pickup", "in 30 min", "2 x  No. 25", "Papaya-Salat")
EXAMPLE_CALL: dict[str, list[str]] = {
    "order": EXAMPLE_SAID,
    "name": ["„Auf den Namen Mueller.“"],
    "readback": [
        "„Zweimal Nummer 25 Papaya-Salat.",
        "Macht 15 Euro 80, abholbereit in",
        "etwa 30 Minuten, auf den Namen",
        "Mueller. Passt das so?“",
    ],
    "yes": ["„Ja, passt so.“"],
    "confirmed": ["„Ihre Bestellung auf den Namen", "Mueller ist aufgegeben. …“"],
    # Not in the replay: the caller's sentence of the eval case
    # evals/cases/eskalation_0001_beschwerde.json, which escalates.
    "complaint": ["„Ich moechte mich beschweren,", "die letzte Lieferung war kalt.“"],
}


def asset_banner() -> str:
    """One call from left to right: a spoken sentence goes in, a confirmed slip
    comes out. No measured number stands here, so the banner never goes stale;
    stage and eval result are badges below it."""
    width, height = 1200, 440
    glow = (
        '<radialGradient id="g1" cx="0.15" cy="0.6" r="0.5">'
        f'<stop offset="0" stop-color="{TOKENS["neon"]}" stop-opacity="0.13"/>'
        f'<stop offset="1" stop-color="{TOKENS["neon"]}" stop-opacity="0"/></radialGradient>'
        '<radialGradient id="g2" cx="0.88" cy="0.55" r="0.55">'
        f'<stop offset="0" stop-color="{TOKENS["violet"]}" stop-opacity="0.22"/>'
        f'<stop offset="1" stop-color="{TOKENS["violet"]}" stop-opacity="0"/></radialGradient>'
    )
    parts = [
        f"<defs>{glow}</defs>",
        dot_grid(width, height),
        f'<rect width="{width}" height="{height}" rx="24" fill="url(#g1)"/>',
        f'<rect width="{width}" height="{height}" rx="24" fill="url(#g2)"/>',
        text(64, 70, "Maex Voice Agent", 24, "ink", weight=700),
        text(
            64,
            100,
            "An AI phone agent for restaurants: reservations, pickups, deliveries.",
            15,
            "muted",
        ),
        text(64, 152, "the caller says", 14, "muted"),
    ]
    # Bottom-aligned above the wave, however many lines the example has.
    first = 236 - (len(EXAMPLE_SAID) - 1) * 26
    for i, line in enumerate(EXAMPLE_SAID):
        parts.append(text(64, first + i * 26, line, 19, "ink"))
    # The voice runs into the sign: the wave ends where the logo's own crossbar,
    # a waveform too, takes over.
    parts.append(neon(wave_points(64, 436, 290, 30, 7, 140), 4.5, "neon"))
    _, logo_width = wordmark(0, 0, 96)
    mark, _ = wordmark(600 - logo_width / 2, 242, 96)
    parts.append(mark)
    parts.append(
        text(
            600,
            384,
            "The AI understands. The code decides.",
            15,
            "neon",
            anchor="middle",
        )
    )
    parts.append(
        f'<path class="wire" d="M764 290 L806 290" fill="none" stroke="{TOKENS["violet"]}" '
        'stroke-width="2.5" stroke-dasharray="5 5"/>'
        f'<path d="M800 282 L812 290 L800 298" fill="none" stroke="{TOKENS["violet"]}" '
        'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
    )
    sx, sy, sw, sh = 826, 140, 314, 268
    header, when, item, dish = EXAMPLE_SLIP
    parts.append(slip(sx, sy, sw, sh, header, when))
    parts.append(text(sx + 24, sy + 90, item, 18, "ink", weight=700))
    parts.append(text(sx + 24, sy + 114, dish, 15, "muted"))
    rule = (
        '<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" '
        f'stroke="{TOKENS["grid"]}" stroke-dasharray="4 6"/>'
    )
    parts.append(rule.format(x1=sx + 24, x2=sx + sw - 24, y=sy + 134))
    for i, (label, value) in enumerate(
        [("read back", "heard"), ("caller's yes", "given")]
    ):
        yy = sy + 164 + i * 28
        parts.append(text(sx + 24, yy, label, 15, "muted"))
        parts.append(text(sx + sw - 24, yy, value, 15, "ink", anchor="end"))
    parts.append(rule.format(x1=sx + 24, x2=sx + sw - 24, y=sy + 208))
    parts.append(text(sx + 24, sy + 242, "order", 15, "muted"))
    parts.append(
        text(sx + sw - 24, sy + 244, "confirmed", 21, "neon", anchor="end", weight=700)
    )
    css = (
        ".wire{animation:flow 1.2s linear infinite}"
        "@keyframes flow{to{stroke-dashoffset:-20}}"
        "@media (prefers-reduced-motion:reduce){.wire{animation:none}}"
    )
    desc = (
        "MÆX, Maex Voice Agent, an AI phone agent for restaurants. An example call "
        "from left to right: the caller orders dish 25 twice for pickup, the neon "
        "sign MÆX stands in the middle, and on the right lies the order slip, ready "
        "in about 30 minutes, read back, confirmed by the caller's yes."
    )
    return document(width, height, "MÆX banner", desc, "\n".join(parts), css)


# ------------------------------------------------------------------------ eval charts

SEGMENTS = [
    ("passed", "neon", "passed"),
    ("pending", "violet", "known gap"),
    ("crashed", "coral", "crashed"),
    ("red", "plum", "not passed"),
]


def split_run(run: EvalRun) -> dict[str, int]:
    crashed, pending = run.crashed or 0, run.pending or 0
    return {
        "passed": run.passed,
        "pending": pending,
        "crashed": crashed,
        "red": max(0, run.total - run.passed - crashed - pending),
    }


def stacked_bar(
    x: float, y: float, w: float, h: float, counts: dict[str, int], total: int
) -> str:
    parts = [
        f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="4" fill="{TOKENS["bg"]}"/>'
    ]
    cursor = x
    shown = [(k, c) for k, c, _ in SEGMENTS if counts.get(k)]
    for key, color in shown:
        seg = w * counts[key] / total if total else 0
        # A 2 px surface gap between fills keeps segments apart without colour.
        parts.append(
            f'<rect x="{f(cursor)}" y="{f(y)}" width="{f(max(seg - 2, 1))}" height="{f(h)}" rx="4" fill="{TOKENS[color]}"/>'
        )
        cursor += seg
    return "\n".join(parts)


def asset_eval_latest(facts: Facts) -> str:
    width, height = 1200, 460
    # One card, one model: the targets of the same model as the regular cases, and
    # only when that model has none, the latest targets with their model named.
    cases = facts.latest("cases")
    targets = facts.latest("targets", cases.model if cases else None)
    if targets is None:
        targets = facts.latest("targets")
    head = cases or targets
    latest = max((r for r in (cases, targets) if r), key=lambda r: r.date, default=None)
    parts = [dot_grid(width, height)]
    parts.append(
        slip(
            40,
            40,
            1120,
            380,
            "Eval slip · real model",
            de(latest.date if latest else None),
        )
    )
    if latest is None:
        parts.append(text(64, 140, "No real-model run recorded yet.", 20, "muted"))
        return document(
            width,
            height,
            "Latest eval run",
            "No real-model run recorded yet.",
            "\n".join(parts),
        )
    parts.append(
        text(64, 124, head.model if head else latest.model, 20, "ink", weight=700)
    )
    parts.append(
        text(1136, 124, "temperature 0, invented cases only", 15, "muted", anchor="end")
    )
    rows = [("Regular cases", cases), ("Target cases", targets)]
    for i, (label, run) in enumerate(rows):
        yy = 168 + i * 78
        if run is None:
            parts.append(text(64, yy + 18, f"{label}: not run", 16, "muted"))
            continue
        parts.append(text(64, yy, label, 15, "muted"))
        parts.append(
            text(64, yy + 30, f"{run.passed}/{run.total}", 26, "neon", weight=700)
        )
        parts.append(
            text(
                64 + 26 * 0.6 * len(f"{run.passed}/{run.total}") + 12,
                yy + 30,
                pct(run.rate),
                16,
                "ink",
            )
        )
        parts.append(stacked_bar(300, yy + 8, 836, 26, split_run(run), run.total))
        when = (
            de(run.date)
            if head and run.model == head.model
            else f"{run.model} · {de(run.date)}"
        )
        parts.append(text(1136, yy, when, 14, "muted", anchor="end"))
    # Legend, always present: identity is never colour alone.
    lx = 300
    for _key, color, label in SEGMENTS:
        parts.append(
            f'<rect x="{lx}" y="318" width="14" height="14" rx="3" fill="{TOKENS[color]}"/>'
        )
        parts.append(text(lx + 22, 330, label, 15, "muted"))
        lx += 34 + 9 * len(label)
    if cases and cases.crashed is None:
        parts.append(
            text(
                1136,
                330,
                "crashed cases not recorded for this run",
                14,
                "muted",
                anchor="end",
            )
        )
    # Line items of the regular cases.
    run = cases or targets
    assert run is not None
    hard = run.hard
    items = [
        (
            "hard violations",
            "not recorded" if hard is None else str(hard),
            hard not in (None, 0),
        ),
        (
            "missed escalations",
            "-" if run.missed_esc is None else str(run.missed_esc),
            bool(run.missed_esc),
        ),
        (
            "false escalation",
            "-" if run.false_esc_rate is None else f"{run.false_esc_rate * 100:.1f} %",
            bool(run.false_esc_rate and run.false_esc_rate > 0.05),
        ),
        (
            "tokens per case",
            "-" if run.tokens_per_case is None else thousands(run.tokens_per_case),
            False,
        ),
    ]
    for i, (label, value, bad) in enumerate(items):
        x = 64 + i * 272
        parts.append(text(x, 376, label, 14, "muted"))
        parts.append(text(x, 404, value, 20, "coral" if bad else "ink", weight=700))
    desc = (
        f"Latest real-model eval on {de(latest.date)}, model {head.model if head else latest.model}. "
        + (f"Regular cases {cases.passed} of {cases.total}. " if cases else "")
        + (
            f"Target cases {targets.passed} of {targets.total}"
            + (
                f", from model {targets.model} on {de(targets.date)}. "
                if head and targets.model != head.model
                else ". "
            )
            if targets
            else ""
        )
        + (
            f"False escalation {run.false_esc_rate * 100:.1f} percent, limit 5 percent."
            if run.false_esc_rate is not None
            else ""
        )
    )
    return document(width, height, "Latest eval run", desc, "\n".join(parts))


def asset_eval_trend(facts: Facts) -> str:
    width, height = 1200, 440
    px0, px1, py0, py1 = 120, 900, 104, 330
    # One model per chart: a line through two models would show a model change as
    # progress over time. The model is the one of the latest regular run.
    head = facts.latest("cases") or facts.latest("targets")
    model = head.model if head else ""
    parts = [dot_grid(width, height)]
    parts.append(
        slip(40, 40, 1120, 370, f"Pass rate over time · {model or 'real model'}", "")
    )
    series = {
        suite: [r for r in facts.runs if r.suite == suite and r.model == model]
        for suite in ("cases", "targets")
    }
    dates = sorted({r.date for s in series.values() for r in s})
    if not dates:
        parts.append(text(64, 140, "No real-model run recorded yet.", 20, "muted"))
        return document(
            width,
            height,
            "Eval trend",
            "No real-model run recorded yet.",
            "\n".join(parts),
        )
    # X: one slot per run, runs of the same day side by side under one date.
    per_day: dict[date, int] = {}
    for items in series.values():
        counted: dict[date, int] = {}
        for r in items:
            counted[r.date] = counted.get(r.date, 0) + 1
        for d, n in counted.items():
            per_day[d] = max(per_day.get(d, 0), n)
    slots: list[date] = []
    for d in dates:
        slots += [d] * per_day[d]
    step = (px1 - px0) / max(len(slots) - 1, 1)

    def sx(day: date, k: int) -> float:
        return px0 + (slots.index(day) + k) * step

    def sy(rate: float) -> float:
        return py1 - (py1 - py0) * rate

    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        y = sy(tick)
        parts.append(
            f'<line x1="{px0}" y1="{f(y)}" x2="{px1}" y2="{f(y)}" stroke="{TOKENS["grid"]}"/>'
        )
        parts.append(
            text(px0 - 14, y + 5, f"{tick * 100:.0f} %", 14, "muted", anchor="end")
        )
    # The target of docs/08: accuracy of at least 99 percent.
    ty = sy(0.99)
    parts.append(
        f'<line x1="{px0}" y1="{f(ty)}" x2="{px1}" y2="{f(ty)}" stroke="{TOKENS["neon"]}" stroke-dasharray="6 6" opacity="0.6"/>'
    )
    parts.append(text(px1 + 12, ty + 5, "target 99 %", 14, "neon"))
    for d in dates:
        parts.append(
            text(
                sx(d, 0) + step * (per_day[d] - 1) / 2,
                py1 + 28,
                d.strftime("%d %b"),
                14,
                "muted",
                anchor="middle",
            )
        )
    names = {"cases": ("regular cases", "neon"), "targets": ("target cases", "violet")}
    for suite, items in series.items():
        if not items:
            continue
        label, color = names[suite]
        seen: dict[date, int] = {}
        pts = []
        for r in items:
            k = seen.get(r.date, 0)
            seen[r.date] = k + 1
            pts.append((sx(r.date, k), sy(r.rate), r))
        parts.append(
            f'<path d="{path_of([(x, y) for x, y, _ in pts])}" fill="none" stroke="{TOKENS[color]}" stroke-width="2" stroke-linejoin="round"/>'
        )
        for x, y, r in pts:
            parts.append(
                f'<circle cx="{f(x)}" cy="{f(y)}" r="5" fill="{TOKENS[color]}" stroke="{TOKENS["surface"]}" stroke-width="2">'
                f"<title>{escape(f'{label} {de(r.date)}: {r.passed}/{r.total}')}</title></circle>"
            )
        lx, ly, last = pts[-1]
        parts.append(
            text(
                lx + 12,
                ly - 10,
                f"{label} {last.passed}/{last.total}",
                15,
                color,
                weight=700,
            )
        )
    ref = facts.stand_in("cases")
    note = (
        f"Scripted stand-in model: {ref.passed}/{ref.total} regular cases."
        if ref
        else ""
    )
    parts.append(text(1136, 73, note, 14, "muted", anchor="end"))
    desc = (
        f"Pass rate of model {model} from {de(dates[0])} to {de(dates[-1])}. "
        + "; ".join(
            f"{names[s][0]} latest {items[-1].passed} of {items[-1].total}"
            for s, items in series.items()
            if items
        )
    )
    return document(width, height, "Eval trend", desc, "\n".join(parts))


# ------------------------------------------------------------------------- progress


def asset_progress(facts: Facts) -> str:
    blocks = facts.blocks
    width = 1200
    height = 400 + len(blocks) * 34
    done = sum(b.count("done") for b in blocks)
    total = sum(b.total for b in blocks)
    prog = sum(b.count("progress") for b in blocks)
    blocked = sum(b.count("blocked") for b in blocks)
    parts = [dot_grid(width, height)]
    parts.append(
        slip(40, 40, 1120, height - 80, "Build progress", de(facts.progress_date))
    )
    parts.append(text(64, 132, f"{done}/{total}", 40, "neon", weight=700))
    parts.append(
        text(
            64 + 40 * 0.6 * len(f"{done}/{total}") + 16,
            132,
            f"tasks done · {pct(done / total if total else 0)}",
            18,
            "ink",
        )
    )
    parts.append(
        text(
            1136,
            132,
            f"{prog} in progress · {blocked} blocked · {total - done - prog - blocked} open",
            15,
            "muted",
            anchor="end",
        )
    )

    def bar(
        x: float,
        y: float,
        w: float,
        h: float,
        b_done: int,
        b_prog: int,
        b_blocked: int,
        b_total: int,
    ) -> str:
        out = [
            f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="{f(h / 2)}" fill="{TOKENS["bg"]}"/>'
        ]
        cursor = x
        for n, color in ((b_done, "neon"), (b_prog, "violet"), (b_blocked, "coral")):
            if not n or not b_total:
                continue
            seg = w * n / b_total
            out.append(
                f'<rect x="{f(cursor)}" y="{f(y)}" width="{f(max(seg - 2, 2))}" height="{f(h)}" rx="{f(h / 2)}" fill="{TOKENS[color]}"/>'
            )
            cursor += seg
        return "".join(out)

    parts.append(bar(64, 152, 1072, 18, done, prog, blocked, total))
    top = 216
    for i, b in enumerate(blocks):
        y = top + i * 34
        parts.append(text(64, y + 13, b.key, 15, "muted", weight=700))
        parts.append(text(110, y + 13, b.name, 15, "ink"))
        parts.append(
            bar(
                330,
                y + 2,
                700,
                12,
                b.count("done"),
                b.count("progress"),
                b.count("blocked"),
                b.total,
            )
        )
        parts.append(
            text(1136, y + 13, f"{b.count('done')}/{b.total}", 15, "ink", anchor="end")
        )
    # Gates as stations on the line from the plan to live operation.
    gy = top + len(blocks) * 34 + 56
    gates = facts.gates
    if gates:
        gx0, gx1 = 170, 1030
        gstep = (gx1 - gx0) / max(len(gates) - 1, 1)
        line = wave_points(gx0, gx1, gy, 5, len(gates) * 1.5, 120)
        parts.append(
            f'<path d="{path_of(line)}" fill="none" stroke="{TOKENS["grid"]}" stroke-width="2"/>'
        )
        upto = max((i for i, g in enumerate(gates) if g.passed), default=-1)
        if upto > 0:
            lit = wave_points(gx0, gx0 + gstep * upto, gy, 5, upto * 1.5, 60)
            parts.append(
                f'<path d="{path_of(lit)}" fill="none" stroke="{TOKENS["neon"]}" stroke-width="2"/>'
            )
        nxt = next((i for i, g in enumerate(gates) if not g.passed), None)
        for i, g in enumerate(gates):
            cx = gx0 + i * gstep
            if g.passed:
                fill, stroke, label = TOKENS["neon"], TOKENS["neon"], "passed"
            elif i == nxt:
                fill, stroke, label = TOKENS["surface"], TOKENS["violet"], "next"
            else:
                fill, stroke, label = TOKENS["surface"], TOKENS["plum"], "open"
            parts.append(
                f'<circle cx="{f(cx)}" cy="{gy}" r="9" fill="{fill}" stroke="{stroke}" stroke-width="3"/>'
            )
            parts.append(
                text(cx, gy - 18, g.key, 15, "ink", anchor="middle", weight=700)
            )
            parts.append(text(cx, gy + 30, label, 13, "muted", anchor="middle"))
        parts.append(text(64, gy + 5, "gates", 14, "muted"))
    legend = [
        ("done", "neon"),
        ("in progress", "violet"),
        ("blocked", "coral"),
        ("open", "bg"),
    ]
    lx = 330
    ly = height - 66
    for label, color in legend:
        stroke = f' stroke="{TOKENS["plum"]}"' if color == "bg" else ""
        parts.append(
            f'<rect x="{lx}" y="{ly - 11}" width="14" height="14" rx="7" fill="{TOKENS[color]}"{stroke}/>'
        )
        parts.append(text(lx + 22, ly, label, 14, "muted"))
        lx += 44 + 8 * len(label)
    desc = (
        f"{done} of {total} tasks done, {prog} in progress, {blocked} blocked, as of {de(facts.progress_date)}. "
        + "; ".join(
            f"block {b.key} {b.name} {b.count('done')} of {b.total}" for b in blocks
        )
        + ". Gates: "
        + ", ".join(f"{g.key} {'passed' if g.passed else 'open'}" for g in gates)
        + "."
    )
    return document(width, height, "Build progress", desc, "\n".join(parts))


# ------------------------------------------------------------------------ call flow

# A node's edge, and a rail chip's, shows its build state; the dashes keep the
# state readable without its colour.
STATE_STROKE = {
    "built": ("neon", ""),
    "progress": ("violet", ' stroke-dasharray="2 3"'),
    "planned": ("plum", ' stroke-dasharray="6 4"'),
}
STATE_NAMES = {"built": "built", "progress": "in progress", "planned": "planned"}


def _legend(right: float, y: float) -> list[str]:
    """The three build states, right-aligned at `right`."""
    widths = [44 + 8 * len(name) for name in STATE_NAMES.values()]
    x = right - sum(widths) + 8
    parts = []
    for (state, name), w in zip(STATE_NAMES.items(), widths, strict=True):
        color, dash = STATE_STROKE[state]
        parts.append(
            f'<rect x="{f(x)}" y="{f(y - 12)}" width="22" height="16" rx="5" '
            f'fill="{TOKENS["surface_hi"]}" stroke="{TOKENS[color]}" stroke-width="2"{dash}/>'
        )
        parts.append(text(x + 30, y + 1, name, 13, "muted"))
        x += w
    return parts


def _person(cx: float, cy: float, color: str) -> str:
    c = TOKENS[color]
    return (
        f'<circle cx="{f(cx)}" cy="{f(cy - 10)}" r="10" fill="none" stroke="{c}" stroke-width="3"/>'
        f'<path d="M{f(cx - 18)} {f(cy + 21)} Q{f(cx - 18)} {f(cy + 3)} {f(cx)} {f(cy + 3)} '
        f'Q{f(cx + 18)} {f(cy + 3)} {f(cx + 18)} {f(cy + 21)}" fill="none" stroke="{c}" stroke-width="3" stroke-linecap="round"/>'
    )


def _rounded(points: list[tuple[float, float]], r: float = 12) -> str:
    """Path through points with rounded corners."""
    if len(points) < 3:
        return path_of(points)
    out = f"M{f(points[0][0])} {f(points[0][1])}"
    for i in range(1, len(points) - 1):
        (x0, y0), (x1, y1), (x2, y2) = points[i - 1], points[i], points[i + 1]
        d1 = math.hypot(x1 - x0, y1 - y0)
        d2 = math.hypot(x2 - x1, y2 - y1)
        if d1 < 1 or d2 < 1:
            continue
        rr = min(r, d1 / 2, d2 / 2)
        p1 = (x1 - (x1 - x0) / d1 * rr, y1 - (y1 - y0) / d1 * rr)
        p2 = (x1 + (x2 - x1) / d2 * rr, y1 + (y2 - y1) / d2 * rr)
        out += f" L{f(p1[0])} {f(p1[1])} Q{f(x1)} {f(y1)} {f(p2[0])} {f(p2[1])}"
    out += f" L{f(points[-1][0])} {f(points[-1][1])}"
    return out


def _cylinder(x: float, y: float, w: float, h: float, stroke: str) -> str:
    """A database drum: top ellipse, body, front of the bottom ellipse."""
    rx, ry = w / 2, 10
    cx = x + w / 2
    body = (
        f'<path d="M{f(x)} {f(y + ry)} V{f(y + h - ry)} '
        f'A{f(rx)} {f(ry)} 0 0 0 {f(x + w)} {f(y + h - ry)} V{f(y + ry)}" '
        f'fill="{TOKENS["surface_hi"]}" stroke="{stroke}" stroke-width="2"/>'
    )
    top = (
        f'<ellipse cx="{f(cx)}" cy="{f(y + ry)}" rx="{f(rx)}" ry="{ry}" '
        f'fill="{TOKENS["surface_hi"]}" stroke="{stroke}" stroke-width="2"/>'
    )
    return body + top


def _arrow(x1: float, x2: float, y: float, color: str, dash: str = "") -> str:
    """A horizontal arrow from x1 to x2, its head at x2."""
    c, s = TOKENS[color], 1 if x2 > x1 else -1
    return (
        f'<path d="M{f(x1)} {f(y)} H{f(x2)}" fill="none" stroke="{c}" stroke-width="1.8"{dash}/>'
        f'<path d="M{f(x2 - s * 6)} {f(y - 4.5)} L{f(x2)} {f(y)} L{f(x2 - s * 6)} {f(y + 4.5)}" '
        f'fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
    )


# The station rail of the call flow: the nodes a sentence passes on its way in
# (the caller's words) and out (the agent's reply). The core is the station next
# to the code column: it runs every tool. The LLM is no station on the way; where
# the core asks it, it hangs above the core as a spur, joined to the core only.
# A station keeps its slot from row to row, so ear and mouth share one.
FLOW_RAIL_IN = ["access", "asterisk", "ear", "core"]
FLOW_RAIL_OUT = ["core", "mouth", "asterisk", "access"]
RAIL_SLOT = {"access": 0, "asterisk": 1, "ear": 2, "mouth": 2, "core": 3}
RAIL_WIDTH = (60, 74, 54, 48)
RAIL_X, RAIL_GAP = 408.0, 36.0
LLM_W = 42.0
BUBBLE_X, BUBBLE_W = 40.0, 330.0
CODE_X, CODE_W = 776.0, 384.0
ROW_COLOR = {"caller": "neon", "agent": "violet", "outage": "coral"}


def _slot(key: str) -> tuple[float, float]:
    """Left edge and width of a station's chip."""
    i = RAIL_SLOT[key]
    return RAIL_X + sum(RAIL_WIDTH[:i]) + i * RAIL_GAP, RAIL_WIDTH[i]


@dataclass(frozen=True)
class CallRow:
    """One row of the call flow: who speaks and what, the stations the sentence
    passes, whether the core asks the LLM on the way, the tools the code runs
    and what they establish."""

    who: str
    said: list[str]
    rail: list[str]
    tools: list[str]
    does: list[str]
    llm: bool = False
    tone: str = ""
    apart: bool = False


# The pickup call of EXAMPLE_CALL as the code runs it (api/agent/loop.py,
# api/agent/guards.py, api/domain/ordering/confirm.py, docs/02 §3 and §5).
FLOW_ROWS: list[CallRow] = [
    CallRow(
        "caller",
        EXAMPLE_CALL["order"],
        FLOW_RAIL_IN,
        ["get_service_status, search_menu"],
        # loop.MAX_TOOL_HOPS; the LLM's only link is the core (FLOW_LINKS).
        [
            "one tool per hop, at most six; the core runs",
            "each against Postgres. The LLM has no line",
            "to a tool or the database.",
        ],
        llm=True,
    ),
    CallRow(
        "caller",
        EXAMPLE_CALL["name"],
        FLOW_RAIL_IN,
        ["draft_order"],
        # guards.py rule 2 for the guest; prices only from the database (rule 1).
        [
            "the name as the caller said it; every",
            "price from Postgres, never from the model",
        ],
        llm=True,
    ),
    CallRow(
        "agent",
        EXAMPLE_CALL["readback"],
        FLOW_RAIL_OUT,
        ["readback of draft_order"],
        # The model speaks it; guards.py checks it was the draft's own sentence
        # (`note_said`, `read_out`) and refuses confirm otherwise (HINT_CONFIRM).
        [
            "the LLM must read it out word for word;",
            "the core checks that the caller heard it",
        ],
        llm=True,
    ),
    CallRow(
        "caller",
        EXAMPLE_CALL["yes"],
        FLOW_RAIL_IN,
        ["confirm"],
        # guards.py rule 3: confirm only on an explicit yes to the read-back.
        ["only now, on a clear yes to the read-back:", "the draft becomes confirmed"],
        llm=True,
    ),
    CallRow(
        "agent",
        EXAMPLE_CALL["confirmed"],
        FLOW_RAIL_OUT,
        ["confirm result"],
        # loop.py: "Booked: the core says so itself" (`_booked_say`, no model).
        # "aufgegeben" is SAY_ORDER_PLACED, said when the slip is not held back;
        # a held one is "aufgenommen" (confirm.py `_waits_for`). The tablet
        # reads Postgres and does not wait for the spoken reply.
        [
            "the core says the booking itself from the",
            "result, not the model; meanwhile the",
            "tablet shows the order. The slip waits for",
            "the team's approval; primary mode: at once,",
            "up to the amount limit, as in this call",
        ],
    ),
    CallRow(
        "caller",
        EXAMPLE_CALL["complaint"],
        FLOW_RAIL_IN,
        ["transfer_to_team"],
        # loop.py: escalation.check() reads the complaint before the model;
        # `_handoff` transfers, transfer.py counts the team as reachable while a
        # service is open; a complaint a rule covers gets its remedy (D19).
        [
            "checked before the model; a complaint no",
            "rule covers, unsure or a model failure:",
            "the team phone rings while open; after",
            "hours, number known: a callback on the tablet",
        ],
        tone="violet",
        apart=True,
    ),
    CallRow(
        "outage",
        ["the service voice fails"],
        ["access", "asterisk"],
        ["Asterisk dialplan"],
        # deploy/asterisk/conf/extensions.conf: `voice` unreached dials TEAM_PHONE.
        ["the exchange rings the team phone", "(extension unreached, TEAM_PHONE)"],
        tone="coral",
    ),
]
FLOW_STILL = (
    "What is said, the stations it passes, what the code does. "
    "Nothing is confirmed before the yes."
)
# Reveal timing in seconds: the rows appear quickly, one after the other, and the
# finished picture then holds at least as long as the reveal took.
REVEAL_FADE = 0.3
REVEAL_OUT = 0.4
REVEAL_STEP = {"first": 0.3, "chip": 0.12, "spur": 0.15, "last": 0.45}


def _row_height(row: CallRow) -> float:
    bubble = 46 + 19 * len(row.said)
    code = 32 + 18 * len(row.tools) + 17 * len(row.does)
    return max(bubble, code)


def _bubble(row: CallRow, y: float, h: float) -> str:
    color = ROW_COLOR[row.who]
    x = BUBBLE_X + (24 if row.who == "agent" else 0)
    dash = ' stroke-dasharray="5 4"' if row.who == "outage" else ""
    top = y + h / 2 - (18 + 19 * len(row.said)) / 2
    out = (
        f'<rect x="{f(x)}" y="{f(y)}" width="{f(BUBBLE_W)}" height="{f(h)}" rx="14" '
        f'fill="{TOKENS["surface"]}" stroke="{TOKENS[color]}" stroke-width="1.6"{dash}/>'
    )
    out += text(x + 18, top + 12, row.who, 12, color, weight=700)
    for i, line in enumerate(row.said):
        out += text(x + 18, top + 33 + 19 * i, line, 14, "ink")
    return out


def _code_box(row: CallRow, y: float, h: float) -> str:
    stroke = TOKENS[row.tone] if row.tone else TOKENS["grid"]
    top = y + h / 2 - (4 + 18 * len(row.tools) + 17 * len(row.does)) / 2
    out = (
        f'<rect x="{f(CODE_X)}" y="{f(y)}" width="{f(CODE_W)}" height="{f(h)}" rx="10" '
        f'fill="{TOKENS["surface"]}" stroke="{stroke}" stroke-width="1.6"/>'
    )
    for i, line in enumerate(row.tools):
        out += text(
            CODE_X + 20, top + 13 + 18 * i, line, 14, row.tone or "neon", weight=700
        )
    first = top + 13 + 18 * (len(row.tools) - 1) + 22
    for i, line in enumerate(row.does):
        out += text(CODE_X + 20, first + 17 * i, line, 12.5, "muted")
    return out


def _chip(
    key: str, x: float, w: float, cy: float, h: float, states: dict[str, str]
) -> str:
    node = FLOW_NODES[key]
    color, dash = STATE_STROKE[node_state(node["tasks"], states)]
    return (
        f'<rect x="{f(x)}" y="{f(cy - h / 2)}" width="{f(w)}" height="{f(h)}" rx="8" '
        f'fill="{TOKENS["surface_hi"]}" stroke="{TOKENS[color]}" stroke-width="2"{dash}/>'
        + text(
            x + w / 2,
            cy + 4.5,
            node.get("chip", node["label"][0]),
            12,
            "ink",
            "middle",
            700,
        )
    )


def _spur(cy: float, color: str, states: dict[str, str]) -> str:
    """The LLM above the core, joined to it by a two-way line: the core asks,
    the LLM answers. It touches nothing else on the rail."""
    x, w = _slot("core")
    cx = x + w / 2
    top, bottom = cy - 40 + 11, cy - 13
    c = TOKENS[color]
    lines = ""
    for lx, y1, y2 in ((cx - 6, bottom, top), (cx + 6, top, bottom)):
        s = 1 if y2 > y1 else -1
        lines += (
            f'<path d="M{f(lx)} {f(y1)} V{f(y2)}" stroke="{c}" stroke-width="1.6"/>'
            f'<path d="M{f(lx - 3.5)} {f(y2 - s * 4.5)} L{f(lx)} {f(y2)} '
            f'L{f(lx + 3.5)} {f(y2 - s * 4.5)}" fill="none" stroke="{c}" '
            'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    return lines + _chip("llm", cx - LLM_W / 2, LLM_W, cy - 40, 22, states)


def asset_call_flow(facts: Facts, animated: bool = True) -> str:
    """The example call as a transcript: each sentence on the phone, the
    stations it passes, and what the code does with it. Animated, the rows
    appear in the order the call runs, with CSS only, and the finished picture
    holds; the first row stands from the first frame."""
    states = facts.states
    width = 1200
    parts = [
        text(40, 50, "HOW A CALL FLOWS", 14, "muted", spacing=2.4, weight=700),
        *_legend(1160, 48),
        text(40, 82, FLOW_STILL, 15, "neon"),
        text(BUBBLE_X, 126, "on the phone", 13, "muted"),
        text(RAIL_X, 126, "stations on the way", 13, "muted"),
        text(CODE_X, 126, "in the code", 13, "muted"),
    ]
    starts: list[float] = []
    clock = 0.0

    def reveal(content: str, then: float) -> None:
        nonlocal clock
        # The first piece never hides, so the picture is never empty.
        if animated and clock > 0:
            parts.append(f'<g class="s k{len(starts)}">{content}</g>')
            starts.append(clock)
        else:
            parts.append(content)
        clock += then

    y = 140.0
    for row in FLOW_ROWS:
        h = _row_height(row)
        head = ""
        if row.apart:
            head = (
                f'<line x1="40" y1="{f(y + 6)}" x2="1160" y2="{f(y + 6)}" '
                f'stroke="{TOKENS["grid"]}" stroke-width="1.5" stroke-dasharray="4 6"/>'
                + text(40, y + 32, "the ways to the team", 13, "muted")
            )
            y += 44
        color = ROW_COLOR[row.who]
        # With the LLM above the core, the rail sits a little lower in its row.
        cy = y + h / 2 + (14 if row.llm else 0)
        out = row.who == "agent"
        bubble_edge = BUBBLE_X + BUBBLE_W + (24 if out else 0)
        # Each piece arrives with the arrow that leads into it.
        pieces: list[tuple[str, str]] = []
        edge = CODE_X - 4 if out else bubble_edge + 4
        for key in row.rail:
            x, w = _slot(key)
            near, far = (x + w + 3, x) if out else (x - 3, x + w)
            chip = _chip(key, x, w, cy, 26, states)
            pieces.append(("chip", _arrow(edge, near, cy, color) + chip))
            edge = far - 4 if out else far + 4
            if key == "core" and row.llm:
                pieces.append(("spur", _spur(cy, color, states)))
        if out:
            pieces.append(
                ("last", _arrow(edge, bubble_edge + 3, cy, color) + _bubble(row, y, h))
            )
            pieces.insert(0, ("first", head + _code_box(row, y, h)))
        else:
            dash = ' stroke-dasharray="4 4"' if row.who == "outage" else ""
            pieces.append(
                (
                    "last",
                    _arrow(edge, CODE_X - 3, cy, color, dash) + _code_box(row, y, h),
                )
            )
            pieces.insert(0, ("first", head + _bubble(row, y, h)))
        for kind, piece in pieces:
            reveal(piece, REVEAL_STEP[kind])
        y += h + 12
    height = int(y + 16)
    css = ""
    if animated:
        # Hold the whole picture at least as long as the reveal took.
        loop = 2 * clock + REVEAL_OUT
        end = 100 * (loop - REVEAL_OUT) / loop
        css = f".s{{animation-duration:{loop:.2f}s;animation-iteration-count:infinite;animation-timing-function:linear}}"
        for n, start in enumerate(starts):
            a, b = 100 * start / loop, 100 * (start + REVEAL_FADE) / loop
            css += (
                f".k{n}{{animation-name:k{n}}}"
                f"@keyframes k{n}{{0%,{a:.1f}%{{opacity:0}}{b:.1f}%,{end:.1f}%{{opacity:1}}100%{{opacity:0}}}}"
            )
        css += "@media (prefers-reduced-motion:reduce){.s{animation:none}}"
    body = dot_grid(width, height) + "\n" + "\n".join(parts)
    desc = (
        "One pickup call as a transcript, in three columns: what is said on the phone, "
        "the stations the sentence passes, and what the code does with it. The caller's "
        "words pass the access, Asterisk, the ear and the conversation core; the agent's "
        "reply goes from the core through the mouth, Asterisk and the access. The core "
        "is the only station that reaches the code; where it asks the LLM, the LLM hangs "
        "above the core, linked to it alone. The caller orders: get_service_status and "
        "search_menu, one tool per hop, at most six, each run by the core against "
        "Postgres; the LLM has no line to a tool or the database. With the caller's name "
        "the core runs draft_order, every price from Postgres. The LLM reads the draft "
        "back word for word, and the core checks that the caller heard it. Only after "
        "the caller's yes does the core run confirm. The core says the booking itself "
        "from confirm's result, not the model; meanwhile the tablet shows the order. "
        "The slip waits for the team's approval, in primary mode up to the amount limit "
        "it is due at once, as in this call. Set apart, the ways to the team: the core "
        "checks for a complaint before the model; for a complaint no rule covers, when "
        "the AI is unsure or the model fails, the team phone rings while the restaurant "
        "is open; after hours, when the caller's number is known, a callback waits on "
        "the tablet. When the service voice fails, the exchange rings the team phone. "
        "Each station's edge shows whether it is built, in progress or planned."
    )
    return document(width, height, "How a call flows", desc, body, css)


# --------------------------------------------------------------------- architecture

# Where each node of FLOW_NODES stands in the architecture: x, y, width, height.
# A person is a 60 by 60 box around its circle.
ARCH_AT: dict[str, tuple[float, float, float, float]] = {
    "caller": (62, 260, 60, 60),
    "access": (140, 262, 104, 56),
    "asterisk": (268, 262, 112, 56),
    "ear": (404, 210, 132, 52),
    "mouth": (404, 318, 132, 52),
    "core": (562, 258, 132, 64),
    "llm": (562, 156, 132, 48),
    "status": (730, 176, 176, 48),
    "menu": (730, 236, 176, 48),
    "draft": (730, 296, 176, 48),
    "confirm": (730, 356, 176, 48),
    "postgres": (950, 222, 100, 136),
    "teamphone": (244, 544, 160, 44),
    "team": (590, 536, 60, 60),
    "tablet": (760, 488, 160, 44),
    "slip": (760, 544, 160, 44),
    "push": (760, 600, 160, 44),
}
# The bands: hot while the caller is on the line, cold after the yes (docs/02 §2).
HOT_BAND = (40.0, 104.0, 1120.0, 316.0)
COLD_BAND = (40.0, 460.0, 1120.0, 220.0)
# The one wire the architecture adds to FLOW_LINKS: the exchange rings the team
# phone on a transfer and when `voice` is not reached (docs/02 §5).
ARCH_RING = ("asterisk", "teamphone")
HOT_SENTENCE = (
    "The caller waits: core and tools run in the voice process, no HTTP; "
    "each tool has a 300 ms budget (p95, local database)."
)
COLD_SENTENCE = (
    "Seconds are fine here: the dispatcher retries a failed delivery with backoff, "
    "then raises an alarm. The print bridge fetches the slip."
)


def _wire(a: str, b: str) -> list[tuple[float, float]]:
    """Route between two nodes: straight when they share a column, down and then
    across into a band below, otherwise across with one elbow."""
    ax, ay, aw, ah = ARCH_AT[a]
    bx, by, bw, bh = ARCH_AT[b]
    acx, acy, bcx, bcy = ax + aw / 2, ay + ah / 2, bx + bw / 2, by + bh / 2
    if abs(acx - bcx) < 5:
        return [(acx, ay + ah), (bcx, by)] if bcy > acy else [(acx, ay), (bcx, by + bh)]
    if by > ay + ah + 40:
        return [(acx, ay + ah), (acx, bcy), (bx + bw if bcx < acx else bx, bcy)]
    sx, ex = (ax + aw, bx) if bcx > acx else (ax, bx + bw)
    mid = (sx + ex) / 2
    return [(sx, acy), (mid, acy), (mid, bcy), (ex, bcy)]


def _in_band(key: str) -> tuple[float, float, float, float]:
    return HOT_BAND if ARCH_AT[key][1] < COLD_BAND[1] else COLD_BAND


def _band(box: tuple[float, float, float, float], label: str) -> str:
    x, y, w, h = box
    return (
        f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="14" '
        f'fill="{TOKENS["surface"]}" opacity="0.55" stroke="{TOKENS["grid"]}"/>'
        + text(x + 16, y + 28, label, 12, "muted", spacing=2, weight=700)
    )


def _arch_node(key: str, states: dict[str, str]) -> str:
    x, y, w, h = ARCH_AT[key]
    node = FLOW_NODES[key]
    cx, cy = x + w / 2, y + h / 2
    if node.get("kind") == "person":
        return (
            f'<circle cx="{f(cx)}" cy="{f(cy)}" r="28" fill="{TOKENS["surface"]}" '
            f'stroke="{TOKENS["grid"]}" stroke-width="2"/>'
            + _person(cx, cy - 2, "ink")
            + text(cx, cy + 50, node["label"][0], 14, "ink", "middle", 700)
        )
    color, dash = STATE_STROKE[node_state(node["tasks"], states)]
    if node.get("kind") == "db":
        shape = _cylinder(x, y, w, h, TOKENS[color])
        cy += 6
    else:
        shape = (
            f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="10" '
            f'fill="{TOKENS["surface_hi"]}" stroke="{TOKENS[color]}" stroke-width="2"{dash}/>'
        )
    lines = node["label"]
    base = cy + 5 - 9 * (len(lines) - 1)
    for i, line in enumerate(lines):
        code = node.get("code") and i == 1
        shape += text(
            cx,
            base + 18 * i,
            line,
            14 if i == 0 else 12.5,
            "neon" if code else "ink" if i == 0 else "muted",
            "middle",
            700 if i == 0 else 400,
        )
    return shape


def asset_architecture(facts: Facts) -> str:
    """Every component of FLOW_NODES in two bands: the hot path while the caller
    waits, the cold path after the yes, and confirm as the hinge between them."""
    states = facts.states
    width, height = 1200, 704
    hx, hy, _, hh = HOT_BAND
    cx, cy, _, ch = COLD_BAND
    parts = [
        dot_grid(width, height),
        text(40, 50, "ARCHITECTURE", 14, "muted", spacing=2.4, weight=700),
        *_legend(1160, 48),
        text(
            40,
            82,
            "Hot path while the caller is on the line, cold path after the yes.",
            15,
            "neon",
        ),
        _band(HOT_BAND, "HOT PATH"),
        text(hx + 136, hy + 28, HOT_SENTENCE, 13, "muted"),
        _band(COLD_BAND, "COLD PATH"),
        text(cx + 20, cy + ch - 16, COLD_SENTENCE, 13, "muted"),
    ]
    for a, b in FLOW_LINKS:
        planned = "planned" in (
            node_state(FLOW_NODES[a]["tasks"], states),
            node_state(FLOW_NODES[b]["tasks"], states),
        )
        # The hinge: confirm writes the order, and the cold path picks it up.
        hinge = _in_band(a) != _in_band(b)
        color = "violet" if hinge else "plum"
        dash = ' stroke-dasharray="5 5"' if hinge or planned else ""
        parts.append(
            f'<path d="{_rounded(_wire(a, b))}" fill="none" stroke="{TOKENS[color]}" '
            f'stroke-width="2"{dash}/>'
        )
    ring = _rounded(_wire(*ARCH_RING))
    parts.append(
        f'<path d="{ring}" fill="none" stroke="{TOKENS["violet"]}" stroke-width="2" '
        'stroke-dasharray="5 5"/>'
    )
    ax, _, aw, _ = ARCH_AT["asterisk"]
    parts.append(
        text(
            ax + aw / 2 + 12,
            hy + hh + 26,
            "the exchange rings the team phone: transfer or voice outage",
            12.5,
            "violet",
        )
    )
    px, _, pw, _ = ARCH_AT["postgres"]
    parts.append(
        text(
            px + pw / 2 - 12,
            hy + hh + 26,
            "confirm writes the order",
            12.5,
            "violet",
            "end",
        )
    )
    parts += [_arch_node(key, states) for key in FLOW_NODES]
    built = [
        k
        for k, n in FLOW_NODES.items()
        if n["tasks"] and node_state(n["tasks"], states) == "built"
    ]
    unfinished = [
        k
        for k, n in FLOW_NODES.items()
        if n["tasks"] and node_state(n["tasks"], states) != "built"
    ]
    desc = (
        "The architecture in two bands. The hot path, while the caller is on the line: "
        "the caller reaches the access, router or SIP trunk, and Asterisk, our own "
        "exchange; the ear turns speech into text for the conversation core, the mouth "
        "speaks its reply. The core asks the LLM, which connects to the core only, and "
        "runs the tools get_service_status, search_menu, draft_order and confirm against "
        "Postgres, in the voice process without HTTP, each within a 300 ms budget at p95 "
        "with a local database. confirm writes the order, the hinge to the cold path after "
        "the yes: the tablet shows it, the print bridge fetches the order slip, and the "
        "outbox carries events through the dispatcher to n8n and the push, retried with "
        "backoff and then an alarm. The team phone stands in the cold band but is rung "
        "during the call: the exchange rings it on a transfer or when the service voice "
        "is down. Built: "
        + ", ".join(" ".join(FLOW_NODES[k]["label"]) for k in built)
        + ". Not finished: "
        + (", ".join(" ".join(FLOW_NODES[k]["label"]) for k in unfinished) or "none")
        + "."
    )
    return document(width, height, "Architecture", desc, "\n".join(parts))


# ---------------------------------------------------------------------- voice bench

VERDICT_STYLE = {
    "go": ("neon", "go"),
    "no-go": ("coral", "no-go"),
    "at limit": ("violet", "at limit"),
}


def asset_voice_bench(facts: Facts) -> str:
    # The latest row of each quantity: the kits record one row per run.
    latest = {row.metric: row for row in facts.voice}
    rows = sorted(latest.values(), key=lambda row: row.date)
    width = 1200
    height = 170 + max(len(rows), 1) * 58
    parts = [dot_grid(width, height)]
    parts.append(
        slip(
            40,
            40,
            1120,
            height - 80,
            "Voice layer · measured",
            de(rows[-1].date if rows else None),
        )
    )
    if not rows:
        parts.append(text(64, 140, "No voice measurement recorded yet.", 20, "muted"))
    for i, row in enumerate(rows):
        y = 128 + i * 58
        parts.append(text(64, y, row.date.strftime("%d %b"), 15, "muted"))
        parts.append(text(160, y, row.metric, 16, "ink", weight=700))
        parts.append(
            text(
                160,
                y + 22,
                f"{row.package} · {row.note}" if row.note else row.package,
                14,
                "muted",
            )
        )
        value = f"{row.shown or f'{row.value:g}'} {row.unit}".strip()
        parts.append(text(870, y, value, 20, "ink", anchor="end", weight=700))
        if row.limit:
            parts.append(
                text(870, y + 22, f"limit {row.limit}", 13, "muted", anchor="end")
            )
        color, label = VERDICT_STYLE.get(row.verdict, ("plum", row.verdict))
        cx = 1050
        parts.append(
            f'<rect x="{cx - 56}" y="{y - 20}" width="112" height="28" rx="14" fill="none" stroke="{TOKENS[color]}" stroke-width="2"/>'
        )
        parts.append(
            text(
                cx,
                y - 1,
                label.upper(),
                14,
                color,
                anchor="middle",
                weight=700,
                spacing=1.5,
            )
        )
    desc = "Voice layer measurements: " + "; ".join(
        f"{de(r.date)} {r.metric} {r.value:g} {r.unit}, {r.verdict}" for r in rows
    )
    return document(width, height, "Voice layer measurements", desc, "\n".join(parts))


# ------------------------------------------------------------------- rules, divider


def asset_rules() -> str:
    width, height = 1200, 420
    parts = [dot_grid(width, height)]
    parts.append(text(40, 48, "SIX HARD RULES", 14, "muted", spacing=2.4, weight=700))
    for i, (title, l1, l2) in enumerate(HARD_RULES):
        col, row = i % 3, i // 3
        x, y = 40 + col * 380, 72 + row * 168
        parts.append(
            f'<rect x="{x}" y="{y}" width="360" height="148" rx="14" fill="{TOKENS["surface"]}" stroke="{TOKENS["grid"]}"/>'
        )
        parts.append(text(x + 24, y + 44, f"{i + 1:02d}", 24, "neon", weight=700))
        parts.append(text(x + 24, y + 76, title, 16, "ink", weight=700))
        parts.append(text(x + 24, y + 106, l1, 15, "muted"))
        parts.append(text(x + 24, y + 128, l2, 15, "muted"))
    desc = "The six hard rules: " + "; ".join(f"{t}: {a} {b}" for t, a, b in HARD_RULES)
    return document(width, height, "Six hard rules", desc, "\n".join(parts))


def asset_divider() -> str:
    width, height = 1200, 40
    line = wave_points(20, 1180, 20, 9, 22, 400)
    defs = (
        '<defs><linearGradient id="dv" x1="0" x2="1">'
        f'<stop offset="0" stop-color="{TOKENS["neon"]}" stop-opacity="0"/>'
        f'<stop offset="0.3" stop-color="{TOKENS["neon"]}"/>'
        f'<stop offset="0.7" stop-color="{TOKENS["violet"]}"/>'
        f'<stop offset="1" stop-color="{TOKENS["violet"]}" stop-opacity="0"/></linearGradient></defs>'
    )
    body = (
        defs
        + f'<path d="{path_of(line)}" fill="none" stroke="url(#dv)" stroke-width="2" stroke-linecap="round"/>'
    )
    svg = document(width, height, "Divider", "A voice waveform.", body)
    # The divider sits on the page itself: no background card.
    return svg.replace(
        f'<rect width="{width}" height="{height}" rx="24" fill="{TOKENS["bg"]}"/>\n', ""
    )


def asset_social() -> str:
    """The banner's example call at the size of a link preview. The PNG made from it
    is uploaded by hand and CI cannot check it, so it carries no measured number."""
    width, height = 1280, 640
    glow = (
        '<radialGradient id="g1" cx="0.15" cy="0.6" r="0.5">'
        f'<stop offset="0" stop-color="{TOKENS["neon"]}" stop-opacity="0.13"/>'
        f'<stop offset="1" stop-color="{TOKENS["neon"]}" stop-opacity="0"/></radialGradient>'
        '<radialGradient id="g2" cx="0.88" cy="0.55" r="0.55">'
        f'<stop offset="0" stop-color="{TOKENS["violet"]}" stop-opacity="0.22"/>'
        f'<stop offset="1" stop-color="{TOKENS["violet"]}" stop-opacity="0"/></radialGradient>'
    )
    parts = [
        f"<defs>{glow}</defs>",
        dot_grid(width, height),
        f'<rect width="{width}" height="{height}" fill="url(#g1)"/>',
        f'<rect width="{width}" height="{height}" fill="url(#g2)"/>',
        text(72, 108, "Maex Voice Agent", 46, "ink", weight=700),
        text(
            72,
            150,
            "AI phone agent for restaurants: reservations, pickups, deliveries",
            23,
            "muted",
        ),
        text(72, 262, "the caller says", 19, "muted"),
        # Only the order itself: a link preview is read in a second.
        text(72, 300, EXAMPLE_SAID[-1], 28, "ink"),
        neon(wave_points(72, 420, 392, 40, 6, 140), 6, "neon"),
    ]
    mark, logo_width = wordmark(440, 322, 140)
    parts.append(mark)
    arrow = 440 + logo_width + 18
    parts.append(
        f'<path d="M{f(arrow)} 392 L{f(arrow + 34)} 392" fill="none" '
        f'stroke="{TOKENS["violet"]}" stroke-width="3.5" stroke-dasharray="6 6"/>'
        f'<path d="M{f(arrow + 28)} 382 L{f(arrow + 42)} 392 L{f(arrow + 28)} 402" '
        f'fill="none" stroke="{TOKENS["violet"]}" stroke-width="3.5" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
    )
    sx, sy, sw, sh = 948, 250, 268, 270
    header, when, item, dish = EXAMPLE_SLIP
    parts.append(slip(sx, sy, sw, sh, header, when))
    parts.append(text(sx + 24, sy + 100, item, 24, "ink", weight=700))
    parts.append(text(sx + 24, sy + 132, dish, 19, "muted"))
    parts.append(
        f'<line x1="{sx + 24}" y1="{sy + 160}" x2="{sx + sw - 24}" y2="{sy + 160}" '
        f'stroke="{TOKENS["grid"]}" stroke-dasharray="4 6"/>'
    )
    parts.append(text(sx + 24, sy + 204, "after the yes", 18, "muted"))
    parts.append(text(sx + 24, sy + 242, "confirmed", 30, "neon", weight=700))
    parts.append(
        text(
            width / 2,
            590,
            "The AI understands. The code decides.",
            24,
            "neon",
            anchor="middle",
        )
    )
    svg = document(
        width,
        height,
        "MÆX social preview",
        "MÆX, Maex Voice Agent, an AI phone agent for restaurants. An example call: "
        "the caller orders dish 25 twice, the neon sign MÆX, and the order slip, "
        "confirmed after the caller's yes.",
        "\n".join(parts),
    )
    # Full bleed: link previews crop the corners themselves.
    return svg.replace(f'height="{height}" rx="24" fill=', f'height="{height}" fill=')


# ------------------------------------------------------------------- README text

# The README's text regions, each between `<!-- readme:<name> -->` and
# `<!-- /readme:<name> -->`; everything outside them is written by hand.
REGION_RE = r"(<!-- readme:{name} -->\n).*?(\n<!-- /readme:{name} -->)"


@dataclass
class Capability:
    works: str
    tasks: list[str]
    missing: str = ""
    waits_for: str = ""
    area: str = ""

    def done(self, states: dict[str, str]) -> bool:
        return not self.waits_for and all(states.get(t) == "done" for t in self.tasks)


@dataclass
class Area:
    """One row of the table under "What works": several capabilities read as one."""

    name: str
    summary: str


def read_capabilities(path: Path = CAPABILITIES) -> list[Capability]:
    with path.open("rb") as fh:
        return [Capability(**entry) for entry in tomllib.load(fh)["capability"]]


def read_areas(path: Path = CAPABILITIES) -> list[Area]:
    with path.open("rb") as fh:
        return [Area(**entry) for entry in tomllib.load(fh).get("area", [])]


def area_state(
    area: str, capabilities: list[Capability], states: dict[str, str]
) -> str:
    """built when every capability of the area is done, open when none is and no
    task of theirs has started, partly otherwise."""
    own = [c for c in capabilities if c.area == area]
    if own and all(c.done(states) for c in own):
        return "built"
    started = any(
        c.done(states) or any(states.get(t) in ("done", "progress") for t in c.tasks)
        for c in own
    )
    return "partly" if started else "open"


def status_region(facts: Facts) -> str:
    """The status table: the version as written in it by hand (`/gate`), stage and
    next gate from docs/01, the date of the last finished task."""
    stage = facts.stage
    if m := re.fullmatch(r"Stage (\S+) \((.+)\)", stage):
        stage = f"{m.group(1)}, {m.group(2)}"
    if not facts.gates:
        # A renamed heading or a moved table, never "every gate passed".
        raise SystemExit("docs/01_STATUS.md: no gate rows under '## Gates in Detail'")
    gate = next((g for g in facts.gates if not g.passed), None)
    next_gate = f"{gate.key}: {gate.content}" if gate else "none, every gate passed"
    return "\n".join(
        [
            "| | |",
            "|---|---|",
            f"| Version | {facts.version} |",
            f"| Stage | {stage} |",
            f"| Next Gate | {next_gate} |",
            f"| As of | {de(facts.progress_date)} |",
        ]
    )


def capabilities_region(
    facts: Facts, capabilities: list[Capability], areas: list[Area] | None = None
) -> str:
    """The table by area, and under it, folded, the two full lists."""
    states = facts.states
    works = [c.works for c in capabilities if c.done(states)]
    missing = [c.missing or c.works for c in capabilities if not c.done(states)]
    lists = [
        "**What works:**",
        "",
        *(f"- {s}" for s in works),
        "",
        "**Not yet working:**",
        "",
        *(f"- {s}" for s in missing),
    ]
    if not areas:
        return "\n".join(lists)
    return "\n".join(
        [
            "Built means: it runs on the workbench and in CI with invented test data. "
            "Nothing here has taken a real call yet.",
            "",
            "| Capability | State | What it is |",
            "|---|---|---|",
            *(
                f"| {a.name} | {area_state(a.name, capabilities, states)} | {a.summary} |"
                for a in areas
            ),
            "",
            "<details>",
            "<summary>Every capability in full</summary>",
            "",
            *lists,
            "",
            "</details>",
        ]
    )


def fill_regions(text: str, regions: dict[str, str]) -> str:
    for name, body in regions.items():
        m = re.search(REGION_RE.format(name=name), text, re.S)
        if m is None:
            raise SystemExit(f"README.md has no region readme:{name}")
        text = text[: m.end(1)] + body + text[m.start(2) :]
    return text


def readme_text(facts: Facts, current: str) -> str:
    return fill_regions(
        current,
        {
            "status": status_region(facts),
            "capabilities": capabilities_region(
                facts, read_capabilities(), read_areas()
            ),
        },
    )


# --------------------------------------------------------------------------- render


# ------------------------------------------------------------------------- roadmap
# The stages of docs/01_STATUS.md "Roadmap" as stations on one line. Name and goal
# are short forms of that table; the blocks of docs/07_WORKPACKAGES.md that build a
# stage give its bar. Block 5b (deployment) is needed from stage 1 on, block 6
# covers stages 5 and 6 and stands under 5. A test holds both lists to the docs.
STAGES: list[tuple[str, list[str], list[str]]] = [
    ("Foundation", ["facts, rights,", "budget"], ["0"]),
    ("Reservation", ["one call,", "end to end"], ["1", "1b", "1c", "1d", "2", "5b"]),
    ("Pickup", ["the menu,", "understood"], ["3"]),
    ("Delivery", ["address", "and zone"], ["4"]),
    ("Shadow run", ["real errors,", "measured"], ["5"]),
    ("Overflow", ["AI answers", "when busy"], ["6"]),
    ("Primary", ["AI answers", "first"], []),
]


def current_stage(facts: Facts) -> int:
    m = re.search(r"\d+", facts.stage)
    return int(m.group()) if m else 0


def asset_roadmap(facts: Facts) -> str:
    width, height = 1200, 390
    here = current_stage(facts)
    done = sum(b.count("done") for b in facts.blocks)
    total = sum(b.total for b in facts.blocks)
    next_gate = next((g.key for g in facts.gates if not g.passed), "none")
    passed = {g.key for g in facts.gates if g.passed}
    line_y = 200
    xs = [100 + i * 166 for i in range(len(STAGES))]
    parts = [
        dot_grid(width, height),
        text(40, 50, "ROADMAP", 14, "muted", spacing=2.4, weight=700),
        text(1160, 50, de(facts.progress_date), 14, "muted", anchor="end"),
        text(
            40,
            80,
            f"Stage {here} of {len(STAGES) - 1}, next gate {next_gate}. "
            f"Built ahead of the gates: {done} of {total} tasks done.",
            15,
            "neon",
        ),
        f'<path d="{path_of(wave_points(40, 1160, line_y, 5, 14, 200))}" fill="none" '
        f'stroke="{TOKENS["plum"]}" stroke-width="2.5" stroke-linecap="round"/>',
        # The lit part of the line: from the plan to the stage that is running.
        neon(wave_points(40, xs[here], line_y, 5, 1 + here * 2, 30 + here * 30), 3.5),
    ]
    for i, (name, goal, keys) in enumerate(STAGES):
        x = xs[i]
        if i == here:
            parts.append(
                f'<circle cx="{x}" cy="{line_y}" r="26" fill="{TOKENS["neon"]}" opacity="0.18"/>'
            )
            parts.append(
                text(
                    x,
                    line_y - 44,
                    "we are here",
                    13,
                    "neon",
                    anchor="middle",
                    weight=700,
                )
            )
        lit = i <= here
        parts.append(
            f'<circle cx="{x}" cy="{line_y}" r="17" '
            f'fill="{TOKENS["neon"] if i == here else TOKENS["bg"]}" '
            f'stroke="{TOKENS["neon"] if lit else TOKENS["plum"]}" stroke-width="2.5"/>'
        )
        parts.append(
            text(
                x,
                line_y + 6,
                str(i),
                16,
                "bg" if i == here else "neon" if lit else "muted",
                anchor="middle",
                weight=700,
            )
        )
        parts.append(text(x, line_y + 56, name, 15, "ink", anchor="middle", weight=700))
        for j, row in enumerate(goal):
            parts.append(
                text(x, line_y + 78 + j * 17, row, 12.5, "muted", anchor="middle")
            )
        blocks = [b for b in facts.blocks if b.key in keys]
        b_done = sum(b.count("done") for b in blocks)
        b_total = sum(b.total for b in blocks)
        if b_total:
            parts.append(
                f'<rect x="{x - 55}" y="{line_y + 122}" width="110" height="6" rx="3" '
                f'fill="{TOKENS["grid"]}"/>'
                f'<rect x="{x - 55}" y="{line_y + 122}" width="{f(110 * b_done / b_total)}" '
                f'height="6" rx="3" fill="{TOKENS["neon"]}"/>'
            )
            label = f"{b_done}/{b_total} built"
        else:
            label = "built with stage 5"
        parts.append(text(x, line_y + 148, label, 12, "muted", anchor="middle"))
        gate = f"G{i}"
        if i < len(STAGES) - 1:
            gx = x + 83
            color = (
                "neon" if gate in passed else "violet" if gate == next_gate else "plum"
            )
            parts.append(
                f'<rect x="{gx - 8}" y="{line_y - 8}" width="16" height="16" '
                f'transform="rotate(45 {gx} {line_y})" fill="{TOKENS["bg"]}" '
                f'stroke="{TOKENS[color]}" stroke-width="2"/>'
            )
            parts.append(
                text(
                    gx,
                    line_y - 20,
                    gate,
                    12.5,
                    "muted" if color == "plum" else color,
                    anchor="middle",
                    weight=700,
                )
            )
    desc = (
        f"The roadmap as one line with a station per stage, 0 to {len(STAGES) - 1}, and "
        f"a gate between two stages. Stage {here} is running, the next gate is "
        f"{next_gate}. Under each station: how many of its tasks are built. "
        f"{done} of {total} tasks are done as of {de(facts.progress_date)}."
    )
    return document(width, height, "Roadmap", desc, "\n".join(parts))


# --------------------------------------------------------------------------- badges
# Local files in the palette instead of an image service: the README loads nothing
# from outside but GitHub's own CI badge. The two versions are checked against the
# Dockerfile and the Compose file by a test.
PYTHON_VERSION = "3.12"
POSTGRES_VERSION = "16"


def badge(label: str, value: str, color: str = "violet") -> str:
    advance = 6.7  # of one character of the 11 px monospace face
    left, right = len(label) * advance + 16, len(value) * advance + 16
    width = round(left + right)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20" '
        f'viewBox="0 0 {width} 20" role="img" aria-labelledby="t d">\n'
        f'<title id="t">{escape(label)}: {escape(value)}</title>\n'
        f'<desc id="d">Badge: {escape(label)}, {escape(value)}.</desc>\n'
        f"<style>text{{font-family:{FONT};font-size:11px}}</style>\n"
        f'<rect width="{width}" height="20" rx="4" fill="{TOKENS["surface"]}"/>'
        f'<path d="M{f(left)} 0H{width - 4}a4 4 0 0 1 4 4v12a4 4 0 0 1-4 4H{f(left)}z" '
        f'fill="{TOKENS[color]}"/>'
        f'<text x="{f(left / 2)}" y="14" text-anchor="middle" fill="{TOKENS["ink"]}">'
        f"{escape(label)}</text>"
        f'<text x="{f(left + right / 2)}" y="14" text-anchor="middle" '
        f'fill="{TOKENS["bg"]}" font-weight="700">{escape(value)}</text>\n</svg>\n'
    )


def badges(facts: Facts) -> dict[str, str]:
    stage = facts.stage.replace("Stage ", "").replace(" (", " ").rstrip(")")
    cases = facts.latest("cases")
    return {
        "badge-python.svg": badge("python", PYTHON_VERSION),
        "badge-postgres.svg": badge("postgresql", POSTGRES_VERSION),
        "badge-license.svg": badge("license", "proprietary", "neon"),
        "badge-stage.svg": badge("stage", stage, "neon"),
        # A measured number never stands without its date.
        "badge-evals.svg": badge(
            "eval, real model",
            f"{cases.passed}/{cases.total} on {de(cases.date)}"
            if cases
            else "no run yet",
            "neon" if cases else "plum",
        ),
    }


def build(facts: Facts) -> dict[str, str]:
    return {
        "logo-mark.svg": asset_logo_mark(),
        "logo-wordmark.svg": asset_logo_wordmark(),
        "banner.svg": asset_banner(),
        "call-flow.svg": asset_call_flow(facts),
        "call-flow-static.svg": asset_call_flow(facts, animated=False),
        "architecture.svg": asset_architecture(facts),
        "eval-latest.svg": asset_eval_latest(facts),
        "eval-trend.svg": asset_eval_trend(facts),
        "progress.svg": asset_progress(facts),
        "roadmap.svg": asset_roadmap(facts),
        "voice-bench.svg": asset_voice_bench(facts),
        "rules.svg": asset_rules(),
        "divider.svg": asset_divider(),
        "social-preview.svg": asset_social(),
        **badges(facts),
    }


REPORTS = ROOT / "evals" / "reports"


def newest_report(suite: str | None, reports: Path = REPORTS) -> Path | None:
    """The newest report of the runner: `evals/reports/` for the regular cases and
    a run with tags, `evals/reports/targets/` for the target cases (runner.py
    `default_report_dir`). File names sort by time (`eval_%Y%m%d_%H%M%S_%f`)."""
    found = sorted(report_folder(suite, reports).glob("eval_*.json"))
    return found[-1] if found else None


def report_folder(suite: str | None, reports: Path = REPORTS) -> Path:
    return reports / "targets" if suite == "targets" else reports


# ------------------------------------------------------------- recording on its own

# A measurement made on `main` waits here, in the ignored report folder.
VOICE_PENDING = "voice_pending.csv"
VOICE_PATH = VOICE_HISTORY.relative_to(ROOT).as_posix()
# What each voice kit reports, by the package that built it (docs/20 §10).
KIT_PACKAGE = {"drill": "T-1.11", "spike": "T-10.1 step 1", "speech": "T-10.3"}
KIT_METRIC = {
    "drill": "Outage drills end at the team phone",
    "spike": "Telephony spike: audio both ways",
}


def git(*args: str, root: Path = ROOT) -> str | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def recording_branch(root: Path = ROOT) -> str | None:
    """The branch a measurement may be written on, None on `main`.

    Nothing is edited on `main` (CLAUDE.md §8), and a file left in the main folder
    blocks the sync hook. A detached head or no git at all counts as `main`."""
    name = (git("rev-parse", "--abbrev-ref", "HEAD", root=root) or "").strip()
    return None if name in ("", "HEAD", "main") else name


def report_folders(root: Path = ROOT) -> list[Path]:
    """`evals/reports/` of this checkout and of the folder that has `main` checked
    out, where a run made on `main` waits for a branch."""
    folders = [root / "evals" / "reports"]
    listing = git("worktree", "list", "--porcelain", root=root) or ""
    for record in listing.split("\n\n"):
        lines = record.splitlines()
        if "branch refs/heads/main" in lines and lines[0].startswith("worktree "):
            main = Path(lines[0].removeprefix("worktree ")) / "evals" / "reports"
            if main.resolve() != folders[0].resolve():
                folders.append(main)
    return folders


def shown(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """An exclusive lock between processes, released when the file closes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def pending_lock(folder: Path) -> AbstractContextManager[None]:
    """One writer at a time on a folder's pending file: a kit appending a row, or
    `pending --record` reading it and dropping the rows `main` holds."""
    return file_lock(folder / "voice_pending.lock")


def under_history_lock[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Recording and redrawing as one step. Two recorders that finish together
    would otherwise let the older drawing overwrite the newer one. Always taken
    before a pending lock, never after."""

    @functools.wraps(fn)
    def run(*args: P.args, **kwargs: P.kwargs) -> R:
        with file_lock(REPORTS / "history.lock"):
            return fn(*args, **kwargs)

    return run


def suite_of(tags: list[str]) -> str:
    """A suite name for a partial run, from its tags: menu, noise -> menu-noise."""
    return re.sub(r"[^a-z0-9-]+", "-", "-".join(tags).lower()).strip("-") or "partial"


@under_history_lock
def auto_record(suite: str, since: float, reports: Path = REPORTS) -> int:
    """After `make eval` and `make eval-targets`: record every full real-model
    report written since `since`, and redraw. Not only the newest: of two runs
    that overlap, the later one to finish still records the other. Says why when
    it records nothing."""
    fresh = [
        path
        for path in sorted(report_folder(suite, reports).glob("eval_*.json"))
        if path.stat().st_mtime >= since
    ]
    if not fresh:
        print("readme_assets: the run wrote no report, nothing recorded")
        return 0
    on_branch = recording_branch() is not None
    recorded = False
    for path in fresh:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            print(f"readme_assets: {shown(path)} is no complete report, not recorded")
            continue
        if (report.get("model") or "") in STAND_IN_NAMES:
            print("readme_assets: a run of the scripted stand-in is not recorded")
            continue
        if tags := report.get("tags"):
            print(
                f"readme_assets: a run with tags ({', '.join(tags)}) is partial and "
                "not recorded on its own"
                + (
                    ". To record it under a suite of its own:\n  python3 "
                    f"scripts/readme_assets.py add {shown(path)} --suite {suite_of(tags)}"
                    if on_branch
                    else "; on main nothing is recorded"
                )
            )
            continue
        try:
            checked_row(report, suite, report_name=path.name)
        except RecordError as exc:
            print(f"readme_assets: not recorded: {exc}")
            continue
        if not on_branch:
            print(
                "readme_assets: on main nothing is recorded; the report stays in "
                f"{shown(path.parent)} and `python3 scripts/readme_assets.py pending` "
                "lists it until /done records it on a branch"
            )
            continue
        print(
            f"{shown(path)}: "
            + record_report(report, suite, report_name=path.name, path=EVAL_HISTORY)
        )
        recorded = True
    return render() if recorded else 0


def pending(
    folders: list[Path] | None = None,
    history: Path = EVAL_HISTORY,
    voice: Path = VOICE_HISTORY,
) -> tuple[list[tuple[Path, str]], list[dict[str, str]]]:
    """Full real-model reports and voice results not yet in `docs/metrics/`."""
    reports: list[tuple[Path, str]] = []
    rows: list[dict[str, str]] = []
    names: set[tuple[str, str]] = set()
    seen = voice_identities(voice.read_text(encoding="utf-8") if voice.exists() else "")
    for folder in report_folders() if folders is None else folders:
        for suite, sub in (("cases", folder), ("targets", folder / "targets")):
            for path in sorted(sub.glob("eval_*.json")):
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                    row = checked_row(report, suite, report_name=path.name)
                except (ValueError, KeyError, RecordError):
                    # Not a full real-model run, or a file the runner never finished.
                    continue
                if (path.name, suite) not in names and not is_recorded(row, history):
                    names.add((path.name, suite))
                    reports.append((path, suite))
        waiting = folder / VOICE_PENDING
        if waiting.exists():
            with waiting.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    if (key := _waiting_key(row)) is None:
                        print(
                            f"readme_assets: {shown(waiting)} has a row that does not "
                            f"parse, skipped: {row}",
                            file=sys.stderr,
                        )
                    elif key not in seen:
                        seen.add(key)
                        rows.append(row)
    return reports, rows


def merged_voice(root: Path = ROOT) -> set[object]:
    """The voice rows `main` holds on GitHub, as of the last fetch."""
    text = git("show", f"origin/main:{VOICE_PATH}", root=root)
    if text is None:
        print(
            f"readme_assets: no origin/main:{VOICE_PATH} to compare with, "
            "waiting voice rows not pruned",
            file=sys.stderr,
        )
    return voice_identities(text)


def prune_pending(path: Path, merged: set[object]) -> None:
    """Drop the waiting rows `main` already holds, and the file once none is left.
    A row that does not parse stays as it is, for a person to look at."""
    if not merged or not path.exists():
        return
    with path.open(encoding="utf-8", newline="") as fh:
        table = list(csv.reader(fh))
    if not table:
        return
    header, *lines = table
    # As DictReader reads a row: a missing field is None, extra ones sit under None.
    keep = [
        line
        for line in lines
        if _waiting_key(dict(itertools.zip_longest(header, line))) not in merged
    ]
    if len(keep) == len(lines):
        return
    # A new file swapped in whole: a kill halfway through loses no waiting row.
    fresh = path.with_name(path.name + ".tmp")
    try:
        if not keep:
            path.unlink()
            return
        with fresh.open("w", encoding="utf-8", newline="") as fh:
            csv.writer(fh, lineterminator="\n").writerows([header, *keep])
        os.replace(fresh, path)
    except OSError as exc:
        # On Windows, a `pending` listing in another session that holds the file
        # open. Pruning is housekeeping: the rows stay for the next recording.
        print(f"readme_assets: {shown(path)} not pruned: {exc}", file=sys.stderr)
        fresh.unlink(missing_ok=True)


@under_history_lock
def record_pending(folders: list[Path] | None = None) -> int:
    folders = report_folders() if folders is None else folders
    with ExitStack() as held:
        # No pending file is read or pruned while a kit appends to it.
        for folder in folders:
            held.enter_context(pending_lock(folder))
        # A row leaves only once `main` holds it: if the branch that recorded it is
        # closed without a merge, `pending` lists the row again (#207).
        waiting = [f / VOICE_PENDING for f in folders if (f / VOICE_PENDING).exists()]
        merged = merged_voice() if waiting else set()
        for path in waiting:
            prune_pending(path, merged)
        reports, rows = pending(folders, EVAL_HISTORY, VOICE_HISTORY)
        if not reports and not rows:
            print("nothing pending")
            return 0
        if recording_branch() is None:
            print("on main nothing is recorded: run this on a branch", file=sys.stderr)
            return 1
        for path, suite in reports:
            report = json.loads(path.read_text(encoding="utf-8"))
            print(
                f"{shown(path)}: "
                + record_report(report, suite, report_name=path.name, path=EVAL_HISTORY)
            )
        for row in rows:
            print(record_voice(row, VOICE_HISTORY))
    return render()


def kit_rows(kit: str, report: object, run_at: datetime) -> list[dict[str, str]]:
    """The headline numbers of one run of a voice kit, as rows of the voice history.

    The drill and the spike report a list of points; a run is go when every point
    passed. The speech kit reports one silent-wrong rate per judged ear against its
    own limit, and the dishes its best ear heard right. A trial (`--limit`) is no
    measurement and gives no row."""
    base = {
        "date": run_at.astimezone(BERLIN).date().isoformat(),
        "package": KIT_PACKAGE[kit],
        "note": "",
        # In UTC: a local time sorts wrong across the hour that repeats in October.
        # With microseconds: two kits in parallel can end in the same second.
        "run_at": run_at.astimezone(UTC).isoformat(),
    }
    if kit in KIT_METRIC:
        assert isinstance(report, list)
        if not report:
            return []
        ok = sum(1 for point in report if point.get("ok", point.get("passed")))
        return [
            {
                **base,
                "metric": KIT_METRIC[kit],
                "value": str(ok),
                "unit": f"of {len(report)} points",
                "limit": "",
                "verdict": "go" if ok == len(report) else "no-go",
            }
        ]
    assert isinstance(report, dict)
    if report["sentences"] < report["all_sentences"]:
        return []
    limit = float(report["wrong_limit"])
    if dishes := report.get("own_menu_dishes"):
        base["note"] = f"own menu of {dishes} dishes"
    judged = [
        e
        for e in report["ears"]
        if "error" not in e and e["id"] not in report.get("sampled", [])
    ]
    orders = any("order_dishes" in e for e in judged)
    rows = [
        {
            **base,
            "metric": f"{'Orders taken wrong silently' if orders else 'Silent wrong numbers'} ({e['id']})",
            "value": f"{e['wrong_share'] * 100:.1f}",
            "unit": "%",
            "limit": f"{limit * 100:g} %",
            "verdict": "go" if e["wrong_share"] <= limit else "no-go",
        }
        for e in judged
    ]
    best = next((e for e in judged if e["id"] == report.get("ear")), None)
    if best:
        share = best["order_dishes_right_share"] if orders else best["dishes_share"]
        what = "Dishes heard right in orders" if orders else "Dish names heard right"
        rows.append(
            {
                **base,
                "metric": f"{what} ({best['id']})",
                "value": f"{share * 100:.1f}",
                "unit": "%",
                "limit": "",
                "verdict": "go" if report["go"] else "no-go",
            }
        )
    return rows


@under_history_lock
def record_measurements(rows: list[dict[str, str]], reports: Path = REPORTS) -> None:
    """Into the history on a branch, else into the pending file under evals/reports/."""
    if not rows:
        return
    if recording_branch() is None:
        with pending_lock(reports):
            for row in rows:
                append_row(reports / VOICE_PENDING, VOICE_FIELDS, row)
        print(
            f"readme_assets: on main nothing is recorded; {len(rows)} voice result(s) "
            f"wait in {shown(reports / VOICE_PENDING)} until /done records them on a branch"
        )
        return
    for row in rows:
        print(f"readme_assets: {record_voice(row, VOICE_HISTORY)}")
    render()


def record_kit_run(kit: str, argv: list[str], run_dir: Path, started: float) -> None:
    """After a voice kit (`scripts/voice_drill.py` and its siblings): record what its
    run reported. Best effort: a failure here prints a warning and never changes
    the kit's exit code."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run-dir", type=Path, default=run_dir)
    for flag in ("--dry-run", "--down"):
        parser.add_argument(flag, action="store_true")
    # A trial of the speech kit, also with a limit above the number of sentences.
    parser.add_argument("--limit")
    try:
        args, _ = parser.parse_known_args(argv)
        result = args.run_dir.resolve() / "report.json"
        if args.dry_run or args.down or args.limit is not None:
            return
        # A report from before this run (a step broke before the kit wrote one).
        if not result.exists() or result.stat().st_mtime < started:
            return
        report = json.loads(result.read_text(encoding="utf-8"))
        # The run's own time, from its report: the identity of the row.
        ended = datetime.fromtimestamp(result.stat().st_mtime, BERLIN)
        record_measurements(kit_rows(kit, report, ended))
    except Exception as exc:  # noqa: BLE001 - the kit's result must survive this
        print(
            f"readme_assets: warning: the run was not recorded: {exc}", file=sys.stderr
        )


PNGS = {
    "social-preview.png": ("social-preview.svg", 1280, 640),
    "logo-mark-512.png": ("logo-mark.svg", 512, 512),
}


def chromium() -> str | None:
    """Prefer Chromium's headless shell: the full browser cuts about 90 px off the
    bottom of a screenshot, even with the new headless mode (checked 14.10.2026)."""
    shells = sorted(
        Path("/opt/pw-browsers").glob(
            "chromium_headless_shell-*/chrome-linux*/headless_shell"
        )
    )
    if shells:
        return str(shells[-1])
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        if found := shutil.which(name):
            return found
    # Windows keeps Chrome off the PATH.
    windows = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    return str(windows) if windows.is_file() else None


def to_png(svg: Path, out: Path, width: int, height: int) -> None:
    browser = chromium()
    if browser is None:
        raise SystemExit("no Chromium found for --png")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(
            f'<html><body style="margin:0;background:transparent">'
            f'<img src="{svg.resolve().as_uri()}" width="{width}" height="{height}"></body></html>',
            encoding="utf-8",
        )
        subprocess.run(
            [
                browser,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--hide-scrollbars",
                "--default-background-color=00000000",
                f"--window-size={width},{height}",
                f"--screenshot={out}",
                page.as_uri(),
            ],
            check=True,
            capture_output=True,
        )


PNG_SOURCES = "png-sources.json"


def svg_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def stale_pngs(assets: dict[str, str]) -> list[str]:
    """PNGs that are missing, changed by hand, or drawn from another version of
    their SVG than the one built now.

    CI cannot run Chromium, so `render --png` records the digest of each source
    SVG and of the PNG it drew; `--check` compares both with today."""
    path = ASSETS / PNG_SOURCES
    recorded = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return [
        name
        for name, (source, _w, _h) in PNGS.items()
        if recorded.get(name)
        != {"source": svg_digest(assets[source]), "png": file_digest(ASSETS / name)}
    ]


def outdated(facts: Facts, assets: dict[str, str]) -> dict[Path, str]:
    """Every generated file whose committed text differs from what the data gives
    now, with the text it should have: the README's regions and the SVGs."""
    out: dict[Path, str] = {}
    readme = README.read_text(encoding="utf-8")
    if (text := readme_text(facts, readme)) != readme:
        out[README] = text
    for name, content in assets.items():
        target = ASSETS / name
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            out[target] = content
    return out


# The digest of the social preview as last uploaded in the repository settings.
# Its own file: `render --png` rewrites png-sources.json as a whole.
UPLOADED = "social-preview.uploaded"
IMAGES_HEADING = "README images"


def image_findings() -> dict[str, list[dict[str, str]]]:
    """What the maintenance bot reports about the images on `main`, for the one
    decision issue (`scripts/decision_issue.py --also`). Keyed by asset and
    digest, so the same stale state is reported once and a new one again.
    A stale SVG is not reported: the weekly README update redraws them
    (docs/15_README_STRATEGY.md, "Keeping them current"), only what needs Maxi is."""
    assets = build(gather())
    entries = [
        {
            "label": f"README images out of date on main ({name}): run python "
            "scripts/readme_assets.py render --png in the next PR",
            "id": f"{name}:{svg_digest(assets[PNGS[name][0]])}",
        }
        for name in stale_pngs(assets)
    ]
    social = file_digest(ASSETS / "social-preview.png")
    uploaded = ASSETS / UPLOADED
    if social and social != (
        uploaded.read_text(encoding="utf-8").strip() if uploaded.exists() else ""
    ):
        entries.append(
            {
                "label": "Social preview changed: upload "
                ".github/assets/social-preview.png in Settings, then run python "
                "scripts/readme_assets.py mark-uploaded",
                "id": f"social-preview.png:{social}",
            }
        )
    # The heading with an empty list says: nothing open here (decision_issue.py).
    return {IMAGES_HEADING: entries}


def mark_uploaded() -> int:
    digest = file_digest(ASSETS / "social-preview.png")
    if digest is None:
        print("no .github/assets/social-preview.png to mark", file=sys.stderr)
        return 1
    (ASSETS / UPLOADED).write_text(digest + "\n", encoding="utf-8")
    print(f"marked as uploaded: {digest[:12]}; commit .github/assets/{UPLOADED}")
    return 0


def render(check: bool = False, png: bool = False) -> int:
    facts = gather()
    assets = build(facts)
    changed = outdated(facts, assets)
    if not check:
        for target, content in changed.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            print(f"wrote {target}")
    elif changed:
        print(
            "README assets are stale: " + ", ".join(t.name for t in changed) + ".\n"
            "Run `python scripts/readme_assets.py render` and commit .github/assets/ "
            "and README.md.",
            file=sys.stderr,
        )
        return 1
    if png and not check:
        digests = {}
        for name, (source, w, h) in PNGS.items():
            to_png(ASSETS / source, ASSETS / name, w, h)
            digests[name] = {
                "source": svg_digest(assets[source]),
                "png": file_digest(ASSETS / name),
            }
            print(f"wrote .github/assets/{name}")
        (ASSETS / PNG_SOURCES).write_text(
            json.dumps(digests, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif old_pngs := stale_pngs(assets):
        print(
            "README PNGs are stale: " + ", ".join(old_pngs) + ".\n"
            "Run `python scripts/readme_assets.py render --png` (needs Chromium) and "
            "commit .github/assets/.",
            file=sys.stderr,
        )
        # Writing the SVGs still succeeded; only the check fails on it.
        return 1 if check else 0
    return 0


@under_history_lock
def record_by_hand(args: argparse.Namespace) -> int:
    """`add` and `add-voice`: one row, then the images drawn from it."""
    if args.cmd == "add":
        path = args.report or newest_report(args.suite)
        if path is None:
            print("not recorded: no report found in evals/reports/", file=sys.stderr)
            return 1
        report = json.loads(path.read_text(encoding="utf-8"))
        try:
            print(
                f"{path}: "
                + record_report(
                    report,
                    args.suite,
                    args.reference,
                    args.note,
                    path=EVAL_HISTORY,
                    report_name=path.name,
                )
            )
        except RecordError as exc:
            print(f"not recorded: {exc}", file=sys.stderr)
            return 1
    else:
        row = {
            "date": args.date.isoformat(),
            "package": args.package,
            "metric": args.metric,
            "value": f"{args.value:g}",
            "unit": args.unit,
            "limit": args.limit,
            "verdict": args.verdict,
            "note": args.note,
        }
        print(record_voice(row, VOICE_HISTORY))
    return render()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="write the SVG assets")
    r.add_argument("--check", action="store_true", help="fail if an asset is stale")
    r.add_argument("--png", action="store_true", help="also write the PNGs")
    a = sub.add_parser("add", help="record an eval report JSON")
    a.add_argument(
        "report",
        type=Path,
        nargs="?",
        help="report JSON; default: the newest one the runner wrote for this suite",
    )
    a.add_argument(
        "--suite",
        help="cases (default for a run without tags), targets, or the name of a partial run",
    )
    a.add_argument("--note", default="")
    a.add_argument(
        "--reference",
        action="store_true",
        help="record the scripted stand-in as reference",
    )
    v = sub.add_parser("add-voice", help="record a voice layer measurement")
    v.add_argument("date", type=date.fromisoformat)
    v.add_argument("package")
    v.add_argument("metric")
    v.add_argument("value", type=float)
    v.add_argument("unit")
    v.add_argument("verdict", choices=sorted(VERDICT_STYLE))
    v.add_argument("--limit", default="")
    v.add_argument("--note", default="")
    auto = sub.add_parser(
        "auto-record", help="record the run `make eval` just made (Makefile only)"
    )
    auto.add_argument("--suite", default="cases", choices=FULL_SUITES)
    auto.add_argument(
        "--since", type=float, default=0.0, help="epoch seconds the run started at"
    )
    pend = sub.add_parser(
        "pending", help="list the runs and voice results not yet in docs/metrics/"
    )
    pend.add_argument("--record", action="store_true", help="record them (not on main)")
    fnd = sub.add_parser(
        "findings", help="stale images and the social preview, as decision issue JSON"
    )
    fnd.add_argument("out", type=Path)
    sub.add_parser(
        "mark-uploaded", help="record the social preview as uploaded in Settings"
    )
    c = sub.add_parser("concepts", help="write the logo concept sheet")
    c.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    if args.cmd in ("add", "add-voice", "mark-uploaded") and recording_branch() is None:
        print(
            "not recorded: on main nothing is edited (CLAUDE.md §8); switch to a branch",
            file=sys.stderr,
        )
        return 1
    if args.cmd == "render":
        return render(args.check, args.png)
    if args.cmd == "findings":
        found = image_findings()
        args.out.write_text(json.dumps(found, indent=2) + "\n", encoding="utf-8")
        for entry in found[IMAGES_HEADING]:
            print(entry["label"])
        return 0
    if args.cmd == "mark-uploaded":
        return mark_uploaded()
    if args.cmd == "auto-record":
        return auto_record(args.suite, args.since)
    if args.cmd == "pending":
        if args.record:
            return record_pending()
        reports, rows = pending()
        for report_path, suite in reports:
            print(f"eval {suite}: {shown(report_path)}")
        for row in rows:
            print(f"voice: {row['date']} {row['metric']} {row['value']} {row['unit']}")
        if reports or rows:
            print(
                "record them on a branch: python3 scripts/readme_assets.py pending --record"
            )
        return 0
    if args.cmd in ("add", "add-voice"):
        return record_by_hand(args)
    args.out.write_text(concept_sheet(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
