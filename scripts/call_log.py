"""Evaluate the call log: the baseline for C1 and drafts of eval cases (docs/17).

Usage:
    python -m scripts.call_log imports/anrufprotokoll.csv
    python -m scripts.call_log imports/anrufprotokoll.csv --cases imports/eval_entwuerfe/
    python -m scripts.call_log imports/anrufprotokoll.csv --retention-days 90 [--delete]

The team keeps the log by hand, without a recording: intent, outcome and the
caller's sentences word for word, never names or phone numbers. While the legal
check (docs/09) is open it is the only source of real calls. The file lives in
imports/ (in .gitignore), and so do the drafts. A draft holds no real caller
sentence (DPIA M15): the team restates the sentences in its own words, and only
then does the case move to evals/cases/.

Exit code: 0 evaluated, 1 a check failed in the file (also: not UTF-8),
2 file not found or not readable, target folder inside evals/cases/, or the ID
directory (.call_log_ids.json) or the salt (.call_log_salt) unreadable.
A time with minutes is cut to the hour in the CSV by every run (docs/09 L15).
No database and no network.
The column values the team types (intents, outcomes, the short forms of the
paper sheet) and the caller sentences stay German: they are product content.
"""

import argparse
import csv
import dataclasses
import hashlib
import io
import json
import math
import os
import re
import secrets
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from api.config import Settings
from api.domain.menu.numberwords import ARTICLES, fold, parse_cardinal

COLUMNS = (
    "date",
    "time",
    "duration_min",
    "intent",
    "outcome",
    "phrases",
    "items",
    "problems",
)
REQUIRED = ("date", "time", "intent", "outcome")
FREE_TEXT = ("phrases", "items", "problems")

# Intents as the team writes them, and the value in `expected.intent` (docs/08).
# A question and "other" have no outcome to check and make no eval case.
INTENTS = {
    "reservierung": "reservation",
    "abholung": "pickup",
    "lieferung": "delivery",
    "beschwerde": None,
    "frage": None,
    "sonstiges": None,
}
OUTCOMES = ("erledigt", "rueckruf", "abgelehnt", "abgebrochen")
# Short forms of the paper sheet (docs/vorlagen/anrufprotokoll_druck.html), so
# nobody has to write the words out when typing it up.
INTENT_SHORT = {
    "r": "reservierung",
    "a": "abholung",
    "l": "lieferung",
    "b": "beschwerde",
    "f": "frage",
    "s": "sonstiges",
}
OUTCOME_SHORT = {
    "e": "erledigt",
    "rr": "rueckruf",
    "al": "abgelehnt",
    "ab": "abgebrochen",
}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Nobody takes an order on the phone for longer; anything above is a typo.
MAX_MINUTES = 240
# Reviewed cases move here; the draft run empties its own folder and so must
# never write here.
EVAL_CASES = Path(__file__).resolve().parents[1] / "evals" / "cases"
# Name and phone number are never in the log, and the agent needs both before
# `confirm`. A confirmed case therefore gets made-up values: the placeholder
# name of the evals and the example number of docs/08 section 1.
SYNTHETIC_CALLER_ID = "+497215551234"
SYNTHETIC_NAME_TURN = "Auf den Namen Mueller."
# A confirmed case always ends with a clear yes; whether the last sentence in
# the log was one ("Danke" is none) is left open.
SYNTHETIC_YES_TURN = "Ja, das passt."
SYNTHETIC_ADDRESS_TURN = "Die Adresse ist Musterstrasse 1 in 12345 Musterstadt."

