"""Kundenzug → Modell → Tool-Aufrufe → Antwort; Abbruch bei `max_call_seconds` (docs/11 §agent).

Zwei Prüfungen laufen vor jedem Modell-Aufruf, nicht danach (docs/11 §agent:
"escalation.py prüft vor dem Modell"): `escalation.check()` auf dem rohen
Kundentext (Beschwerde, Mensch-Wunsch, Storno — docs/05 §4) und die
Verständnis-Leiter (`ladder.py`, docs/05 §2), die Fehlversuche je Information
zählt und nach drei Stufenwechseln ohne Erfolg selbst eskaliert. Bricht der
Loop aus eigenem Antrieb ab (Zeitlimit, zu viele Tool-Hops, Leiter erschöpft),
übernimmt er die Übergabe an das Team wirklich, statt sie dem Kunden nur
anzukündigen (CLAUDE.md §2 Regel 5: kein Anruf geht verloren).
"""

import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, get_args

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.agent import escalation, guards, recording
from api.agent.consent import (
    after_recording_answer,
    bare_recording_answer,
    consent_answer,
    is_yes,
    only_stops_recording,
    recording_answer,
    stops_recording,
)
from api.agent.dispatch import ToolResult, dispatch, in_local_time, log_refused
from api.agent.guest_details import name_said
from api.agent.ladder import (
    FAILURES_BEFORE_LEVEL_UP,
    LEVEL_UPS_BEFORE_END,
    UnderstandingLadder,
)
from api.agent.llm import LLMClient, LLMError, ToolCall
from api.agent.state import (
    ALLERGY_SAID_MAX,
    ConversationState,
    RemedyOffer,
    apply_state_patch,
    apply_tool_result,
    cart_lines,
    draft_replaced,
    last_words,
    note_guest_details,
    note_intent,
    order_dropped,
)
from api.config import settings
from api.core.envelope import SAY_ON_FAILURE
from api.core.errors import SAY_TO_THE_TEAM, Conflict
from api.core.logging import get_logger, log
from api.core.time import to_local, utcnow
from api.domain.callbacks import matching_rule, note_remedy

# Re-exported: the agent's tests name it from here (#998 moved it).
from api.domain.callbacks.create import SUMMARY_OPEN_ORDER as SUMMARY_OPEN_ORDER
from api.domain.callbacks.create import booked
from api.domain.callbacks.transfer import SUMMARY_MISSED

# Re-exported: the agent's tests and cards name it from here (#956 moved it).
from api.domain.callbacks.transfer import (
    SUMMARY_NOTE_TO_CLEAR as SUMMARY_NOTE_TO_CLEAR,
)
from api.domain.menu.details import SAY_ALLERGENS_LEAD, SAY_ALLERGENS_UNKNOWN
from api.domain.menu.pos_convert import allergens_named, names_an_allergen
from api.domain.menu.wishes import (
    HEALTH_NOTE_RE,
    names_coeliac,
    names_someone_else,
    speaks_of_health,
)
from api.domain.ordering.readback import spoken_euro
from api.domain.reservations.spoken import WEEKDAYS
from api.models import Order, Reservation, Tenant
from api.schemas.callbacks import CallbackReason
from api.schemas.reservations import CheckSlotRequest
from api.schemas.transfer import TransferReason

logger = get_logger("api.agent.loop")

# Schutz gegen ein Modell, das sich zwischen Tool-Aufrufen verheddert und nie zu
# einem Satz für den Kunden kommt: lieber sauber abbrechen als den Anruf endlos
# im Loop stehen zu lassen.
MAX_TOOL_HOPS = 6
SAY_TIMEOUT = "Wir sind jetzt schon eine Weile dran. Ich gebe an das Team weiter."
SAY_STUCK = "Da komme ich gerade nicht weiter. Ich gebe an das Team weiter."
SAY_ESCALATION = "Ich verbinde Sie sofort mit dem Team."
# The answer to "Sind Sie ein Mensch?" (`escalation.without_human_question`),
# truthful every time (docs/09 L1), and the offer of the team; a yes to the
# offer hands the call over. Draft wording, checked with the texts of docs/05 §6.
SAY_NOT_A_PERSON = "Ich bin ein KI-Assistent, kein Mensch."
SAY_TEAM_OFFER = "Möchten Sie lieber mit jemandem aus dem Team sprechen?"
SAY_HUMAN_ANSWER = f"{SAY_NOT_A_PERSON} {SAY_TEAM_OFFER}"
SAY_NOT_UNDERSTOOD = "Da komme ich gerade nicht weiter. Ich gebe an das Team weiter."
# The team is not reachable and no number is known, after an allergen
# question beyond the database (L17): the core asks for one before it gives
# up (Codex PR #369). Draft wording, checked with the texts of docs/05 §6.
SAY_CALLBACK_NUMBER = (
    "Unser Team ist gerade nicht erreichbar. Unter welcher Telefonnummer mit "
    "Vorwahl darf es Sie zurückrufen?"
)
# How often the core asks for the callback number once more, after an answer
# without one ("Moment, ich schaue nach", a "Ja" to "Sind Sie noch da?").
CALLBACK_NUMBER_ASKS = 2
# The last resort of `_handoff`: the team is not reachable and there is no number
# for a callback. Every sentence above promises the team; here nobody takes
# over, so the guest hears exactly that (Codex PR #208, P1). Draft wording,
# checked with the announcement texts of docs/05 §6.
SAY_NOBODY_REACHABLE = (
    "Ich kann Ihnen gerade leider nicht weiterhelfen und erreiche im Restaurant "
    "niemanden. Bitte rufen Sie später noch einmal an."
)

# What the core took from the guest in a call. A guest turn that leaves all of
# it as it was understood nothing, whatever the model said: a real model
# answered a line with nothing but noise with a question every turn and never
# reported a failed answer, so the call reached nobody (`eskalation_0016`,
# finding on #37). The model's own status lookup is no understanding. A
# question of the core for the callback number is: the guest asked something
# only the team can answer (Codex PR #437, P1).
_HEARD = {
    "intent",
    "stage",
    "slots",
    "reservation_id",
    "order_id",
    "readback",
    "cart",
    "known_item_ids",
    "seen_items",
    "allergy_said",
    "callback_waits",
    "remedy_offer",
}
# Turns in a row without any of it before the call goes to the team: the
# ladder's budget for one piece of information (docs/05 §2).
UNHEARD_TURNS = FAILURES_BEFORE_LEVEL_UP * LEVEL_UPS_BEFORE_END

ENDED_STAGES = frozenset({"transferred", "ended"})
# Name under which a refused order of the model (`LLMTurn.cart`) goes back to
# it and into `calls.tool_calls`. No tool: nothing is dispatched under it.
CART = "cart"
# The same for a sentence the core did not let through (`guards.false_booking`).
SAY = "say"
# What the model hands a complaint over with. The core reads a rule first (D19).
_COMPLAINT_TOOLS = ("transfer_to_team", "create_callback")
# The model's handover did not go out: a rule of the catalogue was offered.
_RULE_FIRST = ToolResult(ok=False, error_code="conflict")

_CALLBACK_REASONS = frozenset(get_args(CallbackReason))
# What a model may hand over with: an outage is the core's to say (#769).
_TRANSFER_REASONS = frozenset(get_args(TransferReason)) - {"outage"}
# Fallback-Grund für Rückrufe, wenn der eigentliche Grund (z. B. "cancellation")
# in CallbackReason gar nicht existiert (docs/03: Storno ist nur ein
# Transfer-Grund, kein Rückruf-Grund — siehe api/schemas/transfer.py).
_CALLBACK_REASON_FALLBACK: CallbackReason = "human_requested"
# An outage's card keeps the reason it had before #769; its text names the fault.
_CALLBACK_REASON_FOR: dict[str, CallbackReason] = {"outage": "not_understood"}
# The card of an order read back and never answered (#977): the core's own
# reason, as an outage is for a transfer.
ORDER_OPEN: CallbackReason = "order_open"
# What a model may write a card with (prompts/tools_v3.md). Any other reason is
# refused with these named: pydantic's own hint would offer `order_open` too.
_MODEL_CALLBACK_REASONS = tuple(r for r in get_args(CallbackReason) if r != ORDER_OPEN)
_CALLBACK_REASON_REFUSED = ToolResult(
    ok=False,
    error_code="invalid_input",
    hint="Abgelehnt: `reason` ist einer von "
    + ", ".join(f"`{reason}`" for reason in _MODEL_CALLBACK_REASONS)
    + ".",
)

_HANDOFF_SUMMARIES = {
    "complaint": "Kunde hat sich beschwert.",
    "human_requested": "Kunde wollte mit einem Menschen sprechen.",
    "cancellation": "Kunde wollte etwas stornieren.",
    "not_understood": "Anliegen konnte automatisch nicht geklärt werden.",
}
_DEFAULT_HANDOFF_SUMMARY = "Anruf konnte nicht automatisch abgeschlossen werden."
# The card of a fault of ours (`_outage`): the team must not read it as a
# guest it did not understand. Draft wording.
SUMMARY_OUTAGE = "Technische Störung, Anruf nicht automatisch abgeschlossen."
# How the card of a table the team decides on begins (`team_callback`, #252).
SUMMARY_TEAM_TABLE = "Reservierungswunsch, vom Team zu bestätigen:"
# The card when the guest asked about allergens beyond the maintained values
# (L17). It names the question, never the guest's allergy (L13). Draft wording.
SUMMARY_ALLERGENS = (
    "Frage zu Allergenen über die hinterlegten Angaben hinaus, vor einer "
    "Bestellung zu klären."
)
# The card when the guest asked about an additive, which no table holds
# (#638). Draft wording.
SUMMARY_ADDITIVES = "Frage zu Zusatzstoffen, vor einer Bestellung zu klären."
# A line's note on a card when it names the guest's health
# (`SUMMARY_NOTE_TO_CLEAR`): the card is kept for the period of a callback, the
# allergy only until the night after its order (L13).
# The same as a card's first sentence (`_marked`).
_MARKER = f"{SUMMARY_NOTE_TO_CLEAR}."
# The cards of a rule of the catalogue (D19): name and amount from its row, the
# guest's sentence behind them. Draft wording.
SUMMARY_REMEDY_BOOKED = "Beschwerde, Gast hat zugestimmt, bitte ausführen: {remedy}."
SUMMARY_REMEDY_OPEN = "Kunde hat sich beschwert, Angebot nicht angenommen: {remedy}."
# The call ended after an order's read-back and before its yes (#953). Draft
# wording.
SUMMARY_READBACK_UNANSWERED = "Anruf endete vor dem Ja zur vorgelesenen Bestellung."
SAY_ORDER_NOT_PLACED = "Ihre Bestellung ist damit noch nicht aufgegeben."
# A cancel of the order read back, before its yes (#678): nothing to undo.
SAY_ORDER_DROPPED = (
    "In Ordnung, dann ist die Bestellung storniert. "
    "Kann ich sonst noch etwas für Sie tun?"
)
# The same for a table read back and not yet confirmed (own review of PR #369).
SAY_TABLE_NOT_BOOKED = "Ihre Reservierung ist damit noch nicht bestätigt."
SUMMARY_OPEN_TABLE = "Offene Reservierung, nicht bestätigt:"
# What an open table's card says for a draft no tool could read (#940).
SUMMARY_TABLE_UNREAD = "Angaben mit dem Gast klären"
# A table the domain refused in the same turn: no team can book it as it
# stands, the card names it as what the guest asked for (#862).
SUMMARY_REFUSED_TABLE = "Reservierung nicht möglich, nicht gebucht:"


class RefusedTable(str):
    """The text of a table on the card that the domain refused for good."""

    __slots__ = ()


