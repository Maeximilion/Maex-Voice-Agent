"""The notice a table needs, per weekday, set on the tablet (T-3.13, #499).

On a busy evening a guest cannot reserve ten minutes ahead: inside the notice
`check_slot` books nothing and names the seats by the bookings. It lives on
`capacity`, per weekday like the seats, so a day "wie Sonntag" takes Sunday's
notice with Sunday's seats. A weekday without seats has none: it is closed.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput
from api.domain.status.config import ACTOR_TABLET
from api.models import AuditLog, Capacity

ACTION_NOTICE = "capacity.notice_changed"
# A day: the longest the restaurant is expected to want (assumption, 02.11.2026).
MAX_NOTICE_MINUTES = 1440


def weekday_notices(session: Session, tenant_id: uuid.UUID) -> dict[int, int | None]:
    """Monday is 0. None for a weekday without seats."""
    rows = session.execute(
        select(Capacity.weekday, func.max(Capacity.notice_minutes))
        .where(Capacity.tenant_id == tenant_id)
        .group_by(Capacity.weekday)
    ).all()
    found = {weekday: minutes for weekday, minutes in rows}
    return {weekday: found.get(weekday) for weekday in range(7)}


def set_notices(
    session: Session,
    tenant_id: uuid.UUID,
    minutes_by_weekday: dict[int, int],
    actor: str = ACTOR_TABLET,
) -> None:
    """All or nothing: one value out of bounds changes no weekday."""
    if not all(0 <= m <= MAX_NOTICE_MINUTES for m in minutes_by_weekday.values()):
        raise InvalidInput(
            "Vorbestellzeit ausserhalb der Grenzen",
            say=f"Die Vorbestellzeit geht von 0 bis {MAX_NOTICE_MINUTES} Minuten.",
        )
    rows = session.scalars(
        select(Capacity).where(Capacity.tenant_id == tenant_id).with_for_update()
    ).all()
    for weekday, minutes in minutes_by_weekday.items():
        windows = [row for row in rows if row.weekday == weekday]
        if not windows:
            raise InvalidInput(f"Wochentag {weekday} hat keine Plaetze")
        before = max(row.notice_minutes for row in windows)
        if minutes != before:
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    actor=actor,
                    action=ACTION_NOTICE,
                    entity="capacity",
                    entity_id=tenant_id,
                    payload={"weekday": weekday, "from": before, "to": minutes},
                )
            )
        for row in windows:
            row.notice_minutes = minutes
    session.commit()
