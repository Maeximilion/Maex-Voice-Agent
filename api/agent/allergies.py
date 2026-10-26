"""The caller's allergies, read and written by the core (T-11.1, legal check L13).

An allergy never travels in free text the model writes. Five review rounds on
PR #368 each found another sentence shape where re-reading a model's note lost
a restriction or put it on the wrong person, so the core reads the allergy
from the guest's own sentence on every turn (`note_allergies`), with the
parser of the menu search, and keeps it as `state.Allergy`: the ingredient,
whose it is, and the dish it was said with. When a draft goes out, the core
writes it onto the order line, the order or the table itself (`noted_call`):
"WICHTIG: Keine <Zutat>. Grund: Allergie" after the caller's explicit "ja" to
`consent.ALLERGY_QUESTION`, "ohne <Zutat>" without it, and "ohne <Zutat>" for
another adult in any case. Before that, the core asks what it still lacks
(`question`): "Wogegen?" for an allergy not read in full, then the consent. A
note the model writes with an allergy word is refused (`guards.py`).
"""

import re
from typing import Any

from sqlalchemy.orm import Session

from api.agent.consent import ALLERGY_QUESTION
from api.agent.llm import ToolCall
from api.agent.state import Allergy, ConversationState
from api.domain.menu.items import active_numbers
from api.domain.menu.numberwords import CardFormat, canonical_card
from api.domain.menu.search import SAY_ALLERGY_WHICH
from api.domain.menu.wishes import (
    ALLERGY_NOTE,
    classify_wish,
    mentions_allergy,
    names_an_order,
    only_denies,
    which_answer,
    wish_candidates,
)
from api.schemas.menu import Wish
from api.schemas.orders import NOTE_MAX

# The calls that store a note: on an order line, on the whole order, on a table.
NOTED = frozenset({"draft_order", "create_reservation"})
# The question for the ingredient as the search, the stand-in or the core puts
# it, at the end of what the guest heard last ("Gern, Nummer 13 Pho Bo.
# Wogegen sind Sie allergisch?", "Wogegen sind Sie bei Pho Bo allergisch?").
_ASKED_WHICH = re.compile(r"Wogegen sind Sie(?: bei (?P<dish>.+?))? allergisch\?\s*$")


def note_allergies(session: Session, state: ConversationState, text: str) -> None:
    """Reads the allergy of the guest's sentence into the state. Call it on
    every turn, before anything else is decided on the sentence: a draft the
    core holds for its question takes its notes from the state when it goes
    out, so an allergy said in any sentence reaches it (Codex PR #368, P1:
    "Ja, und ich bin auch gegen Sesam allergisch" after "Sind Sie noch da?").
    A sentence that only denies an allergy takes none back: a restriction
    too many is the safe side."""
    asked = _ASKED_WHICH.search(state.last_say or "")
    if asked is not None:
        _answer(session, state, text, asked.group("dish"))
        return
    if not mentions_allergy(text):
        return
    wish = _read(text)
    if wish is not None:
        _add(state, Allergy(ingredient=wish.ingredient, own=not wish.another_adult))


def _read(text: str) -> Wish | None:
    """The allergy of a whole sentence, None for none. Read whole, and read
    as the menu search reads it, behind the dish (`wish_candidates`). Whole,
    a list before the allergy word keeps its allergens ("Milch, Ei und
    Nussallergie"), but a dish or a name there becomes one too ("Pho Bo und
    Sommerrollen, Erdnussallergie", "Auf Müller, Erdnussallergie"); behind
    the dish, it is the other way round. Where the two differ, the allergy is
    not read in full and the guest is asked "Wogegen?", never guessed.
    ponytail: one question more for a list said in one breath; the search
    with the menu could tell the dish apart if the call log shows the need."""
    whole = classify_wish(text, [])
    if whole is None or whole.kind != "allergy":
        return None
    if whole.ingredient is None:
        # A dish in front keeps a denial from counting when read whole:
        # "Pho Bo, ich habe keine Allergie" would ask "Wogegen?".
        return None if only_denies(text) else whole
    said = next((w for _, w, _ in wish_candidates(text) if mentions_allergy(w)), None)
    behind = classify_wish(said, []) if said is not None else None
    if behind is None or behind.ingredient in (None, whole.ingredient):
        # Nothing behind the dish to compare with: a list cut in two ("Ich
        # bin gegen Milch, Ei und Nuesse allergisch") reads only whole.
        return whole
    return whole.model_copy(
        update={
            "ingredient": None,
            "another_adult": whole.another_adult or behind.another_adult,
        }
    )


