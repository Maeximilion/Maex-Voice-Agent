"""Kompakter Gesprächszustand statt wachsendem Verlauf (docs/05 §5).

Nach jedem Zug wird der Stand zusammengefasst und dem Modell mitgegeben, statt den
ganzen bisherigen Wortlaut erneut zu schicken: der Zustand wächst nicht mit der
Gesprächsdauer, der Verlauf schon.
"""

import uuid
from contextlib import suppress
from typing import Any, Literal

from pydantic import BaseModel, Field

from api.agent.dispatch import ToolResult
from api.core.errors import InvalidInput
from api.domain.customers.phone import normalize_phone
from api.domain.menu.search import CLEAR_MATCHES

Stage = Literal[
    "start",
    "collecting",
    "readback_pending",
    "confirmed",
    "callback",
    "transferred",
    "ended",
]


class ConversationState(BaseModel):
    call_id: uuid.UUID
    tenant_id: uuid.UUID
    intent: str | None = None
    stage: Stage = "start"
    slots: dict[str, Any] = Field(default_factory=dict)
    open_questions: list[str] = Field(default_factory=list)
    reservation_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    transferred: bool = False
    # Memory of the core, never sent to the model (guards.py, rule 2): the
    # dishes an order may name. `known` came from a clear match of search_menu,
    # or was a candidate the guest heard by name or card number. `offered` are
    # the candidates of an unclear result of this turn, id to (number, name),
    # until the sentence that ends the turn shows which of them were named.
    known_item_ids: set[str] = Field(default_factory=set)
    offered_items: dict[str, tuple[str, str]] = Field(default_factory=dict)
    # Set by the phone line (telephony/handler.py), which says the AI disclosure
    # before the first turn: the model must not greet a second time.
    greeted: bool = False
    # Team extension from transfer_to_team, for the phone line to dial. Not part
    # of the prompt: the model has no use for the number.
    transfer_to: str | None = None

    def to_prompt_json(self) -> dict[str, Any]:
        """Format aus docs/05 §5: geht bei jedem Zug ans Modell statt des Verlaufs."""
        data: dict[str, Any] = {"stage": self.stage, "open": self.open_questions}
        if self.greeted:
            data["greeted"] = True
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
        return data


def initial_state(
    call_id: uuid.UUID, tenant_id: uuid.UUID, caller_id: str | None = None
) -> ConversationState:
    """Zustand zu Gespraechsbeginn. Der Gast ruft an, seine Nummer kennt die
    Rufnummernerkennung schon: sie steht vorab in `slots.phone`, und der Agent
    fragt nicht danach (Maxi, PR #127). Nennt der Gast eine andere, ueberschreibt
    sein `state_patch` sie. Unterdrueckt oder keine gueltige Nummer: der Slot
    bleibt leer, und der Agent fragt wie bisher."""
    slots: dict[str, Any] = {}
    if caller_id:
        with suppress(InvalidInput):
            slots["phone"] = normalize_phone(caller_id)
    return ConversationState(call_id=call_id, tenant_id=tenant_id, slots=slots)


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
    was der Gast gesagt hat). Returns what was taken: only `GUEST_SLOTS`."""
    named = {key: value for key, value in patch.items() if key in GUEST_SLOTS}
    state.slots.update(named)
    return named


def apply_tool_result(state: ConversationState, name: str, result: ToolResult) -> None:
    """Schreibt ein erfolgreiches Tool-Ergebnis in den Zustand. Fehlschläge ändern
    den Zustand nicht: das Modell entscheidet auf Basis von `say`/`error_code`,
    was als Nächstes versucht wird (Verständnis-Leiter, T-2.2).

    Ausnahme: der Gast korrigiert nach dem Vorlesen. Dann ist der vorgelesene
    Entwurf nicht mehr, was er will, auch wenn die Korrektur scheitert."""
    if state.stage == "readback_pending" and _supersedes(name, result):
        _drop_readback(state, name)
    if not result.ok:
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
    elif name == "draft_order":
        state.intent = "pickup"
        state.order_id = uuid.UUID(result.data["order_id"])
        state.reservation_id = None
        state.stage = "readback_pending"
    elif name == "search_menu":
        _note_search(state, result.data)
    elif name == "confirm":
        state.stage = "confirmed"
    elif name == "create_callback":
        state.stage = "callback"
    elif name == "transfer_to_team" and result.data.get("available"):
        state.transferred = True
        state.stage = "transferred"
        state.transfer_to = result.data.get("transfer_to")


def _note_search(state: ConversationState, data: dict[str, Any]) -> None:
    """Remembers which dishes a search delivered and how sure it was. A sentence
    with several positions carries one result per part (`agent/dispatch.py`)."""
    parts = data.get("positions") if data.get("match_type") == "positions" else [data]
    for part in parts or []:
        hits = part.get("results") or []
        if part.get("match_type") in CLEAR_MATCHES:
            # The hit of a clear match is its first result, as everywhere else.
            state.known_item_ids.update(str(h["menu_item_id"]) for h in hits[:1])
        else:
            for h in hits:
                state.offered_items[str(h["menu_item_id"])] = (
                    str(h.get("number") or ""),
                    str(h.get("name") or ""),
                )


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


def _drop_readback(state: ConversationState, name: str) -> None:
    """Die Absicht folgt dem Ablauf, der den alten Entwurf abloest: wechselt der
    Gast von der Bestellung zum Tisch und der Slot ist belegt, geht es danach um
    Alternativen, nicht mehr um die Abholung (Codex PR #127, P2)."""
    state.order_id = None
    state.reservation_id = None
    state.intent = "pickup" if name in _PICKUP_TOOLS else "reservation"
    state.stage = "collecting"