# Six digits in a row, also with blanks, a slash, a hyphen or a bracket between
# them, are nearly always a phone number. A period counts only right between two
# digits (0176.123.45.67): "am 25.09. 19 Uhr" is a date without a year, as
# docs/17 recommends. Quantities and card numbers ("2x 23", "die 147") stay
# below it. A date with a year (24.09.2026) hits: better once too often than
# one number too few.
_PHONE = re.compile(r"\d(?:(?:[\s/()-]|\.(?=\d))*\d){5,}")
_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
# Dictated too: "mueller at gmx punkt de", "mueller @ gmx.de", "(at)".
# Any domain with any number of levels and any ending of two letters or more
# (mail.example.de, example.io). An "@" between two word characters is enough.
_DOT = r"\s*(?:\.|\bpunkt\b|\bdot\b)\s*"
_EMAIL = re.compile(
    r"\w@\w|[\w.+-]+\s*(?:@|\(at\)|\bat\b)\s*[\w-]+"
    rf"(?:{_DOT}[\w-]+)*{_DOT}[a-z]{{2,}}\b"
)
# DPIA M8: an allergy as a trait of a person is health data. A list of words
# for people is never complete ("er", "der Kunde", ...), so every "Allergi..."
# is red; the question about a dish says "Allergene" ("welche Allergene hat
# die 23?"), which the pattern does not hit.
_HEALTH = re.compile(r"allergi|unvertraeglich|intoleran")
# A street with a house number. A district ("in die Weststadt") stays allowed.
# Also with a hyphen or apart ("Kaiser-Str. 12", "Kaiser Strasse 12a"), without
# an ending after a preposition of place ("Am Stadtgarten 5", "an der Alten
# Post 12"), the word Hausnummer and a postal code before a town. A number
# before Uhr, Personen, mal and the like is no house number.
# The word right before a number is no street name when it is an article, a
# time or a quantity: "am Freitag die 23", "am Samstag gegen 19.30", "um halb
# acht". Otherwise every second order would be an address.
_NOT_STREET = (
    r"(?:der|die|das|den|dem|des|ein|eine|einen|um|gegen|ab|bis|ca|circa|etwa"
    r"|halb|viertel|fuer|mit|und|oder|je|nur|noch|so|heute|morgen|abend|mittag"
    r"|abholen|abholung|wochenende|montag|dienstag|mittwoch|donnerstag|freitag"
    r"|samstag|sonntag|januar|februar|maerz|april|mai|juni|juli|august"
    r"|september|oktober|november|dezember|nr|nummer|menue|karte|speisekarte"
    r"|gericht|bestellung|bestellen)"
)
# "Nr." or "Nummer" behind a possible street name is dropped before the address
# check: "Hauptstrasse Nr. 12" becomes "hauptstrasse 12". Behind a word of
# _NOT_STREET it stays: "zum Abholen Nummer 12" and "im Menue Nummer 12" are
# numbers of the menu card.
_NR = re.compile(rf"\b(?!{_NOT_STREET}\s)([a-z-]+\.?)\s+(?:nr\.?|nummer)\s*(?=\d)")

# After the number: no time, no date ("19.30", "25.09."), no quantity.
_NOT_HOUSE_NO = (
    r"(?![.:]\d)"
    r"(?!\s*(?:uhr|personen|leute|leuten|min|minuten|mal|x\b|euro|stueck|:))"
)
_ADDRESS = re.compile(
    r"\b[\w-]*(?:strasse|str\.|weg|platz|allee|gasse|ring|damm|ufer|markt|hof"
    r"|chaussee|steig|stieg|pfad|wall|graben|anger|zeile|promenade|kai)\s*\d+"
    # After a cue word, up to the next punctuation mark, a number behind a name
    # is a house number ("meine Adresse ist Lindenblick 4"). The comma
    # separates: "Lieferung nach Weststadt, zwei Pizza".
    r"|\b(?:adresse|wohne|wohnen|wohnt|liefern an|lieferung an|bringen an"
    rf"|liefern nach|lieferung nach)\b[^|.!?,;]{{0,40}}?\b(?!{_NOT_STREET}\s)"
    rf"[a-z-]+\s+\d{{1,4}}[a-z]?\b{_NOT_HOUSE_NO}"
    r"|\b(?:am|im|an der|an den|auf der|auf dem|in der|in den|zum|zur"
    rf"|hinter der|unter den)\s+(?:[a-z-]+\s+){{0,2}}(?!{_NOT_STREET}\s)"
    rf"[a-z-]+\s+\d{{1,3}}[a-z]?\b{_NOT_HOUSE_NO}"
    r"|\bhausn(?:umme)?r\b|\b\d{5}\s+[a-z]{3,}"
)


@dataclass(frozen=True)
class Entry:
    line: int
    day: date
    hour: int
    # Only an old row still carries the minute; the run cuts it (L15, coarsen).
    minute: int | None
    duration_min: float | None
    intent: str
    outcome: str
    phrases: list[str]
    items: str
    problems: str
    # Order among the calls of the same hour, for the ID.
    ordinal: int = 0
    # Order within the minute as the ID before T-11.4 counted it, for the
    # mapping to the new ID; None once the minute is gone.
    minute_ordinal: int | None = None


def _norm(value: str) -> str:
    """Lower case, umlauts as typed on a keyboard without them."""
    return fold(value.strip())


def _cardinal(token: str) -> int | None:
    """A number word as a number, from a thousand up too: the menu's number
    reader ends at 999, a house number does not ("tausend", "zweitausenddrei")."""
    head, sep, tail = token.partition("tausend")
    if not sep:
        return parse_cardinal(token)
    high = 1 if head == "" else parse_cardinal(head)
    tail = tail.removeprefix("und")
    low = 0 if tail == "" else parse_cardinal(tail)
    if high is None or low is None:
        return None
    return high * 1000 + low


def _numbers_as_digits(text: str) -> str:
    """Number words as digits, for the address check: "Hauptstrasse zwoelf"
    becomes "hauptstrasse 12". Articles ("eine Pizza") stay words, or "am
    Freitag eine Pizza" would look like a house number. Numbers the speech
    recognition writes apart ("ein und zwanzig", "hundert drei") are joined
    first; then an article counts as part of the number too."""

    def digits(match: re.Match[str]) -> str:
        tokens = match.group(0).split()
        out = []
        i = 0
        while i < len(tokens):
            # The longest run from i that is one number, five words at most.
            for j in range(min(len(tokens), i + 5), i + 1, -1):
                value = _cardinal("".join(tokens[i:j]))
                if value is not None:
                    out.append(str(value))
                    i = j
                    break
            else:
                token = tokens[i]
                value = None if token in ARTICLES else _cardinal(token)
                out.append(token if value is None else str(value))
                i += 1
        return " ".join(out)

    return re.sub(r"[a-z]+(?:\s+[a-z]+)*", digits, fold(text))


