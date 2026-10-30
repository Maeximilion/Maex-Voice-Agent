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

from api.agent import escalation
from api.agent.consent import ALLERGY_QUESTION
from api.agent.llm import ToolCall
from api.agent.state import Allergy, ConversationState
from api.domain.menu.items import active_numbers
from api.domain.menu.numberwords import CardFormat, canonical_card
from api.domain.menu.search import SAY_ALLERGY_WHICH, SAY_ALLERGY_WHICH_FOR
from api.domain.menu.wishes import (
    ALLERGY_NOTE,
    allergies_named,
    answers_only,
    classify_wish,
    mentions_allergy,
    names_an_order,
    of_another_adult,
    only_denies,
    read_in_full,
    which_answer,
    wish_candidates,
)
from api.schemas.menu import Wish
from api.schemas.orders import NOTE_MAX
from api.schemas.reservations import RESERVATION_NOTE_MAX

# The calls that store a note: on an order line, on the whole order, on a table.
NOTED = frozenset({"draft_order", "create_reservation"})
# The question for the ingredient as the search, the stand-in or the core puts
# it, at the end of what the guest heard last ("Gern, Nummer 13 Pho Bo.
# Wogegen sind Sie allergisch?", "Wogegen sind Sie bei Pho Bo allergisch?").
_SENTENCE = escalation.SENTENCE
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
    before = _open(state)
    if asked is not None:
        # The first sentence answers the question; a later sentence with an
        # allergy word of its own is read as any other, with its own owner
        # ("Erdnüsse. Mein Mann hat eine Sesamallergie."; Codex PR #368, P1).
        # A sentence without one carries on the one before it ("Erdnüsse.
        # Und gegen Sesam.", Codex PR #368, P1).
        said: list[str] = []
        for sentence in _SENTENCE.split(text.strip()):
            if said and not mentions_allergy(sentence):
                said[-1] += f" {sentence}"
            else:
                said.append(sentence)
        _answer(session, state, said[0], asked.group("dish"))
        for sentence in said[1:]:
            _note(state, sentence)
    else:
        _note(state, text)
    if before - _open(state):
        # An open allergy was answered or taken back: the next one starts
        # with its own answers (#207).
        state.which_asks = 0
    elif asked is not None and before and not _asks_back(text):
        state.which_asks += 1


def _asks_back(text: str) -> bool:
    """A question of the guest's own ("Wie bitte?", "Sind Sie ein Roboter")
    answers no "Wogegen?" and costs no answer (Codex PR #414)."""
    return (
        escalation.is_question(text)
        or escalation.without_human_question(text) is not None
    )


def _open(state: ConversationState) -> set[int]:
    """The allergies still waiting for "Wogegen?", by identity: an answer
    replaces the one it fills (`_answer`)."""
    return {id(allergy) for allergy in state.allergies if allergy.ingredient is None}


def _note(state: ConversationState, text: str) -> None:
    """An allergy said without a question of the core (`note_allergies`)."""
    if not mentions_allergy(text):
        return
    whole = _read(text)
    said = [part for part in _SENTENCE.split(text) if mentions_allergy(part)]
    readings = [whole]
    if len(said) > 1:
        # Each sentence on its own, with its own owner: read whole, the
        # parser takes the first allergy's owner and phrase for all of them
        # ("Ich habe eine Erdnussallergie. Meine Frau hat eine Sesamallergie",
        # "Erdnussallergie. Keine Milch wegen Unverträglichkeit"; Codex PR
        # #368, P1). What the whole leaves unread is still asked about: a
        # sentence without an allergy word may carry the list on ("Und gegen
        # Sesam").
        readings = [_read(part) for part in said]
        if whole is not None and whole.ingredient is None:
            others = any(r is not None and r.another_adult for r in readings)
            readings.append(whole.model_copy(update={"another_adult": others}))
    for wish in readings:
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
    if allergies_named(text) > 1 and of_another_adult(text):
        # Two allergies in one sentence, one of them another adult's ("Ich
        # habe eine Erdnussallergie, meine Frau eine Sesamallergie"): which
        # one is whose is not read, so neither carries the reason (Codex PR
        # #368, P1), the safe side for the other adult.
        whole = whole.model_copy(update={"another_adult": True})
    if whole.ingredient is None:
        # A dish in front keeps a denial from counting when read whole:
        # "Pho Bo, ich habe keine Allergie" would ask "Wogegen?".
        return None if only_denies(text) else whole
    # A part that ends in a hyphen carries a list on, no dish is called so
    # ("Nuss- und Sesamallergie").
    said = next(
        (
            wish
            for dish, wish, _ in wish_candidates(text)
            if mentions_allergy(wish) and not dish.endswith("-")
        ),
        None,
    )
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
    asked on its own) is noted all the same. A question back fills nothing,
    with its mark or without it ("Ist das wichtig"): the speech engine drops
    the mark, and the sentence would stand on the kitchen's note."""
    if escalation.is_question(text):
        return
    rows = active_numbers(session, state.tenant_id)
    numbers = frozenset(canonical_card(number) for number, _ in rows)
    if names_an_order(text, CardFormat.from_items(rows), numbers):
        return
    answer, removals = which_answer(text)
    if isinstance(answer, Wish) and mentions_allergy(text):
        # A whole sentence, not a bare ingredient: read as any other, with
        # both readings and the owner of each allergy in it.
        answer = _read(text)
    if isinstance(answer, Wish) and not read_in_full(
        text, " ".join([answer.ingredient or "", *removals])
    ):
        # Names more than it reads ("Erdnüsse, und mein Mann hat eine
        # Sesamallergie" read as sesame, "Erdnüsse. Und gegen Sesam." read
        # as peanut): the question stays open, and what is read with an
        # allergy word is noted with its owner (Codex PR #368).
        _note(state, text)
        return
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


