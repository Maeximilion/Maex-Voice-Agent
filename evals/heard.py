"""Judge what a speech-to-text engine heard, against what was said.

    python -m evals.heard RUN_DIR

Reads `plan.json` (the sentences with their expectation) and `heard.json` (one
row per sentence, voice and ear with the transcript) from RUN_DIR and writes
`judged.json`: the same rows with `kind`, `expect`, `got` and `verdict`.
Written for the spike T-10.3 (docs/20 §6); the audio evals of T-10.7 are
meant to judge the same way.

A number sentence is resolved by the code that resolves it on the phone
(`number_eval.resolve`), so the verdict says what the ear would have caused:

    right   the same result as for the sentence that was said
    wrong   a card number the guest did not say, taken without a question:
            the silent error of docs/20 §6
    asked   anything else: a question back, a number that does not exist or
            the name search. Costs a turn, never a wrong dish

An order of several dishes in one sentence (kind `order`, the kit's `--orders`)
gets one verdict for the whole order: `order_verdict`.

`wrong` is counted by the form of the number, whatever the menu holds: "23d"
is a card number the rule takes. `on_card` then says whether the menu has
that dish. If it has, the guest gets another dish read back; if not, the
search answers that the number does not exist. A fuller menu has more
numbers, so the form is the cautious count.

The menu is the eval menu, unless the plan carries one (`"menu"`, the kit's
`--menu`): then its numbers decide what is on the card and which letters a
number can carry, and a number sentence without an expectation is expected to
resolve like the sentence that was said.

A dish name is `right` when its letters arrive in the transcript, whatever
the ear did with spaces, hyphens and capitals; else `asked`. The name search
is more forgiving than that, so this is a lower bound.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api.domain.menu.numberwords import CardFormat, canonical_card, find_quantity
from api.domain.menu.split import raw_pieces, split_positions
from evals.number_eval import CARD, MENU_ITEMS, resolve


@dataclass(frozen=True)
class Menu:
    """What a run is judged against: which letters a card number can carry and
    which numbers are dishes."""

    card: CardFormat
    dishes: frozenset[str]

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[str, str]]) -> Menu:
        """From (number, category) of the active dishes."""
        rows = list(rows)
        numbers = frozenset(canonical_card(number) for number, _ in rows)
        return cls(CardFormat.from_items(rows), numbers)


def eval_dishes(path: Path = MENU_ITEMS) -> frozenset[str]:
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    return frozenset(
        canonical_card(r["number"]) for r in rows if r.get("active", "ja") != "nein"
    )


DISHES = eval_dishes()
EVAL = Menu(CARD, DISHES)


def dishes(result: str, menu: Menu = EVAL) -> frozenset[str]:
    """The dishes of the menu a result of the number rule names."""
    if result in ("?", "name") or result.startswith("!"):
        return frozenset()
    cards = (card.rstrip("?") for card in result.split("|"))
    return frozenset(canonical_card(card) for card in cards) & menu.dishes


def dish(result: str, menu: Menu = EVAL) -> str | None:
    """The one dish of the menu a result leads to without a question. With
    several the search asks which one, with none it says the number does not
    exist, and with a number marked "?" (heard through a letter word) the
    search asks before it takes either candidate."""
    found = dishes(result, menu)
    return next(iter(found)) if len(found) == 1 and "?" not in result else None


def number_verdict(expect: str, heard: str, menu: Menu = EVAL) -> tuple[str, str]:
    """Verdict and what the number rule made of the transcript."""
    got = resolve(heard, menu.card)
    one = dish(got, menu)
    # "Sushi zwoelf" said and "S12" heard are two results and one dish.
    if got == expect or (one is not None and one == dish(expect, menu)):
        return "right", got
    taken = got not in ("?", "name") and not got.startswith("!")
    # "Sushi eins" where the menu has S1 and SM1: the search asks which one.
    # "Ist eins", heard through a letter word: the search asks whatever the
    # menu has, so it is never a number taken without a question.
    asks = len(dishes(got, menu)) > 1 or "?" in got
    return ("wrong" if taken and not asks else "asked"), got


def positions(heard: str, menu: Menu = EVAL) -> list[str]:
    """The positions of a sentence, as the conversation finds them
    (`search.position_parts`), for sentences of card numbers.

    First by the sentence alone, then each part with the menu: "Einmal die 36a
    und die S37" stays whole by the sentence, because "die S37" opens no
    position without the menu, and every piece at the separators is a position
    if each of them is one card number - one that leads to a dish, clearly or
    through a heard letter, or one the menu does not have, which the search
    then says. A piece that is no single card number keeps the sentence whole,
    and the search asks. A piece that is a dish name is not looked up here:
    this judge has no name search, so such a sentence stays whole, which is
    the cautious verdict.
    """
    parts = split_positions(heard)
    if len(parts) > 1:
        return [position for part in parts for position in positions(part, menu)]
    pieces = raw_pieces(heard)
    if len(pieces) <= 1:
        return parts
    for piece in pieces:
        got = resolve(piece, menu.card)
        several = len(dishes(got, menu)) > 1 and "?" not in got
        if got in ("?", "name") or several:
            return parts
    return pieces


def order_verdict(expect: list[list], heard: str, menu: Menu = EVAL) -> dict[str, Any]:
    """An order of several dishes said in one sentence, judged as a whole.

    The transcript is split into positions the way the conversation splits a
    sentence (`positions`), and each position goes through the number rule. A
    position that leads to one dish without a question is taken, with its
    quantity; every other one ends in a question or in "that number does not
    exist".

    - right: every dish with its quantity, and nothing else.
    - wrong: a dish or a quantity is taken that was not said, or a dish is
      missing and no position asks - the guest only hears it at the read-back.
    - asked: the rest. The conversation goes on with a question.
    """
    want = Counter((canonical_card(number), quantity) for number, quantity in expect)
    taken: Counter[tuple[str, int]] = Counter()
    said = []
    pieces = positions(heard, menu)
    # Nothing heard is nothing taken: the agent asks what the guest said.
    asks = not pieces
    for piece in pieces:
        got = resolve(piece, menu.card)
        quantity = find_quantity(piece) or 1
        one = dish(got, menu)
        said.append(f"{quantity}x{got}")
        if one is None:
            asks = True
        else:
            taken[(one, quantity)] += 1
    extra = taken - want
    missing = want - taken
    if extra or (missing and not asks):
        verdict = "wrong"
    else:
        verdict = "asked" if missing or asks else "right"
    return {
        "verdict": verdict,
        "expect": ", ".join(f"{quantity}x{number}" for number, quantity in expect),
        "got": ", ".join(said),
        # A dish of the menu the guest did not order, as for a single number.
        "on_card": bool(extra),
        "size": len(expect),
        "positions_right": sum((taken & want).values()),
    }


def dish_verdict(name: str, heard: str) -> str:
    return "right" if _letters(name) in _letters(heard) else "asked"


def _letters(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def judge(plan: dict, heard: dict) -> list[dict]:
    sentences = {row["id"]: row for row in plan["sentences"]}
    rows = plan.get("menu")
    menu = Menu.from_rows((r["number"], r["category"]) for r in rows) if rows else EVAL
    judged = []
    for row in heard["heard"]:
        said = sentences[row["id"]]
        expect = said["expect"]
        if said["kind"] == "order":
            order = order_verdict(expect, row["text"], menu)
            # The sentence as it was said, by the same rule: what no ear can
            # do better than.
            order["as_said"] = order_verdict(expect, said["say"], menu)["verdict"]
            judged.append({**row, "kind": "order", "say": said["say"], **order})
            continue
        if said["kind"] == "number":
            if expect is None:
                # Another menu than the one the expectations were written for:
                # right is what the sentence that was said resolves to on it.
                expect = resolve(said["say"], menu.card)
            verdict, got = number_verdict(expect, row["text"], menu)
        else:
            verdict, got = dish_verdict(expect, row["text"]), ""
        judged.append(
            {
                **row,
                "kind": said["kind"],
                "say": said["say"],
                "expect": expect,
                "got": got,
                "verdict": verdict,
                "on_card": verdict == "wrong" and dish(got, menu) is not None,
            }
        )
    return judged


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    run = Path(argv[0])
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    heard = json.loads((run / "heard.json").read_text(encoding="utf-8"))
    (run / "judged.json").write_text(
        json.dumps(judge(plan, heard), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