# The lead of the handover for a question beyond the database (traces,
# contact, a promise of safety, L17): nothing about what is stored, which may
# be maintained for that dish (own review of PR #369). Draft wording, checked
# with the texts of docs/05 §6.
SAY_ALLERGENS_BEYOND = "Dazu kann ich Ihnen keine verlässliche Auskunft geben."
# What the guest hears when `confirm` went through: the core says it, built from
# the confirm result, never the model (`guards.false_booking`). Draft wording,
# checked with the announcement texts (docs/05 §6).
# The order is named by the name it runs under, not by its pickup code: the
# name is what the guest gives at the counter (Maxi, 23.10.2026).
SAY_ORDER_PLACED = "Ihre Bestellung ist aufgegeben."
SAY_ORDER_WAITS = "Ihre Bestellung ist aufgenommen, das Team bestätigt sie gleich noch."
SAY_FOR_NAME = "Ihre Bestellung auf den Namen {name} ist"
SAY_TABLE_BOOKED = "Ihre Reservierung ist bestätigt."
# A confirmed call is closing (`outcome.CLOSING_STAGES`): the phone line hangs
# up after this sentence and adds no second goodbye to it.
SAY_BOOKED_GOODBYE = "Vielen Dank für Ihren Anruf und bis dann."
# Deckelt die Rohtext-Beigabe zur Zusammenfassung (CreateCallbackRequest.summary
# erlaubt bis zu 1000 Zeichen) - großzügig für eine gesprochene Äußerung, aber
# eine harte Grenze statt eines ungeprüften Anhängens.
_SUMMARY_DETAIL_MAX = 400
# What a callback takes (CreateCallbackRequest.summary).
_SUMMARY_MAX = 1000
# Where the guest's words begin on a card (`_card_tail`).
_SAID_TAIL = " Kunde sagte: "


@dataclass
class TurnResult:
    state: ConversationState
    say: list[str] = field(default_factory=list)
    ended: bool = False


def _opening(state: ConversationState) -> list[str]:
    """What follows the answer to the recording question."""
    if state.recording == "on":
        return [recording.SAY_HOW_TO_STOP, recording.SAY_WHAT_CAN_I_DO]
    return [recording.SAY_WHAT_CAN_I_DO]


def _still_open(state: ConversationState) -> str:
    """The sentence the guest has not answered yet, in full."""
    if state.readback_heard and state.readback:
        return state.readback
    return state.last_said or recording.SAY_WHAT_CAN_I_DO


