#!/usr/bin/env python3
"""Eval set for number recognition (rule A, docs/08 §6).

Why it stands alone and is not part of the conversation evals from docs/08 §1:
no model decides here, the code does. The set runs without a database, HTTP or
an LLM in milliseconds and answers exactly one question - does
`sole_item_number` resolve every known sentence the way it must be resolved.

The set began as a net against regressions: the rules for marker, suffix,
quantity and connecting word interlock, and two fixes in a row each broke a
form that had been right before (PR #117). Single test functions do not show
that - a table of all cases does.

Expected values in `cases/nummern.jsonl`:

    "23"      exactly this card number, the search may take it directly
    "!23h"    named as a number but not a valid card form -> not_found; the
              search never falls back to similar names (CLAUDE.md §2 rule 2)
    "?"       not unambiguous -> ambiguous, asking for the one number
    "name"    not a number sentence -> alias and trigram search decide
    "s12|sm12" several card numbers possible ("Sushi zwoelf"); the search
              looks up all of them, and the menu says which one exists

Which prefixes a number can carry ("S12", "SM1") is not in the code; it
follows from the numbers on the menu (T-4.12). This set uses the eval menu
`evals/menu/menu_items.csv`, the same one as the conversation evals.

Usage:

    python -m evals.number_eval           # table, exit 1 when red
    python -m evals.number_eval --quiet   # summary only

A new case is one line in the JSONL file. If a sentence from the phone that is
resolved wrongly today belongs in it, it goes in with the right expectation -
the set is then red until the code is right (CLAUDE.md §9: red case first,
then the fix).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from api.domain.menu.numberwords import (
    CardFormat,
    find_item_number,
    find_item_number_ref,
    sole_item_number,
)

CASES = Path(__file__).parent / "cases" / "nummern.jsonl"
MENU_ITEMS = Path(__file__).parent / "menu" / "menu_items.csv"


def eval_card(path: Path = MENU_ITEMS) -> CardFormat:
    """Card format of the eval menu: prefixes and category words from the numbers."""
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    return CardFormat.from_items(
        (r["number"], r["category"]) for r in rows if r.get("active", "ja") != "nein"
    )


CARD = eval_card()


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
    ref, unclear = sole_item_number(say, CARD)
    if unclear:
        return "?"
    if ref is None:
        return "name"
    return "|".join(ref.cards) if ref.valid else f"!{ref.text}"


def disagreement(say: str) -> str | None:
    """Sagen beide Ausgaenge dasselbe ueber eine genannte, ungueltige Nummer?

    `sole_item_number` treibt die Suche, `find_item_number` die
    Verstaendnisleiter. Wer als Nummer genannt wurde, aber keine Kartenform
    hat, darf auf keinem der beiden Wege zu einer Zahl werden - sonst antwortet
    die Suche `not_found`, waehrend die Leiter dasselbe Wort als Gericht nimmt.
    Genau so war "Nummer A12" in der Suche richtig und ueber `find_item_number`
    Gericht 12 (Codex PR #117, P2).
    """
    ref, unclear = sole_item_number(say, CARD)
    other = find_item_number_ref(say, CARD)
    if ref is not None and not ref.valid:
        # Die Suche kennt die Nummer nicht. Dann darf sie auf dem anderen Weg
        # auch keine Zahl werden.
        wert = find_item_number(say, CARD)
        if wert is not None:
            return f"ungueltig als {ref.text!r}, aber find_item_number gibt {wert}"
        return None
    # Andersherum: meldet die Leiter eine genannte, ungueltige Nummer, darf die
    # Suche den Satz nicht fuer einen reinen Namenssatz halten. Sonst sagt die
    # Leiter "die Nummer 7up gibt es nicht", waehrend die Suche den Alias
    # findet. Eine Rueckfrage der Suche ist dagegen kein Widerspruch - dann ist
    # auf beiden Seiten von einer Nummer die Rede ("Nummer 1000 und 23").
    if other is not None and not other.valid and ref is None and not unclear:
        return (
            f"find_item_number_ref meldet ungueltig {other.text!r}, "
            "sole_item_number sieht gar keine Nummer"
        )
    return None


def run(cases: list[Case]) -> list[tuple[Case, str]]:
    """Alle Faelle, Rueckgabe nur der Abweichungen."""
    failures = [(c, got) for c in cases if (got := resolve(c.say)) != c.expect]
    return failures + [
        (c, bad) for c in cases if (bad := disagreement(c.say)) is not None
    ]


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
