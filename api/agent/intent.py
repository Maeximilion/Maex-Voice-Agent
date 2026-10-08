"""What the guest called for, heard in their own sentence (docs/05 §5).

A model keeps nothing between turns, and `intent` is no guest slot it could
write: until a draft existed, the state of the next turn did not say what the
call was about. On a real model "Es zwölf bitte", one turn after "ich möchte
etwas zum Abholen bestellen", was read as a table for twelve (07.10.2026). So
the core hears the wish itself, before the model, the way `escalation.py`
hears its triggers: plain words, no understanding of language, no tokens.

It is a hint in the state, not a decision: which tool is called stays with the
model, and what an order or a booking may hold stays with `guards.py`. Strict
on purpose: a wish that is missed leaves the state as it was, which is what
every call got before; a wish that is wrongly heard stands in every turn after
it. So a sentence that names two wishes, rules something out or names none
says nothing.

The stand-in model in `sim/` keeps its own word lists, as it does for the yes
(`agent/consent.py`): it is not the thing this rule is there for.

ponytail: the word lists are hand-made. A wish in other words ("ich hole es
dann ab", "wir kommen zu viert") is missed. Add a word when the call log
shows it.
"""

import re
from typing import Literal

HeardIntent = Literal["pickup", "reservation"]

# Anywhere in a word: Abholung, abholen, abzuholen, Selbstabholer, mitnehmen.
_PICKUP = re.compile(r"ab(?:zu)?hol|mit(?:zu)?nehm")
# Whole words: a guest named Tischler or Platzer asks for no table, nor does
# "Vegetarisches" or "Mittagstisch". "reservier" anywhere (Tischreservierung,
# reserviert), and not shorter: a Gran Reserva is a wine.
_TABLE = re.compile(r"\b(?:tisch(?:e[ns]?)?|platz|pl(?:ä|ae)tze)\b|reservier")
# Not offered by phone yet. Heard so that "bestellen, zum Liefern" is not read
# as a pickup; a value of its own comes with delivery (T-6.5).
_DELIVERY = re.compile(r"liefer")
# "Nicht zum Abholen, wir essen bei Ihnen" names what the guest does not want.
# ponytail: any negation silences the sentence, also "die 23 zum Abholen, aber
# nicht scharf". That sentence names a dish, and the order then stands in the
# state (`cart`).
_NEGATION = re.compile(r"\b(?:nicht|kein)")
# "bestellen" alone is no pickup, a guest also orders a table (Codex PR #130):
# it counts only when nothing else is named.
# ponytail: until delivery is built an order by phone is a pickup (T-6.5).
_ORDER = re.compile(r"bestell")


def heard(text: str, known: str | None = None) -> HeardIntent | None:
    """The one wish the sentence names, or None. `known` is the wish the state
    holds already: "bestellen" alone starts a pickup only in a call that has
    none yet and never talks a known wish away ("können wir das Essen
    vorbestellen?" in a call about a table)."""
    lowered = text.lower()
    pickup = bool(_PICKUP.search(lowered))
    table = bool(_TABLE.search(lowered))
    if (pickup and table) or _DELIVERY.search(lowered) or _NEGATION.search(lowered):
        return None
    if pickup:
        return "pickup"
    if table:
        return "reservation"
    return "pickup" if known is None and _ORDER.search(lowered) else None
