"""Kompakter Gesprächszustand statt wachsendem Verlauf (docs/05 §5).

Nach jedem Zug wird der Stand zusammengefasst und dem Modell mitgegeben, statt den
ganzen bisherigen Wortlaut erneut zu schicken: der Zustand wächst nicht mit der
Gesprächsdauer, der Verlauf schon.
"""

import json
import uuid
from contextlib import suppress
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from api.agent.dispatch import ToolResult
from api.agent.guest_details import (
    clean_name,
    e164,
    name_said,
    names_a_phone_number,
    number_known,
    numbers_said,
    words_said,
)
from api.agent.intent import heard
from api.core.errors import InvalidInput
from api.domain.customers.phone import normalize_phone
from api.domain.menu.items import option_key
from api.domain.menu.search import CLEAR_MATCHES
from api.schemas.orders import MAX_ITEMS, OrderItemIn

Stage = Literal[
    "start",
    "collecting",
    "readback_pending",
    "confirmed",
    "callback",
    "transferred",
    "ended",
]


# Longest `last_say` the model gets back, in characters: a read-back with
# three dishes fits, a model that talks on is cut.
LAST_SAY_MAX = 300


def last_words(say: str) -> str:
    """The sentence as `last_say` keeps it. A long one keeps its end: the
    guest answers the question it closes with, such as the "Passt das so?" of
    a read-back of six dishes (Codex PR #291)."""
    if len(say) <= LAST_SAY_MAX:
        return say
    return "…" + say[-(LAST_SAY_MAX - 1) :]


# What the state keeps of get_service_status: whether the restaurant is open,
# and whether it takes pickups, which can be off while it is open.
SERVICE_FLAGS = ("is_open", "pickup_enabled")


