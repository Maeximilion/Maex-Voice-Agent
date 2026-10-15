"""Outcome and intent of a call from the conversation state (docs/03 §calls).

Shared by every entrance that closes a call: the text telephone (`sim/session.py`)
and the phone line (`telephony/handler.py`). The outcome follows the state, not
the impression of the model.
"""

import logging
import uuid
from dataclasses import asdict
from typing import cast, get_args

from api.agent.llm import Usage, cost_cents
from api.agent.state import ConversationState
from api.core.logging import log
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


def call_usage(llm: object) -> Usage:
    """What the model of a call used. Only a real model counts tokens
    (`ChatCompletionsLLM.usage`); a stand-in has none, and its call gets
    neither a model nor a cost."""
    usage: Usage = getattr(llm, "usage", None) or Usage()
    if not usage.requests:
        # The caller hung up before the model was asked once: the call log
        # must not name a model that never ran (Codex PR #211, P2).
        return Usage()
    return usage


def log_usage(logger: logging.Logger, call_id: uuid.UUID, usage: Usage) -> None:
    """The line "model usage" of a call, with its exact tokens; the call log
    keeps only the cost in whole cents (`cost_cents`). Nothing for a stand-in."""
    if usage.model is not None:
        log(
            logger,
            logging.INFO,
            "model usage",
            call_id=str(call_id),
            **asdict(usage),
            cost_cents=cost_cents(usage),
        )
