"""Cross-cutting probes (#757 with #754). Red = finding confirmed."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import ServiceConfig, SpecialDay
from api.tests.test_gui_staff_changes import (  # noqa: F401
    HX,
    _cards,
    _seen,
    _table,
    _today,
    client,
    dialable,
    engine,
    fixed_clock,
)
from datetime import time


def test_owner_keep_on_another_device_removes_the_list_before_it_is_called(client, engine):  # noqa: F811
    """Staff close a date at the tablet (held at once): the card lists the
    guest to call. The owner taps 'keep' on the PC: the card closes, the SSE
    token moves, and the tablet's fragment no longer names the guest. Nothing
    else lists the tables a closure stranded."""
    later = _today() + timedelta(days=7)
    _table(engine, later, "Familie Bauer")
    client.post(
        "/gui/tageszeiten",
        data={"datum": later.isoformat(), "art": "geschlossen"},
        headers=HX,
    )
    assert "Familie Bauer" in client.get("/gui/fragments/pruefen", headers=HX).text
    (card,) = _cards(engine)
    client.post(f"/gui/pruefen/{card.id}/uebernehmen", data=_seen(card), headers=HX)
    after = client.get("/gui/fragments/pruefen", headers=HX).text
    assert "Familie Bauer" in after, "list gone once the owner kept the closure"


def test_waiting_shortening_kept_by_the_owner_never_lists_its_tables(client, engine):  # noqa: F811
    """A 'wie Sonntag' date (#754: its times wait for the owner even when
    shorter). Staff set 17:00-22:00, a 12:00 table is outside. The waiting
    card lists nothing (holding equals before); the owner keeps it, the hours
    are written and the card closes: the 12:00 guest is never listed."""
    later = _today() + timedelta(days=7)
    with Session(engine) as session:
        session.add(
            SpecialDay(
                tenant_id=session.scalar(select(ServiceConfig.tenant_id)),
                date=later,
                like_weekday=6,
                status="set",
            )
        )
        session.commit()
    _table(engine, later, "Aydin", at=time(12))
    client.post(
        "/gui/tageszeiten",
        data={
            "datum": later.isoformat(),
            "art": "zeiten",
            "von": "17:00",
            "bis": "22:00",
        },
        headers=HX,
    )
    (card,) = _cards(engine)
    assert not card.held, "precondition: the change waits for the owner"
    waiting = client.get("/gui/fragments/pruefen", headers=HX).text
    client.post(f"/gui/pruefen/{card.id}/uebernehmen", data=_seen(card), headers=HX)
    with Session(engine) as session:
        row = session.scalars(select(SpecialDay).where(SpecialDay.date == later)).one()
        assert row.opens_at == time(17)  # the shortening holds now
    kept = client.get("/gui/fragments/pruefen", headers=HX).text
    assert "Aydin" in waiting or "Aydin" in kept, "12:00 guest never listed"