class ConversationLoop:
    def __init__(
        self,
        session: Session,
        llm: LLMClient,
        system_prompt: str,
        *,
        max_call_seconds: int | None = None,
        turn_deadline_seconds: float | None = None,
        now: datetime | None = None,
        clock: Callable[[], float] = time.monotonic,  # austauschbar für Tests
    ):
        self._session = session
        self._llm = llm
        self._system_prompt = system_prompt
        self._max_call_seconds = (
            settings.max_call_seconds if max_call_seconds is None else max_call_seconds
        )
        # How long a guest waits in silence for the answer to one sentence
        # before the call goes to the team (#252). The phone line hands in the
        # tenant's `service_config.turn_deadline_seconds`. None, as on the text
        # phone and in the evals: nobody holds a receiver there, and a local
        # test model may take longer to load than any guest would wait.
        self._turn_deadline = turn_deadline_seconds
        self._now = now
        self._clock = clock
        self._started = clock()
        self._ladder = UnderstandingLadder()
        self._unheard = 0
        # Timezone of the call's tenant, read once (`_local_now`).
        self._zone: str | None = None

    def run_turn(self, state: ConversationState, user_text: str) -> TurnResult:
        """One sentence of the guest. What belongs to the recording comes
        first, before the model and whatever else the sentence asks: the
        answer to the question of the phone line (L2), or its withdrawal."""
        addressed = False
        if state.recording == "asked":
            # The address may sit in the answer clause that is cut off below.
            addressed = escalation.addresses_a_person(user_text)
            answer = recording_answer(user_text)
            recording.settle(self._session, state, answer, self._now)
            if bare_recording_answer(user_text):
                guards.note_said(state, recording.SAY_WHAT_CAN_I_DO)
                say = _opening(state)
                if escalation.addresses_a_person(user_text):
                    # "Nein, junge Frau": the truthful sentence still comes first.
                    say = [SAY_NOT_A_PERSON, *say]
                return TurnResult(state=state, say=say)
            # No consent, and no second question: what the sentence holds
            # besides its answer is what the guest called for, and the only
            # thing the readers of the turn see (#526).
            user_text = after_recording_answer(user_text)
        if state.recording != "on" or not stops_recording(user_text):
            return self._guest_sentence(state, user_text, addressed)
        recording.withdraw(self._session, state, self._now)
        if only_stops_recording(user_text):
            # What the guest was asked stands: said again, it changes nothing
            # of a read-back that waits for its yes.
            return TurnResult(
                state=state, say=[recording.SAY_RECORDING_STOPPED, _still_open(state)]
            )
        result = self._guest_sentence(state, user_text, addressed)
        result.say.insert(0, recording.SAY_RECORDING_STOPPED)
        return result

    def _guest_sentence(
        self, state: ConversationState, user_text: str, addressed: bool = False
    ) -> TurnResult:
        """One sentence of the guest, after the recording. A guest who takes
        the agent for a person hears the truthful sentence first, on every way
        out of the turn (docs/09 L1, #638): asks whether a person is on the
        line or for the agent's name (`rest`, the sentence without that
        question), or addresses it as one ("Ja, junge Frau")."""
        rest = escalation.without_human_question(user_text)
        result = self._guest_turn(state, user_text, rest)
        if (
            rest is not None or addressed or escalation.addresses_a_person(user_text)
        ) and not (result.say and result.say[0].startswith(SAY_NOT_A_PERSON)):
            result.say.insert(0, SAY_NOT_A_PERSON)
        return result

    def _guest_turn(
        self, state: ConversationState, user_text: str, rest: str | None
    ) -> TurnResult:
        # The guest has stopped speaking: from here on they wait.
        turn_started = None if self._turn_deadline is None else self._clock()
        # An allergen question left open lasts this one turn, also when the
        # turn never reaches the model (#798).
        pending, state.allergen_question_open = state.allergen_question_open, False
        asked_before = frozenset(state.allergen_question_codes)
        state.allergen_question_codes = []
        # First of all: a name or number the model writes in this turn may
        # come from this very sentence (agent/guest_details.py, rule 2).
        if state.callback_waits is not None:
            # The answer to the question for the callback number (L17),
            # before the model and before any `confirm`.
            return self._callback_answered(state, user_text)
        if state.remedy_offer is not None:
            return self._offer_answered(state, user_text)
        heard = state.model_dump(include=_HEARD)
        # What the call was about before this sentence: a delivery the guest
        # complains about is no wish the call still has to serve.
        wish = state.intent
        if note_guest_details(state, user_text):
            # The core wrote the number itself, so a model has nothing to
            # report as understood (Codex PR #244, P2).
            self._ladder.record_success("phone")
        # The table read back, before a new allergy takes its draft back: a
        # handover in this very turn still names it (Codex PR #369).
        open_table = self._open_table(state)
        kept = _note_allergy_said(state, user_text)
        if kept:
            # The draft read out before may not carry what the guest just
            # said: a yes must not confirm it (Codex PR #368, P1). The model
            # drafts again, with the note (`guards.unnoted_allergy`).
            draft_replaced(
                state, "create_reservation" if state.reservation_id else "draft_order"
            )
        # A sentence that only asks for a person or the agent's name (#649).
        only_asks = rest is not None and not escalation.asks_more(rest)
        # Read past that question: "Spreche ich mit einem Menschen?" holds the
        # words of the wish to talk to one and only asks, what else the
        # sentence says still counts.
        reason = escalation.check(user_text if rest is None else rest)
        # The offer stands while it is the last thing the guest heard: any
        # sentence after it, the phone line's "Sind Sie noch da?" too, takes
        # it back (Codex PR #372).
        answered_offer = (
            reason is None and state.last_say == SAY_HUMAN_ANSWER and is_yes(user_text)
        )
        if answered_offer:
            reason = "human_requested"
        if reason != "complaint":
            # Before the model and before a handoff: the request of this very
            # turn carries it already, and a call that goes to the team right
            # here is logged with what it was about. Not on a complaint: one
            # about an order is no request for a pickup, and the reason of
            # the transfer says what that call was.
            note_intent(state, user_text)
        escalated: TurnResult | None = None
        if reason == "complaint" and wish is None:
            escalated = self._offer_remedy(state, user_text)
        if (
            state.stage == "readback_pending"
            and state.order_id is not None
            and (
                # Only a plain cancel: anything more may be another booking,
                # one line or a question, and goes to the team as before.
                (reason == "cancellation" and escalation.only_cancels(user_text))
                # A plain decline without a cancel word (Maxi, #977), only to
                # the read-back the guest heard last: to "Noch etwas?" or an
                # allergy question "Nein danke, dann lasse ich es" answers that.
                or (
                    reason is None
                    and state.readback_heard
                    and escalation.only_declines(user_text)
                )
            )
        ):
            # "So lange? Dann stornieren Sie das." Nothing is booked before
            # the yes, so the team has nothing to undo (Maxi, #678), and the
            # hang-up after it writes no card (`offer_unanswered`).
            if self._clock() - self._started > self._max_call_seconds:
                # Its sentence asks for more: no call stays open past its
                # limit on it, as on `only_asks`.
                return self._outage(state, SAY_TIMEOUT, user_text, open_table)
            order_dropped(state)
            # The guest was understood: the count of unheard turns starts anew.
            self._unheard = 0
            guards.note_said(state, SAY_ORDER_DROPPED)
            return TurnResult(state=state, say=[SAY_ORDER_DROPPED])
        if reason is not None and escalated is None:
            # The table read back, which this sentence may have taken out of
            # the state (#690).
            escalated = self._handoff(
                state,
                SAY_ESCALATION,
                reason=reason,
                # A "Ja" tells the team nothing the reason does not say.
                detail=None if answered_offer else user_text,
                # As for the model's `transfer_to_team` (#690).
                ask_number=True,
                table=open_table,
            )
        # The call speaks of allergies: the guest's own, the allergens of a
        # dish, an allergen named ("Ist das glutenfrei?") or coeliac disease,
        # which names none (#763). The model's answers are then checked for a
        # claim (`guards.allergen_claim`).
        talk = (
            state.allergy_talk
            or escalation.speaks_of_allergies(user_text)
            or names_an_allergen(user_text)
            or names_coeliac(user_text)
        )
        if escalated is None:
            escalated = self._beyond_the_database(state, user_text, open_table)
        if escalated is not None:
            return escalated
        state.allergy_talk = talk
        if only_asks:
            if self._clock() - self._started > self._max_call_seconds:
                # The answer keeps no call open past its limit: asked again
                # and again, it would (Codex PR #372).
                return self._outage(state, SAY_TIMEOUT, user_text, open_table)
            guards.note_said(state, SAY_HUMAN_ANSWER)
            return TurnResult(state=state, say=[SAY_HUMAN_ANSWER])
        result = self._converse(
            state,
            user_text,
            turn_started,
            open_table,
            wish,
            kept=kept,
            pending=pending,
            asked_before=asked_before,
        )
        if not result.ended:
            unchanged = state.model_dump(include=_HEARD) == heard
            self._unheard = self._unheard + 1 if unchanged else 0
            if self._unheard >= UNHEARD_TURNS:
                # With nobody to reach and no number known, the core asks for
                # one before it gives up, as for a question beyond the
                # database (Codex PR #437, P1).
                result = self._handoff(
                    state,
                    SAY_NOT_UNDERSTOOD,
                    reason="not_understood",
                    detail=user_text,
                    summary=_HANDOFF_SUMMARIES["not_understood"],
                    ask_number=True,
                )
        # A request next to the question ("Sind Sie ein Roboter? Ich möchte
        # einen Tisch ..."): the model answers it after the truthful sentence
        # (`_guest_sentence`); the guest has said what they want, so no offer of
        # the team (ultrareview of PR #372).
        return result

    def _converse(
        self,
        state: ConversationState,
        user_text: str,
        turn_started: float | None,
        open_table: str | None = None,
        wish: str | None = None,
        *,
        kept: bool = False,
        pending: bool = False,
        asked_before: frozenset[str] = frozenset(),
    ) -> TurnResult:
        """The model's part of a guest turn: model calls and tools until a
        sentence for the guest, a handoff or a limit. `wish` is what the call
        was about before this sentence; `kept`: the sentence speaks of the
        guest's health (`_note_allergy_said`); `pending` and `asked_before`:
        what the turn before left open."""
        start = guards.begin_turn(state)
        # Where an allergen word of this turn can come from (#798). Only an
        # open turn that leaves a question open sets it again (below).
        aturn = guards.begin_allergen_turn(
            state, user_text, kept=kept, pending=pending, asked_before=asked_before
        )
        pending_input = user_text
        # Ein Feld zählt höchstens einmal je Kundenzug: ohne das könnte ein Modell,
        # das denselben Fehlversuch über mehrere Tool-Hops hinweg noch einmal
        # meldet (erst beim Tool-Aufruf, dann noch einmal in der Antwort), die
        # Leiter mit weniger als den vorgesehenen zwei echten Kundenversuchen je
        # Stufe hochtreiben (Codex-Review PR #102, P2).
        reported_failures: set[str] = set()
        # The tools the model called in this guest turn, refused ones and a
        # refused `cart` included.
        called: list[str] = []
        # The draft without the guest's allergy went back once in this turn
        # (`guards.unnoted_allergy`): the next one goes through. After the
        # fixed question about the dish of a later allergy the hint was given
        # the turn before: a draft without it gets the guest's words at once,
        # never the question again each turn (#929).
        allergy_hint_given = guards.SAY_ASK_ALLERGY_DISH in (state.last_say or "")
        # The core's allergen sentences of this turn, one per dish looked up:
        # the only allergen words the guest hears (#669, #717, rule 1).
        allergen_says: list[str] = []
        # The model's sentence went back once for its allergen words.
        allergen_back = False
        # ... for a sentence about a dish of the call (#928), and with no
        # allergen word in it.
        dish_back = dish_only = False
        zone = self._local_now(state).tzinfo
        assert zone is not None  # `to_local` gives an aware time
        for _ in range(MAX_TOOL_HOPS):
            if self._clock() - self._started > self._max_call_seconds:
                return self._outage(state, SAY_TIMEOUT, user_text, open_table)
            # The model calls and tools of one guest turn share one deadline:
            # each call alone may be quick and the guest still waits for all
            # of them. Time up: the model is not asked again.
            left = self._left_in_turn(turn_started)
            if left is not None and left <= 0:
                return self._too_slow(state, user_text, open_table)

            try:
                turn = self._llm.next_turn(
                    self._system_prompt,
                    # On a tool hop the input is the tool result: the
                    # guest's sentence of this turn rides along, or the
                    # model no longer knows what the result is for.
                    self._prompt_state(
                        state,
                        None if pending_input is user_text else user_text,
                        called,
                    ),
                    pending_input,
                    # Only what is left: a call that hangs is cut off
                    # there instead of after the client's own, longer
                    # limit. Not named at all without a deadline, so a
                    # client of the old shape is called as before.
                    **({} if left is None else {"timeout": left}),
                )
            except LLMError as exc:
                # A model that is down or answers outside the contract is
                # an outage: the call goes to the team, never into a retry
                # loop with the guest waiting (CLAUDE.md §2 rule 5).
                log(
                    logger,
                    logging.ERROR,
                    "model failed, handing over to the team",
                    call_id=str(state.call_id),
                    error=str(exc),
                )
                return self._outage(state, SAY_ON_FAILURE, user_text, open_table)
            # Rule 3: a `confirm` on a yes for a draft the guest has not heard
            # last. The core puts the draft's question itself and ends the
            # turn (below, once the time limit is checked). Nothing that
            # stands next to the call is taken: the guest said yes and nothing
            # else, and a patch, a `cart` or a step on the ladder could drop
            # the draft or end the call before the question is put.
            reread = guards.reread_on_yes(state, start, user_text, turn.tool_call)
            if turn.state_patch and reread is None:
                # Only what was taken counts as understood: a name outside
                # `GUEST_SLOTS` is dropped and is no success on the ladder.
                for field_name in apply_state_patch(state, turn.state_patch):
                    self._ladder.record_success(field_name)
            # The limit is checked again once the model has answered: a slow
            # answer that arrives after the limit is neither spoken nor
            # dispatched, the call goes to the team (Codex PR #208, P2). What
            # the model heard is taken first: a phone number from this very
            # sentence is what the callback of the handoff needs when the
            # team is not reachable (Codex PR #222, P1).
            if self._clock() - self._started > self._max_call_seconds:
                return self._outage(state, SAY_TIMEOUT, user_text, open_table)
            # The same for the turn deadline: an answer that took too long is
            # neither spoken nor carried out: the guest has waited long enough
            # and goes to the team. What the model heard is kept, as above.
            left = self._left_in_turn(turn_started)
            if left is not None and left <= 0:
                return self._too_slow(state, user_text, open_table)

            if reread is not None and turn.tool_call is not None:
                # In `calls.tool_calls` like any refused call. The model is
                # not asked again: its answer could only be replaced.
                refused = guards.refusal(state, start, user_text, turn.tool_call)
                assert refused is not None  # `reread_on_yes`: a refused `confirm`
                log_refused(
                    self._session,
                    state.call_id,
                    state.tenant_id,
                    turn.tool_call.name,
                    refused,
                )
                guards.note_said(state, reread)
                return TurnResult(state=state, say=[reread])

            if (
                turn.understanding_failure
                and turn.understanding_failure not in reported_failures
            ):
                reported_failures.add(turn.understanding_failure)
                self._ladder.record_failure(turn.understanding_failure)
                if self._ladder.should_end_call(turn.understanding_failure):
                    return self._handoff(
                        state,
                        SAY_NOT_UNDERSTOOD,
                        reason="not_understood",
                        detail=user_text,
                        table=open_table,
                    )

            if turn.cart is not None:
                # Rule 2 for what a model keeps across turns (guards.py). A
                # refused order stops the whole answer: neither its sentence
                # nor its tool call is carried out on an order the core did
                # not take. It costs a tool hop like any refused call.
                refused = guards.take_cart(state, turn.cart)
                if refused is not None:
                    log_refused(
                        self._session, state.call_id, state.tenant_id, CART, refused
                    )
                    called.append(CART)
                    pending_input = _tool_result_as_input(CART, refused, zone)
                    continue

            if turn.tool_call is None:
                say = turn.say
                assert say is not None  # LLMTurn garantiert genau eins von beidem
                claim = guards.allergen_claim(state, say)
                if claim is not None:
                    # L17: the model answered what no database value answers
                    # ("Das ist sicher", "glutenfrei", "keine Spuren"), or
                    # said it cannot: the guest hears neither from it, the
                    # team answers before any `confirm` (redesign of PR #369).
                    log_refused(
                        self._session, state.call_id, state.tenant_id, SAY, claim
                    )
                    return self._allergens_to_team(
                        state,
                        lead=SAY_ALLERGENS_BEYOND,
                        table=open_table,
                        card=SUMMARY_ADDITIVES
                        if claim is guards.ADDITIVE_CLAIM
                        else SUMMARY_ALLERGENS,
                    )
                # Where its allergen words come from decides what the guest
                # hears (#798, rule 1); the lists above went first.
                open_turn = guards.is_open(aturn, state)
                changed = guards.order_changed(aturn, state)
                # Row 1: the table sentence of every dish looked up, in an
                # open turn that changed no order.
                table = open_turn and bool(aturn.looked_up) and not changed
                # After a refused allergen word in a call about an allergy,
                # the retry may say only core sentences: "Ja, die 13 ist für
                # Sie kein Problem" is the same claim without its noun (deep
                # review of #860, P1).
                in_allergy_call = guards.sticky(state)
                # Where the guest named an allergy, a sentence about a dish of
                # the call is held to core sentences too (`about_a_dish`):
                # "Bei Ihrer Allergie ist die 13 kein Problem" names no
                # allergen (#928, Maxi: option B). Not after a table sentence
                # alone, where the hint and its noted sentence would claim an
                # allergy (sweep of the deep review of #928). An open turn is
                # strict already and keeps its own ending: a price is no
                # answer to an allergen question.
                about_a_dish = (
                    guards.allergy_named(state)
                    and not open_turn
                    and guards.about_a_dish(aturn, state, say, changed=changed)
                )
                # A retry after a refusal only for naming a dish is judged like
                # a first answer: held again when it names a dish or speaks of
                # the allergy, else `unlicensed` checks it (#928, Maxi: R1).
                strict = (
                    open_turn
                    or about_a_dish
                    or (
                        allergen_back
                        and in_allergy_call
                        and (
                            not dish_only
                            or guards.speaks_of_the_allergy(aturn, state, say)
                        )
                    )
                )
                if (
                    not table
                    and not allergen_says
                    and (
                        not guards.core_only(aturn, state, say)
                        if strict
                        else bool(guards.unlicensed(aturn, state, say))
                    )
                ):
                    refused = (
                        guards.ALLERGY_TURN
                        if open_turn
                        else guards.ALLERGY_DISH
                        if about_a_dish
                        else guards.ALLERGY_CALL
                        if in_allergy_call
                        else guards.UNLICENSED_ALLERGEN
                    )
                    log_refused(
                        self._session, state.call_id, state.tenant_id, SAY, refused
                    )
                    prices = (
                        guards.price_says(aturn, state, say)
                        if about_a_dish and guards.states_a_price(aturn, state, say)
                        else []
                    )
                    if prices:
                        # A price the model stated for a dish this turn's
                        # search found: the core says it, from the database
                        # (#928, Maxi: B2). A retry could say only a core
                        # sentence, none better than this one.
                        say = " ".join(prices)
                    elif not allergen_back:
                        # Rows 3 and 6: back to the model once.
                        allergen_back = True
                        dish_back = refused is guards.ALLERGY_DISH
                        # Refused only for the dish it names, no allergen word.
                        dish_only = dish_back and not guards.unlicensed(
                            aturn, state, say
                        )
                        called.append(SAY)
                        pending_input = _tool_result_as_input(SAY, refused, zone)
                        continue
                    # Rows 4 and 6, a second time: the table where a dish was
                    # looked up, the team for a question left open before
                    # (rule 5), else the offer.
                    elif aturn.looked_up and not changed:
                        table = True
                    elif aturn.pending:
                        return self._allergens_to_team(
                            state, lead=SAY_ALLERGENS_BEYOND, table=open_table
                        )
                    elif dish_back and aturn.answers:
                        # After a sentence about a dish, the tools' own
                        # sentences of this turn: a sold-out dish, the
                        # candidates of an unclear search (deep review of #928).
                        say = " ".join(dict.fromkeys(aturn.answers))
                    else:
                        say = guards.SAY_ALLERGENS_OFFER
                        # The offer asks for a dish: the next one named is its
                        # answer, as after an open turn (rule 5 counts it;
                        # sweep of the deep review of #928).
                        state.allergen_question_open = True
                if table:
                    sentences = guards.table_says(aturn)
                    if sentences is None:
                        unknown = guards.unknown_dish(aturn)
                        assert unknown is not None  # `table_says`: one has no value
                        return self._allergens_to_team(
                            state, _dish(unknown), table=open_table
                        )
                    say = " ".join(sentences)
                elif allergen_says:
                    # A flagged lookup (R8): the model relays nothing. A
                    # dropped allergen ("Die 23 enthält Gluten", soy missing)
                    # or an added one passed every guard. shortcut: its other
                    # words are dropped too; the guest asks on, the next turn
                    # answers.
                    say = " ".join(allergen_says)
                elif open_turn:
                    # Rows 2 and 4: what the guest said may stay untaken.
                    if guards.left_twice(aturn, state, say):
                        # The second turn in a row that leaves the question
                        # open goes to the team (rule 5).
                        return self._allergens_to_team(
                            state, lead=SAY_ALLERGENS_BEYOND, table=open_table
                        )
                    left = guards.leaves_a_question(aturn, state)
                    # Only the offer makes the next named dish its answer: a
                    # turn that ended in the noted sentence leaves the next
                    # dish step to the model (deep review of #860, P2).
                    state.allergen_question_open = (
                        left and guards.SAY_ALLERGENS_OFFER in say
                    )
                    state.allergen_question_codes = guards.codes_left(aturn, state, say)
                refused = guards.false_booking(state, say)
                if refused is not None:
                    # Rule 3 for what the guest hears: "Ihre Abholung ist
                    # bestätigt, der Abholcode lautet 8842" with nothing booked
                    # sends a guest away who will find no order (13.10.2026).
                    log_refused(
                        self._session, state.call_id, state.tenant_id, SAY, refused
                    )
                    called.append(SAY)
                    pending_input = _tool_result_as_input(SAY, refused, zone)
                    continue
                # What the guest hears decides which offered dishes count as
                # put to them (guards.py, rule 2).
                guards.note_said(state, say)
                return TurnResult(
                    state=state, say=[say], ended=state.stage in ENDED_STAGES
                )

            if (
                turn.tool_call.name in _COMPLAINT_TOOLS
                and turn.tool_call.args.get("reason") == "complaint"
                and wish is None
                and (offered := self._offer_remedy(state, user_text)) is not None
            ):
                # The model only says it is a complaint. What follows is the
                # core's, and its prompt rule stays as it is (D19).
                log_refused(
                    self._session,
                    state.call_id,
                    state.tenant_id,
                    turn.tool_call.name,
                    _RULE_FIRST,
                )
                return offered

            # Hard rules 2 and 3 are held here, not by the prompt (guards.py).
            refused = guards.refusal(
                state,
                start,
                user_text,
                turn.tool_call,
                cart_sent=turn.cart is not None,
            )
            if refused is None and turn.tool_call.name == "transfer_to_team":
                # The model hands the call over: the core carries it out as
                # its own handover, with the card for a team leg nobody
                # answers and the callback when the team is not there. Given
                # "available: false" back, a model answered with a handover
                # sentence and the guest got neither (#655). Asked again
                # after a transfer, a real model called the tool on its own
                # result until the hop limit (13.10.2026).
                reason = _transfer_reason(turn.tool_call.args)
                handover = self._handoff(
                    state,
                    SAY_ESCALATION,
                    reason,
                    user_text,
                    summary=_HANDOFF_SUMMARIES.get(reason, _DEFAULT_HANDOFF_SUMMARY),
                    ask_number=True,
                    table=open_table,
                )
                return handover
            if refused is None and guards.notes_an_additive(turn.tool_call):
                # Read back, the note would promise what no table holds: the
                # draft goes to the team as an additive question (#649).
                log_refused(
                    self._session,
                    state.call_id,
                    state.tenant_id,
                    turn.tool_call.name,
                    guards.ADDITIVE_CLAIM,
                )
                return self._note_to_team(
                    state,
                    turn.tool_call,
                    lead=SAY_ALLERGENS_BEYOND,
                    card=SUMMARY_ADDITIVES,
                    open_table=open_table,
                )
            call = turn.tool_call
            if refused is None:
                # A note read back must not promise a dish free of anything
                # (L17).
                refused = guards.claims_in_a_note(state, call)
            if refused is None:
                refused = _callback_reason_refused(call)
            if refused is None and (missing := guards.unnoted_allergy(state, call)):
                if allergy_hint_given:
                    # Sent again without it: the core writes the guest's own
                    # words onto it and lets it go, never a loop and never a
                    # draft without them (Maxi, 15.11.2026).
                    noted = guards.with_the_guests_words(state, call)
                    if noted is None:
                        return self._note_to_team(
                            state,
                            call,
                            lead=None,
                            card=f"{SUMMARY_NOTE_TO_CLEAR}.",
                            open_table=open_table,
                        )
                    call = noted
                else:
                    # Back to the model once, with the guest's words. The
                    # draft stays on the card of a handover meanwhile.
                    refused, allergy_hint_given = missing, True
                    open_table = self._draft_on_card(state, call, open_table)
            if refused is not None:
                log_refused(
                    self._session,
                    state.call_id,
                    state.tenant_id,
                    turn.tool_call.name,
                    refused,
                )
                result = refused
            else:
                call = _marked_callback(state, _card_without_allergy(state, call))
                result = self._dispatch(state, call.name, call.args)
            if (
                turn.tool_call.name == "search_menu"
                and _found_nothing(result)
                and escalation.asks_about_allergies(user_text)
            ):
                # No dish to look the allergens up on, so nothing the model
                # says can answer it (`allergie_0002`, L17), and nothing about
                # what is stored holds for a dish nobody read. Before the
                # result is applied: a failed search is no change of mind that
                # takes the read-back and the intent back. The search words
                # stay off the card, they can hold the guest's allergy (L13).
                return self._allergens_to_team(
                    state, lead=SAY_ALLERGENS_BEYOND, table=open_table
                )
            if open_table == SUMMARY_TABLE_UNREAD and call.name == "create_reservation":
                # The draft held back unread came back readable: the card
                # names this one, as refused where the domain refuses it
                # below (#862, #940).
                open_table = self._open_draft(state, call) or open_table
            read_back = state.reservation_id
            apply_tool_result(state, call.name, result, call.args)
            guards.note_tool(aturn, call.name, result, call.args)
            if (
                refused is None
                and open_table is not None
                and result.error_code == Conflict.code
                and not result.data.get("team_callback")
                and (
                    # The yes to the table read back: its seats are gone,
                    # reservations are off, the tablet cancelled it. Not a
                    # failed correction: the table read back still holds.
                    (
                        call.name == "confirm"
                        and read_back is not None
                        and state.reservation_id is None
                    )
                    # The draft on the card after an allergy hint (above).
                    or (
                        call.name == "create_reservation"
                        and open_table == self._open_draft(state, call)
                    )
                )
            ):
                # The domain refused the very table on the card: it is named
                # as refused, never as open, and the guest still hears that it
                # is not booked (#862).
                open_table = RefusedTable(open_table)
            told = result.say or ""
            if not result.ok and told.endswith(SAY_TO_THE_TEAM):
                # The tool's sentence promises the team: a draft cancelled on
                # the tablet, a failure the guest cannot fix. The core keeps
                # that promise here. Passed back, the model spoke the
                # sentence, nothing followed, and the guest waited until the
                # line hung up (#655, rule 5).
                lead = told.removesuffix(SAY_TO_THE_TEAM).strip() or None
                # With nobody to reach and no number the core is still on
                # the line: it asks for one (Codex PR #658).
                handover = self._handoff(
                    state,
                    SAY_TO_THE_TEAM,
                    detail=user_text,
                    summary=lead or _DEFAULT_HANDOFF_SUMMARY,
                    ask_number=True,
                )
                handover.say = [s for s in (lead, *handover.say) if s]
                if not handover.ended:
                    guards.note_said(state, " ".join(handover.say))
                return handover
            if (
                turn.tool_call.name == "get_item_details"
                and result.ok
                and result.say == SAY_ALLERGENS_UNKNOWN
            ):
                # Asked for allergens the database does not hold (L17): the
                # team answers before any `confirm`, and the model is not
                # asked again, as for a table the team decides on.
                return self._allergens_to_team(
                    state, _dish(result.data), table=open_table
                )
            if turn.tool_call.name == "confirm" and result.ok:
                # Booked: the core says so itself and ends the turn. A model
                # that phrased it said what the database did not hold (a code
                # it made up, a table never drafted), and every rule that
                # judged its words let a false one through (Codex PR #347).
                state.booking_say = _booked_say(
                    state, result.data, self._order_name(state)
                )
                # A confirm that held the guest past a limit goes to the team
                # like any slow tool on the next hop would; the booking stands
                # (Codex PR #347).
                # The guest still hears what was booked, and the handoff's card
                # says so (`_handoff`), or the team books a second time.
                late = None
                if self._clock() - self._started > self._max_call_seconds:
                    late = self._outage(state, SAY_TIMEOUT, user_text, open_table)
                elif (
                    left := self._left_in_turn(turn_started)
                ) is not None and left <= 0:
                    late = self._too_slow(state, user_text, open_table)
                if late is not None:
                    late.say = [state.booking_say, *late.say]
                    return late
                # A table booked while an order stands unplaced: the line
                # hangs up after this sentence, so it says so, as the team
                # path does (`_table_to_team`; Codex PR #347).
                open_order = state.order_id is None and bool(state.cart)
                said = " ".join(
                    s
                    for s in (
                        state.booking_say,
                        SAY_ORDER_NOT_PLACED if open_order else None,
                        SAY_BOOKED_GOODBYE,
                    )
                    if s
                )
                guards.note_said(state, said)
                return TurnResult(
                    state=state, say=[said], ended=state.stage in ENDED_STAGES
                )
            if result is guards.CALLBACK_WITHOUT_NUMBER:
                handed = self._callback_without_number(
                    state,
                    turn.tool_call.args,
                    user_text,
                    reported_failures,
                    open_table,
                )
                if handed is not None:
                    return handed
            if result.data.get("team_callback"):
                # A table that is free, but the team decides (#252). The core
                # hands the wish over here and now, and the model is not asked
                # again: it answers with a sentence or a tool call, never
                # both, so the tool's sentence would end its turn, and a guest
                # who hangs up then would be lost (Codex PR #280).
                return self._table_to_team(state, turn.tool_call.args, result.say)
            called.append(turn.tool_call.name)
            if (
                result.ok
                and turn.tool_call.name == "get_item_details"
                and told.startswith(SAY_ALLERGENS_LEAD)
                and told not in allergen_says
            ):
                allergen_says.append(told)
            pending_input = _tool_result_as_input(turn.tool_call.name, result, zone)

        return self._outage(state, SAY_STUCK, user_text, open_table)

    def _offer_remedy(
        self, state: ConversationState, user_text: str
    ) -> TurnResult | None:
        """A complaint with a fixed answer in the catalogue (D19): the core
        offers the rule in the words of its row and the model is not asked.
        None leaves the complaint with the team, as with an empty catalogue,
        no match or any doubt. The call ends with the remedy, so nothing else
        may be open in it; and without a number nobody could carry it out."""
        if (
            state.stage != "start"
            or state.slots.keys() != {"phone"}
            or state.cart
            or state.seen_items
            or state.allergy_talk
            or state.allergy_said
            or speaks_of_health(user_text)
            or not escalation.only_a_complaint(user_text)
        ):
            return None
        rule = matching_rule(self._session, state.tenant_id, user_text)
        if rule is None:
            return None
        state.remedy_offer = RemedyOffer(
            rule_id=rule.id,
            name=rule.name,
            remedy_cents=rule.remedy_cents,
            offer_say=rule.offer_say,
            granted_say=rule.granted_say,
            complaint=user_text,
        )
        guards.note_said(state, rule.offer_say)
        return TurnResult(state=state, say=[rule.offer_say])

    def _offer_answered(self, state: ConversationState, user_text: str) -> TurnResult:
        """The answer to a rule the core offered. Only a yes as strict as for a
        booking, to the offer as the last thing the guest heard, books it
        (rule 3): a card tells the team what to carry out, with the audit row
        in its transaction, and the money moves when the team does it.
        Anything else, a card that cannot be written too, goes to the team,
        with what was offered and the guest's words on the card: the team
        must not offer it again."""
        offer, state.remedy_offer = state.remedy_offer, None
        assert offer is not None  # `run_turn`
        note_guest_details(state, user_text)
        # As in `_callback_answered`: an allergy in the answer marks the card.
        _note_allergy_said(state, user_text)
        if state.last_say == last_words(offer.offer_say) and is_yes(user_text):
            note_remedy(
                self._session,
                state.tenant_id,
                state.call_id,
                offer.rule_id,
                offer.remedy_cents,
            )
            if self._complaint_card(state, offer, SUMMARY_REMEDY_BOOKED).ok:
                return TurnResult(state=state, say=[offer.granted_say], ended=True)
        result = self._handoff(
            state,
            SAY_ESCALATION,
            "complaint",
            f"{offer.complaint} {user_text}",
            summary=_remedy_card(SUMMARY_REMEDY_OPEN, offer),
        )
        return result

    def offer_unanswered(
        self, state: ConversationState, *, with_the_team: bool = False
    ) -> None:
        """The call ended while a rule waited for its answer: nothing is
        booked, and the complaint stays with the team on a card, or a guest
        who hangs up on the offer is lost (rule 5). Called by whoever closes
        a call; a call without an open offer writes nothing.

        The same for an order drafted and not answered with a yes (#953),
        whatever the guest heard last: the time is a statement that invites a
        goodbye, a "Danke" alone is no yes (Maxi, 23.11.2026), and the guest
        may expect food nobody ordered. A table's read-back asks "Passt das so?", and a
        guest who leaves it unanswered knows nothing is booked. The order
        joins a card the call has open (#993, #998). Not when the
        call goes `with_the_team` (an outage, a failed line): the team has it,
        and its own card waits if nobody answers."""
        offer, state.remedy_offer = state.remedy_offer, None
        if offer is not None:
            self._complaint_card(state, offer, SUMMARY_REMEDY_OPEN)
        elif (
            not with_the_team
            # Also when a card ended the call on the read-back: one the model
            # wrote names no order (#998). The yes moves the cart away, and a
            # card that names the order already gets it no second time.
            and (
                state.stage == "readback_pending"
                or (state.stage == "callback" and state.cart)
            )
            and state.order_id is not None
            and (phone := state.slots.get("phone") or state.caller_phone)
        ):
            # Its own reason (#977): the tablet reads "Bestellung offen", not
            # "Nicht verstanden".
            card, _ = self._card(
                state, ORDER_OPEN, None, summary=SUMMARY_READBACK_UNANSWERED
            )
            args = {"phone": phone, "reason": ORDER_OPEN, "summary": card}
            try:
                result = self._dispatch(state, "create_callback", args)
            except IntegrityError:
                # shortcut: code ahead of its database (a pull before
                # `make migrate`) refuses the reason; the card goes out as
                # before #977 rather than not at all (rule 5). Drop once every
                # stack runs migration 045.
                self._session.rollback()
                args["reason"] = "not_understood"
                result = self._dispatch(state, "create_callback", args)
            apply_tool_result(state, "create_callback", result)

    def _complaint_card(
        self, state: ConversationState, offer: RemedyOffer, sentence: str
    ) -> ToolResult:
        """The callback card of a complaint a rule was offered for."""
        card = self._dispatch(
            state,
            "create_callback",
            {
                "phone": state.slots["phone"],
                "reason": "complaint",
                "summary": _handoff_summary(
                    "complaint", offer.complaint, base=_remedy_card(sentence, offer)
                ),
            },
        )
        apply_tool_result(state, "create_callback", card)
        return card

    def _callback_without_number(
        self,
        state: ConversationState,
        args: dict[str, Any],
        user_text: str,
        reported_failures: set[str],
        open_table: str | None = None,
    ) -> TurnResult | None:
        """The model wants the team to call back, and nobody's number is known.
        The team takes the call instead where it is reachable. Otherwise only
        the number is left, so the model asks for it, and the turn counts on
        the ladder for `phone` like a failed answer: on a line with nothing but
        noise a real model asked for the number on every turn, and the call
        ended with neither a callback nor the team (`eskalation_0016`, Codex
        PR #347, P1). It counts only once the model asked for the number: the
        guest's first "Rufen Sie mich zurück" is no failed answer (Codex PR
        #347). None lets the model ask."""
        reason = _transfer_reason(args)
        # The card before the transfer, as in `_handoff`: once transferred,
        # the state no longer shows a table read back (#690).
        card, lead = self._card(state, reason, user_text, table=open_table)
        transfer = self._dispatch(state, "transfer_to_team", {"reason": reason})
        apply_tool_result(state, "transfer_to_team", transfer)
        if state.transferred:
            # With the card for a team leg nobody answers, as in `_handoff`.
            state.handoff_summary = card
            return TurnResult(
                state=state, say=[*lead, transfer.say or SAY_ESCALATION], ended=True
            )
        if state.asked_for_phone and "phone" not in reported_failures:
            self._ladder.record_failure("phone")
            if self._ladder.should_end_call("phone"):
                return self._handoff(
                    state,
                    SAY_NOT_UNDERSTOOD,
                    reason="not_understood",
                    detail=user_text,
                    table=open_table,
                )
        # The model is told to ask for it: behind this a bare "Nummer" is the
        # number to call back (`guest_details.asks_for_a_phone_number`). A
        # second refused try in this turn is no failed answer: the guest has
        # not heard the question yet (Codex PR #452, P1).
        reported_failures.add("phone")
        state.asked_for_phone = True
        return None

    def _order_name(self, state: ConversationState) -> str | None:
        """The name the confirmed order runs under, from the database, when
        the guest said it: a stored name is not said aloud (L12)."""
        order = self._session.get(Order, state.order_id) if state.order_id else None
        if order is None or not name_said(order.customer_name, state.said_words):
            return None
        return order.customer_name

    def _left_in_turn(self, turn_started: float | None) -> float | None:
        """Seconds the guest still waits for this turn; None without a deadline."""
        if turn_started is None or self._turn_deadline is None:
            return None
        return self._turn_deadline - (self._clock() - turn_started)

    def _too_slow(
        self,
        state: ConversationState,
        user_text: str,
        table: str | None = None,
    ) -> TurnResult:
        """The guest has waited the restaurant's deadline in silence: an outage
        for them, so the call goes to the team with the outage sentence
        (CLAUDE.md §2 rule 5), like a model that failed outright."""
        log(
            logger,
            logging.ERROR,
            "no answer within the turn deadline, handing over to the team",
            call_id=str(state.call_id),
            deadline_seconds=self._turn_deadline,
        )
        return self._outage(state, SAY_ON_FAILURE, user_text, table)

    def _outage(
        self,
        state: ConversationState,
        say: str,
        user_text: str,
        table: str | None = None,
    ) -> TurnResult:
        """A fault of ours ends the turn: the model is down, too slow or goes
        in circles, or the call is past its limit. To the team like every
        handover. With nobody to reach and no number known, the core asks for
        one before it gives up: the question and its answer need no model
        (`_callback_answered`), and the call ended with nobody rung and no
        card (#652, rule 5). Not behind a booking: it stands, and the phone
        line hangs up after its sentence (`outcome.CLOSING_STAGES`). `table`
        is the table the turn began with (`_open_table`)."""
        booked = state.stage == "confirmed"
        if booked:
            table = None  # the table the turn began with is booked now
        return self._handoff(
            state,
            say,
            "outage",
            detail=user_text,
            summary=SUMMARY_OUTAGE,
            ask_number=not booked,
            table=table,
        )

    def _prompt_state(
        self,
        state: ConversationState,
        guest_said: str | None = None,
        called: list[str] | None = None,
    ) -> dict[str, Any]:
        data = state.to_prompt_json()
        if guest_said:
            # Within one guest turn only: the next turn starts from the
            # compact state again, never from a transcript (docs/05 §5).
            data["guest_said"] = guest_said
            # The model's own last sentence was for reading the guest's words,
            # and that is done once a tool was called. Left in sight next to a
            # tool result, a real model said it again word for word instead
            # of the read-back the tool had just delivered (13.10.2026).
            data.pop("last_say", None)
        if called:
            # Also within the turn only. A model sees one tool result at a
            # time and not that it asked for it: without this a real model
            # called get_service_status again on its own result until the
            # hop limit, and the call ended with the team.
            data["called"] = list(called)
        # A model has no clock: "morgen um sieben" and "am Samstag" need today's
        # date and weekday. Local time of the restaurant, from the same clock
        # the tools get (`now`), so a replay with a fixed time stays the same.
        local = self._local_now(state)
        data["now"] = (
            f"{WEEKDAYS[local.weekday()]}, {local.isoformat(timespec='minutes')}"
        )
        hints = self._ladder.active_levels()
        if hints:
            # Sagt dem Modell, auf welcher Verständnis-Stufe eine Information
            # gerade steht (docs/05 §2), ohne den Verlauf mitzuschicken.
            data["ladder"] = hints
        return data

    def _local_now(self, state: ConversationState) -> datetime:
        """In the timezone of the tenant row, which is what the tools compute
        with: the day the model reads and the day `check_slot` means must be
        the same one, also around midnight. Read once per call; an unknown
        tenant falls back to the setting, the tools then answer `not_found`."""
        if self._zone is None:
            self._zone = (
                self._session.scalar(
                    select(Tenant.timezone).where(Tenant.id == state.tenant_id)
                )
                or settings.tenant_timezone
            )
        return to_local(self._now or utcnow(), self._zone)

    def _dispatch(
        self, state: ConversationState, name: str, args: dict[str, Any]
    ) -> ToolResult:
        return dispatch(
            self._session,
            state.call_id,
            state.tenant_id,
            name,
            args,
            now=self._now,
            # A draft the guest dropped is never replayed (#952).
            pending_order=state.order_id,
        )

    def _table_to_team(
        self, state: ConversationState, args: dict[str, Any], lead: str | None
    ) -> TurnResult:
        """The wish for a table the agent does not book itself goes to the
        team and the call ends: as a callback under the number in the state,
        after `lead`, the sentence of the tool. Without a number there is
        nobody to call back, and a callback can fail: both go the way of
        every handover (`_handoff`), a transfer where the team is reachable.
        With neither the team nor a number the core asks for one, and the wish
        waits as the card (`state.callback_waits`, #690)."""
        wish = self._table_wish(state, args)
        phone = state.slots.get("phone")
        if phone:
            callback = self._dispatch(
                state,
                "create_callback",
                {
                    "phone": phone,
                    "reason": "out_of_scope",
                    "summary": _marked(
                        state,
                        _with_open_order(
                            state,
                            wish,
                            reserve=len(self._bookings_on_card(state) + _MARKER) + 2,
                        ),
                    ),
                },
            )
            apply_tool_result(state, "create_callback", callback)
            if callback.ok:
                not_placed = SAY_ORDER_NOT_PLACED if state.cart else None
                said = [s for s in (lead, not_placed, callback.say) if s]
                return TurnResult(state=state, say=said, ended=True)
        # The wish is the card, not a sentence of the guest: as `detail` it
        # was cut short, and dropped whole when a line of the order named the
        # guest's allergy (#655).
        handed = self._handoff(
            state, SAY_ESCALATION, "out_of_scope", summary=wish, ask_number=True
        )
        handed.say = [s for s in (lead, *handed.say) if s]
        if not handed.ended:
            # The question for the number ends the turn (`_allergens_to_team`).
            guards.note_said(state, " ".join(handed.say))
        return handed

    def _beyond_the_database(
        self,
        state: ConversationState,
        user_text: str,
        open_table: str | None,
    ) -> TurnResult | None:
        """What no table answers goes to the team before the model, which
        knows no more than the database (L17): traces and shared frying, or
        an additive, which the data model has no field for (#638). An illness
        or a legal step in the sentence is a complaint the team has to read: its card quotes the guest (#649),
        unless the sentence names an allergy (L13). None for any other
        sentence."""
        if escalation.asks_beyond_the_allergens(user_text):
            card = SUMMARY_ALLERGENS
        elif escalation.names_an_additive(guards.without_menu_names(state, user_text)):
            card = SUMMARY_ADDITIVES
        else:
            return None
        lead: str | None = SAY_ALLERGENS_BEYOND
        reason = "out_of_scope"
        if escalation.names_an_incident(user_text):
            detail = None if _names_health(state, user_text) else user_text
            card = _handoff_summary("complaint", detail)
            lead, reason = None, "complaint"
        return self._allergens_to_team(
            state, lead=lead, table=open_table, card=card, reason=reason
        )

    def _allergens_to_team(
        self,
        state: ConversationState,
        dish: str | None = None,
        lead: str | None = SAY_ALLERGENS_UNKNOWN,
        table: str | None = None,
        card: str = SUMMARY_ALLERGENS,
        reason: str = "out_of_scope",
    ) -> TurnResult:
        """The guest asked about allergens beyond what the database holds
        (legal check L17, D24): the call goes to a team member, or a callback
        when nobody answers, and the call ends before any `confirm`. What
        stands open goes onto the card and the guest hears that it is not
        booked, as on every handover (`_card`); `table` is a table the state
        no longer shows. The card names the question and the dish, never the
        guest's allergy (L13). `card` is another reason the team has to
        clear before an order (#638)."""
        handed = self._handoff(
            state,
            SAY_ESCALATION,
            reason,
            summary=card + (f" Gericht: {dish}." if dish else ""),
            ask_number=True,
            table=table,
        )
        handed.say = [s for s in (lead, *handed.say) if s]
        if not handed.ended:
            # The question for the number ends the turn: the guest's digits
            # are a number to call (`state.note_guest_details`).
            guards.note_said(state, " ".join(handed.say))
        return handed

    def _note_to_team(
        self,
        state: ConversationState,
        call: ToolCall,
        lead: str | None,
        card: str,
        open_table: str | None = None,
    ) -> TurnResult:
        """A `card` the team has to clear first stops the draft `call`: the
        team takes the call, with the order or the table the draft asked for
        on the card (`_draft_on_card`), and the guest hears that it is not
        booked, after `lead`. Nothing is stored."""
        table = self._draft_on_card(state, call, open_table)
        return self._allergens_to_team(
            state, lead=lead, table=table, card=card, reason="out_of_scope"
        )

    def _draft_on_card(
        self, state: ConversationState, call: ToolCall, open_table: str | None
    ) -> str | None:
        """The table a handover's card names while the draft `call` is held
        back: the draft's, else the table read back before (`open_table`). A
        table draft no tool can read is named open without its values: they
        are the model's, checked by nobody (rule 2), and the team clears them
        with the guest, who hears that nothing is booked (#940)."""
        if table := self._open_draft(state, call) or open_table:
            return table
        return SUMMARY_TABLE_UNREAD if call.name == "create_reservation" else None

    def _open_draft(self, state: ConversationState, call: ToolCall) -> str | None:
        """A draft that never reached its tool, for the card of a handover:
        the table it asked for, or its order as the open cart."""
        if call.name == "create_reservation":
            with suppress(ValueError, TypeError):
                return self._table_parts(state, call.args)
            return None
        # The draft, also a correction the model sent without a `cart`
        # (Codex PR #414).
        with suppress(ValidationError):
            state.cart = cart_lines(call.args.get("items"))
            note = call.args.get("note")
            state.order_note = note if isinstance(note, str) and note else None
        return None

    def _open_table(self, state: ConversationState) -> str | None:
        """The table read back and not yet confirmed, as the team reads it on
        a card: party size, time in the restaurant's clock, the name."""
        if state.stage != "readback_pending" or state.reservation_id is None:
            return None
        reservation = self._session.get(Reservation, state.reservation_id)
        if reservation is None:
            return None
        when = reservation.reserved_for.astimezone(self._local_now(state).tzinfo)
        return (
            f"{reservation.party_size} Personen, {when:%d.%m.%Y %H:%M} Uhr, "
            f"Name {reservation.guest_name}"
        )

    def _callback_answered(
        self, state: ConversationState, user_text: str
    ) -> TurnResult:
        """The answer to the question for the callback number (L17). With a
        number the callback is made. Without one the question is put once
        more, up to `CALLBACK_NUMBER_ASKS` times ("Moment, ich schaue nach",
        a "Ja" to the phone line's "Sind Sie noch da?"; Codex and own review
        of PR #369), unless the guest declines; then the last resort. The
        call ends before the model and before any `confirm`."""
        # The digits are a number to call, whatever came between.
        state.asked_for_phone = True
        note_guest_details(state, user_text)
        # An allergy named with the number is kept for the net and marks the
        # card that waited (#940); the call ends here, no draft to take back.
        _note_allergy_said(state, user_text)
        rest = escalation.without_human_question(user_text)
        # "Spreche ich mit einem Menschen?" asks, it wishes nothing (Codex PR
        # #667): read past it, as `_guest_turn` does.
        if (wish := escalation.check(user_text if rest is None else rest)) is not None:
            # A complaint or the wish for a person in the answer: the team
            # calls back for that, not for the question's reason (#652).
            state.callback_reason = wish
        if (
            not state.slots.get("phone")
            and state.callback_asks < CALLBACK_NUMBER_ASKS
            and consent_answer(user_text) != "no"
        ):
            state.callback_asks += 1
            guards.note_said(state, SAY_CALLBACK_NUMBER)
            result = TurnResult(state=state, say=[SAY_CALLBACK_NUMBER])
        else:
            summary, state.callback_waits = state.callback_waits, None
            # The card that waited names what stands open, and the guest
            # heard so with the question for the number.
            result = self._handoff(
                state,
                SAY_ESCALATION,
                state.callback_reason,
                summary=summary,
                open_named=True,
            )
        return result

    def _table_wish(self, state: ConversationState, args: dict[str, Any]) -> str:
        """The wish as the team reads it on the card (`_table_parts`)."""
        return f"{SUMMARY_TEAM_TABLE} {self._table_parts(state, args)}."

    def _table_parts(self, state: ConversationState, args: dict[str, Any]) -> str:
        """Party size, time and name of a table wish. Party size and time are
        read the way the tool read them (`CheckSlotRequest`), not from the raw
        arguments: a time a model wrote as seconds since 1970 or in another
        zone is shown as the restaurant's clock shows it. A name only when the
        guest said it in this call: `check_slot` takes no name, so one a model
        wrote next to its arguments has passed no guard (Codex PR #280)."""
        # Read the way the tool read it: a time without an offset is the
        # restaurant's local time, or the card raised on it (Codex PR #347).
        args = in_local_time(self._session, state.tenant_id, args)
        wish = CheckSlotRequest.model_validate(
            {
                "call_id": state.call_id,
                "tenant_id": state.tenant_id,
                "reserved_for": args.get("reserved_for"),
                "party_size": args.get("party_size"),
            }
        )
        when = wish.reserved_for.astimezone(self._local_now(state).tzinfo)
        names = (args.get("guest_name"), state.slots.get("guest_name"))
        name = next((n for n in names if name_said(n, state.said_words)), None)
        parts = [f"{wish.party_size} Personen", f"{when:%d.%m.%Y %H:%M} Uhr"]
        if name:
            parts.append(f"Name {name}")
        return ", ".join(parts)

    def _handoff(
        self,
        state: ConversationState,
        fallback_say: str,
        reason: str = "not_understood",
        detail: str | None = None,
        *,
        summary: str | None = None,
        ask_number: bool = False,
        table: str | None = None,
        open_named: bool = False,
    ) -> TurnResult:
        """Übergabe wirklich ausführen statt nur anzukündigen: erst versuchen, live zu
        verbinden; ist niemand erreichbar und kennen wir eine Rufnummer, stattdessen
        einen Rückruf anlegen. Ohne bekannte Rufnummer bleibt nur der ehrliche
        Fallback-Satz — raten (CLAUDE.md §2 Regel 2) ist keine Option.
        `fallback_say` is spoken only when a transfer or a callback really
        happened and its tool gave no sentence; the last resort has its own.

        `detail` ist der auslösende Kundenzug: ohne ihn bekäme das Team bei einer
        Vorab-Eskalation (kein Modell-Aufruf) nur eine feste Floskel statt der
        eigentlichen Bitte ("Reservierung morgen 18 Uhr auf Müller stornieren"),
        obwohl sonst nirgends ein Transkript gespeichert ist (Codex-Review
        PR #102, P2).

        `ask_number`: with neither a team nor a number, ask the guest for one
        instead of the last resort; the next turn makes the callback with
        the card (`state.callback_waits`, L17; Codex PR #369).

        Whatever ends the call, an order or a table that stands open goes
        onto the card, and the guest hears first that it is not booked
        (`_card`, #655); `table` and `open_named` as there."""
        card, lead = self._card(state, reason, detail, summary, table, open_named)
        transfer = self._dispatch(state, "transfer_to_team", {"reason": reason})
        apply_tool_result(state, "transfer_to_team", transfer)
        if transfer.ok and transfer.data.get("available"):
            # The card goes with the call: the phone line writes it if nobody
            # answers the team leg (Codex PR #369), the same card as for a
            # callback; `create_callback` adds the bookings of the call (#764).
            state.handoff_summary = card
            return TurnResult(
                state=state, say=[*lead, transfer.say or fallback_say], ended=True
            )

        phone = state.slots.get("phone")
        if phone:
            callback_reason = (
                reason
                if reason in _CALLBACK_REASONS
                else _CALLBACK_REASON_FOR.get(reason, _CALLBACK_REASON_FALLBACK)
            )
            callback = self._dispatch(
                state,
                "create_callback",
                {"phone": phone, "reason": callback_reason, "summary": card},
            )
            apply_tool_result(state, "create_callback", callback)
            if callback.ok:
                return TurnResult(
                    state=state, say=[*lead, callback.say or fallback_say], ended=True
                )

        if ask_number and not phone:
            state.callback_waits, state.callback_reason = card, reason
            # The question ends the turn: the guest's digits are a number to
            # call (`state.note_guest_details`), and a "Ja" confirms nothing.
            asked = [*lead, SAY_CALLBACK_NUMBER]
            guards.note_said(state, " ".join(asked))
            return TurnResult(state=state, say=asked)
        if state.stage != "confirmed":
            # A booking of this call stands, and its call is logged as one.
            state.stage = "ended"
        return TurnResult(state=state, say=[*lead, SAY_NOBODY_REACHABLE], ended=True)

    def _card(
        self,
        state: ConversationState,
        reason: str,
        detail: str | None,
        summary: str | None = None,
        table: str | None = None,
        open_named: bool = False,
    ) -> tuple[str, list[str]]:
        """The card of a handover, and what the guest hears before the
        handover sentence. The card is `summary`, or the sentence of the
        reason, with the table read back and not confirmed and the order that
        stands unplaced: time limit, outage, hop limit and the wish for a
        person all end a call in the middle of one, and the card read "Kunde
        sagte: Ja" (#655), and then the guest's sentence `detail`. The card
        is what waits for a callback number (`state.callback_waits`): the
        guest's words must not be lost while the core asks for one (Codex PR
        #658). The bookings of the call are no part of it: `create_callback`
        adds them from the database when the card is written (#764). `table`
        is a table the state no longer shows (`_note_to_team`). `open_named`:
        `summary` is a card that waited for a number and names all of it
        already (`_callback_answered`)."""
        if detail and _names_health(state, detail):
            # The guest's sentence names an allergy, which is health data: it
            # is not written on a card. The guest told it for the kitchen
            # (L13), and the team asks when it calls back.
            detail = None
        head = summary or _HANDOFF_SUMMARIES.get(reason, _DEFAULT_HANDOFF_SUMMARY)
        said = _card_tail(detail)
        if open_named:
            return _marked(state, _judged_again(state, head + said)), []
        table = self._open_table(state) or table
        if table:
            label = (
                SUMMARY_REFUSED_TABLE
                if isinstance(table, RefusedTable)
                else SUMMARY_OPEN_TABLE
            )
            head += f" {label} {table}."
        # The guest spoke of an allergy in the call: the team asks about the
        # kitchen note when it calls back, also where a note may carry it
        # already (ledger #764 review), and the card never names it (L13).
        marker = f" {_MARKER}" if state.allergy_said else ""
        # Room for what follows the order, for the line's first sentence on a
        # team leg nobody answered and for the bookings `create_callback`
        # adds: all of it stands on that card (#207, #764).
        bookings = self._bookings_on_card(state)
        reserve = len(said + marker + SUMMARY_MISSED + bookings) + 2
        head = _marked(state, _with_open_order(state, head, reserve=reserve))
        # What stands open, each: a table and an order both (#940).
        not_booked = [
            say
            for say, stands in (
                (SAY_ORDER_NOT_PLACED, state.cart),
                (SAY_TABLE_NOT_BOOKED, table),
            )
            if stands
        ]
        # A callback takes 1000 characters (`CreateCallbackRequest`): above,
        # the request is refused before `create_callback` could cut it, and
        # the guest's callback is lost (rule 5, Codex review of #944). The
        # end goes first: the guest's words, then what follows the order.
        return (head + said)[:_SUMMARY_MAX], not_booked

    def _bookings_on_card(self, state: ConversationState) -> str:
        """The bookings `create_callback` adds to a card, which an open order
        on it leaves room for (#764). Without an open order nothing is
        fitted, so nothing is read."""
        if not state.cart:
            return ""
        return booked(self._session, state.tenant_id, state.call_id) or ""


