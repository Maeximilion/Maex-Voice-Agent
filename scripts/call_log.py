"""Anrufprotokoll auswerten: Baseline fuer C1 und Entwuerfe fuer Eval-Faelle (docs/17).

Aufruf:
    python -m scripts.call_log imports/anrufprotokoll.csv
    python -m scripts.call_log imports/anrufprotokoll.csv --cases imports/eval_entwuerfe/

Das Protokoll fuehrt das Team von Hand, ohne Tonaufnahme: Anliegen, Ergebnis und
die woertlichen Kundensaetze, nie Namen oder Telefonnummern. Solange der
Rechts-Check (docs/09) offen ist, ist das die einzige Quelle echter Anrufe.
Die Datei liegt in imports/ (im .gitignore), die Entwuerfe ebenso. Ein Entwurf
enthaelt keinen echten Kundensatz (DSFA M15): das Team stellt die Saetze mit
eigenen Worten nach, erst dann wandert der Fall nach evals/cases/.

Exit-Code: 0 ausgewertet, 1 Pruef-Fehler in der Datei (auch: nicht UTF-8),
2 Datei nicht gefunden oder nicht lesbar, oder Zielordner in evals/cases/.
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
from datetime import date, timedelta
from pathlib import Path

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
# Name und Rufnummer stehen nie im Protokoll, der Agent braucht beide vor
# `confirm`. Ein bestaetigter Fall bekommt deshalb erfundene Werte: den
# Platzhalter-Namen der Evals und die Beispielnummer aus docs/08 §1.
SYNTHETIC_CALLER_ID = "+497215551234"
SYNTHETIC_NAME_TURN = "Auf den Namen Mueller."

# Sechs Ziffern in Folge, auch mit Leerzeichen, Schraegstrich, Bindestrich oder
# Klammer dazwischen, sind fast immer eine Telefonnummer. Ein Punkt zaehlt nur
# direkt zwischen zwei Ziffern (0176.123.45.67): "am 25.09. 19 Uhr" ist ein
# Datum ohne Jahr, wie docs/17 es empfiehlt. Mengen und Kartennummern ("2x 23",
# "die 147") bleiben darunter. Ein Datum mit Jahr (24.09.2026) schlaegt an:
# lieber einmal zu oft als eine Nummer zu wenig.
_PHONE = re.compile(r"\d(?:(?:[\s/()-]|\.(?=\d))*\d){5,}")
_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
# Auch diktiert: "mueller at gmx punkt de", "mueller @ gmx.de", "(at)".
_EMAIL = re.compile(
    r"[\w.-]+\s*(?:@|\(at\)|\bat\b)\s*[\w-]+\s*(?:\.|\bpunkt\b|\bdot\b)\s*"
    r"(?:de|com|net|org|eu|info|at|ch)\b"
)
# DSFA M8: eine Allergie als Merkmal einer Person ist ein Gesundheitsdatum.
# Erlaubt ist nur die Frage zum Gericht ("sind in der 23 Nuesse?"). "Allergene"
# und "Allergien" (umgangssprachlich fuer die Allergene eines Gerichts) treffen
# das Muster nicht, "Allergie" als Wortende ("Nussallergie") schon.
_HEALTH = re.compile(r"allergie\b|allergisch|unvertraeglich|intoleran")
# Strasse mit Hausnummer. Ein Stadtteil ("in die Weststadt") bleibt erlaubt.
_ADDRESS = re.compile(
    r"\b\w+(?:strasse|str\.|weg|platz|allee|gasse|ring|damm|ufer)\s*\d+"
)
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Entry:
    line: int
    day: date
    hour: int
    minute: int
    duration_min: float | None
    intent: str
    outcome: str
    phrases: list[str]
    items: str
    problems: str


def _norm(value: str) -> str:
    """Kleinschreibung, Umlaute wie auf der Tastatur ohne Umlaut."""
    return fold(value.strip())


def _spoken_phone(text: str) -> bool:
    """Eine diktierte Nummer: "null sieben zwei eins ..." oder in Zweiergruppen
    "null sieben einundzwanzig ...". Zaehlt die Ziffern einer Folge von
    Zahlwoertern; Artikel ("eine Pizza") unterbrechen die Folge. Ziffern zaehlen
    erst mit, wenn schon ein Zahlwort in der Folge steht, sonst waere jedes
    Datum ohne Jahr eine Nummer (dafuer ist _PHONE da)."""
    digits = 0
    spoken = False
    for token in _WORD.findall(fold(text)):
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
    # Vierstelliges Jahr Pflicht: "24.09.26" waere sonst still das Jahr 26.
    match = _DATE.fullmatch(value.strip())
    if not match:
        raise ValueError(value)
    day, month, year = (int(part) for part in match.groups())
    return date(year, month, day)


def _parse_time(value: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in re.split(r"[:.]", value.strip()))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(value)
    return hour, minute


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
    for raw in reader:
        # line_num statt Zaehler: DictReader ueberspringt Leerzeilen, und eine
        # Excel-Zelle mit Zeilenumbruch belegt mehrere Zeilen. Gemeldet wird die
        # letzte Zeile des Eintrags, bei einzeiligen Eintraegen genau die richtige.
        line = reader.line_num
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
            if _PHONE.search(row[column]) or _spoken_phone(row[column]):
                problems.append(
                    f"Zeile {line}: {column} sieht nach Telefonnummer aus, bitte "
                    "entfernen (Datum ohne Jahr, mehrere Nummern mit Komma trennen)"
                )
            if _EMAIL.search(fold(row[column])):
                problems.append(
                    f"Zeile {line}: {column} enthaelt eine E-Mail-Adresse, bitte entfernen"
                )
            if _HEALTH.search(fold(row[column])):
                problems.append(
                    f"Zeile {line}: {column} nennt eine Allergie oder Unvertraeglichkeit "
                    "einer Person, bitte nur produktbezogen ('sind in der 23 Nuesse?')"
                )
            if _ADDRESS.search(fold(row[column])):
                problems.append(
                    f"Zeile {line}: {column} enthaelt eine Adresse, bitte nur "
                    "den Stadtteil"
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
            hour, minute = _parse_time(row["time"])
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
        # Satzgrenze: | oder ein Zeilenumbruch in der Zelle (Alt+Enter in Excel).
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
    # Mittel je Tag, nicht Summe: in 15 Tagen kommt ein Wochentag dreimal vor,
    # die anderen zweimal, und die Summe zeigte eine Stosszeit, die keine ist.
    weekdays = Counter(e.day.weekday() for e in entries)
    span = (days[-1] - days[0]).days + 1
    occurs = Counter((days[0] + timedelta(n)).weekday() for n in range(span))
    lines.append(
        "Je Wochentag, Mittel je Tag: "
        + " · ".join(
            f"{WEEKDAYS[d]} "
            + (f"{weekdays[d] / occurs[d]:.1f}".replace(".", ",") if occurs[d] else "-")
            for d in range(7)
        )
    )
    noted = [e for e in entries if e.problems]
    lines.append(f"Probleme notiert: {len(noted)}")
    lines.extend(f"  Zeile {e.line}: {e.problems}" for e in noted)
    return "\n".join(lines)


def case_id(entry: Entry) -> str:
    """ID aus dem Inhalt, nicht aus der Zeilennummer: Zeile 2 kommt in jedem
    Wochenprotokoll wieder vor, und in evals/cases/ wuerde der neue Fall den
    alten ueberschreiben. Derselbe Anruf behaelt seine ID ueber jeden Lauf."""
    # Mit Minute: zwei Anrufe derselben Stunde mit gleichem Wortlaut sind zwei Faelle.
    key = (
        f"{entry.day.isoformat()} {entry.hour:02d}:{entry.minute:02d} "
        f"{'|'.join(entry.phrases)}"
    )
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
    # DSFA M15 und CLAUDE.md §8: echte Kundensaetze kommen nie ins Git. Der
    # Entwurf traegt nur den Aufbau des Falls; die Saetze stellt das Team mit
    # eigenen Worten nach (source handcrafted). Der Wortlaut bleibt in der CSV.
    review = [
        f"transcript nachstellen: {len(entry.phrases)} Kundensaetze mit eigenen "
        "Worten, gleiches Anliegen, nie den Wortlaut aus dem Protokoll",
        "Feld review entfernen, dann nach evals/cases/ verschieben",
    ]
    if entry.problems:
        review.insert(1, f"Problem laut Protokoll nachstellen: {entry.problems}")
    if entry.items:
        review.insert(0, f"expected.items aus '{entry.items}' mit Kartennummern")
    case: dict[str, object] = {
        "id": case_id(entry),
        "name": f"Anrufprotokoll Zeile {entry.line}",
        "tags": [entry.intent, "protokoll"],
        "source": "handcrafted",
    }
    if expected.get("confirmed"):
        case["caller_id"] = SYNTHETIC_CALLER_ID
        review.insert(
            0,
            f"Name und caller_id sind erfunden: '{SYNTHETIC_NAME_TURN}' vor den "
            "letzten Kundensatz (das Ja zum Vorlesen) setzen",
        )
    case["transcript"] = []
    case["expected"] = expected
    case["review"] = review
    return case


def write_cases(entries: list[Entry], folder: Path) -> int:
    """Schreibt die Entwuerfe neu. Gleiche Datei, gleiches Ergebnis: alte
    Entwuerfe dieses Scripts fliegen vorher raus, sonst bliebe nach einer
    korrigierten Zeile der ueberholte Fall neben dem neuen liegen.

    Geloescht wird nur, was noch ein Feld `review` traegt, also ein
    unbearbeiteter Entwurf; ein durchgesehener Fall und fremde Dateien bleiben.
    Ein Fall, dessen ID schon in evals/cases/ liegt, wird nicht neu entworfen:
    sonst ueberschriebe das naechste Verschieben den durchgesehenen Fall."""
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("protokoll_*.json"):
        try:
            draft = json.loads(stale.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(draft, dict) and "review" in draft:
            stale.unlink()
    done = {p.name.split("_")[1] for p in EVAL_CASES.glob("protokoll_*.json")}
    written: set[Path] = set()
    for entry in entries:
        case = to_case(entry)
        if case is None or str(case["id"]).removeprefix("protokoll_") in done:
            continue
        path = folder / f"{case['id']}_{entry.intent}.json"
        if path in written or path.exists():
            # Doppelt: dieselbe Zeile zweimal abgetippt (gleiche Minute, gleicher
            # Wortlaut). Vorhanden: nach dem Aufraeumen oben ein durchgesehener Fall.
            continue
        path.write_text(
            json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        written.add(path)
    return len(written)


def _inside_eval_cases(folder: Path) -> bool:
    """evals/cases/ selbst oder ein Ordner darin, ohne Ruecksicht auf Gross- und
    Kleinschreibung: auf macOS ist evals/Cases derselbe Ordner."""
    target = [part.casefold() for part in folder.resolve().parts]
    suite = [part.casefold() for part in EVAL_CASES.resolve().parts]
    return target[: len(suite)] == suite


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
    try:
        text = args.file.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        print(
            f"{args.file} ist nicht UTF-8. In Excel als 'CSV UTF-8 (durch "
            "Trennzeichen getrennt)' speichern.",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"Datei nicht lesbar: {args.file} ({exc.strerror})", file=sys.stderr)
        return 2
    entries, errors = parse(text)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"{len(errors)} Fehler, nichts ausgewertet.", file=sys.stderr)
        return 1
    if args.cases and _inside_eval_cases(args.cases):
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
