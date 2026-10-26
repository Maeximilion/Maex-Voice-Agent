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
- The caller's allergy (legal check L13): a note keeps "Grund: Allergie" only
  after the caller's explicit "ja" to `consent.ALLERGY_QUESTION`, otherwise it
  says "ohne <Zutat>" (`consented_call`). The first draft whose note would
  carry the reason waits for that question (`waits_for_consent`, `loop.py`).
"""

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from api.agent.consent import is_yes, read_out
from api.agent.dispatch import ToolResult, validation_hint
from api.agent.guest_details import (
    e164,
    name_said,
    names_a_phone_number,
    number_known,
)
from api.agent.llm import ToolCall
from api.agent.state import (
    ConversationState,
    cart_lines,
    last_words,
    order_corrected,
    replace_cart,
    same_order,
)
from api.domain.menu.items import option_key
from api.domain.menu.wishes import asks_consent, unread_allergy, with_consent

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
HINT_ALLERGY_UNREAD = (
    "Abgelehnt: ein Hinweis nennt eine Allergie, deren Zutat nicht eindeutig "
    "ist. Frag den Gast: „Wogegen sind Sie allergisch?“ Danach je Allergie "
    "einen eigenen Hinweis mit ihrer Zutat, zum Beispiel „Erdnussallergie“."
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
    state.greeted = True
    state.asked_for_phone = names_a_phone_number(say)
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
        # Before the notes are brought into the form they may be stored in,
        # which would lose an allergy not read in full (Codex PR #368).
        unread = any(
            isinstance(line.get("note"), str) and unread_allergy(line["note"])
            for line in lines
        )
        lines = [_line_with_consent(state, line) for line in lines]
        ids = [line["menu_item_id"] for line in lines]
        if unread:
            refused = _ALLERGY_UNREAD
        elif not all(item_id in state.known_item_ids for item_id in ids):
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
        else:
            replace_cart(state, lines)
            return None
    order_corrected(state)
    return refused


# The calls that store a note: on an order line, on the whole order, on a table.
_NOTED = frozenset({"draft_order", "create_reservation"})


def stored_note(state: ConversationState, note: str) -> str | None:
    """`note` as the caller's answer lets it be stored (L13): with their "ja"
    an allergy keeps the fixed wording, without it only "ohne <Zutat>". While
    the question was not asked, a note that would carry the reason stays as
    it is: the draft with it waits for the question (`waits_for_consent`)."""
    if state.allergy_consent is None and asks_consent(note):
        return note
    return with_consent(note, consented=state.allergy_consent == "yes")


def _line_with_consent(state: ConversationState, line: dict[str, Any]) -> dict:
    """An order line with its note as it may be stored; no note when none is
    left."""
    note = line.get("note")
    if not isinstance(note, str):
        return line
    rest = {key: value for key, value in line.items() if key != "note"}
    stored = stored_note(state, note)
    return {**rest, "note": stored} if stored else rest


def consented_call(state: ConversationState, call: ToolCall) -> ToolCall:
    """The call with each note it would store (`stored_note`). Arguments in a
    wrong form are left to the validation in dispatch."""
    if call.name not in _NOTED:
        return call
    args = dict(call.args)
    if call.name == "draft_order" and "note" not in args and state.order_note:
        # The note of the whole order outlives a correction (`state.order_note`).
        args["note"] = state.order_note
    if call.name == "create_reservation" and "note" not in args and state.table_note:
        args["note"] = state.table_note
    if isinstance(args.get("note"), str):
        args["note"] = stored_note(state, args["note"])
    if isinstance(args.get("items"), list):
        args["items"] = [
            _line_with_consent(state, item) if isinstance(item, dict) else item
            for item in args["items"]
        ]
    return ToolCall(name=call.name, args=args)


def keep_restrictions(state: ConversationState, held: dict[str, Any]) -> None:
    """The draft the core dropped after an unclear answer to the allergy
    question (`loop._consent_answered`): its notes stay in the state, in the
    form they may be stored in without consent, because the model reads
    nothing of the old draft and could make a new one without them (Codex
    PR #368). Lines in a wrong form are left out, as `take_cart` would."""
    args = held["args"]
    note = args.get("note")
    kept = stored_note(state, note) if isinstance(note, str) else None
    if held["name"] == "create_reservation":
        state.table_note = kept
        return
    if kept:
        state.order_note = kept
    try:
        lines = cart_lines(args.get("items"))
    except ValidationError:
        return
    state.cart = [_line_with_consent(state, line) for line in lines]


def waits_for_consent(state: ConversationState, call: ToolCall) -> bool:
    """A draft whose note would carry the allergy as its reason, before the
    caller was asked: it is not dispatched, the core asks first (`loop.py`)."""
    if state.allergy_consent is not None:
        return False
    return any(asks_consent(note) for note in _notes(call))


def _notes(call: ToolCall) -> list[str]:
    """The notes a draft would store: of the whole order or table, and of each
    order line."""
    if call.name not in _NOTED:
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


def unread_refusal(call: ToolCall) -> ToolResult | None:
    """A draft as the model wrote it, before `consented_call` turns its notes
    into what may be stored: a note with an allergy whose ingredient is not
    read in full would lose the allergy there (Codex PR #368)."""
    if any(unread_allergy(note) for note in _notes(call)):
        return _ALLERGY_UNREAD
    return None


def _guest_details(call: ToolCall) -> tuple[Any, Any]:
    """Name and phone number a call books under, None for what it does not
    carry. A detail that is missing is left to the validation in dispatch,
    which names the field."""
    args = call.args
    if call.name == "create_reservation":
        return args.get("guest_name"), args.get("phone")
    if call.name == "create_callback":
        return None, args.get("phone")
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
_ALLERGY_UNREAD = ToolResult(
    ok=False, error_code="invalid_input", hint=HINT_ALLERGY_UNREAD
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