def _with_open_order(state: ConversationState, summary: str, reserve: int = 0) -> str:
    """The card's sentence with the order that stands unplaced, if any (#207),
    fitted so the card stays inside what a callback takes, `reserve`
    characters left for what follows it."""
    if not state.cart:
        return summary
    head = f"{summary} {SUMMARY_OPEN_ORDER} "
    return f"{head}{_order_lines(state, _SUMMARY_MAX - reserve - len(head) - 1)}."


def _order_lines(state: ConversationState, room: int) -> str:
    """The open order as the team reads it, with number and name from the
    menu search (CLAUDE.md §2 rule 1), never from the model, and with the
    chosen options and the note of each line: two pizzas of different sizes
    are two lines (Codex PR #300). Longer than `room`: without the names, the
    team reads the dishes from the card; and what still does not fit is
    counted, never dropped in silence. A note that names the guest's health
    is not on it: a card is kept for the period of a callback, the allergy
    only until the night after its order (L13; Codex PR #369, P1).
    """
    full, short = [], []
    for line in state.cart:
        seen = state.seen_items.get(line["menu_item_id"], {})
        number, name = seen.get("number"), seen.get("name")
        chosen = [f"{o['group']}: {o['name']}" for o in line.get("options", [])]
        note = line.get("note")
        if note and _names_health(state, note):
            # shortcut: a removal in the same note ("ohne Zwiebeln") goes with
            # the health part; the team calls the guest about the note anyway.
            note = SUMMARY_NOTE_TO_CLEAR
        extra = "; ".join([*chosen, *([note] if note else [])])
        tail = f" ({extra})" if extra else ""
        dish = " ".join(x for x in (number, name) if x) or "Gericht"
        full.append(f"{line['quantity']}x {dish}{tail}")
        short.append(f"{line['quantity']}x {number or name or 'Gericht'}{tail}")
    # What the kitchen must know for the whole order comes first and is no
    # position: it never goes and is never counted (#656).
    whole = state.order_note
    if whole and _names_health(state, whole):
        whole = SUMMARY_NOTE_TO_CLEAR
    first = [f"für die ganze Bestellung: {whole}"] if whole else []
    if len(text := ", ".join(first + full)) <= room:
        return text
    for kept in range(len(short), -1, -1):
        rest = len(short) - kept
        parts = (
            first
            + short[:kept]
            + ([f"{rest} weitere Position{'en' * (rest > 1)}"] if rest else [])
        )
        if len(text := ", ".join(parts)) <= room:
            return text
    # The count stands even where nothing fits (bookings of the call take the
    # room, #764): `create_callback` cuts the card's own text at its end.
    return text


