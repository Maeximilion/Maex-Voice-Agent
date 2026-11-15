"""Probe area A, #748: absence forms of the allergen claim guard that still
reach the guest. Each sentence denies a named allergen in a dish."""

import uuid

import pytest

from api.agent import guards
from api.agent.llm import LLMTurn
from api.agent.state import ConversationState
from api.tests.test_agent_allergens_beyond import (  # noqa: F401
    CALLER,
    known_tenant,
    run,
    session,
    state_for,
)

MISSED = [
    "In der 13 ist keine Erdnuss.",
    "In der Pho Bo ist keine Milch.",
    "Milch ist da keine drin.",
    "Soja ist keins drin.",
    "Die 13 enthält Sesam nicht.",
    "Nein, Sesam hat die 13 nicht.",
    "Die 13 enthält weder Nüsse noch Sesam.",
    "Da kommen keine Nüsse rein.",
    "Nüsse? Nein, die sind nicht drin.",
    "Da ist nix von Sesam drin.",
]


@pytest.mark.parametrize("say", MISSED)
def test_the_guard_reads_the_absence(say):
    state = ConversationState(
        call_id=uuid.uuid4(), tenant_id=uuid.uuid4(), caller_phone=CALLER
    )
    assert guards.allergen_claim(state, say) is not None


@pytest.mark.parametrize(
    ("said", "answer"),
    [
        ("Ist in der Pho Bo Milch?", "Nein, in der Pho Bo ist keine Milch."),
        ("Hat die 13 Nüsse?", "Nein, Nüsse hat die 13 nicht."),
        ("Is da Sesam drin in der 13?", "Nee, Sesam ist da keiner drin."),
    ],
)
def test_the_guest_does_not_hear_the_models_absence(session, known_tenant, said, answer):
    # No lookup at all: the guest names an allergen but no allergy word, so
    # `unread_allergy_question` does not fire either.
    state = state_for(session, known_tenant, phone=CALLER)
    result, _ = run(session, state, said, LLMTurn(say=answer))
    assert result.say != [answer]