def _answer(
    session: Session, state: ConversationState, text: str, dish: str | None
) -> None:
    """The answer to "Wogegen?": fills the allergy it asked for, the one of
    the dish the question named or else the first still open. An answer that
    names nothing ("Keine.") takes that allergy back; one the parser cannot
    read leaves it open. One that comes without an open allergy (the model
    asked on its own) is noted all the same."""
    rows = active_numbers(session, state.tenant_id)
    numbers = frozenset(canonical_card(number) for number, _ in rows)
    if names_an_order(text, CardFormat.from_items(rows), numbers):
        return
    answer, _ = which_answer(text)
    item = next(
        (
            id_
            for id_, seen in state.seen_items.items()
            if dish and seen["name"] == dish
        ),
        None,
    )
    still_open = [a for a in state.allergies if a.ingredient is None]
    asked = next((a for a in still_open if item and a.item == item), None) or next(
        iter(still_open), None
    )
    if answer is None:
        return
    if asked is not None:
        state.allergies.remove(asked)
    if not isinstance(answer, Wish):
        return  # WITHDRAWN
    own = (asked is None or asked.own) and not answer.another_adult
    item = asked.item if asked is not None and asked.item else item
    _add(state, Allergy(ingredient=answer.ingredient, own=own, item=item))


def _add(state: ConversationState, allergy: Allergy) -> None:
    if allergy not in state.allergies:
        state.allergies.append(allergy)


def question(state: ConversationState) -> str | None:
    """What the core asks before a draft goes out, None when nothing: "Wogegen
    sind Sie allergisch?" for an allergy the words did not give in full, then
    the consent to note an allergy with its reason, once per call (L13)."""
    if any(allergy.ingredient is None for allergy in state.allergies):
        return SAY_ALLERGY_WHICH
    if state.allergy_consent is None and any(a.own for a in state.allergies):
        return ALLERGY_QUESTION
    return None


def noted_call(state: ConversationState, call: ToolCall) -> ToolCall:
    """The draft with the guest's allergies written onto it: onto the line of
    the dish each was said with, any other onto the note of the whole order,
    all of them onto the note of a table. Arguments in a wrong form are left
    to the validation in dispatch."""
    read = [allergy for allergy in state.allergies if allergy.ingredient]
    if call.name not in NOTED or not read:
        return call
    args = dict(call.args)
    if call.name == "create_reservation":
        _set_note(args, _noted(state, args.get("note"), read, limit=None))
        return ToolCall(call.name, args)
    items = args.get("items")
    if not isinstance(items, list):
        return call
    on_lines = set()
    lines = []
    for item in items:
        if isinstance(item, dict):
            item_id = str(item.get("menu_item_id"))
            on_lines.add(item_id)
            item = dict(item)
            own = [allergy for allergy in read if allergy.item == item_id]
            _set_note(item, _noted(state, item.get("note"), own))
        lines.append(item)
    args["items"] = lines
    rest = [allergy for allergy in read if allergy.item not in on_lines]
    _set_note(args, _noted(state, args.get("note"), rest))
    return ToolCall(call.name, args)


def _set_note(args: dict[str, Any], note: Any) -> None:
    if note is None:
        args.pop("note", None)
    else:
        args["note"] = note


def _noted(
    state: ConversationState,
    note: Any,
    allergies: list[Allergy],
    limit: int | None = NOTE_MAX,
) -> Any:
    """`note` with `allergies` in front, as the kitchen reads them (E14): "WICHTIG:
    Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln" with the caller's "ja",
    "ohne Erdnuss, ohne Zwiebeln" without it. One too long for its field keeps
    the allergies alone: the model's part is the one to lose, as in
    `wishes.kitchen_note`."""
    if not allergies or (note is not None and not isinstance(note, str)):
        return note
    consented = state.allergy_consent == "yes"
    reasoned = any(allergy.own and consented for allergy in allergies)
    notices = [
        ALLERGY_NOTE.format(ingredient=allergy.ingredient)
        if allergy.own and consented
        else f"ohne {allergy.ingredient}"
        for allergy in allergies
    ]
    said = (note or "").strip()
    parts = list(dict.fromkeys(notices + ([said] if said else [])))

    def joined(parts: list[str]) -> str:
        # Never lowered: the model's part may begin with a noun ("Besteck").
        if reasoned:
            return ". ".join(part[0].upper() + part[1:] for part in parts)
        return ", ".join(parts)

    full = joined(parts)
    if limit is not None and len(full) > limit and said:
        return joined(list(dict.fromkeys(notices)))
    return full