class ConversationState(BaseModel):
    call_id: uuid.UUID
    tenant_id: uuid.UUID
    intent: str | None = None
    stage: Stage = "start"
    slots: dict[str, Any] = Field(default_factory=dict)
    open_questions: list[str] = Field(default_factory=list)
    reservation_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    # The sentence of the draft that waits for its yes, as the draft tool
    # returned it, and whether the guest heard it (`guards.note_said`). A yes
    # can only confirm what was read out (CLAUDE.md §2 rule 3, #251).
    readback: str | None = None
    readback_heard: bool = False
    transferred: bool = False
    # What a `confirm` booked in this call and no later draft of the same kind
    # replaced: "order", "reservation". Memory of the core, never sent to the
    # model: no sentence may tell the guest that something is booked that is
    # not (`guards.false_booking`).
    booked: set[str] = Field(default_factory=set)
    # The pickup code the confirmed order got, the only one a sentence may
    # name (`guards.false_booking`). Memory of the core, like `booked`.
    pickup_code: str | None = None
    # The lines of the confirmed order: a `cart` with other lines starts a
    # new order, which nothing has booked yet (`replace_cart`).
    booked_cart: list[dict[str, Any]] = Field(default_factory=list)
    # Memory of the core, never sent to the model (guards.py, rule 2): the
    # dishes an order may name. `known` came from a clear match of search_menu,
    # or was a candidate the guest heard by name or card number. `offered` are
    # the candidates of an unclear result of this turn, id to (number, name),
    # until the sentence that ends the turn shows which of them were named.
    known_item_ids: set[str] = Field(default_factory=set)
    offered_items: dict[str, tuple[str, str]] = Field(default_factory=dict)
    # The order as it stands, lines in the form of `items` of draft_order. The
    # model writes it (`LLMTurn.cart`), the core takes it only with dishes it
    # knows (`guards.take_cart`), and it goes back with every turn: a model
    # keeps nothing between turns, and without it a dish found in one turn is
    # gone in the next.
    cart: list[dict[str, Any]] = Field(default_factory=list)
    # What search_menu said about a dish: id to number, name and mandatory
    # option groups. Never sent as it is. The labels of the `cart` lines in the
    # prompt come from here, never from the model (CLAUDE.md §2 rule 1).
    #
    # Only the order goes back to the model, not what else a search delivered.
    # A first version also showed the dishes a search had found and the order
    # did not hold yet; on a real model the candidate the guest had NOT chosen
    # stayed in sight, and two turns later the model ordered it (07.10.2026).
    # ponytail: an answer like "die erste" to an offer can therefore not be
    # resolved from the state, the model searches again or asks. A view of the
    # open question for the one following turn if the call log shows the need.
    seen_items: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Memory of the core, never sent to the model (agent/guest_details.py,
    # rule 2): the number the guest calls from, and the words and numbers the
    # guest said in this call. A name or phone number a model writes is taken
    # only when it stands in there. No sentence is kept.
    caller_phone: str | None = None
    # Each word with how often the guest said it in one sentence at most.
    said_words: dict[str, int] = Field(default_factory=dict)
    said_numbers: set[str] = Field(default_factory=set)
    # The sentence that ended the last turn asked for the guest's number
    # (`guards.note_said`): the answer needs no word for what its digits are.
    asked_for_phone: bool = False
    # The guest has heard a first sentence: the model must not greet a second
    # time. Set by the phone line (telephony/handler.py), which says the AI
    # disclosure before the first turn, and with the first sentence of a call
    # on the text phone (`guards.note_said`), where the model says it.
    greeted: bool = False
    # The sentence that ended the last turn (`guards.note_said`), cut to
    # `LAST_SAY_MAX`. A model keeps nothing between turns: without it a "Ja",
    # a name or "nein, das wars" answers a question the model no longer knows
    # it asked (first full eval run, 13.10.2026). One sentence, replaced with
    # every turn, so the state stays compact and never grows into a transcript
    # (docs/05 §5).
    last_say: str | None = None
    # What get_service_status said in this call about being open and taking
    # pickups (`SERVICE_FLAGS`); None until then. The model reads from it that
    # the status is known: it asked again at the start of every turn, one
    # request each. Both flags, or a model that sees only "open" could take a
    # pickup while the kitchen takes none (Codex PR #291, P1).
    service: dict[str, bool] | None = None
    # Team extension from transfer_to_team, for the phone line to dial. Not part
    # of the prompt: the model has no use for the number.
    transfer_to: str | None = None

    def to_prompt_json(self) -> dict[str, Any]:
        """Format aus docs/05 §5: geht bei jedem Zug ans Modell statt des Verlaufs."""
        data: dict[str, Any] = {"stage": self.stage, "open": self.open_questions}
        if self.greeted:
            data["greeted"] = True
        if self.last_say:
            data["last_say"] = self.last_say
        if self.service is not None:
            data["service"] = dict(self.service)
        if self.intent:
            data["intent"] = self.intent
        if self.slots:
            data["slots"] = self.slots
        if self.reservation_id:
            # Ohne das kann ein zustandsloses LLMClient auf dem "Ja" nach dem
            # readback kein entity_id für `confirm` liefern (Codex-Review PR #101, P1).
            data["reservation_id"] = str(self.reservation_id)
        if self.order_id:
            # Dasselbe fuer Bestellungen: confirm braucht entity_id.
            data["order_id"] = str(self.order_id)
        if (
            self.stage == "readback_pending"
            and self.readback
            and not self.readback_heard
        ):
            # The draft is not what the guest heard last. A `confirm` is
            # refused until it is, and a model that keeps nothing between
            # turns needs the sentence to read it out. Gone again once heard.
            data["readback"] = self.readback
        if self.cart or self.seen_items:
            # From the first menu search on, also while it is empty: the model
            # has to write the dish it just found into it, and the client sends
            # the rules for that only when the state carries the field
            # (`agent/llm.py` `ORDER_FORMAT`). A call about a table never does.
            data["cart"] = [self._line(line) for line in self.cart]
        return data

    def _line(self, line: dict[str, Any]) -> dict[str, Any]:
        """A line of the order as the model reads it: with the number and name
        of the card, and with `open` for mandatory groups nothing was chosen
        from yet. Without `open` a model would learn of a missing choice only
        from a failed draft_order, and by then no longer know the group."""
        seen = self.seen_items.get(line["menu_item_id"], {})
        chosen = {option_key(option["group"]) for option in line.get("options", [])}
        shown = {**line, **{k: seen[k] for k in ("number", "name") if seen.get(k)}}
        still_open = [
            group
            for group in seen.get("required", [])
            if option_key(group["group"]) not in chosen
        ]
        if still_open:
            shown["open"] = still_open
        return shown


def initial_state(
    call_id: uuid.UUID, tenant_id: uuid.UUID, caller_id: str | None = None
) -> ConversationState:
    """Zustand zu Gespraechsbeginn. Der Gast ruft an, seine Nummer kennt die
    Rufnummernerkennung schon: sie steht vorab in `slots.phone`, und der Agent
    fragt nicht danach (Maxi, PR #127). Nennt der Gast eine andere, ueberschreibt
    sein `state_patch` sie. Unterdrueckt oder keine gueltige Nummer: der Slot
    bleibt leer, und der Agent fragt wie bisher."""
    caller_phone = None
    if caller_id:
        with suppress(InvalidInput):
            caller_phone = normalize_phone(caller_id)
    return ConversationState(
        call_id=call_id,
        tenant_id=tenant_id,
        slots={"phone": caller_phone} if caller_phone else {},
        caller_phone=caller_phone,
    )


# What a guest can say about a reservation or an order. Anything else a model
# puts into its patch is dropped: `slots` goes back into the next prompt as what
# the guest said, and a tool result copied there (`open`, `closes_at`, a price)
# would be answered from on a later turn instead of asked from the database
# (CLAUDE.md §2 rule 1). Extend it when a flow needs a new guest detail.
GUEST_SLOTS = frozenset({"party_size", "reserved_for", "guest_name", "phone", "note"})


def apply_state_patch(
    state: ConversationState, patch: dict[str, Any]
) -> dict[str, Any]:
    """Vom Modell gelieferte Gesprächsdetails in den kompakten Zustand übernehmen
    (`LLMTurn.state_patch`), bevor sie mit dem nächsten Zug verloren gehen. Reine
    Gesprächsangaben (Name, Datum, Personenzahl, ...), keine Fachdaten aus der DB
    (CLAUDE.md §2 Regel 1 betrifft Preise/Zeiten/Verfügbarkeit/Allergene, nicht das,
    was der Gast gesagt hat). Returns what was taken: only `GUEST_SLOTS`, and
    of those a name or a phone number only when the guest said it
    (`agent/guest_details.py`). What is dropped leaves the slot as it was: an
    invented number never replaces the caller ID. The number the guest gave
    stands in `slots` before the model answers (`note_guest_details`); a model
    may only choose between that one and the caller ID."""
    named = {
        key: value
        for key, value in patch.items()
        if key in GUEST_SLOTS and _said_by_the_guest(state, key, value)
    }
    if "phone" in named:
        # One spelling from entry on, as for the caller ID (CLAUDE.md §8).
        named["phone"] = e164(named["phone"])
        _number_set(state, named["phone"])
    if "guest_name" in named:
        # As it was checked: the blanks around a name are no part of it.
        named["guest_name"] = clean_name(named["guest_name"])
    state.slots.update(named)
    return named


def _number_set(state: ConversationState, number: str) -> None:
    """Call before `slots.phone` becomes `number`. The draft that waits for
    its yes was made under the number the state held: with another one it is
    no longer what the guest wants, and the readback does not name the
    number, so a later yes would book under the one the guest replaced
    (CLAUDE.md §2 rule 3). The draft is dropped as after any correction; the
    kind of booking stays what it was."""
    if state.stage == "readback_pending" and number != state.slots.get("phone"):
        _drop_readback(
            state, "create_reservation" if state.reservation_id else "draft_order"
        )


def _said_by_the_guest(state: ConversationState, slot: str, value: Any) -> bool:
    if slot == "guest_name":
        return name_said(value, state.said_words)
    if slot == "phone":
        return number_known(value, state.caller_phone, state.said_numbers)
    return True


def note_guest_details(state: ConversationState, guest_text: str) -> None:
    """Keeps the words and the number of a guest sentence, for every turn
    after it. Call it before the model is asked: what the guest names in a
    sentence is written by the model in the answer to that very sentence.

    A word counts as often as one sentence held it: "Müller" said in two
    turns is no "Müller Müller" (Codex PR #244, P2).

    Digits are a number to call only when the sentence says so or answers
    the agent's question for one (`guest_details.names_a_phone_number`;
    Codex PR #244, P1). Such a number goes into `slots` here, by the guest's
    own words: a guest who names another number than the one they call from
    is reached under that one (docs/05 §3), and a model that does not write
    it down cannot book under the caller ID all the same, since a tool books
    under the number in `slots` (`guards._phone_counts`; Codex PR #244, P2)."""
    for word, count in words_said(guest_text).items():
        state.said_words[word] = max(state.said_words.get(word, 0), count)
    if state.asked_for_phone or names_a_phone_number(guest_text):
        for number in numbers_said(guest_text):
            state.said_numbers.add(number)
            _number_set(state, number)
            state.slots["phone"] = number


def note_intent(state: ConversationState, guest_text: str) -> None:
    """Keeps what the guest called for (`agent/intent.py`) for the turns after
    this one. It only fills an empty `intent`: a word in a later sentence
    never replaces what the state holds (a side question about taking the dog
    along, in a call about a table), and from the draft on `intent` follows
    the tools as before. It is also what the call log says the call was about
    (`agent/outcome.py`).

    And never against what the state shows already: a guest who gave a party
    size without the word for a table asks whether the leftovers can be taken
    along, and a guest whose order stands in the state asks at which table to
    wait.
    ponytail: a guest who changes their mind before a draft, or wants a second
    thing after a booking, keeps the first wish in the state until a tool of
    the other flow drafts. The model reads the change in the sentence itself,
    and from the first menu search on the state carries the order (`cart`)."""
    if state.intent is not None or (wish := heard(guest_text)) is None:
        return
    # An order outranks table details, and those a search that may have been
    # only a question about the menu.
    if state.cart:
        shown = "pickup"
    elif state.slots.keys() & {"party_size", "reserved_for"}:
        shown = "reservation"
    elif state.seen_items:
        shown = "pickup"
    else:
        shown = wish
    if shown == wish:
        state.intent = wish


_LINES: TypeAdapter[list[OrderItemIn]] = TypeAdapter(
    Annotated[list[OrderItemIn], Field(max_length=MAX_ITEMS)]
)


def cart_lines(raw: Any) -> list[dict[str, Any]]:
    """`items` of draft_order, or the `cart` of a model, as the lines the state
    keeps: the same check as draft_order, and one form for the same order
    (`options: []` and no options are the same line). Raises pydantic's
    `ValidationError`. Which dishes a line may name is not checked here
    (`guards.take_cart`)."""
    lines = []
    for item in _LINES.validate_python(raw):
        line: dict[str, Any] = {
            "menu_item_id": str(item.menu_item_id),
            "quantity": item.quantity,
        }
        if item.options:
            line["options"] = [option.model_dump() for option in item.options]
        if note := (item.note or "").strip():
            line["note"] = note
        lines.append(line)
    return lines


def replace_cart(state: ConversationState, lines: list[dict[str, Any]]) -> None:
    """The order as the model now understands it. A change after the readback
    is a correction: the draft that was read out is no longer what the guest
    wants, and a later yes must not confirm it (CLAUDE.md §2 rule 3)."""
    if same_order(lines, state.cart):
        # The state keeps the sequence that was read out (Codex PR #237, P2).
        return
    if "order" in state.booked and lines and not same_order(lines, state.booked_cart):
        # A second order in the call: the first one's booking and code do not
        # speak for it (Codex PR #347). The confirmed order repeated as `cart`
        # in the closing answer is none.
        state.booked.discard("order")
        state.pickup_code = None
    order_corrected(state)
    state.cart = lines


def same_order(lines: list[dict[str, Any]], other: list[dict[str, Any]]) -> bool:
    """The same order, whatever the sequence of its lines and options and the
    spelling of an option."""
    return _as_set(lines) == _as_set(other)


def _as_set(lines: list[dict[str, Any]]) -> list[str]:
    """Lines for comparison. Sorted, not a set: two equal lines are two."""
    return sorted(
        json.dumps(
            {
                **line,
                "options": sorted(
                    (option_key(option["group"]), option_key(option["name"]))
                    for option in line.get("options", [])
                ),
            },
            sort_keys=True,
        )
        for line in lines
    )


def order_corrected(state: ConversationState) -> None:
    """Drops the draft that waits for its yes, an order or a reservation. A
    reservation too: the guest who goes on with the order after a table was
    read back has moved on, as with a menu search (`_supersedes`), and a yes
    to a question about the order must not book the table (Codex PR #237, P1)."""
    if state.stage == "readback_pending":
        _drop_readback(state, "draft_order")


def apply_tool_result(
    state: ConversationState,
    name: str,
    result: ToolResult,
    args: dict[str, Any] | None = None,
) -> None:
    """Schreibt ein erfolgreiches Tool-Ergebnis in den Zustand. Fehlschläge ändern
    den Zustand nicht: das Modell entscheidet auf Basis von `say`/`error_code`,
    was als Nächstes versucht wird (Verständnis-Leiter, T-2.2).

    Ausnahme: der Gast korrigiert nach dem Vorlesen. Dann ist der vorgelesene
    Entwurf nicht mehr, was er will, auch wenn die Korrektur scheitert."""
    if state.stage == "readback_pending" and _supersedes(name, result):
        _drop_readback(state, name)
    if state.stage == "readback_pending" and _refused_for_good(name, result):
        # The intent stays: the guest still wants the table or the order, only
        # not this draft. `_drop_readback` would set it from the tool's name.
        intent = state.intent
        _drop_readback(state, name)
        state.intent = intent
    if not result.ok:
        return
    if (
        name in ("create_reservation", "draft_order")
        and result.data.get("status", "draft") != "draft"
    ):
        # The same key replays a draft that was decided since: the confirmed
        # order comes back as it is. It is no new draft, and taking it as one
        # dropped the booking and its pickup code (Codex PR #347).
        return
    # Aktiv ist der Vorgang, der zuletzt vorgelesen wurde: wechselt der Gast
    # zwischen Reservierung und Bestellung, faellt die andere ID heraus. Sonst
    # stuenden zwei bestaetigbare Vorgaenge im Prompt, und das naechste Ja
    # koennte den verlassenen bestaetigen (Codex PR #127).
    if name == "create_reservation":
        state.intent = "reservation"
        state.reservation_id = uuid.UUID(result.data["reservation_id"])
        state.order_id = None
        state.stage = "readback_pending"
        state.booked.discard("reservation")
        _to_read_out(state, result)
    elif name == "draft_order":
        state.intent = "pickup"
        state.order_id = uuid.UUID(result.data["order_id"])
        state.reservation_id = None
        state.stage = "readback_pending"
        state.booked.discard("order")
        state.pickup_code = None
        _to_read_out(state, result)
        # The order in the state is the one that was read out, also when the
        # model drafted without writing `cart` first: a correction after the
        # readback starts from these lines.
        with suppress(ValidationError):
            state.cart = cart_lines((args or {}).get("items"))
    elif name == "search_menu":
        _note_search(state, result.data)
    elif name == "get_service_status":
        flags = {
            key: result.data[key]
            for key in SERVICE_FLAGS
            if isinstance(result.data.get(key), bool)
        }
        # Only a whole answer: a flag that is missing is unknown, never "no".
        if len(flags) == len(SERVICE_FLAGS):
            state.service = flags
    elif name == "confirm":
        if state.order_id is not None:
            # Booked: no longer an order that is being put together.
            state.booked_cart = state.cart
            state.cart = []
        state.stage = "confirmed"
        state.booked.add("order" if state.order_id is not None else "reservation")
        if state.order_id is not None:
            state.pickup_code = result.data.get("pickup_code")
        # Booked: nothing waits to be read out. Left in place, the sentence
        # would go back to the model with the next turn, as if it still had to
        # read a draft that no longer is one (Codex PR #258).
        state.readback = None
        state.readback_heard = False
    elif name == "create_callback":
        state.stage = "callback"
    elif name == "transfer_to_team" and result.data.get("available"):
        state.transferred = True
        state.stage = "transferred"
        state.transfer_to = result.data.get("transfer_to")


def _to_read_out(state: ConversationState, result: ToolResult) -> None:
    """A new draft is a new sentence, and nobody heard it yet: a read-back of
    the draft before it does not count for this one."""
    state.readback = result.data.get("readback")
    state.readback_heard = False


def _note_search(state: ConversationState, data: dict[str, Any]) -> None:
    """Remembers which dishes a search delivered and how sure it was. A sentence
    with several positions carries one result per part (`agent/dispatch.py`)."""
    parts = data.get("positions") if data.get("match_type") == "positions" else [data]
    for part in parts or []:
        hits = part.get("results") or []
        clear = part.get("match_type") in CLEAR_MATCHES
        if clear:
            # The hit of a clear match is its first result, as everywhere else.
            hits = hits[:1]
            state.known_item_ids.update(str(h["menu_item_id"]) for h in hits)
        for h in hits:
            state.seen_items[str(h["menu_item_id"])] = _seen(h)
        if not clear:
            for h in hits:
                state.offered_items[str(h["menu_item_id"])] = (
                    str(h.get("number") or ""),
                    str(h.get("name") or ""),
                )


def _seen(hit: dict[str, Any]) -> dict[str, Any]:
    """What the state keeps of a search hit. Of the options the groups with the
    names to choose from: no price, the total comes from draft_order (CLAUDE.md
    §2 rule 1). `required` is shown to the model as `open` while nothing is
    chosen; both lists are what `guards.take_cart` checks a line against."""

    def groups(required: bool) -> list[dict[str, Any]]:
        return [
            {
                "group": group["group"],
                "options": [option["name"] for option in group.get("options", [])],
            }
            for group in hit.get("option_groups") or []
            if bool(group.get("required")) is required
        ]

    return {
        "number": str(hit.get("number") or ""),
        "name": str(hit.get("name") or ""),
        # As of this search; for `guards.take_cart`, never shown to the model.
        "sold_out": bool(hit.get("sold_out")),
        "required": groups(True),
        "optional": groups(False),
    }


# Tools, mit denen eine Korrektur nach dem Vorlesen beginnt: eine Reservierung ueber
# check_slot, eine Bestellung ueber draft_order (prompts/system_v2.md).
_CORRECTING = frozenset({"check_slot", "create_reservation", "draft_order"})
# Eine Korrektur der Gerichte beginnt mit search_menu: das loest den
# vorgelesenen Entwurf ab, erfolgreich oder nicht - endet der Zug mit einer
# Rueckfrage, darf ein Ja darauf nicht den alten bestaetigen (Codex PR #127, P1).
# get_item_details nicht: es beantwortet eine Frage (Allergene, Beschreibung) und
# aendert nichts; eine andere Option geht nur ueber draft_order (Codex PR #127, P2).
_MENU_LOOKUP = frozenset({"search_menu"})
_PICKUP_TOOLS = frozenset({"draft_order"}) | _MENU_LOOKUP


def _supersedes(name: str, result: ToolResult) -> bool:
    """Loest dieser Aufruf den vorgelesenen Entwurf ab, ohne selbst einen neuen
    zu liefern? Ein neuer Entwurf ersetzt die ID ohnehin; ein gescheiterter
    draft_order oder create_reservation und jede neue Slotpruefung liefern
    keinen, und der alte bliebe bestaetigbar. Das naechste Ja bestaetigte dann
    den Entwurf, den der Gast gerade korrigiert hat (Codex PR #127, P1)."""
    if name == "check_slot" or name in _MENU_LOOKUP:
        return True
    return name in _CORRECTING and not result.ok


def _refused_for_good(name: str, result: ToolResult) -> bool:
    """`confirm` reached the domain and the domain said no to the draft itself:
    its seats went to someone else, or it was cancelled (`conflict`). It can
    never be confirmed as it is. Left in the state it would still wait for its
    yes, and a "Ja" to the next question would try again and book it once the
    reason has passed (Codex PR #256). Any other failure of `confirm` is a slip
    of the call (`invalid_input`, `not_found` on a wrong `entity`) and says
    nothing about the draft.

    The core's own refusal (`guards.py`: no yes from the guest) is a conflict
    too, and it comes through here like a tool result. It is about the guest's
    sentence, not about the draft, which stays; it is told apart by its `hint`,
    which only the core sets."""
    return (
        name == "confirm"
        and not result.ok
        and result.error_code == "conflict"
        and result.hint is None
    )


def _drop_readback(state: ConversationState, name: str) -> None:
    """Die Absicht folgt dem Ablauf, der den alten Entwurf abloest: wechselt der
    Gast von der Bestellung zum Tisch und der Slot ist belegt, geht es danach um
    Alternativen, nicht mehr um die Abholung (Codex PR #127, P2)."""
    state.order_id = None
    state.reservation_id = None
    state.readback = None
    state.readback_heard = False
    state.intent = "pickup" if name in _PICKUP_TOOLS else "reservation"
    state.stage = "collecting"
