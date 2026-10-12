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

`wrong` is counted by the form of the number, whatever the menu holds: "23d"
is a card number the rule takes. `on_card` then says whether the eval menu has
that dish. If it has, the guest gets another dish read back; if not, the
search answers that the number does not exist. A fuller menu has more
numbers, so the form is the cautious count.

A dish name is `right` when its letters arrive in the transcript, whatever
the ear did with spaces, hyphens and capitals; else `asked`. The name search
is more forgiving than that, so this is a lower bound.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from api.domain.menu.numberwords import canonical_card
from evals.number_eval import MENU_ITEMS, resolve


def eval_dishes(path: Path = MENU_ITEMS) -> frozenset[str]:
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    return frozenset(
        canonical_card(r["number"]) for r in rows if r.get("active", "ja") != "nein"
    )


DISHES = eval_dishes()


def dishes(result: str) -> frozenset[str]:
    """The dishes of the eval menu a result of the number rule names."""
    if result in ("?", "name") or result.startswith("!"):
        return frozenset()
    return frozenset(canonical_card(card) for card in result.split("|")) & DISHES


def dish(result: str) -> str | None:
    """The one dish of the eval menu a result leads to; with several the search
    asks which one, with none it says the number does not exist."""
    found = dishes(result)
    return next(iter(found)) if len(found) == 1 else None


def number_verdict(expect: str, heard: str) -> tuple[str, str]:
    """Verdict and what the number rule made of the transcript."""
    got = resolve(heard)
    # "Sushi zwoelf" said and "S12" heard are two results and one dish.
    if got == expect or (dish(got) is not None and dish(got) == dish(expect)):
        return "right", got
    taken = got not in ("?", "name") and not got.startswith("!")
    # "Sushi eins" where the menu has S1 and SM1: the search asks which one.
    return ("wrong" if taken and len(dishes(got)) < 2 else "asked"), got


def dish_verdict(name: str, heard: str) -> str:
    return "right" if _letters(name) in _letters(heard) else "asked"


def _letters(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def judge(plan: dict, heard: dict) -> list[dict]:
    sentences = {row["id"]: row for row in plan["sentences"]}
    judged = []
    for row in heard["heard"]:
        said = sentences[row["id"]]
        if said["kind"] == "number":
            verdict, got = number_verdict(said["expect"], row["text"])
        else:
            verdict, got = dish_verdict(said["expect"], row["text"]), ""
        judged.append(
            {
                **row,
                "kind": said["kind"],
                "say": said["say"],
                "expect": said["expect"],
                "got": got,
                "verdict": verdict,
                "on_card": verdict == "wrong" and dish(got) is not None,
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