def _spoken_phone(text: str) -> bool:
    """A dictated number: "null sieben zwei eins ..." or in pairs "null sieben
    einundzwanzig ...". Counts the digits of a run of number words; articles
    ("eine Pizza") break the run. Digits count only once a number word is in
    the run, or every date without a year would be a number (_PHONE is for
    those)."""
    digits = 0
    spoken = False
    # A comma, semicolon or | separates two numbers ("hundertzwanzig,
    # hundertdreissig" are two numbers of the menu card, docs/17).
    for token in re.findall(r"[a-z0-9]+|[,;|]", fold(text)):
        if token.isdigit():
            count = len(token) if spoken else 0
        elif token in ARTICLES:
            count = 0
        else:
            value = parse_cardinal(token)
            count = len(str(value)) if value is not None else 0
            spoken = spoken or count > 0
        if count == 0:
            digits, spoken = 0, False
            continue
        digits += count
        if digits >= 6:
            return True
    return False


def _parse_date(value: str) -> date:
    # A four-digit year is required: "24.09.26" would silently be the year 26.
    match = _DATE.fullmatch(value.strip())
    if not match:
        raise ValueError(value)
    day, month, year = (int(part) for part in match.groups())
    return date(year, month, day)


_TIME = re.compile(r"(\d{1,2})(?:[:.](\d{1,2}))?(?:\s*uhr)?")


def _parse_time(value: str) -> tuple[int, int | None]:
    """The hour of the call ("18", "18 Uhr"). An old row with the minute
    ("18:42") still passes; the run cuts it in the CSV (coarsen)."""
    match = _TIME.fullmatch(_norm(value))
    if not match:
        raise ValueError(value)
    hour = int(match[1])
    minute = None if match[2] is None else int(match[2])
    if hour > 23 or (minute is not None and minute > 59):
        raise ValueError(value)
    return hour, minute


def parse(text: str) -> tuple[list[Entry], list[str]]:
    """Reads the log (UTF-8, semicolon) and checks every row.

    Returns the valid entries and the errors. A row with an error drops out
    whole; a data protection hit turns the file red, so nobody overlooks a
    number.
    """
    # Excel saves UTF-8 with a BOM; left on, the first column would be named apart.
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter=";")
    header = [_norm(h) for h in (reader.fieldnames or [])]
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        return [], [f"Columns missing: {', '.join(missing)}"]
    # Exactly the known columns, each once: an extra column ("phone") would
    # otherwise never be checked for numbers, addresses and allergies.
    unknown = [h or "(empty)" for h in header if h not in COLUMNS]
    if unknown:
        return [], [f"Unknown columns: {', '.join(unknown)}, please remove them"]
    doubled = [c for c in COLUMNS if header.count(c) > 1]
    if doubled:
        return [], [f"Duplicate columns: {', '.join(doubled)}"]
    reader.fieldnames = header

    entries: list[Entry] = []
    errors: list[str] = []
    for raw in reader:
        # line_num, not a counter: DictReader skips blank lines, and an Excel
        # cell with a line break takes several lines. The last line of the
        # entry is reported, exactly the right one for a one-line entry.
        line = reader.line_num
        row = {k: (v or "").strip() for k, v in raw.items() if k in COLUMNS}
        # Before the blank-row check: ";;;;;;;;07215551234" has eight empty
        # columns, and the value in the ninth field must not go unchecked.
        if raw.get(None):
            # A semicolon in free text silently shifts every column behind it.
            errors.append(
                f"Line {line}: more fields than columns, a semicolon in the text? "
                "Use a comma in items and problems"
            )
            continue
        if not any(row.values()):
            continue
        problems = [f"Line {line}: {c} is missing" for c in REQUIRED if not row[c]]
        for column in FREE_TEXT:
            if _PHONE.search(row[column]) or _spoken_phone(row[column]):
                problems.append(
                    f"Line {line}: {column} looks like a phone number, please remove "
                    "it (a date without the year, several numbers apart by commas)"
                )
            if _EMAIL.search(fold(row[column])):
                problems.append(
                    f"Line {line}: {column} holds an email address, please remove it"
                )
            if _HEALTH.search(fold(row[column])):
                problems.append(
                    f"Line {line}: {column} names an allergy or intolerance, please "
                    "only ask about the dish: 'welche Allergene hat die 23?'"
                )
            scanned = _NR.sub(r"\1 ", _numbers_as_digits(row[column]))
            if _ADDRESS.search(scanned):
                problems.append(
                    f"Line {line}: {column} holds an address, please write only "
                    "the district"
                )
        if problems:
            errors.extend(problems)
            continue
        try:
            day = _parse_date(row["date"])
        except ValueError:
            errors.append(f"Line {line}: date '{row['date']}' is not DD.MM.YYYY")
            continue
        try:
            hour, minute = _parse_time(row["time"])
        except ValueError:
            errors.append(
                f"Line {line}: time '{row['time']}' is no hour (18 or 18 Uhr)"
            )
            continue
        duration: float | None = None
        if row["duration_min"]:
            try:
                duration = float(row["duration_min"].replace(",", "."))
                # float() takes nan, inf and negatives too; one of them spoils
                # the mean of the whole baseline.
                # Capped above: 1e308 is finite, and so is a pasted phone
                # number, and both spoil the baseline.
                if not math.isfinite(duration) or not 0 <= duration <= MAX_MINUTES:
                    raise ValueError(row["duration_min"])
            except ValueError:
                errors.append(
                    f"Line {line}: duration '{row['duration_min']}' is not a number"
                )
                continue
        intent = _norm(row["intent"])
        intent = INTENT_SHORT.get(intent, intent)
        if intent not in INTENTS:
            errors.append(
                f"Line {line}: intent '{row['intent']}' unknown, allowed: "
                + ", ".join(INTENTS)
            )
            continue
        outcome = _norm(row["outcome"])
        outcome = OUTCOME_SHORT.get(outcome, outcome)
        if outcome not in OUTCOMES:
            errors.append(
                f"Line {line}: outcome '{row['outcome']}' unknown, allowed: "
                + ", ".join(OUTCOMES)
            )
            continue
        # Sentence boundary: | or a line break in the cell (Alt+Enter in Excel).
        phrases = [p.strip() for p in re.split(r"[|\n]", row["phrases"]) if p.strip()]
        entries.append(
            Entry(
                line,
                day,
                hour,
                minute,
                duration,
                intent,
                outcome,
                phrases,
                row["items"],
                row["problems"],
            )
        )
    return _with_ordinals(entries), errors


