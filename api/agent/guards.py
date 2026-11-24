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

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from api.agent.consent import answers_the_read_back, is_yes, read_out
from api.agent.dispatch import ToolResult, validation_hint
from api.agent.escalation import (
    SENTENCE,
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
    line_key,
    order_corrected,
    replace_cart,
    same_order,
)
from api.domain.menu.details import SAY_ALLERGENS_LEAD, say_allergens
from api.domain.menu.items import option_key
from api.domain.menu.normalize import normalize_query
from api.domain.menu.numberwords import canonical_card, find_numbers, spoken_card
from api.domain.menu.pos_convert import allergens_named, names_an_allergen
from api.domain.menu.search import CLEAR_MATCHES
from api.domain.menu.wishes import (
    HEALTH_NOTE_RE,
    _ingredient,
    mentions_allergy,
    names_coeliac,
    speaks_of_health,
)
from api.domain.ordering.readback import spoken_euro
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
# A health note stands, but an allergen the guest named later is on none
# (#929, Maxi 24.11.2026: ask for the dish, or the dish named right before).
# The guest's own words, never a reading of them: a wish or a question in
# the quoted sentence ("..., bitte extra Sesam") must not become an allergy.
HINT_UNNOTED_LATER = "Abgelehnt: kein `note` nennt die Allergie aus „{said}“."
_HINT_LATER_ORDER = (
    " Schreib „WICHTIG !!! “ und dahinter die Worte des Gastes an das "
    "Gericht, das er direkt davor genannt hat, wenn sie dafür gelten, sonst an "
    "die Bestellung, und ruf denselben Schritt noch einmal auf. Ist unklar, für "
    "welches Gericht, sag nur „{ask}“"
)
_HINT_LATER_TABLE = (
    " Schreib „WICHTIG !!! “ und dahinter die Worte des Gastes an den Tisch "
    "und ruf denselben Schritt noch einmal auf."
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
                state,
                [
                    (line["note"], line["menu_item_id"])
                    for line in lines
                    if "note" in line
                ],
                [line["menu_item_id"] for line in lines],
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
# The fixed sentences of an allergy turn (#798). The prompt gives the model
# all but the offer, which the core says itself (`loop.py`). Drafts for the
# legal check of the announcements (docs/09), like `SAY_ALLERGY_NOTE`.
SAY_ALLERGY_NOTED = "Ihren Hinweis gebe ich an die Küche weiter."
SAY_ASK_ALLERGY = "Worauf genau soll die Küche achten?"
# A later allergy whose dish is unclear ("Meine Frau verträgt keinen Sesam",
# #929, Maxi 24.11.2026).
SAY_ASK_ALLERGY_DISH = "Für welches Gericht soll die Küche darauf achten?"
SAY_ALLERGENS_OFFER = (
    "Wenn ich für ein Gericht die Allergene nachsehen soll, nennen Sie mir bitte "
    "seine Nummer."
)
FIXED_ALLERGY_SAYS = (
    SAY_ALLERGY_NOTED,
    SAY_ASK_ALLERGY,
    SAY_ASK_ALLERGY_DISH,
    SAY_ALLERGENS_OFFER,
)
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
    "noch die Bestellung nennt. Was in einem Gericht ist und ob es für den Gast "
    "passt, sagt nur der Kern: lies es mit get_item_details."
)
# Row 6 in a call where the guest named an allergy (`sticky`): the retry may
# say only core sentences (`loop.py`), so the hint names them.
HINT_ALLERGY_CALL = (
    "Abgelehnt: dein Satz nennt ein Allergen, das weder die Karte in diesem Zug "
    "noch die Bestellung nennt, und der Gast hat in diesem Gespräch eine Allergie "
    "genannt: deinen eigenen Satz hört er jetzt nicht. Was in einem Gericht ist, "
    "sagt nur der Kern: lies es mit get_item_details. Sonst sag nur „"
    + SAY_ALLERGY_NOTED
    + "“"
)
# In the same call a sentence about a dish of the call, with no order step in
# the turn (`about_a_dish`, #928): "Bei Ihrer Allergie ist die 13 kein
# Problem" promises without an allergen noun.
HINT_ALLERGY_DISH = (
    "Abgelehnt: der Gast hat in diesem Gespräch eine Allergie genannt, und dein "
    "Satz spricht von einem Gericht: deinen eigenen Satz hört er jetzt nicht. "
    "Nimmt der Gast ein Gericht, schreib es in den `cart`. Fragt er nach dem "
    "Preis, nenn den Preis aus search_menu in diesem Zug: den Satz sagt dann der "
    "Kern. Den `say` eines Werkzeugs aus diesem Zug darfst du wörtlich sagen. Nur "
    "wenn er wissen will, was in einem Gericht ist oder ob es für ihn passt: lies "
    "es mit get_item_details, den Satz sagt der Kern. Sonst sag nur „"
    + SAY_ALLERGY_NOTED
    + "“"
)
ALLERGY_TURN = ToolResult(ok=False, error_code="invalid_input", hint=HINT_ALLERGY_TURN)
ALLERGY_DISH = ToolResult(ok=False, error_code="invalid_input", hint=HINT_ALLERGY_DISH)
UNLICENSED_ALLERGEN = ToolResult(
    ok=False, error_code="invalid_input", hint=HINT_UNLICENSED_ALLERGEN
)
ALLERGY_CALL = ToolResult(ok=False, error_code="invalid_input", hint=HINT_ALLERGY_CALL)
# Words about allergens no code licenses: only the core speaks of a dish's
# allergens or of who may eat it ("Die 13 hat keine Allergene", "für
# Allergiker geeignet"). The guest's own allergy ("Ihre Allergie") is no such
# word.
_ALLERGEN_TALK = re.compile(r"allergen|allergiker", re.IGNORECASE)
# The letter coeliac disease names in a note ("Zöliakie, ohne Gluten").
_GLUTEN = allergens_named("Gluten")
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
    (`unlicensed`) and, in a call where the guest named an allergy, when it
    speaks of no dish of the call (`about_a_dish`, #928)."""

    # The allergen letters the guest's sentence names, without the sentences
    # that only deny an allergy and without the names of dishes the call
    # found (`_guest_words`, `_without_dish_names`).
    guest_codes: frozenset[str]
    # The guest speaks of an allergy (`loop._note_allergy_said` kept the
    # sentence) or of allergens.
    speaks: bool
    # An allergen question was left open by the turn before
    # (`ConversationState.allergen_question_open`).
    pending: bool
    # The allergen letters the turn before left unexplained
    # (`ConversationState.allergen_question_codes`).
    asked_before: frozenset[str]
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
    # Every hit this turn's searches without a wish delivered, by its id: the
    # database's price for `price_says` (#928).
    found: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The dishes a search of this turn delivered with a wish ("die 47 mit
    # Huhn"): their price is not the card's, whatever other search found them.
    wished: set[str] = field(default_factory=set)
    # The searches' sentences among `tool_says` (`about_a_dish`).
    search_says: list[str] = field(default_factory=list)
    # The `tool_says` that tell the guest something: all but a search's repeat
    # of what it understood (`_echoes`).
    answers: list[str] = field(default_factory=list)
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
    asked_before: frozenset[str] | None = None,
) -> AllergenTurn:
    """Call once per guest turn, before the model. `kept`: the guest's
    sentence speaks of their health (`loop._note_allergy_said`); `pending`
    and `asked_before`: what the turn before left open, read from the state
    when not given."""
    said = _guest_words(user_text)
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
    # A dish the call found, named by the guest, is that dish and no
    # allergen question: "Wie lange dauern die Nudeln mit Garnelen?" (deep
    # review of #860, P1). A name made of allergen words only stays.
    codes = allergens_named(_without_dish_names(state, said, plain_only=True))
    # What the guest named stays licensed for the notes of the call: the
    # draft turns later carries "ohne Erdnüsse" (`_note_licence`).
    state.allergens_said = sorted(set(state.allergens_said) | codes)
    return AllergenTurn(
        guest_codes=frozenset(codes),
        speaks=speaks,
        pending=state.allergen_question_open if pending is None else pending,
        asked_before=frozenset(state.allergen_question_codes)
        if asked_before is None
        else asked_before,
        cart_at_start=[dict(line) for line in state.cart],
    )


def _guest_words(text: str) -> str:
    """The guest's sentence without what only denies an allergy, read clause
    by clause: "Ich habe keine Nussallergie, ist in der 13 Sesam?" keeps the
    sesame (deep review of #860, P1). By punctuation, not by telling a
    question from a statement (AGENTS.md, Codex review of #860).
    shortcut: a denial with its list after a comma ("keine Allergie gegen
    Erdnüsse, Sesam und Milch") keeps the list; the turn opens and the
    model's sentence goes back, nothing false is said."""
    return " ".join(
        said
        for sentence in SENTENCE.split(text)
        for clause in sentence.split(",")
        for said in _not_denied(clause)
    )


def note_tool(
    turn: AllergenTurn, name: str, result: ToolResult, args: dict[str, Any]
) -> None:
    """Records what a tool that ran in this turn delivered."""
    data = result.data or {}
    _note_say(turn, name, result.say, data)
    if not result.ok:
        return
    if name == "search_menu":
        positions = data.get("match_type") == "positions"
        parts = data.get("positions") if positions else [data]
        for part in parts or []:
            if positions:
                # A part's own sentence: the candidates of an unclear part,
                # sold out, not on the card (Codex review of #928).
                _note_say(turn, name, part.get("say"), part)
            for hit in part.get("results") or []:
                turn.dish_touched = True
                item_id = str(hit.get("menu_item_id"))
                if part.get("wish"):
                    # Asked with a wish ("die 47 mit Huhn") the dish costs
                    # other than the hit's price (#928, Codex review).
                    turn.wished.add(item_id)
                else:
                    turn.found[item_id] = hit
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


def _note_say(
    turn: AllergenTurn, name: str, said: str | None, data: dict[str, Any]
) -> None:
    """A tool's sentence is a core string the model may repeat, unless it
    carries the model's words of an allergen wish; all but a search's echo
    tell the guest something (`AllergenTurn.answers`)."""
    if not said or (name == "search_menu" and _allergen_wish(data)):
        return
    turn.tool_says.append(said)
    if name == "search_menu":
        turn.search_says.append(said)
        if _echoes(data):
            return
    turn.answers.append(said)


def _echoes(data: dict[str, Any]) -> bool:
    """Does a search's sentence repeat what it understood ("Notiert: Nummer
    13.", several parts of an order)? Alone after "Kann ich die 13 essen?" it
    sounds like a yes (deep review of #928). A sold-out or unclear hit has a
    sentence of its own."""
    results = data.get("results") or []
    return data.get("match_type") == "positions" or (
        data.get("match_type") in CLEAR_MATCHES
        and bool(results)
        and not results[0].get("sold_out")
    )


def _allergen_wish(data: dict[str, Any]) -> bool:
    """Does the search's wish speak of an allergen? search_menu repeats the
    wish of the model's query in its `say` ("Nummer 13, ohne Erdnüsse"):
    those are the model's words, no core string (deep review of #860, P1)."""
    parts = [data, *(data.get("positions") or [])]
    wishes = [p.get("wish") for p in parts if isinstance(p, dict)]
    text = " ".join(
        str(wish.get(key) or "")
        for wish in wishes
        if isinstance(wish, dict)
        for key in ("text", "note")
    )
    return bool(allergens_named(text) or _ALLERGEN_TALK.search(text))


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
    # Read as `same_order` reads them: an old line sent again with its
    # options in another order is no change (deep review of #860, P1).
    before = [line_key(line) for line in turn.cart_at_start]
    changed = []
    for line in state.cart:
        key = line_key(line)
        if key in before:
            before.remove(key)
        else:
            changed.append(line)
    return changed


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
    model (`_wish_codes`, review of #798, P1). A wish note licenses only what
    the guest named in this turn: the model's own "ohne Erdnüsse" licenses
    no "Die 13 hat keine Erdnüsse" (deep review of #860, P1)."""
    changed = _changed_lines(turn, state)
    notes = [*turn.draft_notes, *(line.get("note") for line in changed)]
    return _codes(*(text for line in changed for text in _line_texts(state, line))) | (
        _codes(*notes) if health else _wish_codes(*notes) & turn.guest_codes
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
    return bool(unexplained_codes(turn, state))


def reacts_to(said: str, *, denied: bool = False) -> frozenset[str]:
    """What a guest's sentence names as what someone reacts to: the allergen
    letters, coeliac disease and the ingredient the wish parser reads
    ("Tomatenallergie", "allergisch gegen Kiwi"), read in each sentence from
    its first part that speaks of health on ("Ich habe eine Allergie, gegen
    Erdnüsse."): "Die Pizza mit Käse, ich habe eine Allergie." and "Ist da
    Erdnuss drin?" name nothing, nor does "Ich habe eine Allergie." (ledger
    #764 review). `denied`: also the parts that only deny ("Nein, keine
    Nussallergie."), left out otherwise.
    shortcut: an allergen in a dish after the health part ("Ich habe eine
    Allergie, die 13 mit Käse") counts too, the safe side."""
    found: set[str] = set()
    for sentence in SENTENCE.split(said):
        health = False
        for clause in sentence.split(","):
            for part in [clause] if denied else _not_denied(clause):
                health = health or speaks_of_health(part)
                if not health:
                    continue
                found.update(allergens_named(part))
                if names_coeliac(part):
                    found.add("zöliakie")
                if ingredient := _ingredient(part):
                    found.add(ingredient.casefold())
    return frozenset(found)


def sticky(state: ConversationState) -> bool:
    """The call keeps speaking of allergies: the guest named one
    (`allergy_named`), or the last sentence the guest heard was the core's
    table sentence."""
    return allergy_named(state) or (state.last_said or "").startswith(
        SAY_ALLERGENS_LEAD
    )


def allergy_named(state: ConversationState) -> bool:
    """The guest named an allergy in this call: an allergy word, coeliac
    disease or an allergen in a kept sentence; not every sentence the safety
    net keeps, "die Stoßzeit meiden" is none (#798)."""
    # A denial the safety net keeps ("Haben Sie Pizza? Ich habe keine
    # Allergie.") speaks of none, as in `begin_allergen_turn` (Codex review
    # of #860, P1).
    kept = (_guest_words(said) for said in state.allergy_said)
    return any(
        mentions_allergy(said) or names_coeliac(said) or allergens_named(said)
        for said in kept
    )


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
            # Any case: the core says "Nummer S12", the model's hits carry
            # "s12", and a copy with the stored form is still the core's (#987).
            rest = re.sub(re.escape(own), " ", rest, flags=re.IGNORECASE)
    return rest


def core_only(turn: AllergenTurn, state: ConversationState, say: str) -> bool:
    """Is `say` made of sentences the core wrote and nothing else (row 2)?"""
    return _WORDS.search(_without_core(turn, state, say)) is None


def about_a_dish(
    turn: AllergenTurn, state: ConversationState, say: str, *, changed: bool
) -> bool:
    """Is `say` a sentence of the model about a dish of the call that goes
    core-only in a call about an allergy (#928, Maxi: option B)? Its own words
    name a dish, or speak with a search's sentence that does ("Nummer 13,
    sehr gern. Ja, die können Sie essen."), and the turn took no order step;
    with an order step only when its own words speak of the allergy too ("Bei
    Ihrer Allergie ist die 13 kein Problem", next to the `cart` the model
    writes for every dish a search found clearly) (deep review of #928)."""
    own = _without_core(turn, state, say)
    if _WORDS.search(own) is None:
        return False
    if not _dishes_said(state, _with_search_says(turn, say, own)):
        return False
    return not changed or mentions_allergy(own) or names_coeliac(own)


def speaks_of_the_allergy(
    turn: AllergenTurn, state: ConversationState, say: str
) -> bool:
    """Do the model's own words of `say` speak of the guest's allergy: a word
    for their health ("Bei Ihrer Allergie ...", "Was Sie meiden müssen ...")
    or an allergen ("Ja, die ist ohne Erdnüsse")? Holds a retry after a
    refusal for a dish again (#928, Maxi: R1). An allergen counts here, as
    `unlicensed` licenses one in a sentence without a dish by the whole
    turn's menu text: the 47's option "Erdnuss" licensed "ohne Erdnüsse"
    (deep review of #997)."""
    own = _without_core(turn, state, say)
    return speaks_of_health(own) or bool(allergens_named(own))


def states_a_price(turn: AllergenTurn, state: ConversationState, say: str) -> bool:
    """Do the model's own words of `say` state a price: the answer to a price
    question (Maxi: option B2), read on the model's sentence, not the guest's
    (deep review of #928: a suitability promise gets no price)."""
    return _AMOUNT.search(_without_core(turn, state, say)) is not None


def price_says(turn: AllergenTurn, state: ConversationState, say: str) -> list[str]:
    """The core's sentence for each dish `say` names that this turn's search
    found without a wish: its price from the hit, which is the database
    (rule 1), or that it is sold out. What the guest hears in place of a
    refused sentence that states a price (#928, Maxi: option B2).
    shortcut: the base price without options, as the card prints it; a dish
    any search of the turn delivered with a wish ("die 47 mit Huhn") is left
    out, its own sentence names the difference."""
    own = _without_core(turn, state, say)
    named = _dishes_said(state, _with_search_says(turn, say, own))
    return [
        _price_say(hit)
        for item_id, hit in turn.found.items()
        if item_id in named
        and item_id not in turn.wished
        and isinstance(hit.get("price_cents"), int)
    ]


def _price_say(hit: dict[str, Any]) -> str:
    name, number = hit.get("name"), hit.get("number")
    dish = f"Die {spoken_card(number)}, {name}," if number else str(name)
    if hit.get("sold_out"):
        return f"{dish} ist heute leider aus."
    # "kostet einen Euro 50": `spoken_euro` writes a lone 1 as "ein".
    price = re.sub(r"^ein\b", "einen", spoken_euro(hit["price_cents"]))
    return f"{dish} kostet {price}."


# A price in the model's own words (`states_a_price`).
_AMOUNT = re.compile(r"\b(?:euro|eur|cent)\b|€", re.IGNORECASE)
# Prices, times and dates are no card number ("11,90", "19:30", "24.11.").
_FIGURES = re.compile(r"\d+(?:[.,:]\d+)+\.?")


def _with_search_says(turn: AllergenTurn, say: str, own: str) -> str:
    """The model's own words with the sentences of this turn's searches that
    `say` carries: an echo names the dish a pronoun after it speaks of."""
    # Any case, as `_without_core` removes it: "Nummer s12" copies "Nummer S12".
    lowered = say.lower()
    return " ".join(
        [own, *(said for said in turn.search_says if said.lower() in lowered)]
    )


def _dishes_said(state: ConversationState, text: str) -> set[str]:
    """The dishes of the call `text` names, read as the guest's words are: a
    card number also as a number word or without its leading zeros ("die
    Dreizehn", "013"), a whole name also without its count ("Frühlingsrollen
    (4 Stück)") (deep review of #928). Prices, times and dates name none.
    shortcut: a card number with a letter said apart ("35 A") and other
    numbers (an address, a party size) are read as said; a dish named by a
    word of its name only is missed (docs/24)."""
    text = _FIGURES.sub(" ", text)
    numbers = {str(n) for n in find_numbers(text)}
    lowered = text.lower()
    said = set()
    for item_id, seen in state.seen_items.items():
        number, name = seen.get("number"), seen.get("name")
        short = normalize_query(name) if isinstance(name, str) else ""
        if (
            _names_it(text, seen)
            or (isinstance(number, str) and canonical_card(number) in numbers)
            or (short and re.search(rf"(?<!\w){re.escape(short)}(?!\w)", lowered))
        ):
            said.add(item_id)
    return said


def unlicensed(turn: AllergenTurn, state: ConversationState, say: str) -> set[str]:
    """The allergen words of the model's sentence that nothing of this turn
    licenses (rows 5 and 6), as letters, "allergen" for a word about
    allergens no letter licenses. Licensed: a letter of the turn's menu text,
    of the dish and option names of the order, or of the order text accepted
    in this turn; in a clause that names a dish of the call (card number or
    whole name), only that dish's own text and lines license, every named
    dish for itself, so the option "Erdnuss" of the 47 licenses no "die 13
    hat keine Erdnüsse" nor "die 47 und die 13 enthalten keine Erdnüsse".
    shortcut: a sentence about two dishes that is true of one ("Die 47 mit
    Erdnusssauce und die 13 sind notiert") goes back once. Whole dish
    names the search delivered in this call are menu data and are left out
    ("Erdnuss-Curry").
    A clause that names no dish speaks of the dishes its sentence names, or
    else of those the sentence before it named: "Die 13? Nein, keine
    Erdnüsse." and "Bei der 47 und der 13: keine Erdnüsse." (deep review of
    #860, P1).
    shortcut: a sentence that names no dish while none before it did ("Sie
    hat keine Erdnüsse.") is licensed by the whole turn; the residual R1 of
    the design."""
    rest = _without_core(turn, state, say)
    changed = _changed_lines(turn, state)
    words: set[str] = set()
    before: set[str] = set()
    for sentence in re.split(r"[.!?]+", rest):
        clauses = [(c, _dishes_named(state, c)) for c in re.split(r"[,;:]+", sentence)]
        before = set().union(*(own for _, own in clauses)) or before
        for clause, own in clauses:
            named = own or before
            if len(named) < 2:
                # A whole dish name is menu data in a clause about that dish
                # alone. Next to another dish it is checked like any word: a
                # seen "Milch" or "Erdnusssauce" licenses no "die 13 enthält
                # keine Milch" (Codex review of #860, P1).
                clause = _without_dish_names(state, clause)
            words |= allergens_named(clause) - _licence(turn, state, changed, named)
            if _ALLERGEN_TALK.search(clause):
                words.add("allergen")
    return words


def _dishes_named(state: ConversationState, clause: str) -> set[str]:
    return {
        item_id for item_id, seen in state.seen_items.items() if _names_it(clause, seen)
    }


def _licence(
    turn: AllergenTurn,
    state: ConversationState,
    changed: list[dict[str, Any]],
    named: set[str],
) -> set[str]:
    """The letters a clause about the `named` dishes may say. Each named dish
    licenses by its own text and lines, and a word needs all of them: the
    47's option "Erdnuss" licenses no "Die 47 und die 13 enthalten keine
    Erdnüsse" (Codex review of #860, P1). No dish: the whole turn."""
    if named:
        return set.intersection(
            *(_dish_licence(turn, state, changed, i) for i in named)
        )
    return (
        turn.menu_codes
        | _order_codes(turn, state)
        | _codes(*(t for line in state.cart for t in _line_texts(state, line)))
    )


def _dish_licence(
    turn: AllergenTurn,
    state: ConversationState,
    changed: list[dict[str, Any]],
    item_id: str,
) -> set[str]:
    """The letters one dish licenses: its menu text of this turn, the dish
    and option names of its lines in the order, the wish notes of its lines
    that changed in this turn as far as the guest named them in this turn
    (`_order_codes`)."""
    lines = [line for line in state.cart if line["menu_item_id"] == item_id]
    new = [line for line in changed if line["menu_item_id"] == item_id]
    return (
        turn.dish_codes.get(item_id, set())
        | _codes(*(t for line in lines for t in _line_texts(state, line)))
        | _wish_codes(*(line.get("note") for line in new)) & turn.guest_codes
    )


def _names_it(clause: str, seen: dict[str, Any]) -> bool:
    """Does the clause name the dish: its card number as a token of its own,
    or its whole name?"""
    return any(
        isinstance(text, str)
        and text
        and re.search(rf"(?<!\w){re.escape(text)}(?!\w)", clause, re.IGNORECASE)
        for text in (seen.get("number"), seen.get("name"))
    )


def _without_dish_names(
    state: ConversationState, text: str, *, plain_only: bool = False
) -> str:
    """`text` without the whole names of the dishes the call found.
    `plain_only`: only names with a word that names no allergen ("Nudeln mit
    Garnelen"); "Erdnusssauce" or "Milch" said by the guest may be the
    allergen they ask about."""
    names = {seen.get("name") for seen in state.seen_items.values()}
    names |= {name for _, name in state.offered_items.values()}
    if plain_only:
        names = {
            n
            for n in names
            if isinstance(n, str)
            and not all(allergens_named(w) for w in re.findall(r"\w+", n))
        }
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
    explains (`ConversationState.allergen_question_open` and
    `allergen_question_codes`)."""
    return (turn.speaks and not _noted(turn, state)) or _unexplained(turn, state)


def unexplained_codes(turn: AllergenTurn, state: ConversationState) -> set[str]:
    """The allergen letters the guest named that neither the menu text of
    this turn nor a changed order line explains."""
    return (
        set(turn.guest_codes) - turn.menu_codes - _order_codes(turn, state, health=True)
    )


def codes_left(turn: AllergenTurn, state: ConversationState, say: str) -> list[str]:
    """The letters this open turn leaves unexplained, kept for the next turn
    (`ConversationState.allergen_question_codes`). None when the turn ends
    by asking the guest about the allergy: their next words answer it
    ("Worauf genau soll die Küche achten?" - "Sesam.")."""
    if SAY_ASK_ALLERGY in say or not leaves_a_question(turn, state):
        return []
    return sorted(unexplained_codes(turn, state))


def left_twice(turn: AllergenTurn, state: ConversationState, say: str) -> bool:
    """Does this open turn leave the guest's question open a second time in a
    row (rule 5)? The turn before ended in the offer and this one does again,
    or an allergen the turn before left unexplained is unexplained again,
    whatever core sentence ends the turn: a model that answers every
    question with "Ihren Hinweis gebe ich an die Küche weiter." hands over
    too (deep review of #860, P1). Decided by where the words come from, not
    by question or statement: a second, other allergy named in a row hands
    nothing over. The offer again needs no new allergen word: the guest's
    answer to it is a dish ("Die 13."), and asking for the number twice
    answers nothing (Codex review of #860, P1)."""
    if turn.pending and SAY_ALLERGENS_OFFER in say:
        return True
    if not leaves_a_question(turn, state):
        return False
    return bool(turn.asked_before & unexplained_codes(turn, state))


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
# The core's block as a model may copy it, in any case and spacing.
_EARLIER_BLOCK = re.compile(
    r"wichtig\s*!!!\s*(?:der\s+)?gast\s+sagte\s*:", re.IGNORECASE
)
# A note that only repeats the example of a hint names nothing.
_TEMPLATE = ("…", "<", "WICHTIG-Hinweis")


def unnoted_allergy(state: ConversationState, call: ToolCall) -> ToolResult | None:
    """A draft for a guest who spoke of their health in this call
    (`state.allergy_said`), with no note that names it: the kitchen would
    never hear of it. A note passes by `wishes.HEALTH_NOTE`, the pattern the
    retention job deletes such a note by, so what passes here is deleted in
    time (L13). The hint quotes the guest. The loop gives it back once per
    turn, and writes the guest's words on the next one itself
    (`with_the_guests_words`), never a loop.

    Every allergen the guest named as what someone reacts to must be on such
    a note, compared by letter whatever the wording, coeliac disease as
    gluten, an ingredient without a letter ("Kiwi") by its word: a second
    allergy said after the read-back ("Ach, und meine Frau verträgt keinen
    Sesam.") must not pass on the first one's note (#929). What a kept
    sentence names stays asked for: a bare retraction is not kept, and
    `loop._taken_back` clears what a denial answering a question takes back.
    The hint quotes only the sentences no note covers and asks for the
    guest's words, for an order on the dish named right before or after the
    fixed question about the dish."""
    if not state.allergy_said or call.name not in NOTED:
        return None
    health = [
        note
        for note in _notes(call)
        if HEALTH_NOTE_RE.search(note) and not any(t in note for t in _TEMPLATE)
    ]
    if not health:
        hint = HINT_UNNOTED_ALLERGY + _HINT_UNNOTED_SAID.format(said=_said(state))
        return ToolResult(ok=False, error_code="invalid_input", hint=hint)
    # An allergy a note denies ("keine Sesamallergie") notes nothing; "kein
    # Sesam" is what the kitchen must do and counts.
    noted_text = " ".join(_guest_words(note) for note in health)
    noted = _letters(noted_text)
    unnoted = [
        sentence
        for sentence in state.allergy_said
        if any(
            mark not in noted and not _word_in(mark, noted_text)
            for mark in _named_as_theirs(state, sentence)
        )
    ]
    if not unnoted:
        return None
    quoted = " ".join(s.strip() for s in unnoted).rstrip(" .")
    where = (
        _HINT_LATER_ORDER.format(ask=SAY_ASK_ALLERGY_DISH)
        if call.name == "draft_order"
        else _HINT_LATER_TABLE
    )
    hint = HINT_UNNOTED_LATER.format(said=quoted) + where
    return ToolResult(ok=False, error_code="invalid_input", hint=hint)


def _word_in(word: str, text: str) -> bool:
    """Does `text` hold `word` at the start of one of its words ("Kiwi" in
    "Kiwiallergie"), never inside one ("Erbsen" in "Kichererbsen")?"""
    return re.search(rf"(?<!\w){re.escape(word)}", text.casefold()) is not None


def _letters(text: str) -> set[str]:
    """The allergen letters `text` names, coeliac disease as gluten."""
    return allergens_named(text) | (_GLUTEN if names_coeliac(text) else set())


def _named_as_theirs(state: ConversationState, said: str) -> set[str]:
    """What a kept sentence names: its allergen letters (`_letters`) without
    what it only denies (`_guest_words`) and without the plain names of
    dishes the call found, and the casefolded word of an ingredient without
    a letter ("kiwi", `reacts_to`). No reading of which word is a wish and
    which an allergy (AGENTS.md, #798; Codex review of #1002, P1): "Ich habe
    eine Erdnussallergie, bitte extra Sesam" asks for the sesame too, and the
    hint asks for the guest's words, which name it, so the note says what
    the guest said and never an allergy the model read into it."""
    text = _without_dish_names(state, said, plain_only=True)
    return _letters(_guest_words(text)) | {
        word
        for word in reacts_to(text) - allergens_named(text) - {"zöliakie"}
        if not allergens_named(word)
    }


def with_the_guests_words(state: ConversationState, call: ToolCall) -> ToolCall | None:
    """The draft with the guest's own words about their health on the note of
    the whole order or of the table, behind what the model wrote there, cut
    to the field: the model sent it again without them. Words, never a
    reading of them (Maxi, 15.11.2026). One that would not fit next to the
    model's note takes the field alone: the health part is the one to keep.
    None when the cut would drop what a kept sentence says ("..., ich habe
    eine Erdnussallergie" at the end of a long sentence, Codex PR #788, P1;
    a later allergy behind a long first sentence, ledger #764 review,
    `_each_survives`): the team takes the call instead (rule 5)."""
    added = NOTED_FOR_THE_GUEST.format(said=_said(state))
    limit = NOTE_MAX_FOR[call.name]
    before = call.args.get("note")
    if isinstance(before, str):
        before = _without_earlier_block(state, before)
    note = (
        f"{before.strip()}. {added}"
        if isinstance(before, str) and before.strip()
        else added
    )
    if len(note) > limit:
        note = added[:limit].rstrip()
        kept = note.removeprefix(NOTED_FOR_THE_GUEST.format(said=""))
        if not _each_survives(state, len(kept)):
            return None
    return ToolCall(call.name, {**call.args, "note": note})


# What a cut may take from the end of a sentence without taking anything said.
_END_MARKS = " .!?…"


def _without_earlier_block(state: ConversationState, note: str) -> str:
    """`note` without the core's earlier block a redraft carries: the new
    block holds all it said, so no sentence is noted twice (#929). What the
    model wrote behind it stays ("... Bitte getrennt verpacken.", Codex
    review of #1002, P2): the block's sentences are the kept ones, taken off
    one by one from its start.
    shortcut: a sentence the old block held that is no longer kept (taken
    back) stays as the model's text."""
    head, *rest = _EARLIER_BLOCK.split(note, maxsplit=1)
    tail = rest[0].strip() if rest else ""
    # An empty one would match forever.
    kept = [k for k in (s.strip().rstrip(" .") for s in state.allergy_said) if k]
    while hit := next((s for s in kept if tail.lower().startswith(s.lower())), None):
        tail = tail[len(hit) :].lstrip(" .")
    return ". ".join(
        part.strip().rstrip(" .") for part in (head, tail) if part.strip(" .")
    )


def _each_survives(state: ConversationState, room: int) -> bool:
    """Does every kept sentence keep what it says within the first `room`
    characters of `_said`? One about health keeps its health words and all
    it names (`reacts_to`); one that only says yes ("Ja, genau.") may go; any
    other answer ("Gegen Erdnüsse.") stays whole but for its last mark. A
    later allergy behind a long first sentence must not be cut off the note
    (ledger #764 review).
    shortcut: a health part the wish parser does not read ("Ich reagiere auf
    Erdbeeren") only keeps its health words; the read-back voices the cut."""
    start = 0
    for sentence in (s.strip() for s in state.allergy_said):
        left = sentence[: max(room - start, 0)]
        start += len(sentence) + 1
        if speaks_of_health(sentence):
            kept = speaks_of_health(left) and reacts_to(sentence) <= reacts_to(left)
        else:
            kept = is_yes(sentence) or (
                left.rstrip(_END_MARKS) == sentence.rstrip(_END_MARKS)
            )
        if not kept:
            return False
    return True


def _said(state: ConversationState) -> str:
    # Without the closing full stop: a read-back puts its own behind a note.
    return " ".join(sentence.strip() for sentence in state.allergy_said).rstrip(" .")


def claims_in_a_note(state: ConversationState, call: ToolCall) -> ToolResult | None:
    """A note of a draft that promises a dish free of an allergen ("nussfrei",
    "keine Spuren"), names a dish ("Erdnussallergie. Die 13 hat keine.") or
    names an allergen nobody licensed: read back word for word, it is the
    model's second way to the guest's ear (#798), so it goes back. The same
    lists as the model's sentence (`allergen_claim`); an additive has its
    own way (`notes_an_additive`)."""
    if call.name not in NOTED:
        return None
    items = call.args.get("items")
    lines = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    note = call.args.get("note")
    notes: list[tuple[str, str | None]] = (
        [(note, None)] if isinstance(note, str) else []
    )
    notes += [
        (i["note"], str(i.get("menu_item_id")))
        for i in lines
        if isinstance(i.get("note"), str)
    ]
    return _claims_in(state, notes, [str(i.get("menu_item_id")) for i in lines])


def _claims_in(
    state: ConversationState,
    notes: list[tuple[str, str | None]],
    ids: list[str],
) -> ToolResult | None:
    """`notes`: each note with the dish of its line, None for the note of the
    whole order or table, which stands on all `ids`."""
    # A note the order already carries on the same dish, or as the note of
    # the whole order, was checked when it came: the draft carries the
    # line's "ohne Erdnüsse" on although this turn names none. Moved to
    # another dish it is new there (Codex review of #860, P1).
    lines = {(str(line["menu_item_id"]), line.get("note")) for line in state.cart}
    whole = {state.order_note, state.table_note}
    for note, item_id in notes:
        if allergen_claim(state, note) is ALLERGEN_CLAIM or _names_a_dish(state, note):
            return NOTE_CLAIM
        old = (item_id, note) in lines if item_id else note in whole
        on = [item_id] if item_id else ids
        if not old and allergens_named(note) - _note_licence(state, on):
            # Codex review of #860 (P1): "Enthält keine Erdnüsse" on a line
            # the guest only ordered would be read back as their note.
            return NOTE_CLAIM
    return None


def _note_licence(state: ConversationState, ids: list[str]) -> set[str]:
    """The allergen letters a new note may name: those the guest named in
    this call (`allergens_said`, so a wish said turns before the draft still
    counts), the guest's health sentences (coeliac disease names gluten), and
    the dish and option names of the note's dishes."""
    said = " ".join(state.allergy_said)
    codes = set(state.allergens_said) | allergens_named(said)
    if names_coeliac(said):
        codes |= _GLUTEN
    for item_id in ids:
        seen = state.seen_items.get(item_id, {})
        groups = [*seen.get("required", []), *seen.get("optional", [])]
        codes |= _codes(
            seen.get("name"), *(o for g in groups for o in g.get("options", []))
        )
    return codes


def _names_a_dish(state: ConversationState, note: str) -> bool:
    """Does the note name a dish the search delivered in this call, by its
    card number after "Nummer" or an article ("Die 13 hat keine") or by its
    whole name? A bare number is no card number, as in `names_dish`: "bis
    13 Uhr" or "Klingel 13" go back on every hop and end the call (deep
    review of #860, P2). The guest's own sentences about their health,
    which the prompt asks the model to quote, are cut first: they are the
    guest's words, not the model's (#798).
    shortcut: an article before a number that happens to be a card number
    ("die 2 Stäbchen") counts too; the note goes back once and the model
    writes it again."""
    rest = note.lower()
    for said in state.allergy_said:
        cut = said.strip().rstrip(" .").lower()
        if cut:
            # An empty cut would split every letter (deep review of #860, P1).
            rest = rest.replace(cut, " ")
    return any(
        (
            seen.get("number")
            and re.search(
                r"\b(?:nummer|nr\.?|die|der|den|dem|das|zur|zum)\s*"
                rf"{re.escape(str(seen['number']).lower())}(?!\w)",
                rest,
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
    if (
        call is None
        or call.name != "confirm"
        or not answers_the_read_back(user_text, state.readback)
    ):
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
        and answers_the_read_back(user_text, state.readback)
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
