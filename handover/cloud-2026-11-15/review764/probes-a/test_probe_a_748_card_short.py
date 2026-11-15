"""Probe area A, #748 vs #788: the guest's allergy ingredient on a callback
card. #748 matched the noted ingredient (stems of two letters and more);
#788's `_names_health` skips every word under five letters, so a short
non-LMIV ingredient (Kiwi, Mais, Zimt, Reis, Mohn, Hefe, Lamm) said by the
guest is written on the card (L13)."""

import uuid

import pytest

from api.agent.llm import LLMTurn, ToolCall
from api.agent.loop import _names_health
from api.agent.state import ConversationState
from api.tests.test_agent_allergens_beyond import (  # noqa: F401
    CALLER,
    CLOSED,
    known_tenant,
    run,
    session,
    state_for,
)


@pytest.mark.parametrize(
    ("said", "card"),
    [
        ("Ich bin allergisch gegen Kiwi.", "Rückruf wegen Kiwi im Obstsalat"),
        ("Ich vertrage keinen Mais.", "Frage zu Mais in der Suppe"),
        ("Ich bin allergisch gegen Zimt.", "Rückruf: Zimt im Dessert?"),
        ("Ich habe eine Allergie gegen Mohn.", "Rückruf: Mohn im Brötchen"),
        ("Meine Frau verträgt kein Lamm.", "Rückruf: Lamm im Curry"),
        ("Mein Sohn hat Zöliakie.", "Rückruf: Gluten in der 13"),
    ],
)
def test_the_card_does_not_carry_the_guests_ingredient(said, card):
    state = ConversationState(
        call_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        caller_phone=CALLER,
        allergy_said=[said],
    )
    assert _names_health(state, card)


def test_the_guests_sentence_on_a_handover_card(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ich bin allergisch gegen Kiwi.",
        LLMTurn(say="Notiert."),
        now=CLOSED,
    )
    run(
        session,
        state,
        "Ich möchte bitte mit jemandem sprechen, ist im Obstsalat Kiwi?",
        LLMTurn(
            tool_call=ToolCall("transfer_to_team", {"reason": "human_requested"})
        ),
        now=CLOSED,
    )
    from sqlalchemy import select

    from api.models import Callback

    cards = [c.summary for c in session.scalars(select(Callback)).all()]
    cards.append(state.handoff_summary or "")
    print(cards)
    assert any(cards)
    assert not any("Kiwi" in c for c in cards if c)
