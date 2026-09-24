"""Anrufprotokoll auswerten: Baseline fuer C1 und Entwuerfe fuer Eval-Faelle (docs/17).

Aufruf:
    python -m scripts.call_log imports/anrufprotokoll.csv
    python -m scripts.call_log imports/anrufprotokoll.csv --cases imports/eval_entwuerfe/

Das Protokoll fuehrt das Team von Hand, ohne Tonaufnahme: Anliegen, Ergebnis und
die woertlichen Kundensaetze, nie Namen oder Telefonnummern. Solange der
Rechts-Check (docs/09) offen ist, ist das die einzige Quelle echter Anrufe.
Die Datei liegt in imports/ (im .gitignore), die Entwuerfe ebenso: ein Entwurf
wandert erst nach Durchsicht von Hand nach evals/cases/.

Exit-Code: 0 ausgewertet, 1 Pruef-Fehler in der Datei, 2 Datei nicht gefunden
oder Zielordner evals/cases/.
Ohne Datenbank und ohne Netz.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

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

# Anliegen, wie das Team sie schreibt, und der Wert in `expected.intent` (docs/08).
# Frage und Sonstiges haben kein pruefbares Ergebnis und werden kein Eval-Fall.
INTENTS = {
    "reservierung": "reservation",
    "abholung": "pickup",
    "lieferung": "delivery",
    "beschwerde": None,
    "frage": None,
    "sonstiges": None,
}
OUTCOMES = ("erledigt", "rueckruf", "abgelehnt", "abgebrochen")
# Kuerzel vom Druckbogen (docs/vorlagen/anrufprotokoll_druck.html), damit beim
# Abtippen niemand ausschreiben muss.
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
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
# Hierhin wandern durchgesehene Faelle; der Entwurfslauf raeumt seinen Ordner
# leer und darf deshalb nie dorthin schreiben.
EVAL_CASES = Path(__file__).resolve().parents[1] / "evals" / "cases"

# Sechs Ziffern in Folge, auch mit Leerzeichen, Schraegstrich, Bindestrich,
# Punkt oder Klammer dazwischen, sind fast immer eine Telefonnummer. Mengen und
# Kartennummern ("2x 23", "die 147") bleiben darunter. Ein Datum mit Jahr
# (24.09.2026) schlaegt auch an: lieber einmal zu oft als eine Nummer zu wenig.
_PHONE = re.compile(r"(?:\d[\s/.()-]*){6,}")
_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_EMAIL = re.compile(r"\S+@\S+\.\S+")


@dataclass(frozen=True)
class Entry:
    line: int
    day: date
    hour: int
    duration_min: float | None
    intent: str
    outcome: str
    phrases: list[str]
    items: str
    problems: str


def _norm(value: str) -> str:
    """Kleinschreibung, Umlaute wie auf der Tastatur ohne Umlaut."""
    value = value.strip().lower()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(src, dst)
    return value


def _parse_date(value: str) -> date:
    # Vierstelliges Jahr Pflicht: "24.09.26" waere sonst still das Jahr 26.
    match = _DATE.fullmatch(value.strip())
    if not match:
        raise ValueError(value)
    day, month, year = (int(part) for part in match.groups())
    return date(year, month, day)


def _parse_hour(value: str) -> int:
    hour, minute = (int(part) for part in re.split(r"[:.]", value.strip()))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(value)
    return hour


def parse(text: str) -> tuple[list[Entry], list[str]]:
    """Liest das Protokoll (UTF-8, Semikolon) und prueft jede Zeile.

    Gibt die gueltigen Eintraege und die Fehler zurueck. Eine Zeile mit Fehler
    faellt ganz heraus; ein Datenschutz-Treffer macht die Datei rot, damit
    niemand eine Nummer uebersieht.
    """
    # Excel speichert UTF-8 mit BOM; ohne Abschneiden hiesse die erste Spalte anders.
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter=";")
    header = [_norm(h) for h in (reader.fieldnames or [])]
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        return [], [f"Spalten fehlen: {', '.join(missing)}"]
    reader.fieldnames = header

    entries: list[Entry] = []
    errors: list[str] = []
    for line, raw in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in raw.items() if k in COLUMNS}
        if not any(row.values()):
            continue
        if raw.get(None):  # type: ignore[call-overload]
            # Ein Semikolon im Freitext verschiebt jede Spalte dahinter still.
            errors.append(
                f"Zeile {line}: mehr Felder als Spalten, Semikolon im Text? "
                "In items und problems Komma verwenden"
            )
            continue
        problems = [f"Zeile {line}: {c} fehlt" for c in REQUIRED if not row[c]]
        for column in FREE_TEXT:
            if _PHONE.search(row[column]):
                problems.append(
                    f"Zeile {line}: {column} sieht nach Telefonnummer aus, bitte entfernen"
                )
            if _EMAIL.search(row[column]):
                problems.append(
                    f"Zeile {line}: {column} enthaelt eine E-Mail-Adresse, bitte entfernen"
                )
        if problems:
            errors.extend(problems)
            continue
        try:
            day = _parse_date(row["date"])
        except ValueError:
            errors.append(f"Zeile {line}: Datum '{row['date']}' nicht TT.MM.JJJJ")
            continue
        try:
            hour = _parse_hour(row["time"])
        except ValueError:
            errors.append(f"Zeile {line}: Uhrzeit '{row['time']}' nicht HH:MM")
            continue
        duration: float | None = None
        if row["duration_min"]:
            try:
                duration = float(row["duration_min"].replace(",", "."))
                # float() nimmt auch nan, inf und Negatives; ein Wert davon
                # verdirbt den Mittelwert der ganzen Baseline.
                if not math.isfinite(duration) or duration < 0:
                    raise ValueError(row["duration_min"])
            except ValueError:
                errors.append(
                    f"Zeile {line}: Dauer '{row['duration_min']}' ist keine Zahl"
                )
                continue
        intent = _norm(row["intent"])
        intent = INTENT_SHORT.get(intent, intent)
        if intent not in INTENTS:
            errors.append(
                f"Zeile {line}: Anliegen '{row['intent']}' unbekannt, erlaubt: "
                + ", ".join(INTENTS)
            )
            continue
        outcome = _norm(row["outcome"])
        outcome = OUTCOME_SHORT.get(outcome, outcome)
        if outcome not in OUTCOMES:
            errors.append(
                f"Zeile {line}: Ergebnis '{row['outcome']}' unbekannt, erlaubt: "
                + ", ".join(OUTCOMES)
            )
            continue
        phrases = [p.strip() for p in row["phrases"].split("|") if p.strip()]
        entries.append(
            Entry(
                line,
                day,
                hour,
                duration,
                intent,
                outcome,
                phrases,
                row["items"],
                row["problems"],
            )
        )
    return entries, errors


def _share(counter: Counter[str], total: int) -> str:
    return " · ".join(
        f"{key} {n} ({round(100 * n / total)} %)" for key, n in counter.most_common()
    )


def report(entries: list[Entry]) -> str:
    """Baseline fuer C1: Menge, Anliegen, Ergebnis, Dauer, Stoßzeiten."""
    if not entries:
        return "Keine Eintraege."
    total = len(entries)
    days = sorted({e.day for e in entries})
    lines = [
        f"Anrufe: {total} an {len(days)} Tagen "
        f"({days[0]:%d.%m.%Y} bis {days[-1]:%d.%m.%Y})",
        "Anliegen: " + _share(Counter(e.intent for e in entries), total),
        "Ergebnis: " + _share(Counter(e.outcome for e in entries), total),
    ]
    durations = [e.duration_min for e in entries if e.duration_min is not None]
    if durations:
        mean = sum(durations) / len(durations)
        lines.append(
            f"Dauer: Mittel {mean:.1f} min, max {max(durations):g} min "
            f"(n={len(durations)})".replace(".", ",")
        )
    hours = Counter(e.hour for e in entries)
    lines.append(
        "Je Stunde: " + " · ".join(f"{h} Uhr {hours[h]}" for h in sorted(hours))
    )
    weekdays = Counter(e.day.weekday() for e in entries)
    lines.append(
        "Je Wochentag: " + " · ".join(f"{WEEKDAYS[d]} {weekdays[d]}" for d in range(7))
    )
    noted = [e for e in entries if e.problems]
    lines.append(f"Probleme notiert: {len(noted)}")
    lines.extend(f"  Zeile {e.line}: {e.problems}" for e in noted)
    return "\n".join(lines)


def case_id(entry: Entry) -> str:
    """ID aus dem Inhalt, nicht aus der Zeilennummer: Zeile 2 kommt in jedem
    Wochenprotokoll wieder vor, und in evals/cases/ wuerde der neue Fall den
    alten ueberschreiben. Derselbe Anruf behaelt seine ID ueber jeden Lauf."""
    key = f"{entry.day.isoformat()} {entry.hour} {'|'.join(entry.phrases)}"
    return "protokoll_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


def to_case(entry: Entry) -> dict | None:
    """Entwurf eines Eval-Falls (docs/08 §1) oder None, wenn nichts pruefbar ist.

    Erwartet wird nur, was das Protokoll sicher hergibt: Anliegen, bestaetigt
    oder nicht, eskaliert oder nicht. Positionen mit Kartennummer traegt der
    Mensch bei der Durchsicht nach, `review` sagt was.
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
    review = [
        "Namen im Transkript durch Mueller ersetzen, falls noch einer drinsteht",
        "Feld review entfernen, dann nach evals/cases/ verschieben",
    ]
    if entry.items:
        review.insert(0, f"expected.items aus '{entry.items}' mit Kartennummern")
    return {
        "id": case_id(entry),
        "name": f"Anrufprotokoll Zeile {entry.line}",
        "tags": [entry.intent, "protokoll"],
        "source": "call_log",
        "transcript": [{"role": "customer", "text": p} for p in entry.phrases],
        "expected": expected,
        "review": review,
    }


