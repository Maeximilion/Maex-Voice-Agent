"""Is this sentence an explicit yes? (CLAUDE.md §2 rule 3)

One detector for two users: `guards.py` lets a `confirm` through only on a yes,
and `evals/recorder.py` counts a `confirm` without one as a hard violation.
Both must agree, or the eval would report what the core already stops, or
miss what it lets through. The stand-in model in `sim/` keeps its own
detection, so it is still checked by a rule that is not its own.

The rule: a yes is a sentence that is nothing but the assent. It holds an
assent word, and every other word carries no order ("bitte", "danke", "so").
"Ja, und noch eine Cola", "Okay, die 23" and "Ich hätte gerne noch eine Suppe"
hold a yes word and are the start of a change. One closed rule instead of a
growing list of what must not follow a yes (as for card numbers, docs/04).

Strict on purpose: a yes that is missed costs the guest one more question, a
yes that is wrongly heard books a table or an order nobody agreed to.
ponytail: the word lists are hand-made. An unusual yes ("von mir aus") is
missed; add the word when the call log shows it, never a pattern that lets
free text through.
"""

import re

_YES = frozenset(
    {
        "ja",
        "jawohl",
        "jo",
        "genau",
        "richtig",
        "passt",
        "stimmt",
        "korrekt",
        "okay",
        "ok",
        "einverstanden",
        "bestaetigt",
        "bestätigt",
    }
)
# Words that may stand next to the assent. "gern" and "gerne" are here and not
# above: alone they are how an order begins ("Ich hätte gerne ..."), next to a
# yes they are politeness (Codex PR #222, P1).
_FILLER = frozenset(
    {
        "gern",
        "gerne",
        "bitte",
        "danke",
        "dankeschön",
        "dankeschoen",
        "schön",
        "schoen",
        "vielen",
        "dank",
        "so",
        "das",
        "alles",
        "ist",
        "es",
        "gut",
        "super",
        "prima",
        "perfekt",
        "wunderbar",
        "klar",
        "in",
        "ordnung",
        "doch",
        "natürlich",
        "natuerlich",
        "sicher",
    }
)
# "nicht" and "kein" are never filler: they turn a yes around ("stimmt nicht",
# "Ja, kein Reis"). These two phrases are the exception and stay a yes
# (review PR #142).
_HARMLESS = ("kein problem", "nicht schlecht")
_WORD = re.compile(r"[a-zäöüß]+")


def is_yes(text: str) -> bool:
    """An assent word, and nothing else but filler. A digit is never part of a
    yes: "Ja, 2" changes a quantity."""
    lowered = text.lower()
    if any(char.isdigit() for char in lowered):
        return False
    for phrase in _HARMLESS:
        lowered = lowered.replace(phrase, " ")
    words = _WORD.findall(lowered)
    return any(word in _YES for word in words) and all(
        word in _YES or word in _FILLER for word in words
    )