def asked_which(say: str | None) -> bool:
    """Did the sentence the guest heard last ask "Wogegen?"?"""
    return _ASKED_WHICH.search(say or "") is not None


def answered(state: ConversationState, text: str) -> bool:
    """Was the guest's sentence the answer to "Wogegen?" and nothing else,
    with no allergy left open? Then the draft held for it goes out; any
    other sentence goes to the model (Codex PR #368: "Erdnüsse, aber bitte
    erst übermorgen")."""
    if any(allergy.ingredient is None for allergy in state.allergies):
        return False
    return answers_only(text)


def _add(state: ConversationState, allergy: Allergy) -> None:
    """Notes `allergy` once: said again without a dish, it is the same
    restriction, and the draft that carries it stays (`loop.py`). Said for
    another dish, it is one more: each dish keeps its own (Codex PR #368)."""
    said = (allergy.ingredient or "").casefold(), allergy.own
    if allergy.ingredient is None or not any(
        ((a.ingredient or "").casefold(), a.own) == said
        and allergy.item in (None, a.item)
        for a in state.allergies
    ):
        state.allergies.append(allergy)


def question(state: ConversationState) -> str | None:
    """What the core asks before a draft goes out, None when nothing: "Wogegen
    sind Sie allergisch?" for an allergy the words did not give in full, with
    its dish while another one is open too, then the consent to note an
    allergy with its reason, once per call (L13)."""
    still_open = [allergy for allergy in state.allergies if allergy.ingredient is None]
    if still_open:
        dish = state.seen_items.get(still_open[0].item or "", {}).get("name")
        if len(still_open) > 1 and dish:
            return SAY_ALLERGY_WHICH_FOR.format(name=dish)
        return SAY_ALLERGY_WHICH
    if state.allergy_consent is None and any(a.own for a in state.allergies):
        return ALLERGY_QUESTION
    return None


# How many answers to "Wogegen?" may leave the allergy open before the team
# takes the call (#207): one the core cannot read ("Weiß ich nicht") held every
# draft behind the same question until the call ran out of time. A sentence
# put in between (the line's "Sind Sie noch da?", the answer to "Sind Sie ein
# Roboter?") is no answer and costs nothing.
WHICH_ASKS = 2


def asked_out(state: ConversationState, question: str) -> bool:
    """Is `question` "Wogegen?" once more after `WHICH_ASKS` answers that left
    the allergy open? Then the team takes the call instead."""
    return asked_which(question) and state.which_asks >= WHICH_ASKS


def fits(call: ToolCall, written: ToolCall) -> bool:
    """Do the notes the core wrote onto `written`, the draft as the model sent
    it, fit their fields? Too many allergies for one note (#207) would fail
    validation on every draft, and the call would go round until its time ran
    out; the team takes it instead. A note the core left as the model wrote
    it is the model's to shorten, after the validation in dispatch."""
    if call.name not in NOTED:
        return True
    limit = RESERVATION_NOTE_MAX if call.name == "create_reservation" else NOTE_MAX
    return all(
        not isinstance(note, str) or len(note) <= limit or note == before
        for note, before in zip(_notes(call), _notes(written), strict=False)
    )


def _notes(call: ToolCall) -> list[Any]:
    """The note of the draft, then the note of each line."""
    items = call.args.get("items")
    lines = items if isinstance(items, list) else []
    return [call.args.get("note")] + [
        line.get("note") if isinstance(line, dict) else None for line in lines
    ]


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
        _set_note(args, _noted(state, args.get("note"), read, RESERVATION_NOTE_MAX))
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
    limit: int = NOTE_MAX,
) -> Any:
    """`note` with `allergies` in front, as the kitchen reads them (E14): "WICHTIG:
    Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln" with the caller's "ja",
    "ohne Erdnuss, ohne Zwiebeln" without it. One too long for its field keeps
    the allergies alone: the model's part is the one to lose, as in
    `wishes.kitchen_note`. Still too long, the allergies share one notice
    ("WICHTIG: Keine Erdnuss, Sesam und Senf. Grund: Allergie", #207); one
    that does not fit even so is left for `fits`."""
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
    if len(full) <= limit:
        return full
    alone = joined(list(dict.fromkeys(notices)))
    if len(alone) <= limit:
        return alone
    reasons = [a.ingredient for a in allergies if a.own and consented]
    plain = [a.ingredient for a in allergies if not (a.own and consented)]
    shared = [ALLERGY_NOTE.format(ingredient=_listed(reasons))] if reasons else []
    shared += [f"ohne {_listed(plain)}"] if plain else []
    with_said = joined(shared + ([said] if said else []))
    return with_said if len(with_said) <= limit else joined(shared)


def _listed(ingredients: list[str | None]) -> str:
    """'Erdnuss, Sesam und Senf'."""
    said = list(dict.fromkeys(i for i in ingredients if i))
    if len(said) < 2:
        return "".join(said)
    return f"{', '.join(said[:-1])} und {said[-1]}"
