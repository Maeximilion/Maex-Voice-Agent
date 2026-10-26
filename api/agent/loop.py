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
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, get_args

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.agent import allergies, escalation, guards
from api.agent.consent import ALLERGY_QUESTION, consent_answer, is_yes
from api.agent.dispatch import ToolResult, dispatch, in_local_time, log_refused
from api.agent.guest_details import name_said
from api.agent.ladder import UnderstandingLadder
from api.agent.llm import LLMClient, LLMError, LLMTurn, ToolCall
from api.agent.state import (
    ConversationState,
    apply_state_patch,
    apply_tool_result,
    draft_replaced,
    note_guest_details,
    note_intent,
)
from api.config import settings
from api.core.envelope import SAY_ON_FAILURE
from api.core.logging import get_logger, log
from api.core.time import to_local, utcnow
from api.domain.calls import record_consent
from api.domain.menu.wishes import mentions_allergy
from api.domain.reservations.spoken import WEEKDAYS
from api.models import Order, Tenant
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
# The last resort of `_handoff`: the team is not reachable and there is no number
# for a callback. Every sentence above promises the team; here nobody takes
# over, so the guest hears exactly that (Codex PR #208, P1). Draft wording,
# checked with the announcement texts of docs/05 §6.
SAY_NOBODY_REACHABLE = (
    "Ich kann Ihnen gerade leider nicht weiterhelfen und erreiche im Restaurant "
    "niemanden. Bitte rufen Sie später noch einmal an."
)

ENDED_STAGES = frozenset({"transferred", "ended"})
# Name under which a refused order of the model (`LLMTurn.cart`) goes back to
# it and into `calls.tool_calls`. No tool: nothing is dispatched under it.
CART = "cart"
# The same for a sentence the core did not let through (`guards.false_booking`).
SAY = "say"

_CALLBACK_REASONS = frozenset(get_args(CallbackReason))
_TRANSFER_REASONS = frozenset(get_args(TransferReason))
# Fallback-Grund für Rückrufe, wenn der eigentliche Grund (z. B. "cancellation")
# in CallbackReason gar nicht existiert (docs/03: Storno ist nur ein
# Transfer-Grund, kein Rückruf-Grund — siehe api/schemas/transfer.py).
_CALLBACK_REASON_FALLBACK: CallbackReason = "human_requested"

_HANDOFF_SUMMARIES = {
    "complaint": "Kunde hat sich beschwert.",
    "human_requested": "Kunde wollte mit einem Menschen sprechen.",
    "cancellation": "Kunde wollte etwas stornieren.",
    "not_understood": "Anliegen konnte automatisch nicht geklärt werden.",
}
_DEFAULT_HANDOFF_SUMMARY = "Anruf konnte nicht automatisch abgeschlossen werden."
# How the card of a table the team decides on begins (`team_callback`, #252).
SUMMARY_TEAM_TABLE = "Reservierungswunsch, vom Team zu bestätigen:"
# A table for the team ends the call while an order is still open: the order
# is not booked, and both the card and the guest say so (#207). Draft wording,
# checked with the announcement texts (docs/05 §6).
SUMMARY_OPEN_ORDER = "Offene Bestellung, nicht aufgegeben:"
SAY_ORDER_NOT_PLACED = "Ihre Bestellung ist damit noch nicht aufgegeben."
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


