"""Probe C2: the #743 card of a guest who hangs up on the outage sentence,
after a booking of the same call stands: does the card say so (#347, #655)?"""

from sqlalchemy import select

from api.models import Callback
from api.telephony.handler import SAY_OUTAGE
from api.tests.test_telephony import (  # noqa: F401 - fixtures
    BrokenLLM,
    FakeTelephony,
    handler_for,
    session,
    tenant,
    the_call,
)

BOOKED = "Ihr Tisch fuer 4 Personen am 20.11. um 19 Uhr ist reserviert."


def test_probe_outage_hangup_card_names_the_booking(session, tenant):  # noqa: F811
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, llm_factory=lambda s: BrokenLLM())
    port.open("s-1", caller_id="+497215551234")
    handler.on_call_started("s-1")
    live = handler._live["s-1"]
    # A booking of this call stands (`confirm` went through earlier).
    live.state.stage = "confirmed"
    live.state.booking_say = BOOKED
    handler.on_user_turn("s-1", "Und noch eine Frage")
    assert port.says("s-1")[-1] == SAY_OUTAGE

    handler.on_call_ended("s-1", caller_hung_up=True)

    [card] = session.scalars(select(Callback)).all()
    print(card.summary, card.reason)
    assert "gebucht" in card.summary or BOOKED in card.summary, card.summary
