"""What the core enforces itself, whatever the model returns (CLAUDE.md §2).

The system prompt asks the model to follow the hard rules. A prompt is no
enforcement: a real model can return `confirm` after a "Nein", or put the first
of several candidates into an order. The loop therefore asks here before it
dispatches a tool call, and a refused call goes back to the model as a failed
tool result with a `hint`. It costs one of the turn's tool hops, so a model
that insists ends in the handoff to the team, not in a booking.

- Rule 3: `confirm` only when the guest's current sentence is an explicit yes
  to the draft that was read back before this turn. Read back means that the
  last sentence the guest heard was the draft's `readback` (`note_said`,
  `consent.read_out`). A draft the model built and then talked past is none,
  and neither is one that was read out before another question (#251).
- Rule 2: `draft_order` only with dishes from a clear match of `search_menu`,
  or from candidates of an unclear match that the guest heard by name. The
  same holds for the order a model keeps across turns (`cart`, `take_cart`):
  a dish nobody searched for never gets into the state, where the next turn
  would read it as found.
- Rule 2 for the guest: a tool that books under a name or a phone number
  (`create_reservation`, `draft_order`, `create_callback`) gets them only as
  the guest said them, the number also when it is the caller ID
  (`agent/guest_details.py`). The same check keeps them out of `slots`
  (`state.apply_state_patch`), without a hint: there the answer goes on. With
  a number in `slots`, that one is the number a call books under.
- Rule 1 sits in `state.apply_state_patch`: a model writes only guest details.
  Of an order line it writes dish, quantity, options and note; number, name
  and the options still open are added by the state from the search result.
- The guest's allergy (Maxi, 15.11.2026): the model writes it as a note on
  the dish, the order or the table. A guest who named one in this call gets
  no draft without such a note: the first one goes back with a hint
  (`unnoted_allergy`).
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from api.agent.consent import is_yes, read_out
from api.agent.dispatch import ToolResult, validation_hint
from api.agent.escalation import (
    _not_denied,
    asks_about_allergies,
    is_question,
    names_an_additive,
)
from api.agent.guest_details import (
    asks_for_a_phone_number,
    e164,
    name_said,
    number_known,
)
from api.agent.llm import ToolCall
from api.agent.state import (
    _FLAG,
    ConversationState,
    cart_lines,
    last_words,
    order_corrected,
    replace_cart,
    same_order,
)
from api.domain.menu.details import SAY_ALLERGENS_LEAD, say_allergens
from api.domain.menu.items import option_key
from api.domain.menu.pos_convert import allergens_named, names_an_allergen
from api.domain.menu.wishes import (
    HEALTH_NOTE_RE,
    mentions_allergy,
    names_coeliac,
    speaks_of_health,
)
from api.schemas.orders import NOTE_MAX
from api.schemas.reservations import RESERVATION_NOTE_MAX

# For the model, part of what it reads next to the prompt: German like the prompt.
HINT_CONFIRM = (
    "Abgelehnt: kein ausdrückliches Ja des Gastes auf den zuletzt vorgelesenen "
    "Entwurf. Erst den readback wörtlich vorlesen (steht im Zustand, solange er "
    "nicht vorgelesen ist), dann auf ein klares Ja warten."
)
HINT_NO_DRAFT = (
    "Abgelehnt: es gibt keinen Entwurf, den der Gast bestätigen könnte. Erst "
    "`draft_order` mit dem `cart` aus dem Zustand aufrufen, bei einem Tisch "
    "`create_reservation`, dann dessen `readback` vorlesen und auf ein klares Ja "
    "warten."
)
HINT_ITEM = (
    "Abgelehnt: eine menu_item_id stammt nicht aus einem eindeutigen Treffer von "
    "search_menu in diesem Anruf. Erst suchen; bei mehreren Treffern den Gast "
    "wählen lassen."
)
HINT_CART_ITEM = (
    "Abgelehnt: `cart` nennt eine menu_item_id, die nicht aus einem eindeutigen "
    "Treffer von search_menu in diesem Anruf stammt. Erst suchen; bei mehreren "
    "Treffern den Gast wählen lassen. Die Bestellung im Zustand ist unverändert."
)
HINT_CART_SOLD_OUT = (
    "Abgelehnt: `cart` nennt ein Gericht, das heute aus ist. Es kommt nicht in "
    "die Bestellung; sag dem Gast, dass es aus ist. Die Bestellung im Zustand ist "
    "unverändert."
)
HINT_CART_OPTION = (
    "Abgelehnt: `cart` nennt eine Option, die es zu dem Gericht nicht gibt, "
    "dieselbe Option zweimal oder zwei aus einer Pflichtgruppe. Die Optionen "
    "eines Gerichts stehen im Ergebnis von search_menu und unter `open`. Die "
    "Bestellung im Zustand ist unverändert."
)
HINT_DRAFT_DIFFERS = (
    "Abgelehnt: die `items` von draft_order sind nicht der `cart` derselben "
    "Antwort. Beide müssen dieselbe Bestellung sein."
)
HINT_CART_FORM = (
    "Abgelehnt: `cart` ist keine Liste von Positionen wie `items` von "
    "draft_order (menu_item_id, quantity, optional options und note). Die "
    "Bestellung im Zustand ist unverändert."
)
HINT_GUEST_NAME = (
    "Abgelehnt: diesen Namen hat der Gast in diesem Anruf so nicht gesagt. Nie "
    "einen Namen erfinden, ergänzen oder einen Platzhalter eintragen. Frag den "
    "Gast nach dem Namen und übernimm ihn so, wie er ihn sagt, ohne Zusatz."
)
HINT_GUEST_PHONE = (
    "Abgelehnt: unter dieser Rufnummer wird nichts angelegt. Es gilt `phone` aus "
    "`slots`, Ziffer für Ziffer: die Nummer, von der der Gast anruft, oder die, "
    "die er als seine Rufnummer genannt hat. Steht kein `phone` in `slots`, frag "
    "den Gast, unter welcher Rufnummer mit Vorwahl er erreichbar ist."
)
# The loop has tried the team before the model reads this (`loop.py`).
HINT_CALLBACK_PHONE = (
    "Abgelehnt: ein Rückruf braucht eine Rufnummer, und in `slots` steht kein "
    "`phone`; das Team ist gerade nicht erreichbar. Frag den Gast, unter welcher "
    "Nummer mit Vorwahl er erreichbar ist."
)
HINT_NOT_BOOKED = (
    "Abgelehnt: eine Buchung und den Abholcode sagt der Kern, nicht du. Die "
    "Wörter bestätigt, gebucht, reserviert, Abholcode und einen Code sagst du "
    "nie, auch nicht verneint oder in einer Frage. Fragt der Gast nach seiner "
    "Buchung, sag `booking_say` aus dem Zustand wörtlich; sonst lies den "
    "readback vor und warte auf ein klares Ja."
)
# The hint of the safety net, with the guest's words behind it: the model
# keeps nothing between turns (`unnoted_allergy`).
HINT_UNNOTED_ALLERGY = "Abgelehnt: kein `note` nennt die Allergie des Gastes."
_HINT_UNNOTED_SAID = (
    " Der Gast sagte: „{said}“. Notiere die Allergie als WICHTIG-Hinweis am "
    "Gericht, an der Bestellung oder am Tisch und ruf denselben Schritt noch "
    "einmal auf."
)
HINT_NOTE_CLAIM = (
    "Abgelehnt: ein `note` sagt etwas über ein Gericht (frei von etwas, sicher, "
    "oder es nennt ein Gericht). Notiere nur den Wunsch oder die Allergie, wie "
    "der Gast sie gesagt hat, und ruf denselben Schritt noch einmal auf."
)
# Words that tell a guest something is booked, with any ending ("Ihre
# bestätigte Bestellung"; Codex PR #347).
_BOOKING_CLAIM = re.compile(
    # "aufgegeben", "aufgenommen": the core's own order sentences (Codex PR #347).
    r"\b(?:bestätigt|bestaetigt|gebucht|reserviert|aufgegeben|aufgenommen"
    # "Abhol-Code" or "Abhol Code" is the same word (Codex PR #347, P1 twice).
    r"|abhol[\s-]*code|abhol[\s-]*nummer)\w*",
    re.IGNORECASE,
)
# A sentence that speaks of a code and holds a token shaped like one (A1, s12,
# 8842; "B 99" joined first). "Code" on its own, not inside "Gutscheincode" or
# after "WLAN-"; a code word without such a token ("Den QR Code kann ich nicht
# prüfen") names no code.
_CODE_WORD = re.compile(r"(?<![\w-])code\b", re.IGNORECASE)
_CODE_SHAPED = re.compile(r"\b(?:[A-Za-z]\d{1,4}|\d{3,6})\b")
_SPLIT_CODE = re.compile(r"\b([A-Za-z])[\s-]+(\d{1,4})\b")


@dataclass(frozen=True)
class TurnStart:
    """What held when the guest began to speak."""

    # The draft that was read back before this turn, if one waits for its yes.
    readback_id: str | None


def begin_turn(state: ConversationState) -> TurnStart:
    """Call once per guest turn, before the model. A draft whose sentence the
    guest has not heard was not read back, whatever the stage says."""
    return TurnStart(readback_id=_readback_id(state) if state.readback_heard else None)


def note_said(state: ConversationState, say: str) -> None:
    """Call with the sentence that ends a turn. It decides two things the
    guest's next sentence is measured against: whether the draft that waits
    for its yes was read out (rule 3), and which offered dishes were named
    (rule 2).

    Only the last sentence counts. A side question the model answers after
    the read-back takes it back: the guest's next "Ja" would follow that
    answer. The model then reads the draft out again, and finds the sentence in
    its state for that (`ConversationState.to_prompt_json`).

    A candidate of an unclear
    search may enter an order only once the guest heard it: by its full name
    or as "Nummer <card number>". A model that does not pass the offer on and
    asks for something else makes no candidate usable (Codex PR #222, P1).

    A candidate that was not named yet stays offered: the offer may come a
    turn later, as when an open allergy question goes first and the two
    questions are never asked at once (Codex PR #139).

    And whether the sentence asked for the guest's number: then the digits of
    the answer are a number to call without a word that says so
    (`state.note_guest_details`).

    The sentence itself goes back to the model with the next turn
    (`ConversationState.last_say`): the guest's next words answer it."""
    state.readback_heard = state.readback is not None and read_out(say, state.readback)
    # What the guest answers next refers to this sentence (`last_say`), and
    # with a first sentence said the call has begun: no second greeting.
    state.last_say = last_words(say)
    state.last_said = say
    state.greeted = True
    state.asked_for_phone = asks_for_a_phone_number(
        say, asked_before=state.asked_for_phone
    )
    heard = say.lower()
    named = {
        item_id
        for item_id, (number, name) in state.offered_items.items()
        if names_dish(heard, number.lower(), name.lower())
    }
    state.known_item_ids |= named
    state.offered_items = {
        item_id: label
        for item_id, label in state.offered_items.items()
        if item_id not in named
    }


# What a model may never claim about allergens (L17, E14), and the fixed
# sentence the prompt gives it for such a question, which hands over in any
# call (Codex PR #369). A word that names an allergen's absence itself
# ("glutenfrei", "spurenfrei") is a claim anywhere.
# ponytail: closed lists; add the wording when the call log shows it. A
# sentence they miss reaches the guest, so the prompt forbids the claim too.
_ALWAYS = re.compile(
    r"keine verl(?:ä|ae)ssliche auskunft"
    r"|(?:allergen|spuren|gluten|laktose|lactose|nuss|sesam|soja|milch|sellerie"
    r"|senf|lupinen|sulfit|weizen|fisch)frei(?:e[mnrs]?)?\b|\beie?r?frei(?:e[mnrs]?)?\b"
)
# Safety and separate preparation: a claim in a call that speaks of
# allergies.
# The model asking the guest whether they are sure ("Sind Sie sicher, dass
# ...?") is no promise: only that phrase, opening its sentence with the verb,
# is cut before `_SAFE` reads, so a claim in the rest of the question still
# counts. "Mit diesem Gericht sind Sie sicher." stays a claim (#656, Codex PR
# #714, two rounds).
_ASKS_IF_SURE = re.compile(
    r"(?:^|(?<=[.!?:;]))\s*(?:sind|bist|seid)\s+(?:sie|du|ihr)(?:\s+\w+){0,2}?\s+sicher\b"
)
_SAFE = re.compile(
    r"\b(?:keine|ohne) (?:spuren|kontamination|kreuzkontakt|kontakt)\b"
    r"|unbedenklich|bedenkenlos|garantier|hundertprozentig|\b100 ?(?:%|prozent)"
    r"|\b(?:ist|sind|wäre|waere|bleibt)\b(?:\s+\w+){0,3}\s+sicher"
    r"\s*(?:[.!?,]|$|f(?:ü|ue)r\b|zu essen)"
    r"|getrennt (?:zubereitet|frittiert|gekocht|gelagert)"
    r"|eigene[nmrs]? (?:fritteuse|pfanne|öl|oel)"
)
HINT_ALLERGEN_CLAIM = (
    "Abgelehnt: eine Aussage zu Allergenen über die hinterlegten Werte hinaus. "
    "Der Kern gibt das Gespräch ans Team."
)
ALLERGEN_CLAIM = ToolResult(
    ok=False, error_code="out_of_scope", hint=HINT_ALLERGEN_CLAIM
)
HINT_ADDITIVE_CLAIM = (
    "Abgelehnt: eine Aussage zu Zusatzstoffen, die keine Tabelle hält. "
    "Der Kern gibt das Gespräch ans Team."
)
# Its own code in the call log (#649): a model sentence or note that names an
# additive points at a guest's word the list of `escalation` misses.
ADDITIVE_CLAIM = ToolResult(ok=False, error_code="additive", hint=HINT_ADDITIVE_CLAIM)


def without_menu_names(state: ConversationState, text: str) -> str:
    """`text` without the dish and option names the search delivered that
    themselves say "...frei" or name an additive: menu data, not a claim of
    the model nor a question of the guest. "Nudeln (glutenfrei)" read back
    sent a plain order to the team (repo audit 27.10.2026). Only as a whole:
    a dish "Milch" must not cut "milchfrei" (Codex PR #388, P1). Also said
    without its brackets ("Cola mit Farbstoff", #649). Never an option name:
    "glutenfrei" as the option of one dish would hide that promise about any
    other (Codex PR #650, P1).
    shortcut: only names the search delivered in this call; a guest who names
    such a dish before any search, or a sentence that names such an option,
    goes to the team, the careful side."""
    names = [seen.get("name") for seen in state.seen_items.values()]
    names += [name for _, name in state.offered_items.values()]
    for name in names:
        if not isinstance(name, str) or not (
            _ALWAYS.search(name.lower()) or names_an_additive(name)
        ):
            continue
        for form in (name, " ".join(re.sub(r"[()]", " ", name).split())):
            whole = rf"(?<!\w){re.escape(form)}(?!\w)"
            text = re.sub(whole, " ", text, flags=re.IGNORECASE)
    return text


def allergen_claim(state: ConversationState, say: str) -> ToolResult | None:
    """Does the model's sentence claim about allergens what no database value
    answers, or say it cannot answer such a question? The word lists that
    stay after #798: a word that names an allergen's absence itself
    ("glutenfrei") and the fixed sentence anywhere, safety words in a call
    that speaks of allergies or a sentence that names an allergen, the name
    of an additive anywhere. The core's own sentences (read-back, booking)
    and the names of the dishes the search delivered are left out, as in
    `false_booking`. Every other allergen word of the model is decided by
    where it comes from (`AllergenTurn`). The loop hands the call to the
    team instead of saying it (L17, redesign of PR #369). Returns what the
    log shows for it: `ADDITIVE_CLAIM` for an additive, `ALLERGEN_CLAIM`
    otherwise, None for no claim."""
    rest = say
    for own in (state.booking_say, state.readback):
        if own:
            rest = rest.replace(own, " ")
    # Whether the sentence names an allergen is read with the dish names in
    # it: "Die Nudeln (glutenfrei) werden getrennt zubereitet." is a safety
    # promise about gluten (Codex PR #388, P1).
    named = names_an_allergen(rest) or "allerg" in rest.lower()
    lowered = without_menu_names(state, rest).lower()
    # An additive at all (#638): no table holds one, and a guest's sentence
    # that names one never reaches the model, so whatever a model says about
    # one is its own. Absence has too many forms to list ("glutamatfrei",
    # "ohne Glutamat zubereitet"; Codex PR #643).
    if names_an_additive(lowered):
        return ADDITIVE_CLAIM
    if _ALWAYS.search(lowered) or (
        (state.allergy_talk or named) and _SAFE.search(_ASKS_IF_SURE.sub(" ", lowered))
    ):
        return ALLERGEN_CLAIM
    return None


def false_booking(state: ConversationState, say: str) -> ToolResult | None:
    """The failed result for a model sentence that speaks of a booking or a
    code; None lets it through. The core says every booking itself, from the
    `confirm` result (`loop.py`), and keeps that sentence as `booking_say`;
    the model may repeat it word for word and say nothing else of the kind.
    On "Ja, passt" with no draft a real model said "Ihre Abholung ist
    bestätigt. Der Abholcode lautet 8842" (13.10.2026), and the hard metrics,
    which read the database, did not see it.

    No exemption is reasoned about: a denial ("Es ist noch nichts gebucht")
    and a question ("Haben Sie bereits reserviert?") are refused too. Fifteen
    Codex rounds on PR #347 showed that every rule that judged whether a model
    sentence about a booking was true let a false one through somewhere
    (negations, questions, abbreviations, a second order, codes written apart
    or before their word). A refused sentence costs the model one hop, and the
    hint says what to say instead.

    ponytail: a claim without these words ("Ihre Bestellung geht in die
    Küche") is not caught. More words if the call log shows them."""
    # The core's own sentences, word for word: the booking, and the readback
    # of a draft, which may hold a guest's note such as "für die bereits
    # gebuchte Feier" (Codex PR #347).
    rest = say
    for own in (state.booking_say, state.readback):
        if own:
            rest = rest.replace(own, " ")
    if _BOOKING_CLAIM.search(rest) or _names_a_code(rest):
        return _NOT_BOOKED
    return None


def _names_a_code(text: str) -> bool:
    joined = _SPLIT_CODE.sub(r"\1\2", text)
    return bool(_CODE_WORD.search(joined) and _CODE_SHAPED.search(joined))


def names_dish(heard: str, number: str, name: str) -> bool:
    """Whether a sentence (lower case) names the dish (`number` and `name` in
    lower case). A bare number is no card number: "um 13 Uhr" or "13,47 Euro" name no
    dish. Half a name names none either, and a name counts only as whole
    words: "Reis" is not named by "Preis" or "Reisnudeln" (Codex PR #222)."""
    if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", heard):
        return True
    return bool(number) and (
        re.search(rf"\b(nummer|nr\.?)\s*{re.escape(number)}(?!\w)", heard) is not None
    )


def take_cart(state: ConversationState, cart: Any) -> ToolResult | None:
    """The order a model wants to keep for the next turn (`LLMTurn.cart`).
    Taken as a whole or not at all: the order in the state is always one the
    core accepted in full. None when it was taken; otherwise the failed result
    the model gets instead of having its answer carried out.

    A refused order is still an attempt to change the order: a draft that
    waits for its yes is dropped as after a failed correction (`state.py`,
    `_supersedes`), so a yes cannot confirm what the guest just changed."""
    try:
        lines = cart_lines(cart)
    except ValidationError as exc:
        # Field and problem, never the value: a note can hold what a guest said.
        refused = ToolResult(
            ok=False,
            error_code="invalid_input",
            hint=f"{HINT_CART_FORM} {validation_hint(exc)}",
        )
    else:
        ids = [line["menu_item_id"] for line in lines]
        if not all(item_id in state.known_item_ids for item_id in ids):
            refused = _CART_ITEM
        elif any(state.seen_items.get(i, {}).get("sold_out") for i in ids):
            # A clear hit that is sold out is known, and draft_order would
            # refuse it in the end. In the state it would stand like any
            # other line until then (Codex PR #237, P2).
            refused = _CART_SOLD_OUT
        elif not all(
            _options_fit(state.seen_items.get(line["menu_item_id"], {}), line)
            for line in lines
        ):
            refused = _CART_OPTION
        elif (
            claimed := _claims_in(
                state, [line["note"] for line in lines if "note" in line]
            )
        ) is not None:
            # A changed note licenses words of the model (`AllergenTurn`), and
            # the read-back speaks it (#798).
            refused = claimed
        else:
            replace_cart(state, lines)
            return None
    order_corrected(state)
    return refused


# The calls that store a note: on an order line, on the whole order, on a table.
NOTED = frozenset({"draft_order", "create_reservation"})


def notes_an_additive(call: ToolCall) -> bool:
    """Does a note of the draft name an additive? Read back, it would promise
    what no table holds, so the loop hands the draft to the team instead
    (#649). A guest's sentence that names one never reaches the model, so
    such a note is the model's word or a guest's word the list misses."""
    return any(names_an_additive(note) for note in _notes(call))


def _notes(call: ToolCall) -> list[str]:
    """The notes a draft would store: of the whole order or table, and of each
    order line."""
    if call.name not in NOTED:
        return []
    items = call.args.get("items")
    lines = items if isinstance(items, list) else []
    notes = [
        call.args.get("note"),
        *(line.get("note") for line in lines if isinstance(line, dict)),
    ]
    return [note for note in notes if isinstance(note, str)]


def _options_fit(seen: dict[str, Any], line: dict[str, Any]) -> bool:
    """Every chosen option exists for the dish, none is chosen twice, and at
    most one comes from a mandatory group: the check of draft_order
    (`domain/ordering/validation.py`), against what the search delivered. A
    mandatory group still without a choice is fine here, the state shows it as
    `open`. Without the check "Rind" would stand in the state as the choice
    for Fleisch and `open` would be gone (Codex PR #237, P2)."""
    required = seen.get("required", [])
    offered = {
        (option_key(group["group"]), option_key(name))
        for group in [*required, *seen.get("optional", [])]
        for name in group["options"]
    }
    chosen = [
        (option_key(option["group"]), option_key(option["name"]))
        for option in line.get("options", [])
    ]
    groups = [group for group, _ in chosen]
    return (
        set(chosen) <= offered
        and len(set(chosen)) == len(chosen)
        and all(groups.count(option_key(group["group"])) <= 1 for group in required)
    )


def refusal(
    state: ConversationState,
    start: TurnStart,
    user_text: str,
    call: ToolCall,
    *,
    cart_sent: bool = False,
) -> ToolResult | None:
    """None lets the call through to `dispatch`; otherwise the failed result
    the model gets instead. `cart_sent`: the same answer carried a `cart`,
    which the core has taken by now."""
    if call.name == "get_item_details" and _states_an_allergy(user_text, call):
        return _STATED_ALLERGY
    if call.name == "confirm":
        if _may_confirm(state, start, user_text, call):
            return None
        # Without any draft "read it back first" points nowhere: a real model
        # then answered the guest's yes with a `confirm` of an id it made up,
        # turn after turn (eval run of 20.10.2026).
        return _CONFIRM if _readback_id(state) else _NO_DRAFT
    name, phone = _guest_details(call)
    if name is not None and not name_said(name, state.said_words):
        return _GUEST_NAME
    # A value that is no phone number ("unbekannt") is none either: it went
    # past this branch and the transfer was never tried (Codex PR #347).
    if not e164(phone) and _books_under_a_number(call):
        # The validation would only say "phone: Input should be a valid
        # string", and a real model sent `create_callback` without a number
        # four times in a row instead of asking for one, also on a line that
        # carried nothing but noise, where only the team can help (20.10.2026).
        # With a number in `slots` the general hint names it.
        nobody_known = call.name == "create_callback" and not state.slots.get("phone")
        return CALLBACK_WITHOUT_NUMBER if nobody_known else _GUEST_PHONE
    if phone is not None and not _phone_counts(state, phone):
        return _GUEST_PHONE
    if call.name == "draft_order":
        if not _all_known(state, call):
            return _ITEM
        if cart_sent and not _drafts_the_cart(state, call):
            return _DRAFT_DIFFERS
    return None


def _states_an_allergy(user_text: str, call: ToolCall) -> bool:
    """An allergen lookup on a guest sentence that names an allergy of their
    own and asks nothing ("Die 13, ich habe eine Erdnussallergie."). The
    allergy goes on the note for the kitchen whatever the dish contains (Maxi,
    27.10.2026, 15.11.2026); the lookup would find nothing maintained for most dishes and
    end the order with the team (L17), though the guest asked no question
    (real-model run of 27.10.2026). Refused only where no reading finds a
    question, clause by clause too: one may ask without naming the allergy
    ("Ich habe eine Nussallergie, ist die 13 okay für mich"), and the lookup
    is the safe side."""
    try:
        # Read as the tool reads it (`state.apply_tool_result`).
        asked = _FLAG.validate_python(call.args.get("allergen_question"))
    except ValidationError:
        return False
    return (
        asked
        and mentions_allergy(user_text)
        and not any(map(is_question, user_text.split(",")))
        and not asks_about_allergies(user_text)
    )


def _guest_details(call: ToolCall) -> tuple[Any, Any]:
    """Name and phone number a call books under, None for what it does not
    carry. A detail that is missing is left to the validation in dispatch,
    which names the field."""
    args = call.args
    if call.name == "create_reservation":
        return args.get("guest_name"), args.get("phone")
    if call.name == "create_callback":
        return None, args.get("phone")
    if call.name == "find_customer":
        # Only what the guest said: a name a model tries would read out who
        # stands behind the number (L12).
        return args.get("guest_name"), None
    customer = args.get("customer") if call.name == "draft_order" else None
    if isinstance(customer, dict):
        return customer.get("name"), customer.get("phone")
    return None, None


def _books_under_a_number(call: ToolCall) -> bool:
    """A call that cannot go through without a number. A `draft_order`
    without a `customer` object is left to the validation, which names it."""
    if call.name == "draft_order":
        return isinstance(call.args.get("customer"), dict)
    return call.name in ("create_reservation", "create_callback")


def _phone_counts(state: ConversationState, phone: Any) -> bool:
    """The number a call books under is the one the state holds: the caller
    ID, or the number the guest gave, which the core wrote there itself
    (`state.note_guest_details`). Any number known in the call is not enough:
    a guest who names another number than the one they call from is reached
    under that one (docs/05 §3), and a model that books under the caller ID
    all the same would go unnoticed, the number is not read back (Codex
    PR #244, P2 twice). Without a number in the state it must be one the
    guest said."""
    current = state.slots.get("phone")
    if current is not None:
        written = e164(phone)
        return bool(written) and written == e164(current)
    return number_known(phone, state.caller_phone, state.said_numbers)


def _drafts_the_cart(state: ConversationState, call: ToolCall) -> bool:
    """One answer, one order: with two in `cart` and one in the items of
    draft_order the draft would win, overwrite the cart and be read back with
    the wrong quantity (Codex PR #237, P2). Items in a wrong form are left to
    the validation in dispatch."""
    try:
        items = cart_lines(call.args.get("items"))
    except ValidationError:
        return True
    return same_order(items, state.cart)


_CONFIRM = ToolResult(ok=False, error_code="conflict", hint=HINT_CONFIRM)
_NO_DRAFT = ToolResult(ok=False, error_code="conflict", hint=HINT_NO_DRAFT)
# Public: the loop tries the team first on it (`loop.py`).
CALLBACK_WITHOUT_NUMBER = ToolResult(
    ok=False, error_code="invalid_input", hint=HINT_CALLBACK_PHONE
)
_ITEM = ToolResult(ok=False, error_code="invalid_input", hint=HINT_ITEM)
_CART_ITEM = ToolResult(ok=False, error_code="invalid_input", hint=HINT_CART_ITEM)
_CART_SOLD_OUT = ToolResult(ok=False, error_code="conflict", hint=HINT_CART_SOLD_OUT)
_CART_OPTION = ToolResult(ok=False, error_code="invalid_input", hint=HINT_CART_OPTION)
_DRAFT_DIFFERS = ToolResult(ok=False, error_code="conflict", hint=HINT_DRAFT_DIFFERS)
_GUEST_NAME = ToolResult(ok=False, error_code="invalid_input", hint=HINT_GUEST_NAME)
_GUEST_PHONE = ToolResult(ok=False, error_code="invalid_input", hint=HINT_GUEST_PHONE)
_NOT_BOOKED = ToolResult(ok=False, error_code="conflict", hint=HINT_NOT_BOOKED)
# The three fixed sentences of an allergy turn (#798). The prompt gives the
# first two to the model; the core says the third itself (`loop.py`). Drafts
# for the legal check of the announcements (docs/09), like `SAY_ALLERGY_NOTE`.
SAY_ALLERGY_NOTED = "Ihren Hinweis gebe ich an die Küche weiter."
SAY_ASK_ALLERGY = "Worauf genau soll die Küche achten?"
SAY_ALLERGENS_OFFER = (
    "Wenn ich für ein Gericht die Allergene nachsehen soll, nennen Sie mir bitte "
    "seine Nummer."
)
FIXED_ALLERGY_SAYS = (SAY_ALLERGY_NOTED, SAY_ASK_ALLERGY, SAY_ALLERGENS_OFFER)
HINT_ALLERGY_TURN = (
    "Abgelehnt: der Gast spricht in diesem Zug von einer Allergie oder einem "
    "Allergen, deinen eigenen Satz hört er hier nicht. Fragt er, was in einem "
    "Gericht ist: such es und lies es mit get_item_details, den Satz sagt der "
    "Kern. Ist es ein Wunsch zu einem Gericht: schreib ihn als `note` der "
    "Position in den `cart`. Nennt er seine Allergie: notiere sie als "
    "WICHTIG-Hinweis und sag nur „" + SAY_ALLERGY_NOTED + "“ Ist unklar, worauf "
    "die Küche achten soll, sag nur „" + SAY_ASK_ALLERGY + "“"
)
HINT_UNLICENSED_ALLERGEN = (
    "Abgelehnt: dein Satz nennt ein Allergen, das weder die Karte in diesem Zug "
    "noch die Bestellung nennt. Allergene eines Gerichts sagt nur der Kern: lies "
    "sie mit get_item_details, oder sag den Satz ohne das Allergen."
)
ALLERGY_TURN = ToolResult(ok=False, error_code="invalid_input", hint=HINT_ALLERGY_TURN)
UNLICENSED_ALLERGEN = ToolResult(
    ok=False, error_code="invalid_input", hint=HINT_UNLICENSED_ALLERGEN
)
# Words about allergens no code licenses: only the core speaks of a dish's
# allergens or of who may eat it ("Die 13 hat keine Allergene", "für
# Allergiker geeignet"). The guest's own allergy ("Ihre Allergie") is no such
# word.
_ALLERGEN_TALK = re.compile(r"allergen|allergiker", re.IGNORECASE)
_WORDS = re.compile(r"\w")


@dataclass
class AllergenTurn:
    """Where an allergen word of one guest turn can come from (#798): the
    guest's sentence, the menu text the turn's tools delivered, and the order
    text the core accepted in this turn. Built by the loop while the tools
    run; no database access and never sent to the model.

    The core reads no German grammar for it: a turn is open when the guest
    speaks of an allergy or of allergens, or names an allergen that neither
    that menu text nor a changed order line explains (`is_open`). In an open
    turn the guest hears only sentences the core wrote; in every other turn
    the model's sentence is spoken when each allergen word in it is licensed
    (`unlicensed`)."""

    # The allergen letters the guest's sentence names, without the sentences
    # that only deny an allergy.
    guest_codes: frozenset[str]
    # The guest speaks of an allergy (`loop._note_allergy_said` kept the
    # sentence) or of allergens.
    speaks: bool
    # An allergen question was left open by the turn before
    # (`ConversationState.allergen_question_open`).
    pending: bool
    # The order as it stood when the guest began to speak.
    cart_at_start: list[dict[str, Any]]
    # The allergen letters of the dish, option and description names the
    # turn's searches and lookups delivered. Never the search's `wish` (the
    # model's query words) nor a lookup's `allergens`: those only the core's
    # table sentence speaks.
    menu_codes: set[str] = field(default_factory=set)
    # The same letters by the dish that delivered them, by its id: a word in
    # a clause that names a dish is licensed by that dish's text only.
    dish_codes: dict[str, set[str]] = field(default_factory=dict)
    # The notes of the drafts made in this turn.
    draft_notes: list[str] = field(default_factory=list)
    # The tools' sentences of this turn, core strings the model may repeat.
    tool_says: list[str] = field(default_factory=list)
    # Every dish a lookup that worked delivered in this turn, by its id.
    looked_up: dict[str, dict[str, Any]] = field(default_factory=dict)
    # A draft of this turn noted the guest's health.
    noted_in_draft: bool = False
    # A search hit or a lookup in this turn.
    dish_touched: bool = False
    # A draft (`draft_order`, `create_reservation`) went through in this turn.
    drafted: bool = False


def begin_allergen_turn(
    state: ConversationState,
    user_text: str,
    *,
    kept: bool,
    pending: bool | None = None,
) -> AllergenTurn:
    """Call once per guest turn, before the model. `kept`: the guest's
    sentence speaks of their health (`loop._note_allergy_said`)."""
    said = " ".join(_not_denied(user_text))
    # A kept sentence speaks of an allergy by an allergy word or coeliac
    # disease outside a denial, or it is the answer to the model's question
    # (no health word at all). "Wir wollen die Stoßzeit meiden" and "die 13,
    # ich habe keine Erdnussallergie" are kept for the safety net and speak
    # of none (#798); a health word with an allergen opens by its letter.
    speaks = "allergen" in user_text.lower() or (
        kept
        and (
            mentions_allergy(said)
            or names_coeliac(said)
            or not speaks_of_health(user_text)
        )
    )
    return AllergenTurn(
        guest_codes=frozenset(allergens_named(said)),
        speaks=speaks,
        pending=state.allergen_question_open if pending is None else pending,
        cart_at_start=[dict(line) for line in state.cart],
    )


def note_tool(
    turn: AllergenTurn, name: str, result: ToolResult, args: dict[str, Any]
) -> None:
    """Records what a tool that ran in this turn delivered."""
    if result.say:
        turn.tool_says.append(result.say)
    if not result.ok:
        return
    data = result.data
    if name == "search_menu":
        parts = (
            data.get("positions") if data.get("match_type") == "positions" else [data]
        )
        for part in parts or []:
            for hit in part.get("results") or []:
                turn.dish_touched = True
                _menu_text(turn, hit, _codes(hit.get("name"), *_option_names(hit)))
    elif name == "get_item_details":
        turn.dish_touched = True
        turn.looked_up[str(data["menu_item_id"])] = data
        _menu_text(
            turn,
            data,
            _codes(data.get("name"), data.get("description"), *_option_names(data)),
        )
    elif name in NOTED and data.get("status", "draft") == "draft":
        turn.drafted = True
        note = args.get("note")
        if isinstance(note, str):
            turn.draft_notes.append(note)
            turn.noted_in_draft = turn.noted_in_draft or bool(
                HEALTH_NOTE_RE.search(note)
            )


def _menu_text(turn: AllergenTurn, dish: dict[str, Any], codes: set[str]) -> None:
    turn.menu_codes |= codes
    turn.dish_codes.setdefault(str(dish.get("menu_item_id")), set()).update(codes)


def _option_names(dish: dict[str, Any]) -> list[Any]:
    return [
        option.get("name")
        for group in dish.get("option_groups") or []
        for option in group.get("options") or []
        if isinstance(option, dict)
    ]


def _wish_codes(*notes: Any) -> set[str]:
    """The allergen letters of the notes that are wishes ("ohne Erdnüsse").
    A note about the guest's health ("WICHTIG !!! Kunde ist allergisch auf
    Erdnüsse") is no order text: it names what the guest reacts to, never
    what a dish holds, and licenses nothing (review of #798, P1)."""
    return _codes(
        *(n for n in notes if isinstance(n, str) and not HEALTH_NOTE_RE.search(n))
    )


def _codes(*texts: Any) -> set[str]:
    return allergens_named(" ".join(t for t in texts if isinstance(t, str)))


def order_changed(turn: AllergenTurn, state: ConversationState) -> bool:
    """The order changed in this turn: the cart differs from the one at its
    start, or a draft went through."""
    return turn.drafted or not same_order(state.cart, turn.cart_at_start)


def _changed_lines(
    turn: AllergenTurn, state: ConversationState
) -> list[dict[str, Any]]:
    """The lines of the order that were not in it at the start of the turn,
    a changed note or option included."""
    before = [_line_key(line) for line in turn.cart_at_start]
    changed = []
    for line in state.cart:
        key = _line_key(line)
        if key in before:
            before.remove(key)
        else:
            changed.append(line)
    return changed


def _line_key(line: dict[str, Any]) -> str:
    return json.dumps(line, sort_keys=True, default=str)


def _line_texts(state: ConversationState, line: dict[str, Any]) -> list[str]:
    """The dish name of a line and the names of its options."""
    name = state.seen_items.get(line["menu_item_id"], {}).get("name")
    return [name, *(option.get("name") for option in line.get("options", []))]


def _order_codes(
    turn: AllergenTurn, state: ConversationState, *, health: bool = False
) -> set[str]:
    """The allergen letters of the order text the core accepted in this turn:
    the dish, options and note of each changed line, the notes of a draft.
    `health`: a note about the guest's health counts too. It takes up the
    guest's own word (`_unexplained`), but never licenses a word of the
    model (`_wish_codes`, review of #798, P1)."""
    changed = _changed_lines(turn, state)
    notes = [*turn.draft_notes, *(line.get("note") for line in changed)]
    return _codes(*(text for line in changed for text in _line_texts(state, line))) | (
        _codes(*notes) if health else _wish_codes(*notes)
    )


def _noted(turn: AllergenTurn, state: ConversationState) -> bool:
    """The guest's health went onto a note in this turn."""
    return turn.noted_in_draft or any(
        HEALTH_NOTE_RE.search(line.get("note") or "")
        for line in _changed_lines(turn, state)
    )


def _unexplained(turn: AllergenTurn, state: ConversationState) -> bool:
    """The guest named an allergen that neither the menu text of this turn
    nor a changed order line explains."""
    return bool(
        turn.guest_codes - turn.menu_codes - _order_codes(turn, state, health=True)
    )


def sticky(state: ConversationState) -> bool:
    """The call keeps speaking of allergies: the guest named one (an allergy
    word, coeliac disease or an allergen in a kept sentence; not every
    sentence the safety net keeps, "die Stoßzeit meiden" is none, #798), or
    the last sentence the guest heard was the core's table sentence."""
    return any(
        mentions_allergy(said) or names_coeliac(said) or allergens_named(said)
        for said in state.allergy_said
    ) or (state.last_said or "").startswith(SAY_ALLERGENS_LEAD)


def is_open(turn: AllergenTurn, state: ConversationState) -> bool:
    """Does the guest's turn ask for allergen words only the core may say?
    The guest speaks of an allergy or of allergens; names an allergen the
    turn's menu text and changed order do not explain; or, with the order
    unchanged, a question was pending and a dish was touched, or the call is
    sticky and a dish was looked up (#798)."""
    if turn.speaks or _unexplained(turn, state):
        return True
    if order_changed(turn, state):
        return False
    return (turn.pending and turn.dish_touched) or (
        sticky(state) and bool(turn.looked_up)
    )


def _without_core(turn: AllergenTurn, state: ConversationState, say: str) -> str:
    """`say` without the sentences the core wrote: the read-back, the
    booking, the tools' sentences of this turn, the fixed sentences, and the
    table sentence the guest heard last."""
    rest = say
    last = state.last_said or ""
    table = last if last.startswith(SAY_ALLERGENS_LEAD) else None
    for own in (
        state.readback,
        state.booking_say,
        table,
        *turn.tool_says,
        *FIXED_ALLERGY_SAYS,
    ):
        if own:
            rest = rest.replace(own, " ")
    return rest


def core_only(turn: AllergenTurn, state: ConversationState, say: str) -> bool:
    """Is `say` made of sentences the core wrote and nothing else (row 2)?"""
    return _WORDS.search(_without_core(turn, state, say)) is None


def unlicensed(turn: AllergenTurn, state: ConversationState, say: str) -> set[str]:
    """The allergen words of the model's sentence that nothing of this turn
    licenses (rows 5 and 6), as letters, "allergen" for a word about
    allergens no letter licenses. Licensed: a letter of the turn's menu text,
    of the dish and option names of the order, or of the order text accepted
    in this turn; in a clause that names a dish of the call (card number or
    whole name), only that dish's own text and lines license, so the option
    "Erdnuss" of the 47 licenses no "die 13 hat keine Erdnüsse". Whole dish
    names the search delivered in this call are menu data and are left out
    ("Erdnuss-Curry").
    shortcut: a clause that refers to a dish without naming it ("Sie hat
    keine Erdnüsse.") is licensed by the whole turn; the residual R1 of the
    design."""
    rest = _without_core(turn, state, say)
    changed = _changed_lines(turn, state)
    words: set[str] = set()
    for clause in re.split(r"[.,;:!?]+", rest):
        named = {
            item_id
            for item_id, seen in state.seen_items.items()
            if _names_it(clause, seen)
        }
        clause = _without_dish_names(state, clause)
        if named:
            lines = [line for line in state.cart if line["menu_item_id"] in named]
            new = [line for line in changed if line["menu_item_id"] in named]
            allowed = set().union(*(turn.dish_codes.get(i, set()) for i in named))
            allowed |= _codes(*(t for line in lines for t in _line_texts(state, line)))
            allowed |= _wish_codes(*(line.get("note") for line in new))
        else:
            allowed = turn.menu_codes | _order_codes(turn, state)
            allowed |= _codes(
                *(t for line in state.cart for t in _line_texts(state, line))
            )
        words |= allergens_named(clause) - allowed
        if _ALLERGEN_TALK.search(clause):
            words.add("allergen")
    return words


def _names_it(clause: str, seen: dict[str, Any]) -> bool:
    """Does the clause name the dish: its card number as a token of its own,
    or its whole name?"""
    return any(
        isinstance(text, str)
        and text
        and re.search(rf"(?<!\w){re.escape(text)}(?!\w)", clause, re.IGNORECASE)
        for text in (seen.get("number"), seen.get("name"))
    )


def _without_dish_names(state: ConversationState, text: str) -> str:
    names = {seen.get("name") for seen in state.seen_items.values()}
    names |= {name for _, name in state.offered_items.values()}
    for name in sorted(
        (n for n in names if isinstance(n, str) and n), key=len, reverse=True
    ):
        text = re.sub(
            rf"(?<!\w){re.escape(name)}(?!\w)", " ", text, flags=re.IGNORECASE
        )
    return text


def table_says(turn: AllergenTurn) -> list[str] | None:
    """The core's table sentence of every dish looked up in this turn (row
    1); None when one of them has no maintained value: the team answers
    (L17)."""
    if any(not dish["allergens"]["known"] for dish in turn.looked_up.values()):
        return None
    return [
        say_allergens(dish["number"], dish["name"], dish["allergens"]["names"])
        for dish in turn.looked_up.values()
    ]


def unknown_dish(turn: AllergenTurn) -> dict[str, Any] | None:
    """The first dish looked up in this turn without a maintained value."""
    return next(
        (d for d in turn.looked_up.values() if not d["allergens"]["known"]), None
    )


def leaves_a_question(turn: AllergenTurn, state: ConversationState) -> bool:
    """An open turn that ends in a core string or the offer sentence leaves
    the guest's signal untaken: an allergy not noted, or an allergen nothing
    explains. It stays pending for one turn
    (`ConversationState.allergen_question_open`)."""
    return (turn.speaks and not _noted(turn, state)) or _unexplained(turn, state)


HINT_STATED_ALLERGY = (
    "Abgelehnt: der Gast hat nur seine Allergie genannt und nichts gefragt. "
    "Notiere sie als Hinweis für die Küche; prüf keine Allergene und mach mit "
    "der Bestellung weiter."
)
_STATED_ALLERGY = ToolResult(
    ok=False, error_code="invalid_input", hint=HINT_STATED_ALLERGY
)
# The longest note of the whole order and of a table.
NOTE_MAX_FOR = {"draft_order": NOTE_MAX, "create_reservation": RESERVATION_NOTE_MAX}
NOTE_CLAIM = ToolResult(ok=False, error_code="invalid_input", hint=HINT_NOTE_CLAIM)
# What the core writes onto a draft the model sent twice without the guest's
# words (`with_the_guests_words`).
NOTED_FOR_THE_GUEST = "WICHTIG !!! Gast sagte: {said}"
# A note that only repeats the example of a hint names nothing.
_TEMPLATE = ("…", "<", "WICHTIG-Hinweis")


def unnoted_allergy(state: ConversationState, call: ToolCall) -> ToolResult | None:
    """A draft for a guest who spoke of their health in this call
    (`state.allergy_said`), with no note that names it: the kitchen would
    never hear of it. A note passes by `wishes.HEALTH_NOTE`, the pattern the
    retention job deletes such a note by, so what passes here is deleted in
    time (L13). The hint quotes the guest. The loop gives it back once per
    turn, and writes the guest's words on the next one itself
    (`with_the_guests_words`), never a loop."""
    if not state.allergy_said or call.name not in NOTED:
        return None
    if any(
        HEALTH_NOTE_RE.search(note) and not any(t in note for t in _TEMPLATE)
        for note in _notes(call)
    ):
        return None
    hint = HINT_UNNOTED_ALLERGY + _HINT_UNNOTED_SAID.format(said=_said(state))
    return ToolResult(ok=False, error_code="invalid_input", hint=hint)


def with_the_guests_words(state: ConversationState, call: ToolCall) -> ToolCall | None:
    """The draft with the guest's own words about their health on the note of
    the whole order or of the table, behind what the model wrote there, cut
    to the field: the model sent it again without them. Words, never a
    reading of them (Maxi, 15.11.2026). One that would not fit next to the
    model's note takes the field alone: the health part is the one to keep.
    None when the cut would drop the health words themselves ("..., ich habe
    eine Erdnussallergie" at the end of a long sentence, Codex PR #788, P1):
    the team takes the call instead (rule 5)."""
    added = NOTED_FOR_THE_GUEST.format(said=_said(state))
    limit = NOTE_MAX_FOR[call.name]
    before = call.args.get("note")
    note = (
        f"{before.strip()}. {added}"
        if isinstance(before, str) and before.strip()
        else added
    )
    if len(note) > limit:
        note = added[:limit].rstrip()
        kept = note.removeprefix(NOTED_FOR_THE_GUEST.format(said=""))
        if not speaks_of_health(kept):
            return None
    return ToolCall(call.name, {**call.args, "note": note})


def _said(state: ConversationState) -> str:
    # Without the closing full stop: a read-back puts its own behind a note.
    return " ".join(sentence.strip() for sentence in state.allergy_said).rstrip(" .")


def claims_in_a_note(state: ConversationState, call: ToolCall) -> ToolResult | None:
    """A note of a draft that promises a dish free of an allergen ("nussfrei",
    "keine Spuren") or names a dish ("Erdnussallergie. Die 13 hat keine."):
    read back word for word, it is the model's second way to the guest's ear
    (#798), so it goes back. The same lists as the model's sentence
    (`allergen_claim`); an additive has its own way (`notes_an_additive`)."""
    return _claims_in(state, _notes(call))


def _claims_in(state: ConversationState, notes: list[str]) -> ToolResult | None:
    if any(
        allergen_claim(state, note) is ALLERGEN_CLAIM or _names_a_dish(state, note)
        for note in notes
    ):
        return NOTE_CLAIM
    return None


def _names_a_dish(state: ConversationState, note: str) -> bool:
    """Does the note name a dish the search delivered in this call, by its
    card number as a token of its own or by its whole name? The guest's own
    sentences about their health, which the prompt asks the model to quote,
    are cut first: they are the guest's words, not the model's (#798).
    shortcut: a bare number that happens to be a card number ("2 Stäbchen")
    counts too; the note goes back once and the model writes it again."""
    rest = note.lower()
    for said in state.allergy_said:
        rest = rest.replace(said.strip().rstrip(" .").lower(), " ")
    return any(
        (
            seen.get("number")
            and re.search(
                rf"(?<!\w){re.escape(str(seen['number']).lower())}(?!\w)", rest
            )
        )
        or (
            seen.get("name")
            and re.search(rf"(?<!\w){re.escape(str(seen['name']).lower())}(?!\w)", rest)
        )
        for seen in state.seen_items.values()
    )


def unheard_readback(state: ConversationState) -> str | None:
    """The sentence of the draft that waits, while it is not what the guest
    heard last: right after the draft, or once another sentence ended a turn."""
    if state.stage == "readback_pending" and not state.readback_heard:
        return state.readback
    return None


def reread_on_yes(
    state: ConversationState, start: TurnStart, user_text: str, call: ToolCall | None
) -> str | None:
    """The draft's sentence, when `call` is a `confirm` on a yes for a draft
    that is not what the guest heard last; None for every other call.

    Such a `confirm` is refused (`refusal`). The yes may have meant another
    question, so the question for it has to be put, and that is not left to
    the model: the stand-in handed the call to the team, and a model that
    answers "gebucht" or asks in its own words keeps the guest in refused
    yeses. The loop ends the turn with this sentence and asks the model
    nothing more, so nothing a further answer could carry gets in the way
    (finding on PR #258).

    Only on a yes: after "Nein, ich möchte etwas ändern" the model has to ask
    what, not put the old question again."""
    if call is None or call.name != "confirm" or not is_yes(user_text):
        return None
    if _may_confirm(state, start, user_text, call):
        return None
    return unheard_readback(state)


def _readback_id(state: ConversationState) -> str | None:
    if state.stage != "readback_pending":
        return None
    draft = state.order_id or state.reservation_id
    return str(draft) if draft else None


def _may_confirm(
    state: ConversationState, start: TurnStart, user_text: str, call: ToolCall
) -> bool:
    """The draft must be the one read back before this turn and still the
    pending one: a draft built or replaced inside the turn was never read to
    the guest, so a yes in the same sentence is no consent to it."""
    return (
        start.readback_id is not None
        and _readback_id(state) == start.readback_id
        and str(call.args.get("entity_id")) == start.readback_id
        and is_yes(user_text)
    )


def _all_known(state: ConversationState, call: ToolCall) -> bool:
    """Which candidate the guest's answer means stays with the model and is
    measured by the evals; here only an id nobody was asked about is stopped.
    Arguments that are no list of items are left to the validation in dispatch."""
    items = call.args.get("items")
    if not isinstance(items, list):
        return True
    return all(
        str(item.get("menu_item_id")) in state.known_item_ids
        for item in items
        if isinstance(item, dict)
    )
