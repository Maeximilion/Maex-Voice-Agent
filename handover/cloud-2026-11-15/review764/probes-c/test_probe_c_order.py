"""Probe C1: on the own voice layer, is the team number read in `_fail`
after the outage sentence has gone out (handler.py comment, docs/20 #710)?"""

from api.telephony import handler as handler_module
from api.telephony.adapters.fake_speech import FakeEar, FakeLine, FakeMouth
from api.telephony.handler import SAY_OUTAGE
from api.telephony.session import CallSession, Lines
from api.telephony.speech import Heard
from api.tests.test_telephony import (  # noqa: F401 - fixtures
    TEAM_PHONE,
    BrokenLLM,
    handler_for,
    session,
    tenant,
)
from api.tests.test_telephony_session import Clock


def test_probe_outage_sentence_plays_before_the_team_number_read(
    session, tenant, monkeypatch  # noqa: F811
):
    clock, line, lines = Clock(), FakeLine(), Lines()
    ear = FakeEar()
    handler = handler_for(session, tenant, lines, llm_factory=lambda s: BrokenLLM())
    s = CallSession(
        "s-1",
        line=line,
        ear=ear,
        mouth=FakeMouth(),
        events=handler,
        caller_id="+497215551234",
        clock=clock,
        silence_seconds=60.0,
    )
    lines.add(s)
    real = handler_module.current_team_phone

    def recording_read(*a, **k):
        line.actions.append(("team_number_read",))
        return real(*a, **k)

    s.start()
    s.drain()
    s.played(line.mark)
    s.drain()
    monkeypatch.setattr(handler_module, "current_team_phone", recording_read)
    clock.now += 1.0
    ear.hear(Heard("Einen Tisch fuer vier bitte", ended_at=clock.now))
    s.drain()
    kinds = [a[0] if a[0] != "play" else ("play", a[1]) for a in line.actions]
    print(line.actions)
    read_at = kinds.index("team_number_read")
    outage_at = kinds.index(("play", SAY_OUTAGE))
    # The comment claims: read after the sentence has gone out.
    assert outage_at < read_at, line.actions