# What a row says, without where and when: two rows with the same body read the same.
Body = tuple[str, str, tuple[str, ...], str, str, float | None]


def _body(e: Entry) -> Body:
    return (e.intent, e.outcome, tuple(e.phrases), e.items, e.problems, e.duration_min)


def _with_ordinals(entries: list[Entry]) -> list[Entry]:
    """Numbers the calls of one hour in the order of the CSV, for the ID. Every
    row is a call: without the minute a row typed twice looks like two calls
    with the same content, and merging them would lose the second real one;
    the report names such rows instead (_twins)."""
    hours: Counter[tuple[date, int]] = Counter()
    minutes: dict[tuple[date, int, int], list[Body]] = {}
    out = []
    for e in entries:
        minute_ordinal = None
        if e.minute is not None:
            # The ID before T-11.4 counted the distinct rows of a minute.
            bodies = minutes.setdefault((e.day, e.hour, e.minute), [])
            if _body(e) not in bodies:
                bodies.append(_body(e))
            minute_ordinal = bodies.index(_body(e))
        hour = (e.day, e.hour)
        out.append(
            dataclasses.replace(e, ordinal=hours[hour], minute_ordinal=minute_ordinal)
        )
        hours[hour] += 1
    return out


def _twins(entries: list[Entry]) -> list[list[int]]:
    """Lines with the same content in the same hour, maybe typed twice. Only
    rows with free text: two bare rows ("frage", "erledigt") are common."""
    groups: dict[tuple[date, int, Body], list[int]] = {}
    for e in entries:
        if e.phrases or e.items or e.problems:
            groups.setdefault((e.day, e.hour, _body(e)), []).append(e.line)
    return [lines for lines in groups.values() if len(lines) > 1]


def _share(counter: Counter[str], total: int) -> str:
    return " · ".join(
        f"{key} {n} ({round(100 * n / total)} %)" for key, n in counter.most_common()
    )


def report(entries: list[Entry]) -> str:
    """The baseline for C1: volume, intents, outcomes, duration, peak hours."""
    if not entries:
        return "No entries."
    total = len(entries)
    days = sorted({e.day for e in entries})
    lines = [
        f"Calls: {total} on {len(days)} days "
        f"({days[0]:%d.%m.%Y} to {days[-1]:%d.%m.%Y})",
        "Intent: " + _share(Counter(e.intent for e in entries), total),
        "Outcome: " + _share(Counter(e.outcome for e in entries), total),
    ]
    durations = [e.duration_min for e in entries if e.duration_min is not None]
    if durations:
        mean = sum(durations) / len(durations)
        lines.append(
            f"Duration: mean {mean:.1f} min, max {max(durations):g} min "
            f"(n={len(durations)})"
        )
    hours = Counter(e.hour for e in entries)
    lines.append("Per hour: " + " · ".join(f"{h}h {hours[h]}" for h in sorted(hours)))
    # Mean per day, not the sum: in 15 days one weekday comes three times, the
    # others twice, and the sum would show a peak that is none.
    weekdays = Counter(e.day.weekday() for e in entries)
    span = (days[-1] - days[0]).days + 1
    occurs = Counter((days[0] + timedelta(n)).weekday() for n in range(span))
    lines.append(
        "Per weekday, mean per day: "
        + " · ".join(
            f"{WEEKDAYS[d]} " + (f"{weekdays[d] / occurs[d]:.1f}" if occurs[d] else "-")
            for d in range(7)
        )
    )
    lines.extend(
        "Same row in the same hour, typed twice? Then delete one: "
        "lines " + ", ".join(map(str, twin))
        for twin in _twins(entries)
    )
    noted = [e for e in entries if e.problems]
    lines.append(f"Problems noted: {len(noted)}")
    lines.extend(f"  Line {e.line}: {e.problems}" for e in noted)
    return "\n".join(lines)