def write_cases(entries: list[Entry], folder: Path) -> int:
    """Schreibt die Entwuerfe neu. Gleiche Datei, gleiches Ergebnis: alte
    Entwuerfe dieses Scripts fliegen vorher raus, sonst bliebe nach einer
    korrigierten Zeile der ueberholte Fall neben dem neuen liegen. Andere
    Dateien im Ordner bleiben unberuehrt."""
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("protokoll_*.json"):
        stale.unlink()
    written = 0
    for entry in entries:
        case = to_case(entry)
        if case is None:
            continue
        path = folder / f"{case['id']}_{entry.intent}.json"
        path.write_text(
            json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path, help="Protokoll als CSV (docs/17)")
    parser.add_argument(
        "--cases",
        type=Path,
        help="Ordner fuer Eval-Entwuerfe, z. B. imports/eval_entwuerfe/",
    )
    args = parser.parse_args(argv)

    if not args.file.is_file():
        print(f"Datei nicht gefunden: {args.file}", file=sys.stderr)
        return 2
    entries, errors = parse(args.file.read_text(encoding="utf-8-sig"))
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"{len(errors)} Fehler, nichts ausgewertet.", file=sys.stderr)
        return 1
    if args.cases and args.cases.resolve() == EVAL_CASES:
        print(
            "Entwuerfe nie direkt nach evals/cases: der Lauf loescht dort "
            "protokoll_*.json. Anderen Ordner nehmen, z. B. imports/eval_entwuerfe/",
            file=sys.stderr,
        )
        return 2
    print(report(entries))
    if args.cases:
        written = write_cases(entries, args.cases)
        print(f"Eval-Entwuerfe: {written} nach {args.cases}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
