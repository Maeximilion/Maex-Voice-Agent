"""Anrufprotokoll auswerten: Baseline fuer C1 und Entwuerfe fuer Eval-Faelle (docs/17).

Aufruf:
    python -m scripts.call_log imports/anrufprotokoll.csv
    python -m scripts.call_log imports/anrufprotokoll.csv --cases imports/eval_entwuerfe/
    python -m scripts.call_log imports/anrufprotokoll.csv --frist-tage 90 [--loeschen]

Das Protokoll fuehrt das Team von Hand, ohne Tonaufnahme: Anliegen, Ergebnis und
die woertlichen Kundensaetze, nie Namen oder Telefonnummern. Solange der
Rechts-Check (docs/09) offen ist, ist das die einzige Quelle echter Anrufe.
Die Datei liegt in imports/ (im .gitignore), die Entwuerfe ebenso. Ein Entwurf
enthaelt keinen echten Kundensatz (DSFA M15): das Team stellt die Saetze mit
eigenen Worten nach, erst dann wandert der Fall nach evals/cases/.

Exit-Code: 0 ausgewertet, 1 Pruef-Fehler in der Datei (auch: nicht UTF-8),
2 Datei nicht gefunden oder nicht lesbar, Zielordner in evals/cases/ oder
ID-Verzeichnis (.call_log_ids.json) unlesbar.
Ohne Datenbank und ohne Netz.
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
from zoneinfo import ZoneInfo

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
# Laenger telefoniert niemand eine Bestellung; alles darueber ist ein Tippfehler.
MAX_MINUTES = 240
# Hierhin wandern durchgesehene Faelle; der Entwurfslauf raeumt seinen Ordner
# leer und darf deshalb nie dorthin schreiben.
EVAL_CASES = Path(__file__).resolve().parents[1] / "evals" / "cases"
# Name und Rufnummer stehen nie im Protokoll, der Agent braucht beide vor
# `confirm`. Ein bestaetigter Fall bekommt deshalb erfundene Werte: den
# Platzhalter-Namen der Evals und die Beispielnummer aus docs/08 §1.
SYNTHETIC_CALLER_ID = "+497215551234"
SYNTHETIC_NAME_TURN = "Auf den Namen Mueller."
# Ein bestaetigter Fall endet fest mit einem klaren Ja; ob der letzte Satz im
# Protokoll eines war ("Danke" ist keines), bleibt offen.
SYNTHETIC_YES_TURN = "Ja, das passt."
SYNTHETIC_ADDRESS_TURN = "Die Adresse ist Musterstrasse 1 in 12345 Musterstadt."

# Sechs Ziffern in Folge, auch mit Leerzeichen, Schraegstrich, Bindestrich oder
# Klammer dazwischen, sind fast immer eine Telefonnummer. Ein Punkt zaehlt nur
# direkt zwischen zwei Ziffern (0176.123.45.67): "am 25.09. 19 Uhr" ist ein
# Datum ohne Jahr, wie docs/17 es empfiehlt. Mengen und Kartennummern ("2x 23",
# "die 147") bleiben darunter. Ein Datum mit Jahr (24.09.2026) schlaegt an:
# lieber einmal zu oft als eine Nummer zu wenig.
_PHONE = re.compile(r"\d(?:(?:[\s/()-]|\.(?=\d))*\d){5,}")
_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
# Auch diktiert: "mueller at gmx punkt de", "mueller @ gmx.de", "(at)".
# Jede Domain mit beliebig vielen Stufen und jeder Endung ab zwei Buchstaben
# (mail.example.de, example.io). Ein "@" zwischen zwei Wortzeichen reicht allein.
_DOT = r"\s*(?:\.|\bpunkt\b|\bdot\b)\s*"
_EMAIL = re.compile(
    r"\w@\w|[\w.+-]+\s*(?:@|\(at\)|\bat\b)\s*[\w-]+"
    rf"(?:{_DOT}[\w-]+)*{_DOT}[a-z]{{2,}}\b"
)
# DSFA M8: eine Allergie als Merkmal einer Person ist ein Gesundheitsdatum. Eine
# Liste von Personenwoertern wird nie vollstaendig ("er", "der Kunde", ...),
# deshalb ist jedes "Allergi..." rot; die Frage zum Gericht heisst "Allergene"
# ("welche Allergene hat die 23?"), das trifft das Muster nicht.
_HEALTH = re.compile(r"allergi|unvertraeglich|intoleran")
# Strasse mit Hausnummer. Ein Stadtteil ("in die Weststadt") bleibt erlaubt.
# Auch mit Bindestrich oder getrennt ("Kaiser-Str. 12", "Kaiser Strasse 12a"),
# ohne Endung nach einer Ortspraeposition ("Am Stadtgarten 5", "an der Alten
# Post 12"), das Wort Hausnummer und eine Postleitzahl vor einem Ort. Eine Zahl
# vor Uhr, Personen, mal und aehnlichem ist keine Hausnummer.
# Das Wort direkt vor einer Zahl ist kein Strassenname, wenn es ein Artikel,
# eine Zeit- oder Mengenangabe ist: "am Freitag die 23", "am Samstag gegen
# 19.30", "um halb acht". Sonst waere jede zweite Bestellung eine Adresse.
_NOT_STREET = (
    r"(?:der|die|das|den|dem|des|ein|eine|einen|um|gegen|ab|bis|ca|circa|etwa"
    r"|halb|viertel|fuer|mit|und|oder|je|nur|noch|so|heute|morgen|abend|mittag"
    r"|abholen|abholung|wochenende|montag|dienstag|mittwoch|donnerstag|freitag"
    r"|samstag|sonntag|januar|februar|maerz|april|mai|juni|juli|august"
    r"|september|oktober|november|dezember|nr|nummer|menue|karte|speisekarte"
    r"|gericht|bestellung|bestellen)"
)
# "Nr." oder "Nummer" hinter einem moeglichen Strassennamen faellt vor der
# Adresspruefung weg: "Hauptstrasse Nr. 12" wird "hauptstrasse 12". Hinter
# einem Wort aus _NOT_STREET bleibt es stehen: "zum Abholen Nummer 12" und
# "im Menue Nummer 12" sind Nummern der Karte.
_NR = re.compile(rf"\b(?!{_NOT_STREET}\s)([a-z-]+\.?)\s+(?:nr\.?|nummer)\s*(?=\d)")

# Nach der Zahl: keine Uhrzeit, kein Datum ("19.30", "25.09."), keine Menge.
_NOT_HOUSE_NO = (
    r"(?![.:]\d)"
    r"(?!\s*(?:uhr|personen|leute|leuten|min|minuten|mal|x\b|euro|stueck|:))"
)
_ADDRESS = re.compile(
    r"\b[\w-]*(?:strasse|str\.|weg|platz|allee|gasse|ring|damm|ufer|markt|hof"
    r"|chaussee|steig|stieg|pfad|wall|graben|anger|zeile|promenade|kai)\s*\d+"
    # Nach einem Hinweiswort bis zum naechsten Satzzeichen ist eine Zahl hinter
    # einem Namen eine Hausnummer ("meine Adresse ist Lindenblick 4"). Das Komma
    # trennt: "Lieferung nach Weststadt, zwei Pizza".
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
    minute: int
    duration_min: float | None
    intent: str
    outcome: str
    phrases: list[str]
    items: str
    problems: str
    # Reihenfolge unter verschiedenen Anrufen derselben Minute, fuer die ID.
    ordinal: int = 0


def _norm(value: str) -> str:
    """Kleinschreibung, Umlaute wie auf der Tastatur ohne Umlaut."""
    return fold(value.strip())


def _cardinal(token: str) -> int | None:
    """Zahlwort als Zahl, auch ab tausend: der Zahlenleser der Karte endet bei
    999, eine Hausnummer nicht ("tausend", "zweitausenddrei")."""
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
    """Zahlwoerter als Ziffern, fuer die Adresspruefung: "Hauptstrasse zwoelf"
    wird "hauptstrasse 12". Artikel ("eine Pizza") bleiben Woerter, sonst
    saehe "am Freitag eine Pizza" wie eine Hausnummer aus. Getrennt geschriebene
    Zahlen der Spracherkennung ("ein und zwanzig", "hundert drei") werden erst
    zusammengezogen; dann zaehlt auch ein Artikel als Teil der Zahl."""

    def digits(match: re.Match[str]) -> str:
        tokens = match.group(0).split()
        out = []
        i = 0
        while i < len(tokens):
            # Laengste Folge ab i, die zusammen eine Zahl ist, hoechstens fuenf Woerter.
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
    """Eine diktierte Nummer: "null sieben zwei eins ..." oder in Zweiergruppen
    "null sieben einundzwanzig ...". Zaehlt die Ziffern einer Folge von
    Zahlwoertern; Artikel ("eine Pizza") unterbrechen die Folge. Ziffern zaehlen
    erst mit, wenn schon ein Zahlwort in der Folge steht, sonst waere jedes
    Datum ohne Jahr eine Nummer (dafuer ist _PHONE da)."""
    digits = 0
    spoken = False
    # Ein Komma, Semikolon oder | trennt zwei Zahlen ("hundertzwanzig,
    # hundertdreissig" sind zwei Nummern der Karte, docs/17).
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
    # Genau die bekannten Spalten, jede einmal: eine Zusatzspalte ("phone")
    # wuerde sonst nie auf Nummern, Adressen und Allergien geprueft.
    unknown = [h or "(leer)" for h in header if h not in COLUMNS]
    if unknown:
        return [], [f"Unbekannte Spalten: {', '.join(unknown)}, bitte entfernen"]
    doubled = [c for c in COLUMNS if header.count(c) > 1]
    if doubled:
        return [], [f"Doppelte Spalten: {', '.join(doubled)}"]
    reader.fieldnames = header

    entries: list[Entry] = []
    errors: list[str] = []
    for raw in reader:
        # line_num statt Zaehler: DictReader ueberspringt Leerzeilen, und eine
        # Excel-Zelle mit Zeilenumbruch belegt mehrere Zeilen. Gemeldet wird die
        # letzte Zeile des Eintrags, bei einzeiligen Eintraegen genau die richtige.
        line = reader.line_num
        row = {k: (v or "").strip() for k, v in raw.items() if k in COLUMNS}
        # Vor der Leerzeilen-Pruefung: ";;;;;;;;07215551234" hat acht leere
        # Spalten, der Wert im neunten Feld darf trotzdem nicht ungeprueft bleiben.
        if raw.get(None):  # type: ignore[call-overload]
            # Ein Semikolon im Freitext verschiebt jede Spalte dahinter still.
            errors.append(
                f"Zeile {line}: mehr Felder als Spalten, Semikolon im Text? "
                "In items und problems Komma verwenden"
            )
            continue
        if not any(row.values()):
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
                    f"Zeile {line}: {column} nennt eine Allergie oder Unvertraeglichkeit, "
                    "bitte nur zum Gericht: 'welche Allergene hat die 23?'"
                )
            scanned = _NR.sub(r"\1 ", _numbers_as_digits(row[column]))
            if _ADDRESS.search(scanned):
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
                # Oben begrenzt: 1e308 ist endlich, eine eingefuegte Telefonnummer
                # auch, und beide verderben die Baseline.
                if not math.isfinite(duration) or not 0 <= duration <= MAX_MINUTES:
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
    return _with_ordinals(entries), errors


def _with_ordinals(entries: list[Entry]) -> list[Entry]:
    """Zaehlt verschiedene Anrufe derselben Minute durch. Eine doppelt
    abgetippte Zeile ist derselbe Anruf und faellt weg, sonst zaehlte die
    Baseline ihn zweimal."""
    seen: dict[tuple, list[tuple]] = {}
    out = []
    for e in entries:
        minute = (e.day, e.hour, e.minute)
        body = (
            e.intent,
            e.outcome,
            tuple(e.phrases),
            e.items,
            e.problems,
            e.duration_min,
        )
        bodies = seen.setdefault(minute, [])
        if body in bodies:
            continue
        out.append(dataclasses.replace(e, ordinal=len(bodies)))
        bodies.append(body)
    return out


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


def case_id(entry: Entry, salt: str = "") -> str:
    """ID des Anrufs: Datum, Minute und Reihenfolge in dieser Minute. Nicht aus
    der Zeilennummer (Zeile 2 kommt jede Woche wieder) und nicht aus dem
    Wortlaut (ein korrigierter Tippfehler wuerde den durchgesehenen Fall
    verwaisen lassen). Das lokale Salz verhindert, dass sich aus einer ID in
    evals/cases/ die Anrufzeit zurueckrechnen und mit der Anrufliste des
    Routers abgleichen laesst."""
    key = (
        f"{salt}|{entry.day.isoformat()} {entry.hour:02d}:{entry.minute:02d}"
        f"|{entry.ordinal}"
    )
    return "protokoll_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


def to_case(entry: Entry, salt: str = "") -> dict | None:
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
        "id": case_id(entry, salt),
        "name": f"Anrufprotokoll Zeile {entry.line}",
        "tags": [entry.intent, "protokoll"],
        "source": "handcrafted",
    }
    transcript: list[dict[str, str]] = []
    if expected.get("confirmed"):
        case["caller_id"] = SYNTHETIC_CALLER_ID
        turns = [SYNTHETIC_NAME_TURN, SYNTHETIC_YES_TURN]
        if entry.intent == "lieferung":
            # Adressen stehen nie im Protokoll, confirm braucht aber eine.
            turns.insert(0, SYNTHETIC_ADDRESS_TURN)
            review.insert(
                0,
                "Adresse erfunden: muss in einer Lieferzone der Testdaten liegen "
                "(scripts/seed_zones.py), sonst PLZ anpassen",
            )
        transcript = [{"role": "customer", "text": t} for t in turns]
        review.insert(
            0,
            "Name, Ja und caller_id sind erfunden: die nachgestellten Kundensaetze "
            f"vor diese {len(turns)} Zeilen setzen",
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
            # Liegen lassen, nicht still: vielleicht ist es ein halb bearbeiteter Fall.
            print(f"Entwurf nicht lesbar, bitte pruefen: {stale.name}", file=sys.stderr)
            continue
        if isinstance(draft, dict) and "review" in draft:
            stale.unlink()
    done = {p.name.split("_")[1]: p for p in EVAL_CASES.glob("protokoll_*.json")}
    written: set[Path] = set()
    for entry in entries:
        case = to_case(entry, salt)
        # Vor dem None-Zweig: eine Zeile, die nach einer Korrektur keinen Fall
        # mehr ergibt (zum Beispiel jetzt "frage"), muss ihren Fall melden.
        cid = case_id(entry, salt).removeprefix("protokoll_")
        reviewed = done.get(cid)
        if known is not None:
            fp = _fingerprint(entry, salt)
            before = known.get(cid, {}).get("fp")
            if reviewed is not None and before is not None and before != fp:
                print(
                    f"Zeile zu {reviewed} hat sich seit dem letzten Lauf geaendert: "
                    "korrigiert, oder ein Anruf derselben Minute wurde eingefuegt "
                    "oder umsortiert. Zuordnung und Fall pruefen",
                    file=sys.stderr,
                )
            known[cid] = {"day": entry.day.isoformat(), "fp": fp}
        if reviewed is not None:
            if case is None:
                print(
                    f"Protokoll ergibt keinen Fall mehr fuer {reviewed}: "
                    "Fall von Hand pruefen oder entfernen",
                    file=sys.stderr,
                )
            else:
                _report_correction(case, reviewed)
            continue
        if case is None:
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
    _report_orphans(entries, salt, done)
    if known is not None:
        _reconcile(entries, salt, done, known)
    return len(written)


def _fingerprint(entry: Entry, salt: str) -> str:
    """Inhalt der Zeile, nur fuer das lokale Verzeichnis. Die ID haengt an der
    Reihenfolge in der Minute; ein nachgetragener Anruf davor verschiebt sie.
    Aendert sich der Inhalt hinter einer durchgesehenen ID, faellt das so auf."""
    body = json.dumps(
        [
            entry.intent,
            entry.outcome,
            entry.phrases,
            entry.items,
            entry.problems,
            entry.duration_min,
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(f"{salt}|{body}".encode()).hexdigest()[:10]


def _reconcile(
    entries: list[Entry],
    salt: str,
    done: dict[str, Path],
    known: dict[str, dict[str, str]],
) -> None:
    """Jeder durchgesehene Fall, dessen ID im lokalen Verzeichnis steht, braucht
    eine Zeile in der CSV. Keine Datumsgrenze: faellt der einzige Anruf am Rand
    des Zeitraums weg, laege sein Datum sonst ausserhalb. Abgelaufene IDs hat
    --loeschen schon aus dem Verzeichnis entfernt (_forget_before), sie sind
    kein Alarm."""
    current = {case_id(e, salt).removeprefix("protokoll_") for e in entries}
    for cid, path in sorted(done.items()):
        if cid in known and cid not in current:
            print(
                f"Durchgesehener Fall hat keine Protokollzeile mehr: {path}. "
                "Fall anpassen oder entfernen",
                file=sys.stderr,
            )


def _report_orphans(entries: list[Entry], salt: str, done: dict[str, Path]) -> None:
    """Die ID haengt an der Reihenfolge in der Minute. Faellt ein Anruf einer
    Minute weg, rutscht der naechste nach vorn, und der durchgesehene Fall des
    letzten Platzes hat keine Zeile mehr: fuer jede Minute im Protokoll die
    Plaetze hinter dem letzten belegten pruefen und melden."""
    last: dict[tuple, Entry] = {}
    for e in entries:
        key = (e.day, e.hour, e.minute)
        if key not in last or e.ordinal > last[key].ordinal:
            last[key] = e
    for e in last.values():
        for ordinal in range(e.ordinal + 1, e.ordinal + 4):
            gone = case_id(dataclasses.replace(e, ordinal=ordinal), salt)
            orphan = done.get(gone.removeprefix("protokoll_"))
            if orphan is not None:
                print(
                    f"Durchgesehener Fall hat keine Protokollzeile mehr: {orphan}. "
                    "Anrufe derselben Minute pruefen, Fall anpassen oder entfernen",
                    file=sys.stderr,
                )


def _report_correction(case: dict, reviewed: Path) -> None:
    """Ein durchgesehener Fall wird nie ueberschrieben, eine spaetere Korrektur
    der Zeile darf aber nicht still bleiben: Abweichungen im Ergebnis melden.
    items fehlen im Entwurf und traegt der Mensch nach, sie zaehlen nicht."""
    try:
        existing = json.loads(reviewed.read_text(encoding="utf-8")).get("expected", {})
    except (OSError, ValueError, AttributeError):
        existing = None
    if not isinstance(existing, dict):
        print(f"Fall nicht lesbar, bitte pruefen: {reviewed}", file=sys.stderr)
        return
    # Beide Seiten: auch ein Feld, das die Korrektur entfernt (intent bei einer
    # Beschwerde), zaehlt. Nur die Kernfelder; items traegt der Mensch nach.
    core = ("intent", "confirmed", "escalated")
    changed = [k for k in core if existing.get(k) != case["expected"].get(k)]
    if changed:
        print(
            f"Protokoll weicht von {reviewed} ab ({', '.join(changed)}): "
            "Fall von Hand anpassen",
            file=sys.stderr,
        )


_SALT = re.compile(r"[0-9a-f]{32}")


def _salt(csv_file: Path) -> str:
    """Lokales Salz fuer die Fall-IDs, neben der CSV (imports/, im .gitignore).
    Einmal angelegt, danach wiederverwendet, damit IDs stabil bleiben. Ein
    leeres oder kaputtes Salz bricht ab (ValueError): leer waeren die IDs
    wieder berechenbar, ein neues aenderte alle IDs."""
    path = csv_file.with_name(".call_log_salt")
    try:
        if not path.exists():
            _replace(path, secrets.token_hex(16))
        salt = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(getattr(exc, "strerror", None) or str(exc)) from exc
    if not _SALT.fullmatch(salt):
        raise ValueError("keine 32 Hex-Zeichen")
    return salt


def _load_known(path: Path) -> dict[str, dict[str, str]]:
    """Lokales Verzeichnis ID -> Anrufdatum und Fingerabdruck der Zeile, neben
    der CSV (imports/, ignoriert). Fehlt es, ist es leer. Ist es unlesbar, bricht
    der Lauf ab (ValueError): ein leeres Verzeichnis an seiner Stelle wuerde die
    IDs geloeschter Anrufe fuer immer vergessen."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("kein JSON-Objekt")
    known: dict[str, dict[str, str]] = {}
    for cid, value in data.items():
        # Aeltere Form: nur das Datum.
        entry = {"day": value} if isinstance(value, str) else value
        if not isinstance(entry, dict) or not isinstance(entry.get("day"), str):
            raise ValueError(f"Eintrag {cid} ohne Datum")
        # _forget_before vergleicht Texte: nur ein echtes Datum in einer Form.
        try:
            entry["day"] = date.fromisoformat(entry["day"]).isoformat()
        except ValueError:
            raise ValueError(f"Eintrag {cid} mit ungueltigem Datum") from None
        known[cid] = entry
    return known


