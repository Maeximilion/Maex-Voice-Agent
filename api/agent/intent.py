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
says nothing, and `state.note_intent` takes only the first wish of a call.

The stand-in model in `sim/` keeps its own word lists, as it does for the yes
(`agent/consent.py`): it is not the thing this rule is there for.

ponytail: the word lists are hand-made, and words are not understanding. A
wish in other words ("ich hole es dann ab", "haben Sie noch Platz?") is
missed; a pickup word that means something else in the first such sentence of
a call ("ich wollte meine Jacke abholen") is heard as a pickup. If the call
log shows either often, let the model write the wish instead: that needs a
line in the answer format and a run against the baseline (docs/05 §5).
"""

import re
from typing import Literal

HeardIntent = Literal["pickup", "reservation"]

# Anywhere in a word: Abholung, abholen, abzuholen, Selbstabholer, mitnehmen.
_PICKUP = re.compile(r"ab(?:zu)?hol|mit(?:zu)?nehm")
# "Tisch" as a whole word: a guest named Tischler asks for no table, nor does
# "Asiatisches" or "Mittagstisch". "reservier" anywhere (Tischreservierung,
# reserviert), and not shorter: a Gran Reserva is a wine. "Platz" is no table
# word: it also stands in every second address.
_TABLE = re.compile(r"\btisch(?:e[ns]?)?\b|reservier")
# Not offered by phone yet. Heard so that "bestellen, zum Liefern" is not read
# as a pickup; a value of its own comes with delivery (T-6.5).
_DELIVERY = re.compile(r"liefer")
# "Nicht zum Abholen, wir essen bei Ihnen" names what the guest does not want.
# ponytail: any of these words silences the sentence, also "die 23 zum
# Abholen, aber ohne Zwiebeln". That sentence names a dish, and the order then
# stands in the state (`cart`).
_NEGATION = re.compile(r"\b(?:nicht|kein|nein|ohne|(?:an)?statt)")
# "bestellen" alone is no pickup, a guest also orders a table (Codex PR #130):
# it counts only when nothing table-like is named.
# ponytail: until delivery is built an order by phone is a pickup (T-6.5).
_ORDER = re.compile(r"bestell")
# Anything that may be a table, compounds included ("Vierertisch",
# "Sitzplätze"): not sure enough to be heard as one, but enough to make a
# pickup or an order word next to it say nothing. It also holds "tisch" in
# "etwas Asiatisches zum Mitnehmen": that wish is missed, which is the safe side.
_TABLE_LIKE = re.compile(r"tisch|platz|pl(?:ä|ae)tz|reserv")


def heard(text: str) -> HeardIntent | None:
    """The one wish the sentence names, or None."""
    lowered = text.lower()
    if _DELIVERY.search(lowered) or _NEGATION.search(lowered):
        return None
    table_like = bool(_TABLE_LIKE.search(lowered))
    if _PICKUP.search(lowered):
        # Next to anything table-like it is two wishes, or one that words
        # cannot tell ("noch Platz für zwei, oder sollen wir es mitnehmen?").
        return None if table_like else "pickup"
    if _TABLE.search(lowered):
        return "reservation"
    return "pickup" if _ORDER.search(lowered) and not table_like else None
