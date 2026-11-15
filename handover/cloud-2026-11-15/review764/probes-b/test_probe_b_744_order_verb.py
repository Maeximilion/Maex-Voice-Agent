"""Probe (review #764, area B, PR #744): the order's own wish verb, within four
words of "beschwer"/"reklam", is swallowed as the complaint's wish
(`_WANTS_TO_COMPLAIN` starts its match on it); the remainder holds no wish
word, `only_a_complaint` is True, a remedy is offered and a "Ja" ends the call
with the order never taken (#738 class, not covered by the gap fix)."""

import pytest

from api.agent.llm import LLMTurn, ToolCall
from api.tests.test_agent_complaint_rules import (  # noqa: F401
    GRANTED,
    OFFER,
    SAY_ESCALATION,
    booked,
    loop,
    rule,
    session,
    state,
    tenant_id,
)

ORDER_IN_FRONT = [
    # trigger word "beschweren" / "reklamation": the core path
    "Ich möchte die 23, beschweren muss ich mich aber auch, die Pizza war kalt.",
    "Ich nehme die 23, aber beschweren muss ich mich auch, die Pizza war kalt.",
    "Ich hätte gern zwei Pizzen, außerdem eine Reklamation, die Pizza war kalt.",
]


@pytest.mark.parametrize("said", ORDER_IN_FRONT)
def test_an_order_in_front_of_the_complaint_goes_to_the_team(
    session, tenant_id, state, said
):
    rule(session, tenant_id)

    result = loop(session).run_turn(state, said)

    assert result.say == [SAY_ESCALATION], result.say
    assert state.remedy_offer is None


@pytest.mark.parametrize(
    "said",
    [
        "Ich möchte die 23, außerdem beschwere ich mich, die Pizza war kalt.",
        "Ich bestelle die 23, außerdem reklamiere ich die Pizza, die war kalt.",
    ],
)
def test_the_models_complaint_handover_keeps_the_order_too(
    session, tenant_id, state, said
):
    rule(session, tenant_id)
    call = ToolCall(name="transfer_to_team", args={"reason": "complaint"})

    result = loop(session, [LLMTurn(tool_call=call)] * 3).run_turn(state, said)

    assert result.say != [OFFER], "remedy offered, the order would be lost"


def test_the_yes_then_ends_the_call_with_the_order_never_taken(
    session, tenant_id, state
):
    rule(session, tenant_id)
    agent = loop(session)
    first = agent.run_turn(state, ORDER_IN_FRONT[0])
    assert first.say == [OFFER]
    second = agent.run_turn(state, "Ja.")
    assert second.say == [GRANTED] and second.ended and booked(session)
    raise AssertionError("call ended with the remedy, the 23 was never ordered")