def _hashed(key: str, salt: str) -> str:
    return "protokoll_" + hashlib.sha256(f"{salt}|{key}".encode()).hexdigest()[:10]


def case_id(entry: Entry, salt: str = "") -> str:
    """ID of the call: date, hour and order within that hour. Not from the
    line number (line 2 comes back every week) and not from the wording (a
    corrected typo would orphan the reviewed case). The local salt keeps the
    time of the call from being worked out of an ID in evals/cases/ and matched
    with the router's call list."""
    return _hashed(f"{entry.day.isoformat()} {entry.hour:02d}|{entry.ordinal}", salt)


def _minute_id(entry: Entry, salt: str) -> str | None:
    """The ID before T-11.4, from the minute and the order within it; only a
    row that still carries its minute has one."""
    if entry.minute is None:
        return None
    key = f"{entry.day.isoformat()} {entry.hour:02d}:{entry.minute:02d}"
    return _hashed(f"{key}|{entry.minute_ordinal}", salt)


def _remap(entries: list[Entry], salt: str, known: dict[str, dict[str, str]]) -> bool:
    """The cut to the hour changes the ID of every old row. A reviewed case
    keeps its old ID in its file name, so the directory notes it under the new
    ID ("was"), with the fingerprint it had; write_cases looks the case up
    through it. Without that the case loses its row and its draft comes back.
    True when the directory changed."""
    done = _reviewed_cases()
    changed = False
    mapped: set[str] = set()
    for e in entries:
        old = _minute_id(e, salt)
        if old is None:
            continue
        old = old.removeprefix("protokoll_")
        new = case_id(e, salt).removeprefix("protokoll_")
        # The same row twice in one minute had one ID, for the first of them.
        # Mapped already: a run that stopped before the cut of the CSV.
        if (
            old in mapped
            or known.get(new, {}).get("was") == old
            or (old not in known and old not in done)
        ):
            continue
        mapped.add(old)
        record = known.pop(old, None) or {
            "day": e.day.isoformat(),
            "fp": _fingerprint(e, salt),
        }
        known[new] = {**record, "was": old}
        changed = True
    return changed


def to_case(entry: Entry, salt: str = "") -> dict[str, Any] | None:
    """Draft of an eval case (docs/08 section 1), or None when nothing can be
    checked.

    Expected is only what the log surely gives: the intent, confirmed or not,
    escalated or not. A person adds the items with card numbers on review;
    `review` says what.
    """
    if not entry.phrases:
        return None
    escalated = entry.intent == "beschwerde" or entry.outcome == "rueckruf"
    intent = INTENTS[entry.intent]
    if intent is None and not escalated:
        return None
    expected: dict[str, object] = {}
    if intent is not None:
        expected["intent"] = intent
    if not escalated:
        expected["confirmed"] = entry.outcome == "erledigt"
    expected["escalated"] = escalated
    # DPIA M15 and CLAUDE.md section 8: real caller sentences never go into git.
    # The draft carries only the shape of the case; the team restates the
    # sentences in its own words (source handcrafted). The wording stays in the CSV.
    review = [
        f"restate transcript: {len(entry.phrases)} caller sentences in your own "
        "words, same intent, never the wording of the log",
        "remove the field review, then move the case to evals/cases/",
    ]
    if entry.problems:
        review.insert(1, f"restate the problem the log notes: {entry.problems}")
    if entry.items:
        review.insert(0, f"expected.items from '{entry.items}' with card numbers")
    case: dict[str, object] = {
        "id": case_id(entry, salt),
        "name": f"Call log line {entry.line}",
        "tags": [entry.intent, "protokoll"],
        "source": "handcrafted",
    }
    transcript: list[dict[str, str]] = []
    if expected.get("confirmed"):
        case["caller_id"] = SYNTHETIC_CALLER_ID
        turns = [SYNTHETIC_NAME_TURN, SYNTHETIC_YES_TURN]
        if entry.intent == "lieferung":
            # Addresses are never in the log, but confirm needs one.
            turns.insert(0, SYNTHETIC_ADDRESS_TURN)
            review.insert(
                0,
                "address made up: it must lie in a delivery zone of the test data "
                "(scripts/seed_zones.py), else change the postal code",
            )
        transcript = [{"role": "customer", "text": t} for t in turns]
        review.insert(
            0,
            "name, yes and caller_id are made up: put the restated caller "
            f"sentences before these {len(turns)} lines",
        )
    case["transcript"] = transcript
    case["expected"] = expected
    case["review"] = review
    return case


