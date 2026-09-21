#!/usr/bin/env python3
"""Eval-Satz fuer die Nummernerkennung (Regel A, docs/08 §6).

Warum eigenstaendig und nicht Teil der Gespraechs-Evals aus docs/08 §1: hier
faellt keine Entscheidung eines Modells, sondern eine des Codes. Der Satz
laeuft ohne Datenbank, ohne HTTP und ohne LLM in Millisekunden und beantwortet
genau eine Frage - loest `sole_item_number` jeden bekannten Satz so auf, wie er
aufgeloest werden muss.

Der Satz ist als Netz gegen Regressionen entstanden: die Regeln fuer Marker,
Endung, Menge und Verbindungswort greifen ineinander, und zwei Korrekturen in
Folge haben je eine frueher richtige Form wieder kaputt gemacht (PR #117).
Einzelne Testfunktionen zeigen das nicht - eine Tabelle aller Faelle schon.

Erwartungswerte in `cases/nummern.jsonl`:

    "23"      genau diese Kartennummer, die Suche darf sie direkt nehmen
    "!23g"    als Nummer genannt, aber keine gueltige Kartenform -> not_found;
              die Suche weicht nie auf aehnliche Namen aus (CLAUDE.md §2 Regel 2)
    "?"       nicht eindeutig -> ambiguous mit der Frage nach der einen Nummer
    "name"    kein Nummernsatz -> Alias- und Trigram-Suche entscheiden

Aufruf:

    python -m evals.number_eval           # Tabelle, Exit 1 bei rot
    python -m evals.number_eval --quiet   # nur die Zusammenfassung

Ein neuer Fall ist eine Zeile in der JSONL-Datei. Gehoert ein Satz vom Telefon
dazu, der heute falsch aufgeloest wird, kommt er mit der richtigen Erwartung
hinein - der Satz ist dann rot, bis der Code stimmt (CLAUDE.md §9: erst der
rote Fall, dann der Fix).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from api.domain.menu.numberwords import sole_item_number

CASES = Path(__file__).parent / "cases" / "nummern.jsonl"


@dataclass(frozen=True)
class Case:
    say: str
    expect: str
    why: str


def load(path: Path = CASES) -> list[Case]:
    """Faelle aus der JSONL-Datei, Leerzeilen und # -Zeilen uebersprungen."""
    cases: list[Case] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        row = json.loads(line)
        cases.append(Case(row["say"], row["expect"], row.get("why", "")))
    return cases


def resolve(say: str) -> str:
    """Das Ergebnis von `sole_item_number` in der Sprache der Erwartungswerte."""
    ref, unclear = sole_item_number(say)
    if unclear:
        return "?"
    if ref is None:
        return "name"
    return ref.text if ref.valid else f"!{ref.text}"


def run(cases: list[Case]) -> list[tuple[Case, str]]:
    """Alle Faelle, Rueckgabe nur der Abweichungen."""
    return [(c, got) for c in cases if (got := resolve(c.say)) != c.expect]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="nur die Summe")
    parser.add_argument("--file", type=Path, default=CASES)
    args = parser.parse_args(argv)

    cases = load(args.file)
    failures = run(cases)

    if failures and not args.quiet:
        width = max(len(c.say) for c, _ in failures)
        print("Abweichungen:\n")
        for case, got in failures:
            print(f"  {case.say:<{width}}  erwartet {case.expect:<16} bekommen {got}")
            if case.why:
                print(f"  {'':<{width}}  {case.why}")
        print()

    green = len(cases) - len(failures)
    print(f"Nummernerkennung: {green}/{len(cases)} Faelle richtig")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