def _booked_say(
    state: ConversationState, data: dict[str, Any], name: str | None
) -> str:
    """The sentence of a confirm that went through: what was booked, from the
    confirm result and the order in the database. An order that waits for the
    team says so (`handover`), and it is named by the name it runs under."""
    if state.order_id is None:
        return SAY_TABLE_BOOKED
    placed = (
        SAY_ORDER_WAITS
        if data.get("handover") == "awaiting_approval"
        else SAY_ORDER_PLACED
    )
    if not name:
        return placed
    return placed.replace("Ihre Bestellung ist", SAY_FOR_NAME.format(name=name), 1)


def _note_allergy_said(state: ConversationState, text: str) -> bool:
    """Keeps the guest's sentence when it speaks of their health
    (`escalation.arms_the_net`) or answers the model's question about an
    allergy ("Gegen Erdnüsse."), for the safety net and the model, which
    keeps nothing between turns (Maxi, 15.11.2026). A bare denial in answer
    to such a question takes back what `_taken_back` says. True for every
    sentence kept: a read-back waiting for its yes may not carry it."""
    asked = _asked_about_an_allergy(state)
    if escalation.bare_denial(text, asked=asked):
        if asked:
            state.allergy_said = _taken_back(state.allergy_said, text)
        return False
    armed = escalation.arms_the_net(text)
    if armed or asked:
        # A sentence said again is kept once, as the newest, also in other
        # case or punctuation ("ich habe eine Erdnussallergie!").
        said = text.strip()
        kept = [*(old for old in state.allergy_said if _differs(old, said)), said]
        # Over the bound, the oldest that names nothing goes: five later
        # sentences ("Ich bin Allergiker.") pushed the first out, and the net
        # no longer knew the peanut (#940). One that names what the guest
        # reacts to (`_keeps_a_name`, "reagiere auf Erdbeeren" too) always
        # stays, and so does the newest (it may answer the model's
        # question); there are few in a call.
        while len(kept) > ALLERGY_SAID_MAX and (
            plain := next((said for said in kept[:-1] if not _keeps_a_name(said)), None)
        ):
            kept.remove(plain)
        state.allergy_said = kept
    # An answer to the model's question ("Gegen Erdnüsse.") is new health
    # information too: a read-back before it lacks it (Codex PR #788, P1).
    return armed or asked