def _replace(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Erst eine Kopie daneben schreiben, dann tauschen: ein abgebrochener Lauf
    (Platte voll, Strom weg) hinterlaesst keine leere oder halbe Datei."""
    tmp = path.with_name(path.name + ".tmp")
    # Rechte der alten Datei behalten (eine CSV mit 0600 bliebe sonst nicht
    # privat); neue Dateien (Salz, Verzeichnis) nur fuer den Eigentuemer. Die
    # Kopie ist ab dem Anlegen privat, erst dann kommt der Inhalt hinein.
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
    """Mit der Loeschfrist fallen auch die IDs geloeschter Anrufe aus dem
    Verzeichnis."""
    return {k: v for k, v in known.items() if v["day"] >= cutoff.isoformat()}


def _today() -> date:
    """Heute in Ortszeit; die Tests setzen das Datum fest."""
    return datetime.now(ZoneInfo("Europe/Berlin")).date()


def purge(text: str, cutoff: date) -> str:
    """Das Protokoll ohne Eintraege vor `cutoff` (Loeschfrist, DSFA D12).

    Arbeitet auf den Zeilen der CSV, damit alles andere unveraendert bleibt.
    Nur fuer eine Datei, die `parse` ohne Fehler gelesen hat."""
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))
    header, body = rows[0], rows[1:]
    at = [_norm(h) for h in header].index("date")
    # Leerzeilen, auch nur aus Leerzeichen, ueberspringt parse; hier ebenso.
    keep = [
        r for r in body if any(c.strip() for c in r) and _parse_date(r[at]) >= cutoff
    ]
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(keep)
    return out.getvalue()


def _retention_days(value: int | None) -> int | None:
    """Frist aus --frist-tage, sonst CALL_LOG_RETENTION_DAYS aus Umgebung oder
    .env (ueber die Settings der API, wie jede andere Einstellung), sonst keine.
    Settings frisch bauen: die Umgebung kann sich seit dem Import geaendert haben."""
    if value is not None:
        return value
    return Settings().call_log_retention_days


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
    parser.add_argument(
        "--frist-tage",
        type=int,
        help="Loeschfrist in Tagen; Standard aus CALL_LOG_RETENTION_DAYS",
    )
    parser.add_argument(
        "--loeschen",
        action="store_true",
        help="Eintraege ausserhalb der Frist wirklich aus der CSV entfernen",
    )
    args = parser.parse_args(argv)
    try:
        days = _retention_days(args.frist_tage)
    except ValueError:
        print("CALL_LOG_RETENTION_DAYS ist keine ganze Zahl.", file=sys.stderr)
        return 2
    if days is not None and days < 1:
        print("Die Frist muss mindestens 1 Tag sein.", file=sys.stderr)
        return 2
    if args.loeschen and days is None:
        print(
            "--loeschen braucht eine Frist: --frist-tage N oder CALL_LOG_RETENTION_DAYS.",
            file=sys.stderr,
        )
        return 2

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
    # Vor jedem Schreiben: ein unlesbares Verzeichnis bleibt, wie es ist.
    known_path = args.file.with_name(".call_log_ids.json")
    try:
        known = _load_known(known_path)
    except ValueError as exc:
        print(
            f"ID-Verzeichnis nicht lesbar: {known_path} ({exc}). Nichts geaendert; "
            "Datei reparieren oder aus der Sicherung holen",
            file=sys.stderr,
        )
        return 2
    salt = ""
    if args.cases:
        try:
            salt = _salt(args.file)
        except ValueError as exc:
            print(
                f"Salz nicht lesbar: {args.file.with_name('.call_log_salt')} "
                f"({exc}). Nichts geaendert; aus der Sicherung holen, ein neues "
                "Salz aendert alle Fall-IDs",
                file=sys.stderr,
            )
            return 2
    if days is not None:
        cutoff = _today() - timedelta(days=days)
        old = [e for e in entries if e.day < cutoff]
        print(
            f"Loeschfrist: {len(old)} Eintraege aelter als {days} Tage "
            f"(vor {cutoff:%d.%m.%Y})"
        )
        if args.loeschen:
            if old:
                _replace(args.file, purge(text, cutoff), encoding="utf-8-sig")
                entries = [e for e in entries if e.day >= cutoff]
                print(f"Geloescht: {len(old)} Eintraege aus {args.file}")
            # Auch ohne alte Zeilen: eine von Hand entfernte Zeile laeuft im
            # Verzeichnis sonst nie ab.
            kept = _forget_before(known, cutoff)
            if kept != known:
                known = kept
                _save_known(known_path, known)
    print(report(entries))
    if args.cases:
        written = write_cases(entries, args.cases, salt, known)
        _save_known(known_path, known)
        print(f"Eval-Entwuerfe: {written} nach {args.cases}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