@dataclass
class TurnResult:
    state: ConversationState
    say: list[str] = field(default_factory=list)
    ended: bool = False


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
        # Timezone of the call's tenant, read once (`_local_now`).
        self._zone: str | None = None

    def run_turn(self, state: ConversationState, user_text: str) -> TurnResult:
        # The guest has stopped speaking: from here on they wait.
        turn_started = None if self._turn_deadline is None else self._clock()
        # First of all: a name or number the model writes in this turn may
        # come from this very sentence (agent/guest_details.py, rule 2).
        note_guest_details(state, user_text)
        # The same for an allergy: the core writes it onto the draft from what
        # the guest said, never from a model's note (T-11.1).
        noted = list(state.allergies)
        allergies.note_allergies(self._session, state, user_text)
        if state.allergies != noted:
            # The draft read out before does not carry what the guest just
            # said: a yes must not confirm it (Codex PR #368, P1).
            draft_replaced(
                state, "create_reservation" if state.reservation_id else "draft_order"
            )
        # The answer to a question of the core about the allergy, the consent
        # logged also when this sentence goes to the team (L13): the draft
        # that waited for it goes first, before the model is asked.
        held = self._held_answered(state, user_text)
        # Read past the question whether a person is on the line: "Spreche ich
        # mit einem Menschen?" holds the words of the wish to talk to one and
        # only asks, what else the sentence says still counts.
        rest = escalation.without_human_question(user_text)
        asks = rest is not None
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
        if reason is not None:
            escalated = self._handoff(
                state,
                SAY_ESCALATION,
                reason=reason,
                # A "Ja" tells the team nothing the reason does not say.
                detail=None if answered_offer else user_text,
            )
        elif asks and self._clock() - self._started > self._max_call_seconds:
            # The answer keeps no call open past its limit: asked again and
            # again, it would (Codex PR #372).
            escalated = self._handoff(state, SAY_TIMEOUT, detail=user_text)
        if escalated is not None:
            if asks:
                escalated.say.insert(0, SAY_NOT_A_PERSON)
            return escalated
        if held is None and rest is not None and not escalation.asks_more(rest):
            guards.note_said(state, SAY_HUMAN_ANSWER)
            return TurnResult(state=state, say=[SAY_HUMAN_ANSWER])
        if state.held_call is not None:
            # The question was not what the guest heard last: put it again,
            # unless the call is past its limit (Codex PR #368).
            if self._clock() - self._started > self._max_call_seconds:
                return self._handoff(state, SAY_TIMEOUT, detail=user_text)
            if (question := allergies.question(state)) is not None:
                guards.note_said(state, question)
                say = [question]
                if asks:
                    say.insert(0, SAY_NOT_A_PERSON)
                return TurnResult(state=state, say=say)
            state.held_call = None
        result = self._converse(state, user_text, turn_started, held)
        if asks:
            # A request next to the question ("Sind Sie ein Roboter? Ich
            # möchte einen Tisch ..."): the truthful sentence first, then the
            # model's answer to the request; the guest has said what they
            # want, so no offer of the team (ultrareview of PR #372).
            result.say.insert(0, SAY_NOT_A_PERSON)
        return result

    def _converse(
        self,
        state: ConversationState,
        user_text: str,
        turn_started: float | None,
        held: ToolCall | None = None,
    ) -> TurnResult:
        """The model's part of a guest turn: model calls and tools until a
        sentence for the guest, a handoff or a limit. `held` is the draft the
        guest just answered a question of the core for (`_held_answered`):
        carried out first, in place of a model call."""
        start = guards.begin_turn(state)
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
        zone = self._local_now(state).tzinfo
        assert zone is not None  # `to_local` gives an aware time
        for _ in range(MAX_TOOL_HOPS):
            if self._clock() - self._started > self._max_call_seconds:
                return self._handoff(state, SAY_TIMEOUT, detail=user_text)
            # The model calls and tools of one guest turn share one deadline:
            # each call alone may be quick and the guest still waits for all
            # of them. Time up: the model is not asked again.
            left = self._left_in_turn(turn_started)
            if left is not None and left <= 0:
                return self._too_slow(state, user_text)

            # The draft that waited for the guest's answer is carried out as
            # the model called it, with its notes as the answer allows.
            from_hold = held is not None
            if held is not None:
                turn, held = LLMTurn(tool_call=held), None
            else:
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
                    return self._handoff(state, SAY_ON_FAILURE, detail=user_text)
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
                return self._handoff(state, SAY_TIMEOUT, detail=user_text)
            # The same for the turn deadline: an answer that took too long is
            # neither spoken nor carried out: the guest has waited long enough
            # and goes to the team. What the model heard is kept, as above.
            left = self._left_in_turn(turn_started)
            if left is not None and left <= 0:
                return self._too_slow(state, user_text)

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

            # Hard rules 2 and 3 are held here, not by the prompt (guards.py).
            refused = guards.refusal(
                state,
                start,
                user_text,
                turn.tool_call,
                cart_sent=turn.cart is not None,
            )
            question = (
                allergies.question(state)
                if turn.tool_call.name in allergies.NOTED
                else None
            )
            if refused is None and question is not None:
                # The core asks what the allergy note still lacks before
                # anything is stored, and the draft waits for the answer
                # (`_held_answered`).
                state.held_call = {
                    "name": turn.tool_call.name,
                    "args": turn.tool_call.args,
                }
                draft_replaced(state, turn.tool_call.name)
                guards.note_said(state, question)
                return TurnResult(state=state, say=[question])
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
                # The guest's allergies onto the draft, as their answer lets
                # them be stored (L13). The state keeps the call as the model
                # wrote it: the cart goes back to the model without them.
                call = allergies.noted_call(state, turn.tool_call)
                result = self._dispatch(state, call.name, call.args)
            apply_tool_result(state, turn.tool_call.name, result, turn.tool_call.args)
            if from_hold and result.ok and (said := guards.unheard_readback(state)):
                # The draft the guest answered the question for: read out by
                # the core, so the guest's next "Ja" is the yes to it. Unless
                # it took the guest past a limit: then to the team, as any
                # slow tool on the next hop would (Codex PR #368).
                if self._clock() - self._started > self._max_call_seconds:
                    return self._handoff(state, SAY_TIMEOUT, detail=user_text)
                if (left := self._left_in_turn(turn_started)) is not None and left <= 0:
                    return self._too_slow(state, user_text)
                guards.note_said(state, said)
                return TurnResult(state=state, say=[said])
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
                    late = self._handoff(state, SAY_TIMEOUT, detail=user_text)
                elif (
                    left := self._left_in_turn(turn_started)
                ) is not None and left <= 0:
                    late = self._too_slow(state, user_text)
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
                    state, turn.tool_call.args, user_text, reported_failures
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
            if turn.tool_call.name == "transfer_to_team" and state.transferred:
                # The model handed the call over itself, and the team takes it:
                # the call ends here, as in `_handoff`. Asked again, a real
                # model called transfer_to_team on its own result until the hop
                # limit, and the call went to the team as stuck (13.10.2026).
                return TurnResult(
                    state=state, say=[result.say or SAY_ESCALATION], ended=True
                )
            called.append(turn.tool_call.name)
            pending_input = _tool_result_as_input(turn.tool_call.name, result, zone)

        return self._handoff(state, SAY_STUCK, detail=user_text)

    def _callback_without_number(
        self,
        state: ConversationState,
        args: dict[str, Any],
        user_text: str,
        reported_failures: set[str],
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
        reason = args.get("reason")
        transfer = self._dispatch(
            state,
            "transfer_to_team",
            # A reason that is no string is no reason: `in` on a list or a
            # dict would raise before dispatch could refuse it (Codex PR #347).
            {
                "reason": reason
                if isinstance(reason, str) and reason in _TRANSFER_REASONS
                else "not_understood"
            },
        )
        apply_tool_result(state, "transfer_to_team", transfer)
        if state.transferred:
            return TurnResult(
                state=state, say=[transfer.say or SAY_ESCALATION], ended=True
            )
        if state.asked_for_phone and "phone" not in reported_failures:
            reported_failures.add("phone")
            self._ladder.record_failure("phone")
            if self._ladder.should_end_call("phone"):
                return self._handoff(
                    state, SAY_NOT_UNDERSTOOD, reason="not_understood", detail=user_text
                )
        return None

    def _held_answered(
        self, state: ConversationState, user_text: str
    ) -> ToolCall | None:
        """The guest's sentence as the answer to the question the core put for
        the held draft (`allergies.question`), when that question ended the
        last turn. The answer to "Wogegen?" is in the state already
        (`allergies.note_allergies`); the one to the consent question is
        judged here and logged per call (L13). Returns the draft, which goes
        out with the notes the state now allows or waits for the next
        question. An unclear answer drops it: the sentence was likely about
        something else, the model reads it, and the draft it makes again
        finds the allergies in the state."""
        if state.held_call is None or not (
            state.last_say == ALLERGY_QUESTION or allergies.asked_which(state.last_say)
        ):
            # Another sentence came between, as "Sind Sie noch da?" after a
            # silence on the phone line: a "Ja" answers that one. The draft
            # stays held and the question is put again (`run_turn`; Codex
            # PR #368).
            return None
        held, state.held_call = state.held_call, None
        if state.last_say == ALLERGY_QUESTION:
            answer = consent_answer(user_text)
            record_consent(
                self._session,
                state.tenant_id,
                state.call_id,
                ALLERGY_QUESTION,
                answer,
                now=self._now,
            )
            state.allergy_consent = answer
            if answer == "unclear":
                return None
        elif not allergies.answered(state, user_text):
            # No ingredient in the answer, or more than one ("Erdnüsse, aber
            # bitte erst übermorgen"): the model reads the sentence.
            return None
        return ToolCall(name=held["name"], args=held["args"])

    def _order_name(self, state: ConversationState) -> str | None:
        """The name the confirmed order runs under, from the database."""
        order = self._session.get(Order, state.order_id) if state.order_id else None
        return order.customer_name if order else None

    def _left_in_turn(self, turn_started: float | None) -> float | None:
        """Seconds the guest still waits for this turn; None without a deadline."""
        if turn_started is None or self._turn_deadline is None:
            return None
        return self._turn_deadline - (self._clock() - turn_started)

    def _too_slow(self, state: ConversationState, user_text: str) -> TurnResult:
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
        return self._handoff(state, SAY_ON_FAILURE, detail=user_text)

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

    def _dispatch(self, state: ConversationState, name: str, args: dict) -> ToolResult:
        return dispatch(
            self._session, state.call_id, state.tenant_id, name, args, now=self._now
        )

    def _table_to_team(
        self, state: ConversationState, args: dict[str, Any], lead: str | None
    ) -> TurnResult:
        """The wish for a table the agent does not book itself goes to the
        team and the call ends: as a callback under the number in the state,
        after `lead`, the sentence of the tool. Without a number there is
        nobody to call back, and a callback can fail: both go the way of
        every handover (`_handoff`), a transfer where the team is reachable.

        ponytail: a caller whose number is withheld is not asked for one, and
        outside the opening hours hears the last sentence of `_handoff`. Asking
        would need the wish to wait across turns, and every rule for when it
        still stands either lost a wish or sent a stale one (Codex PR #280).
        The same open point as for every handover (docs/01_STATUS.md)."""
        wish = self._table_wish(state, args)
        if state.cart:
            head = f"{wish} {SUMMARY_OPEN_ORDER} "
            wish = f"{head}{_order_lines(state, _SUMMARY_MAX - len(head) - 1)}."
        lead_say = [lead, SAY_ORDER_NOT_PLACED if state.cart else None]
        phone = state.slots.get("phone")
        if phone:
            callback = self._dispatch(
                state,
                "create_callback",
                {"phone": phone, "reason": "out_of_scope", "summary": wish},
            )
            apply_tool_result(state, "create_callback", callback)
            if callback.ok:
                said = [s for s in (*lead_say, callback.say) if s]
                return TurnResult(state=state, say=said, ended=True)
        handed = self._handoff(state, SAY_ESCALATION, "out_of_scope", wish)
        handed.say = [s for s in (*lead_say, *handed.say) if s]
        return handed

    def _table_wish(self, state: ConversationState, args: dict[str, Any]) -> str:
        """The wish as the team reads it on the card. Party size and time are
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
        return f"{SUMMARY_TEAM_TABLE} {', '.join(parts)}."

    def _handoff(
        self,
        state: ConversationState,
        fallback_say: str,
        reason: str = "not_understood",
        detail: str | None = None,
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
        PR #102, P2)."""
        if detail and mentions_allergy(detail):
            # The guest's sentence names an allergy, which is health data: it
            # is not written on a card. The caller agreed at most to a kitchen
            # note (L13), and the team asks when it calls back.
            detail = None
        transfer = self._dispatch(state, "transfer_to_team", {"reason": reason})
        apply_tool_result(state, "transfer_to_team", transfer)
        if transfer.ok and transfer.data.get("available"):
            return TurnResult(
                state=state, say=[transfer.say or fallback_say], ended=True
            )

        phone = state.slots.get("phone")
        if phone:
            callback_reason = (
                reason if reason in _CALLBACK_REASONS else _CALLBACK_REASON_FALLBACK
            )
            callback = self._dispatch(
                state,
                "create_callback",
                {
                    "phone": phone,
                    "reason": callback_reason,
                    "summary": _handoff_summary(reason, detail, state.booking_say),
                },
            )
            apply_tool_result(state, "create_callback", callback)
            if callback.ok:
                return TurnResult(
                    state=state, say=[callback.say or fallback_say], ended=True
                )

        state.stage = "ended"
        return TurnResult(state=state, say=[SAY_NOBODY_REACHABLE], ended=True)


def _order_lines(state: ConversationState, room: int) -> str:
    """The open order as the team reads it, with number and name from the
    menu search (CLAUDE.md §2 rule 1), never from the model, and with the
    chosen options and the note of each line: two pizzas of different sizes
    are two lines (Codex PR #300). Longer than `room`: without the names, the
    team reads the dishes from the card; and what still does not fit is
    counted, never dropped in silence. The guest's allergies as the kitchen
    has to know them, "ohne <Zutat>", never with the reason: a card is kept
    for the period of a callback, the allergy only until the night after its
    order (L13; Codex PR #369, P1).
    """
    noted = allergies.noted_call(
        state.model_copy(update={"allergy_consent": None}),
        ToolCall("draft_order", {"items": state.cart}),
    ).args
    full, short = [], []
    for line in noted["items"]:
        seen = state.seen_items.get(line["menu_item_id"], {})
        number, name = seen.get("number"), seen.get("name")
        chosen = [f"{o['group']}: {o['name']}" for o in line.get("options", [])]
        note = line.get("note")
        extra = "; ".join([*chosen, *([note] if note else [])])
        tail = f" ({extra})" if extra else ""
        dish = " ".join(x for x in (number, name) if x) or "Gericht"
        full.append(f"{line['quantity']}x {dish}{tail}")
        short.append(f"{line['quantity']}x {number or name or 'Gericht'}{tail}")
    if noted.get("note"):
        full.append(f"für die ganze Bestellung: {noted['note']}")
        short.append(full[-1])
    if len(text := ", ".join(full)) <= room:
        return text
    for kept in range(len(short), -1, -1):
        rest = len(short) - kept
        parts = short[:kept] + (
            [f"{rest} weitere Position{'en' * (rest > 1)}"] if rest else []
        )
        if len(text := ", ".join(parts)) <= room:
            return text
    return text[: max(room, 0)]


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


def _handoff_summary(reason: str, detail: str | None, booked: str | None = None) -> str:
    base = _HANDOFF_SUMMARIES.get(reason, _DEFAULT_HANDOFF_SUMMARY)
    if detail:
        base = f"{base} Kunde sagte: {detail[:_SUMMARY_DETAIL_MAX]}"
    # A booking of this call stands whatever went wrong after it: the card says
    # so, or the team books a second time (Codex PR #347).
    return f"{base} Bereits gebucht: {booked}" if booked else base


def _tool_result_as_input(name: str, result: ToolResult, zone: tzinfo) -> str:
    """Ergebnis dem Modell als nächster Zug zurückgeben, ohne echten Kundeninput.
    Times in the restaurant's local time (`zone`), like `now` in the state."""
    if result.ok:
        payload = {"tool": name, "ok": True, "data": _local_times(result.data, zone)}
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
