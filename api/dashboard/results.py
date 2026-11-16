"""Eval and measurement results for the dev tools (#571, docs/23 §5).

The records live in the repository (`docs/metrics/`, docs/08 §4):
`eval_history.csv` holds one row per recorded eval run, `voice_history.csv`
one per measurement of the voice layer. The dashboard reads those two files in
the folder `OPERATOR_METRICS_DIR` and never writes there.

The files are the operator's own and are still read as input: at most 512 kB,
the newest 50 rows, the named columns only, every cell cut to 200 characters,
shown as text. Whatever is wrong with a file is one sentence in its place, so
the page stands.
"""

import csv
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_BYTES = 512 * 1024
ROWS_SHOWN = 50
CELL_LENGTH = 200
MISSING = "no such file in the folder OPERATOR_METRICS_DIR names"
TOO_LARGE = "the file is larger than 512 kB"
UNREADABLE = "the file cannot be read"
NO_COLUMNS = "the file has none of the expected columns"


@dataclass(frozen=True)
class Source:
    """A file of `docs/metrics/` and the columns shown of it, each with the
    word the page gives it."""

    name: str
    columns: tuple[tuple[str, str], ...]


EVALS = Source(
    "eval_history.csv",
    (
        ("date", "Date"),
        ("model", "Model"),
        ("suite", "Suite"),
        ("total", "Cases"),
        ("passed", "Passed"),
        ("crashed", "Crashed"),
        ("pending", "Pending"),
        ("guessed", "Guessed"),
        ("unconfirmed", "Unconfirmed"),
        ("missed_esc", "Missed escalations"),
        ("false_esc_rate", "False escalation rate"),
        ("tokens_per_case", "Tokens per case"),
        ("note", "Note"),
    ),
)
MEASUREMENTS = Source(
    "voice_history.csv",
    (
        ("date", "Date"),
        ("package", "Package"),
        ("metric", "Metric"),
        ("value", "Value"),
        ("unit", "Unit"),
        ("limit", "Limit"),
        ("verdict", "Verdict"),
        ("note", "Note"),
    ),
)


@dataclass(frozen=True)
class Table:
    """The newest rows of one file, the newest first, or the one sentence why
    there are none."""

    name: str
    columns: tuple[str, ...]
    rows: list[tuple[str, ...]] = field(default_factory=list)
    total: int = 0
    problem: str = ""


def table(folder: str, source: Source) -> Table:
    labels = tuple(label for _, label in source.columns)

    def none(problem: str) -> Table:
        return Table(source.name, labels, problem=problem)

    try:
        with (Path(folder) / source.name).open("rb") as file:
            raw = file.read(MAX_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        return none(MISSING)
    except OSError as error:
        # The kind only: a message can hold a path of the host.
        logger.warning("metrics file not read: %s", type(error).__name__)
        return none(UNREADABLE)
    if len(raw) > MAX_BYTES:
        return none(TOO_LARGE)
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return Table(source.name, labels)
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        rows = list(reader)
    except csv.Error:
        return none(UNREADABLE)
    if not {column for column, _ in source.columns} & set(reader.fieldnames or ()):
        return none(NO_COLUMNS)
    return Table(
        source.name,
        labels,
        rows=[
            tuple(
                str(row.get(column) or "")[:CELL_LENGTH] for column, _ in source.columns
            )
            for row in reversed(rows[-ROWS_SHOWN:])
        ],
        total=len(rows),
    )
