"""Probe area A, #751 vs #788: a second person's allergy behind the caller's.
#751 noted it in the core; #788 leaves the note to the model and the net
(`guards.unnoted_allergy`) passes any note that matches HEALTH_NOTE, so a
note naming only the caller's allergy is stored and the kitchen never hears
of the second person's."""

import pytest

from api.agent.llm import LLMTurn
from api.tests.test_agent_allergens_beyond import CALLER, run, state_for
from api.tests.test_agent_allergy_notes import (  # noqa: F401
    draft,
    known_tenant,
    search,
    session,
    stored,
)


@pytest.mark.parametrize(
    ("said", "note", "missing"),
    [
        (
            "Einmal die Pho Bo auf Müller. Ich bin allergisch gegen Erdnüsse, "
            "meine Frau gegen Sesam.",
            "WICHTIG !!! Allergie: Erdnüsse",
            "Sesam",
        ),
        (
            "Die Pho Bo auf Müller bitte, ich hab ne Nussallergie und mein Sohn "
            "verträgt keine Milch.",
            "WICHTIG !!! Nussallergie",
            "Milch",
        ),
    ],
)
def test_the_second_persons_allergy_reaches_the_kitchen(
    session, known_tenant, said, note, missing
):
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        said,
        search(),
        draft(session, known_tenant, order_note=note),
        LLMTurn(say="Passt das so?"),
    )
    order_note, lines = stored(session)
    assert missing in " ".join(n for n in (order_note, *lines) if n)


def test_an_allergy_told_in_a_later_turn_reaches_the_kitchen(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ich habe eine Erdnussallergie.",
        LLMTurn(say="Notiert. Was darf es sein?"),
    )
    run(
        session,
        state,
        "Die Pho Bo auf Müller. Und meine Tochter verträgt kein Sesam.",
        search(),
        draft(session, known_tenant, order_note="WICHTIG !!! Erdnussallergie"),
        LLMTurn(say="Passt das so?"),
    )
    order_note, lines = stored(session)
    assert "Sesam" in " ".join(n for n in (order_note, *lines) if n)