def _taken_back(kept: list[str], denial: str) -> list[str]:
    """What a bare denial that answers the model's question about an allergy
    leaves of the kept sentences. Another person's always stays: "Nein." to
    "Haben Sie selbst eine Allergie?" after "Meine Frau hat eine
    Sesamallergie" is about the caller (Codex PR #788, P1). Of the caller's
    own, a denial that names what it denies ("Nein, keine Nussallergie.")
    takes back the sentences that name only that; a bare one ("Keine.",
    "Nein.") takes all back when none names what the caller reacts to ("Ich
    habe eine Allergie." - "Wogegen?" - "Keine.", `allergie_0012`), and none
    when one does: after "Ich habe eine Erdnussallergie." a "Nein." to "Haben
    Sie sonst noch Allergien?" denies a further one (ledger #764 review, P1).
    shortcut: a "Nein." to a question that names the allergen ("Sie haben
    also eine Sesamallergie?") keeps it, as the question may ask about a
    further one, and a denial worded unlike the sentence ("keine Allergie
    gegen Nüsse" after "Nussallergie") too; the model words the note, the
    safe side."""
    own = [said for said in kept if not names_someone_else(said)]
    if denied := guards.reacts_to(denial, denied=True):
        gone = [said for said in own if (named := _named(said)) and named <= denied]
    else:
        gone = [] if any(map(_names_any, own)) else own
    return [said for said in kept if said not in gone]


