"""What the guest called for, heard in their own sentence (docs/05 §5).

A model keeps nothing between turns, and `intent` is no guest slot it could
write: until a draft existed, the state of the next turn did not say what the
call was about. On a real model "Es zwölf bitte", one turn after "ich möchte
etwas zum Abholen bestellen", was read as a table for twelve (07.10.2026). So
the core hears the wish itself, before the model, the way `escalation.py`
hears its triggers: plain words, no understanding of language, no tokens.

It is a hint in the state, not a decision: which tool is called stays with the
model, and what an order or a booking may hold stays with `guards.py`. A
sentence that names two wishes, or none, says nothing, and the state keeps
what it held.

ponytail: the word lists are hand-made. A wish in other words ("ich hole es
dann ab", "wir kommen zu viert") is missed and the state stays as it was,
which is what every call got before. Add a word when the call log shows it.
"""

import re
from typing import Literal

HeardIntent = Literal["pickup", "reservation"]

# Anywhere in a word: Abholung, abholen, abzuholen, Selbstabholer, mitnehmen.
_PICKUP = re.compile(r"ab(?:zu)?hol|mit(?:zu)?nehm")
# At the start of a word only: "Vegetarisches", "asiatisch" and "Mittagstisch"
# hold "tisch" and ask for no table.
_TABLE = re.compile(r"\b(?:tisch|reserv|platz|plätze)")
# Not offered by phone yet. Heard so that "bestellen, zum Liefern" is not read
# as a pickup; a value of its own comes with delivery (T-6.5).
_DELIVERY = re.compile(r"liefer")
# "bestellen" alone is no pickup, a guest also orders a table (Codex PR #130):
# it counts only when nothing else is named.
# ponytail: until delivery is built an order by phone is a pickup (T-6.5).
_ORDER = re.compile(r"bestell")


def heard(text: str) -> HeardIntent | None:
    """The one wish the sentence names, or None."""
    lowered = text.lower()
    pickup = bool(_PICKUP.search(lowered))
    table = bool(_TABLE.search(lowered))
    if _DELIVERY.search(lowered) or (pickup and table):
        return None
    if pickup:
        return "pickup"
    if table:
        return "reservation"
    return "pickup" if _ORDER.search(lowered) else None
