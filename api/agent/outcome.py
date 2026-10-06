"""Outcome and intent of a call from the conversation state (docs/03 §calls).

Shared by every entrance that closes a call: the text telephone (`sim/session.py`)
and the phone line (`telephony/handler.py`). The outcome follows the state, not
the impression of the model.
"""

from typing import cast, get_args

from api.agent.state import ConversationState
from api.schemas.calls import Intent, Outcome

# Everything that was neither confirmed nor handed over nor noted as a callback is
# an abandoned call.
OUTCOME_BY_STAGE: dict[str, Outcome] = {
    "confirmed": "completed",
    "transferred": "transferred",
    "callback": "callback",
}
DEFAULT_OUTCOME: Outcome = "abandoned"
INTENTS = frozenset(get_args(Intent))

# After these stages the conversation is over; talking on would address the caller
# again after the goodbye.
CLOSING_STAGES = frozenset({"confirmed", "transferred", "callback", "ended"})


def call_outcome(state: ConversationState) -> tuple[Outcome, Intent | None]:
    outcome = OUTCOME_BY_STAGE.get(state.stage, DEFAULT_OUTCOME)
    intent = cast(Intent, state.intent) if state.intent in INTENTS else None
    return outcome, intent
