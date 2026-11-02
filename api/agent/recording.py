"""Consent to the recording of a call (legal check L2, L11, U1, T-11.3).

The code asks and the code decides, never the model, which hears nothing of this
(`ConversationState.to_prompt_json`). Opt-out is not enough: the recording starts
only after an explicit "ja" (`consent.recording_answer`); a "nein", silence or
anything else leaves the call going on without one. Nothing here may cost a call
(CLAUDE.md §2 rule 5): when the consent log or the row cannot be written, nothing
is recorded and the call goes on.

`state.recording` says where a call stands, and the phone line follows it
(`telephony/handler.py`): None, nothing was asked; "asked", the answer is waited
for; "on", the caller said yes and the row is written; "off", no recording;
"withdrawn", it was stopped and its file goes.
"""

import uuid
from contextlib import suppress
from datetime import datetime

from sqlalchemy.orm import Session

from api.agent.state import ConversationState
from api.config import settings
from api.core.logging import get_logger
from api.domain.calls.consent import Answer, record_consent
from api.domain.calls.recordings import (
    call_with_running_recording,
    open_recording,
    withdraw_recording,
)

# U1 of the legal check, word for word, with the period the deletion job keeps.
RECORDING_QUESTION = (
    "Zur Verbesserung unseres Assistenten würden wir dieses Gespräch gern "
    "aufzeichnen und {days} Tage speichern. Sind Sie damit einverstanden? Bitte "
    "sagen Sie Ja oder Nein."
)
# The rest of the greeting (`telephony/handler.py`): the question stands in its
# place, and the sentence the guest answers is the last one of the first turn.
SAY_WHAT_CAN_I_DO = "Was kann ich für Sie tun?"
# L2 wants the way to stop announced, and U1 leaves it out: said behind the yes.
# Draft wording, as in the privacy notice (docs/vorlagen/datenschutzhinweis.md).
SAY_HOW_TO_STOP = (
    "Sie können die Aufnahme jederzeit mit den Worten Aufnahme stoppen beenden."
)
SAY_RECORDING_STOPPED = "Die Aufnahme ist gestoppt und gelöscht."
# What the consent log holds for a withdrawal: no question was asked, these are
# the caller's words, and the answer is "no".
RECORDING_WITHDRAWN = "Aufnahme stoppen"

logger = get_logger(__name__)


def question() -> str:
    return RECORDING_QUESTION.format(days=settings.retention_recording_days)


def settle(
    session: Session,
    state: ConversationState,
    answer: Answer,
    now: datetime | None = None,
) -> None:
    """The answer to the question, or "unclear" for a silent line. The proof
    stands first; then the row, which every recorded file needs."""
    state.recording = "off"
    try:
        record_consent(session, state.tenant_id, state.call_id, question(), answer, now)
        if answer == "yes":
            state.recording_file = open_recording(
                session, state.tenant_id, state.call_id, now
            )
            state.recording = "on"
    except Exception as exc:  # noqa: BLE001 - never the call (rule 5)
        _carried_on(session, "the recording consent", exc)


def withdraw(
    session: Session, state: ConversationState, now: datetime | None = None
) -> None:
    """The caller stopped it. Whatever fails here, the file goes: the state
    says so, and the phone line tells the session."""
    state.recording = "withdrawn"
    try:
        _withdrawn(session, state.tenant_id, state.call_id, now)
    except Exception as exc:  # noqa: BLE001 - never the call (rule 5)
        _carried_on(session, "the withdrawal", exc)


def withdrawn_on_the_team_leg(
    session: Session,
    tenant_id: uuid.UUID,
    session_id: str,
    now: datetime | None = None,
) -> bool:
    """The team stopped a recorded team leg with the key of their telephone
    (`telephony/router.py`). True when a running recording was withdrawn."""
    call_id = call_with_running_recording(session, tenant_id, session_id)
    if call_id is None:
        return False
    _withdrawn(session, tenant_id, call_id, now)
    return True


def _withdrawn(
    session: Session,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    now: datetime | None,
) -> None:
    record_consent(session, tenant_id, call_id, RECORDING_WITHDRAWN, "no", now)
    withdraw_recording(session, tenant_id, call_id, now)


def _carried_on(session: Session, what: str, exc: Exception) -> None:
    """A failed statement leaves the transaction aborted. The type only: a
    message can carry row values."""
    logger.error(
        "the call goes on without a recording",
        extra={"extra": {"what": what, "error": type(exc).__name__}},
    )
    with suppress(Exception):
        session.rollback()