def _named(said: str) -> frozenset[str]:
    """What a kept sentence names; one without a health word was kept as the
    answer to the model's question and names its allergens ("Gegen
    Erdnüsse.")."""
    if speaks_of_health(said):
        return guards.reacts_to(said)
    return frozenset(allergens_named(said))


def _names_something(said: str) -> bool:
    """Does a kept sentence name what the caller reacts to? An answer to the
    model's question does ("Kiwi."), unless it only says yes."""
    return bool(_named(said)) or not (speaks_of_health(said) or is_yes(said))


def _differs(old: str, said: str) -> bool:
    """Do two kept sentences differ in more than case and punctuation?"""
    return _WORD.findall(_plain(old)) != _WORD.findall(_plain(said))


def _keeps_a_name(said: str) -> bool:
    """A kept sentence that stays over the bound: one that names what someone
    reacts to, or an allergen anywhere, as `_names_health` reads it for a
    card ("Bei Erdnüssen bekomme ich Atemnot, ich bin allergisch.")."""
    return _names_any(said) or bool(allergens_named(said))


def _names_any(said: str) -> bool:
    """Does a kept sentence name what someone reacts to, as the net or a card
    reads it (`_names_something`, `_ingredients`: "reagiere auf Erdbeeren")?"""
    return _names_something(said) or bool(_ingredients(said))


def _asked_about_an_allergy(state: ConversationState) -> bool:
    """Did the model's last sentence ask about an allergy ("Wogegen sind Sie
    allergisch?", "Haben Sie eine Allergie?")? Never a read-back: its note
    names the allergy, and the "Ja" to it answers the order."""
    say = state.last_say or ""
    if guards.SAY_ASK_ALLERGY in say:
        # The fixed question of an allergy turn (#798).
        return True
    if state.readback and say.lstrip("…") in state.readback:
        return False
    # Read without the read-back and the noted sentence beside it: "Ihren
    # Hinweis gebe ich an die Küche weiter. <read-back> Passt das so?" asks
    # about the order (deep review of #860, P1).
    for core in (state.readback, guards.SAY_ALLERGY_NOTED):
        if core:
            say = say.replace(core, " ")
    lowered = say.lower()
    return "?" in say and ("allerg" in lowered or "wogegen" in lowered)


# Words a card may share with the guest's sentences about their health
# without naming it (`_names_health`).
_PLAIN_WORDS = frozenset(
    {"gegen", "keine", "keinen", "einen", "einer", "einem", "bitte", "haben"}
    | {"meine", "meinen", "meiner", "meinem", "nicht", "schon", "danke", "guten"}
    | {"hallo", "nummer", "abholen", "bestellen", "mochte", "heute", "morgen"}
    | {"allergie", "allergisch"}
)