def write_cases(
    entries: list[Entry],
    folder: Path,
    salt: str = "",
    known: dict[str, dict[str, str]] | None = None,
) -> int:
    """Writes the drafts anew. Same file, same result: this script's old
    drafts go first, or after a corrected row the outdated case would lie next
    to the new one.

    Only what still carries a field `review` is deleted, that is a draft no one
    worked on; a reviewed case and other files stay. A case whose ID is in
    evals/cases/ already is not drafted again: the next move would overwrite
    the reviewed case."""
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("protokoll_*.json"):
        try:
            draft = json.loads(stale.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Left where it is, not silently: it may be a half-edited case.
            print(f"Draft not readable, please check: {stale.name}", file=sys.stderr)
            continue
        if isinstance(draft, dict) and "review" in draft:
            stale.unlink()
    done = _reviewed_cases()
    # A case reviewed under its minute ID, found through its new ID (_remap).
    for cid, record in (known or {}).items():
        was = record.get("was")
        if was in done and cid not in done:
            done[cid] = done.pop(was)
    written: set[Path] = set()
    for entry in entries:
        case = to_case(entry, salt)
        # Before the None branch: a row that makes no case any more after a
        # correction (now "frage", for example) must report its case.
        cid = case_id(entry, salt).removeprefix("protokoll_")
        reviewed = done.get(cid)
        if known is not None:
            fp = _fingerprint(entry, salt)
            before = known.get(cid, {}).get("fp")
            if reviewed is not None and before is not None and before != fp:
                print(
                    f"The row of {reviewed} changed since the last run: corrected, "
                    "or a call of the same hour was added or moved. Check the "
                    "mapping and the case",
                    file=sys.stderr,
                )
            known[cid] = {**known.get(cid, {}), "day": entry.day.isoformat(), "fp": fp}
        if reviewed is not None:
            if case is None:
                print(
                    f"The log makes no case any more for {reviewed}: "
                    "check or remove the case by hand",
                    file=sys.stderr,
                )
            else:
                _report_correction(case, reviewed)
            continue
        if case is None:
            continue
        path = folder / f"{case['id']}_{entry.intent}.json"
        if path.exists():
            # After the clean-up above only a reviewed case is left here.
            continue
        path.write_text(
            json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        written.add(path)
    reported = _report_orphans(entries, salt, done)
    if known is not None:
        _reconcile(entries, salt, done, known, reported)
    return len(written)


def _reviewed_cases() -> dict[str, Path]:
    """Reviewed cases in evals/cases/ by the ID they carry, without the prefix:
    the field `id` counts, so a renamed case or one in a subfolder is still
    found; a file without one counts by its name, as before."""
    cases: dict[str, Path] = {}
    for path in sorted(EVAL_CASES.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        cid = data.get("id") if isinstance(data, dict) else None
        if not (isinstance(cid, str) and cid.startswith("protokoll_")):
            if not path.name.startswith("protokoll_"):
                continue
            cid = path.name
        cases[cid.removeprefix("protokoll_").split("_")[0]] = path
    return cases


def _fingerprint(entry: Entry, salt: str) -> str:
    """Content of the row, for the local directory only. The ID hangs on the
    order within the hour; a call added before it shifts the ID. A change of
    content behind a reviewed ID shows up this way."""
    body = json.dumps(_body(entry), ensure_ascii=False)
    return hashlib.sha256(f"{salt}|{body}".encode()).hexdigest()[:10]


def _reconcile(
    entries: list[Entry],
    salt: str,
    done: dict[str, Path],
    known: dict[str, dict[str, str]],
    reported: set[str],
) -> None:
    """Every reviewed case whose ID is in the local directory needs a row in
    the CSV. No date bound: when the only call at the edge of the period drops
    out, its date would lie outside. --delete has removed expired IDs from the
    directory already (_forget_before), they are no alarm. A case
    _report_orphans named (`reported`) is not named twice."""
    current = {case_id(e, salt).removeprefix("protokoll_") for e in entries}
    for cid, path in sorted(done.items()):
        if cid in known and cid not in current and cid not in reported:
            print(
                f"Reviewed case has no log row any more: {path}. "
                "Adjust or remove the case",
                file=sys.stderr,
            )


def _report_orphans(entries: list[Entry], salt: str, done: dict[str, Path]) -> set[str]:
    """The ID hangs on the order within the hour. When a call of an hour
    drops out, the next one moves up, and the reviewed case of the last place
    has no row any more: check and report the places behind the last one taken
    for every hour in the log. Returns the IDs reported."""
    last: dict[tuple[date, int], Entry] = {}
    for e in entries:
        key = (e.day, e.hour)
        if key not in last or e.ordinal > last[key].ordinal:
            last[key] = e
    reported = set()
    for e in last.values():
        for ordinal in range(e.ordinal + 1, e.ordinal + 4):
            gone = case_id(dataclasses.replace(e, ordinal=ordinal), salt)
            cid = gone.removeprefix("protokoll_")
            orphan = done.get(cid)
            if orphan is not None:
                reported.add(cid)
                print(
                    f"Reviewed case has no log row any more: {orphan}. Check "
                    "the calls of the same hour, adjust or remove the case",
                    file=sys.stderr,
                )
    return reported


def _report_correction(case: dict[str, Any], reviewed: Path) -> None:
    """A reviewed case is never overwritten, but a later correction of its row
    must not stay silent: report a difference in the outcome. items are missing
    in the draft and added by a person, they do not count."""
    try:
        existing = json.loads(reviewed.read_text(encoding="utf-8")).get("expected", {})
    except (OSError, ValueError, AttributeError):
        existing = None
    if not isinstance(existing, dict):
        print(f"Case not readable, please check: {reviewed}", file=sys.stderr)
        return
    # Both sides: a field the correction removes (intent on a complaint) counts
    # too. Only the core fields; a person adds the items.
    core = ("intent", "confirmed", "escalated")
    changed = [k for k in core if existing.get(k) != case["expected"].get(k)]
    if changed:
        print(
            f"The log differs from {reviewed} ({', '.join(changed)}): "
            "adjust the case by hand",
            file=sys.stderr,
        )


_SALT = re.compile(r"[0-9a-f]{32}")


def _salt(csv_file: Path) -> str:
    """The local salt of the case IDs, next to the CSV (imports/, in
    .gitignore). Made once and reused after, so IDs stay stable. An empty or
    broken salt stops the run (ValueError): empty, the IDs could be worked out
    again; a new one would change every ID."""
    path = csv_file.with_name(".call_log_salt")
    try:
        if not path.exists():
            _replace(path, secrets.token_hex(16))
        salt = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(getattr(exc, "strerror", None) or str(exc)) from exc
    if not _SALT.fullmatch(salt):
        raise ValueError("not 32 hex characters")
    return salt


def _load_known(path: Path) -> dict[str, dict[str, str]]:
    """The local directory ID -> call date and fingerprint of the row, next to
    the CSV (imports/, ignored). Missing, it is empty. Unreadable, it stops the
    run (ValueError): an empty directory in its place would forget the IDs of
    deleted calls for good."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    known: dict[str, dict[str, str]] = {}
    for cid, value in data.items():
        # The older form: the date only.
        entry = {"day": value} if isinstance(value, str) else value
        if not isinstance(entry, dict) or not isinstance(entry.get("day"), str):
            raise ValueError(f"entry {cid} without a date")
        # _forget_before compares text: only a real date, in one form.
        try:
            entry["day"] = date.fromisoformat(entry["day"]).isoformat()
        except ValueError:
            raise ValueError(f"entry {cid} with an invalid date") from None
        if not isinstance(entry.get("was", ""), str):
            raise ValueError(f"entry {cid} with an invalid old ID")
        known[cid] = entry
    return known


def _replace(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write a copy next to it first, then swap: a run that breaks off (disk
    full, power gone) leaves no empty or half file behind."""
    tmp = path.with_name(path.name + ".tmp")
    # Keep the old file's mode (a CSV with 0600 would not stay private
    # otherwise); new files (salt, directory) for the owner only. The copy is
    # private from its creation, the content goes in after.
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    try:
        tmp.unlink(missing_ok=True)
        tmp.touch(mode=0o600)
        os.chmod(tmp, mode)
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _save_known(path: Path, known: dict[str, dict[str, str]]) -> None:
    _replace(path, json.dumps(known, sort_keys=True))


def _forget_before(
    known: dict[str, dict[str, str]], cutoff: date
) -> dict[str, dict[str, str]]:
    """With the retention period the IDs of deleted calls leave the directory
    too."""
    return {k: v for k, v in known.items() if v["day"] >= cutoff.isoformat()}


def _today() -> date:
    """Today in local time; the tests fix the date."""
    return datetime.now(ZoneInfo("Europe/Berlin")).date()


def _rows(text: str) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))
    return rows[0], rows[1:]


def _csv(header: list[str], body: list[list[str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(body)
    return out.getvalue()


def purge(text: str, cutoff: date) -> str:
    """The log without the entries before `cutoff` (retention period, DPIA D12).

    Works on the rows of the CSV, so everything else stays as it is. Only for
    a file that `parse` read without errors."""
    header, body = _rows(text)
    at = [_norm(h) for h in header].index("date")
    # parse skips blank rows, also ones of blanks only; so does this.
    keep = [
        r for r in body if any(c.strip() for c in r) and _parse_date(r[at]) >= cutoff
    ]
    return _csv(header, keep)


def coarsen(text: str) -> str:
    """The log with the hour in place of the minute (L15: a row then hardly
    matches the router's call list). Every other cell stays as it is. Only for
    a file that `parse` read without errors."""
    header, body = _rows(text)
    at = [_norm(h) for h in header].index("time")
    for r in body:
        if any(c.strip() for c in r):
            r[at] = str(_parse_time(r[at])[0])
    return _csv(header, body)


def _retention_days(value: int | None) -> int | None:
    """The period from --retention-days, else CALL_LOG_RETENTION_DAYS from the
    environment or .env (through the API's settings, like any other setting),
    else none. Settings are built fresh: the environment may have changed since
    the import."""
    if value is not None:
        return value
    return Settings().call_log_retention_days


def _inside_eval_cases(folder: Path) -> bool:
    """evals/cases/ itself or a folder inside it, whatever the case of the
    letters: on macOS evals/Cases is the same folder."""
    target = [part.casefold() for part in folder.resolve().parts]
    suite = [part.casefold() for part in EVAL_CASES.resolve().parts]
    return target[: len(suite)] == suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path, help="the log as a CSV (docs/17)")
    parser.add_argument(
        "--cases",
        type=Path,
        help="folder for eval drafts, for example imports/eval_entwuerfe/",
    )
    # The German names stay as aliases: the team's notes and older docs use them.
    parser.add_argument(
        "--retention-days",
        "--frist-tage",
        type=int,
        help="retention period in days; default CALL_LOG_RETENTION_DAYS",
    )
    parser.add_argument(
        "--delete",
        "--loeschen",
        action="store_true",
        help="really remove the entries outside the period from the CSV",
    )
    args = parser.parse_args(argv)
    try:
        days = _retention_days(args.retention_days)
    except ValidationError as exc:
        # Any invalid line fails the settings, not only this one; the values
        # stay out (hide_input_in_errors), only the names and the rule.
        print("Settings in the environment or .env are not valid:", file=sys.stderr)
        for problem in exc.errors():
            name = "_".join(str(part) for part in problem["loc"]).upper()
            print(
                f"  {name}: {problem['msg']}" if name else f"  {problem['msg']}",
                file=sys.stderr,
            )
        return 2
    if days is not None and days < 1:
        print("The period must be at least 1 day.", file=sys.stderr)
        return 2
    if args.delete and days is None:
        print(
            "--delete needs a period: --retention-days N or CALL_LOG_RETENTION_DAYS.",
            file=sys.stderr,
        )
        return 2

    if not args.file.is_file():
        print(f"File not found: {args.file}", file=sys.stderr)
        return 2
    try:
        text = args.file.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        print(
            f"{args.file} is not UTF-8. Save it in Excel as 'CSV UTF-8 (durch "
            "Trennzeichen getrennt)'.",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"File not readable: {args.file} ({exc.strerror})", file=sys.stderr)
        return 2
    entries, errors = parse(text)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"{len(errors)} errors, nothing evaluated.", file=sys.stderr)
        return 1
    if args.cases and _inside_eval_cases(args.cases):
        print(
            "Drafts never go straight to evals/cases: the run deletes "
            "protokoll_*.json there. Take another folder, for example "
            "imports/eval_entwuerfe/",
            file=sys.stderr,
        )
        return 2
    # Before any write: an unreadable directory stays as it is.
    known_path = args.file.with_name(".call_log_ids.json")
    try:
        known = _load_known(known_path)
    except ValueError as exc:
        print(
            f"ID directory not readable: {known_path} ({exc}). Nothing changed; "
            "repair the file or restore it from the backup",
            file=sys.stderr,
        )
        return 2
    legacy = [e for e in entries if e.minute is not None]
    salt_path = args.file.with_name(".call_log_salt")
    salt = ""
    # Without a salt file no case ID was ever handed out, so the cut needs
    # none; a plain run never makes one.
    if args.cases or (legacy and salt_path.exists()):
        try:
            salt = _salt(args.file)
        except ValueError as exc:
            print(
                f"Salt not readable: {args.file.with_name('.call_log_salt')} "
                f"({exc}). Nothing changed; restore it from the backup, a new "
                "salt changes every case ID",
                file=sys.stderr,
            )
            return 2
    if legacy:
        # Directory first: a run that stops in between finds the minutes still
        # in the CSV and maps again, never a CSV without its mapping.
        if salt and _remap(entries, salt, known):
            _save_known(known_path, known)
        text = coarsen(text)
        _replace(args.file, text, encoding="utf-8-sig")
        print(f"Time cut to the hour: {len(legacy)} rows in {args.file}")
    if days is not None:
        cutoff = _today() - timedelta(days=days)
        old = [e for e in entries if e.day < cutoff]
        print(
            f"Retention: {len(old)} entries older than {days} days "
            f"(before {cutoff:%d.%m.%Y})"
        )
        if args.delete:
            if old:
                _replace(args.file, purge(text, cutoff), encoding="utf-8-sig")
                entries = [e for e in entries if e.day >= cutoff]
                print(f"Deleted: {len(old)} entries from {args.file}")
            # Without old rows too: a row removed by hand would otherwise never
            # expire in the directory.
            kept = _forget_before(known, cutoff)
            if kept != known:
                known = kept
                _save_known(known_path, known)
    print(report(entries))
    if args.cases:
        written = write_cases(entries, args.cases, salt, known)
        _save_known(known_path, known)
        print(f"Eval drafts: {written} to {args.cases}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