def _names_health(state: ConversationState, text: str) -> bool:
    """Does `text` speak of the guest's health: in a health word or a health
    note (`wishes.speaks_of_health`, `wishes.HEALTH_NOTE`), or by a word of a
    sentence the guest said about it in this call (`state.allergy_said`),
    "Erdnüsse" there and "Erdnuss" here? A card kept for a callback never
    carries it (L13).
    shortcut: any shared word of five letters or more counts, a name in that
    sentence too; the card then gets the sentence of its reason, the safe
    side."""
    if speaks_of_health(text) or HEALTH_NOTE_RE.search(text):
        return True
    # The same allergen by name, a short one too: "Soja" there and here
    # (Codex PR #788, P1).
    if allergens_named(" ".join(state.allergy_said)) & allergens_named(text):
        return True
    said = {word for sentence in state.allergy_said for word in _stems(sentence)}
    # By prefix: "Erdnussallergie" there names "Erdnüssen" here (Codex PR
    # #788, P1).
    if any(a.startswith(b) or b.startswith(a) for a in said for b in _stems(text)):
        return True
    # What the guest reacts to, at any length: "Kiwi" there and "Kiwi" or
    # "Kiwis" here, as the allergen names above (#764); and the other way,
    # "Kiwis" or "Mohnbrötchen" there and "Kiwi" or "Mohn" here, which the
    # five-letter cut of `_stems` dropped (#798).
    ingredients = {w for sentence in state.allergy_said for w in _ingredients(sentence)}
    return any(
        word.startswith(ingredient) or (len(word) >= 4 and ingredient.startswith(word))
        for word in _WORD.findall(_plain(text))
        for ingredient in ingredients
    )


# The word in front of what a guest reacts to: "gegen Kiwi", "auf Reis",
# "kein Lamm", "keinen Mais".
_BEFORE_INGREDIENT = frozenset({"gegen", "auf", "kein", "keine", "keinen", "keiner"})
# Words between that word and the ingredient ("gegen die Kiwi").
_NO_INGREDIENT = frozenset(
    {"der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "alle"}
    | {"und", "oder", "auch", "so", "etwas", "was", "mit", "von", "zu"}
)
_WORD = re.compile(r"\w+")
# An allergy word behind its ingredient: "Kiwiallergie".
_ALLERGY_SUFFIX = re.compile(r"^(\w{3,}?)(?:allergi|unvertraglich|intoleran)")


def _plain(text: str) -> str:
    """`text` folded as `_stems` folds it, without dropping a word."""
    folded = text.casefold().replace("ß", "ss")
    for umlaut, plain in (("ä", "a"), ("ö", "o"), ("ü", "u")):
        folded = folded.replace(umlaut, plain)
    return folded


def _ingredients(sentence: str) -> set[str]:
    """The short words a guest's sentence about their health names as what
    they react to (#764): the words after "gegen", "auf" or "kein" up to the
    clause's end ("allergisch gegen Kiwi und Mango"), and the part of a
    compound in front of the allergy word ("Kiwiallergie"). Of three letters
    or more.
    shortcut: a short name after such a word ("auf den Namen Kim") counts
    too; the card then gets the sentence of its reason, the safe side."""
    found: set[str] = set()
    for clause in re.split(r"[.,;:!?]", _plain(sentence)):
        words = _WORD.findall(clause)
        for n, word in enumerate(words):
            if (match := _ALLERGY_SUFFIX.match(word)) is not None:
                found.add(match.group(1))
            if word in _BEFORE_INGREDIENT:
                found.update(
                    w
                    for w in words[n + 1 : n + 4]
                    if w not in _NO_INGREDIENT and w not in _PLAIN_WORDS
                )
    return {w for w in found if len(w) >= 3}


def _stems(text: str) -> set[str]:
    folded = text.casefold().replace("ß", "ss")
    for umlaut, plain in (("ä", "a"), ("ö", "o"), ("ü", "u")):
        folded = folded.replace(umlaut, plain).replace(f"{plain}e", plain)
    stems = set()
    for word in re.findall(r"\w+", folded):
        if len(word) < 5 or word in _PLAIN_WORDS:
            continue
        ending = next((e for e in ("en", "er", "e", "n", "s") if word.endswith(e)), "")
        stems.add(word.removesuffix(ending))
    return stems


def _card_without_allergy(state: ConversationState, call: ToolCall) -> ToolCall:
    """A callback whose `summary` the model wrote names the guest's allergy:
    health data on a card, kept longer than the kitchen note (L13). The card
    gets the sentence of its reason instead, as the core's own cards (`_handoff`),
    and the team asks when it calls back.
    Rewritten, not refused: a refused callback the model does not retry
    loses the guest (rule 5)."""
    summary = call.args.get("summary")
    if (
        call.name != "create_callback"
        or not isinstance(summary, str)
        or not _names_health(state, summary)
    ):
        return call
    reason = str(call.args.get("reason"))
    card = _handoff_summary(reason, None)
    return ToolCall(call.name, {**call.args, "summary": card})


def _marked_callback(state: ConversationState, call: ToolCall) -> ToolCall:
    """A callback the model writes carries the kitchen marker the core's own
    cards carry when the guest spoke of an allergy in the call (#940)."""
    summary = call.args.get("summary")
    if call.name != "create_callback" or not isinstance(summary, str):
        return call
    return ToolCall(call.name, {**call.args, "summary": _marked(state, summary)})


def _callback_reason_refused(call: ToolCall) -> ToolResult | None:
    """A model's card with a reason it may not name: `order_open` is the
    core's (#977), and the tablet would read "Bestellung offen" where nothing
    waits. A tuple, so a list or a dict as the reason compares instead of
    raising (Codex PR #347)."""
    if call.name != "create_callback":
        return None
    reason = call.args.get("reason")
    return None if reason in _MODEL_CALLBACK_REASONS else _CALLBACK_REASON_REFUSED


def _marked(state: ConversationState, card: str) -> str:
    """`card` with the marker that the team asks about the kitchen note, when
    the guest spoke of an allergy in the call and the card does not say so
    yet (L13: never which allergy). In front, never inside the guest's words:
    what follows a card cuts its end (the call's bookings in
    `create_callback`), and inside what a callback takes, or it is refused and
    the guest lost (rule 5, #940)."""
    if not state.allergy_said or SUMMARY_NOTE_TO_CLEAR in card:
        return card
    return f"{_MARKER} {card}".strip()[:_SUMMARY_MAX]


# What `_card` puts behind the guest's words of a summary (`_judged_again`).
_CARD_PARTS = (SUMMARY_OPEN_TABLE, SUMMARY_REFUSED_TABLE, SUMMARY_OPEN_ORDER)


def _judged_again(state: ConversationState, card: str) -> str:
    """A card that waited for a callback number, judged again: it was judged
    against the sentences kept before the guest's answer, which may name the
    allergy (#940). The guest's words and a note of the order that name it now
    stay off, as `_card` and `_order_lines` leave them off (L13).
    shortcut: a note is found by its text; one the cut to the card's length
    split is missed, the team calls about the note anyway."""
    # The guest's words first: a note taken from them would hide what they
    # name once it is replaced (#940 review).
    if (tail_at := card.find(_SAID_TAIL)) >= 0:
        # They end where the card's next part begins: a complaint's card has
        # the table and the order behind them (`_beyond_the_database`).
        ends = (card.find(f" {label}", tail_at) for label in _CARD_PARTS)
        end = min((at for at in ends if at >= 0), default=len(card))
        if _names_health(state, card[tail_at + len(_SAID_TAIL) : end]):
            card = card[:tail_at] + card[end:]
    for note in (state.order_note, *(line.get("note") for line in state.cart)):
        if note and _names_health(state, note):
            card = card.replace(note, SUMMARY_NOTE_TO_CLEAR)
    # A note shorter than the marker makes the card longer: over what a
    # callback takes it is refused and the guest lost (rule 5, #940 review).
    # shortcut: the end goes, the order's last lines too; refit them when a
    # waiting card near the limit with such notes is seen.
    return card[:_SUMMARY_MAX]


def _dish(data: dict[str, Any]) -> str | None:
    """A looked-up dish as a card names it: "13 Pho Bo"."""
    return f"{data.get('number') or ''} {data.get('name') or ''}".strip() or None


def _found_nothing(result: ToolResult) -> bool:
    """A search without a hit. One that split into several positions is ok
    itself, with the `not_found` in each part (`dispatch._search_menu`); a part
    that found its dish leaves something to look up."""
    parts = result.data.get("positions") or [{"error_code": result.error_code}]
    return all(part.get("error_code") == "not_found" for part in parts)


def _handoff_summary(
    reason: str, detail: str | None, *, base: str | None = None
) -> str:
    """The card of a handover. `base` replaces the sentence of the reason.
    The bookings of the call follow from the database (`create_callback`)."""
    base = base or _HANDOFF_SUMMARIES.get(reason, _DEFAULT_HANDOFF_SUMMARY)
    return base + _card_tail(detail)


def _card_tail(detail: str | None) -> str:
    """What follows the first sentence of a card: the guest's words, cut."""
    return f"{_SAID_TAIL}{detail[:_SUMMARY_DETAIL_MAX]}" if detail else ""


def _transfer_reason(args: dict[str, Any]) -> str:
    """The reason a model gave for a handover, where it is one. One that is
    no string is none: `in` on a list or a dict would raise (Codex PR #347)."""
    reason = args.get("reason")
    if isinstance(reason, str) and reason in _TRANSFER_REASONS:
        return reason
    return "not_understood"


def _remedy_card(card: str, offer: RemedyOffer) -> str:
    """A card sentence with the rule's name and amount, both from its row."""
    return card.format(remedy=f"{offer.name}, {spoken_euro(offer.remedy_cents)}")


def _tool_result_as_input(name: str, result: ToolResult, zone: tzinfo) -> str:
    """Ergebnis dem Modell als nächster Zug zurückgeben, ohne echten Kundeninput.
    Times in the restaurant's local time (`zone`), like `now` in the state."""
    if result.ok:
        data = result.data
        if name == "get_item_details":
            # The model has nothing to relay: only the core's table sentence
            # speaks a dish's allergens (#798, rule 1).
            data = {key: value for key, value in data.items() if key != "allergens"}
        payload = {"tool": name, "ok": True, "data": _local_times(data, zone)}
    else:
        payload = {"tool": name, "ok": False, "error_code": result.error_code}
    if result.hint:
        # Why the core refused the call (guards.py): without it the model sees
        # only an error code and has nothing to act on.
        payload["hint"] = result.hint
    if result.say:
        # Die vorgeschriebene Formulierung heikler Fälle (ausverkauft, außerhalb
        # der Zone, ...) kommt aus dem Code, nicht vom Modell (docs/05 §5) - ohne
        # sie hier weiterzugeben, müsste das Modell aus dem bloßen error_code
        # selbst improvisieren.
        payload["say"] = result.say
    return json.dumps(payload, default=str, ensure_ascii=False)


def _local_times(value: Any, zone: tzinfo) -> Any:
    """Every point in time of a tool result in local time. The tools answer in
    UTC (`closes_at` 20:00Z for 22:00), and a real model read the UTC hour as
    the restaurant's and said "bis 21 Uhr" and "bis 22 Uhr" for the same 22:00
    (13.10.2026). Only text with a date, a time and an offset is one; a bare
    date or a number stays as it is."""
    if isinstance(value, dict):
        return {key: _local_times(item, zone) for key, item in value.items()}
    if isinstance(value, list):
        return [_local_times(item, zone) for item in value]
    if isinstance(value, str) and "T" in value:
        # A free text such as a note can look like a time at the edge of the
        # calendar, which no zone can take (Codex PR #347).
        with suppress(ValueError, OverflowError):
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is not None:
                return moment.astimezone(zone).isoformat(timespec="minutes")
    return value
