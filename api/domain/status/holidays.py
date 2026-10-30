"""Statutory holidays of a German state as suggestions in `special_days` (T-9.5).

A holiday says nothing about whether the restaurant opens: many do. So a
holiday is written as `suggested` and counts for nothing (`hours.load_hours`
reads `set` rows only) until the team answers on the tablet: like Sunday,
closed, or the weekday's hours as always (`dismissed`, never suggested again).
The code never decides the hours (CLAUDE.md §2 rule 1).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.models import AuditLog, SpecialDay
from api.models.tenants import HOLIDAY_REGIONS

ACTOR_TABLET = "gui:tablet"
ACTION_ANSWERED = "special_day.answered"
SET, SUGGESTED, DISMISSED = "set", "suggested", "dismissed"
# What a suggestion proposes: most restaurants keep Sunday hours on a holiday
# (gui/mockups/admin-desktop.html, "Wie Sonntag behandeln?").
SUNDAY = 6
# How far ahead a holiday is suggested: early enough for the team to answer,
# few cards at once on the tablet.
SUGGEST_DAYS = 60
# The team's answers: the suggested weekday's hours, closed, or the date's own.
LIKE, CLOSED, OPEN = "like", "closed", "open"
ANSWERS = (LIKE, CLOSED, OPEN)

_ALL = frozenset(HOLIDAY_REGIONS)
_Rule = Callable[[int, date], date]


def easter_sunday(year: int) -> date:
    """Easter Sunday of the Gregorian calendar (the anonymous algorithm of
    Meeus, Jones and Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday) // 451
    month, day = divmod(h + weekday - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _fixed(month: int, day: int) -> _Rule:
    return lambda year, _easter: date(year, month, day)


def _after_easter(days: int) -> _Rule:
    return lambda _year, easter: easter + timedelta(days=days)


def _repentance_day(year: int, _easter: date) -> date:
    """Buß- und Bettag: the last Wednesday before 23 November."""
    last = date(year, 11, 22)
    return last - timedelta(days=(last.weekday() - 2) % 7)


# The statewide statutory holidays as the law stands in 2026. Left out: days kept
# in parts of a state only (Fronleichnam in parts of SN and TH, Mariä
# Himmelfahrt in BY, the Augsburger Friedensfest) and the Sundays of BB (Ostern,
# Pfingsten), which keep Sunday hours anyway.
_RULES: tuple[tuple[str, frozenset[str], _Rule], ...] = (
    ("Neujahr", _ALL, _fixed(1, 1)),
    ("Heilige Drei Könige", frozenset({"BW", "BY", "ST"}), _fixed(1, 6)),
    ("Internationaler Frauentag", frozenset({"BE", "MV"}), _fixed(3, 8)),
    ("Karfreitag", _ALL, _after_easter(-2)),
    ("Ostermontag", _ALL, _after_easter(1)),
    ("Tag der Arbeit", _ALL, _fixed(5, 1)),
    ("Christi Himmelfahrt", _ALL, _after_easter(39)),
    ("Pfingstmontag", _ALL, _after_easter(50)),
    (
        "Fronleichnam",
        frozenset({"BW", "BY", "HE", "NW", "RP", "SL"}),
        _after_easter(60),
    ),
    ("Mariä Himmelfahrt", frozenset({"SL"}), _fixed(8, 15)),
    ("Weltkindertag", frozenset({"TH"}), _fixed(9, 20)),
    ("Tag der Deutschen Einheit", _ALL, _fixed(10, 3)),
    (
        "Reformationstag",
        frozenset({"BB", "HB", "HH", "MV", "NI", "SH", "SN", "ST", "TH"}),
        _fixed(10, 31),
    ),
    ("Allerheiligen", frozenset({"BW", "BY", "NW", "RP", "SL"}), _fixed(11, 1)),
    ("Buß- und Bettag", frozenset({"SN"}), _repentance_day),
    ("1. Weihnachtstag", _ALL, _fixed(12, 25)),
    ("2. Weihnachtstag", _ALL, _fixed(12, 26)),
)


def holidays(region: str, year: int) -> list[tuple[date, str]]:
    """The statewide statutory holidays of `region` in `year`, by date."""
    if region not in _ALL:
        raise InvalidInput(f"Unknown holiday region {region!r}")
    easter = easter_sunday(year)
    return sorted(
        (rule(year, easter), name)
        for name, regions, rule in _RULES
        if region in regions
    )


def suggest(session: Session, tenant_id: uuid.UUID, region: str, today: date) -> int:
    """Suggest the holidays of the next `SUGGEST_DAYS` days that have no row in
    `special_days` yet, whatever its status: a day the team already set or
    dismissed is its answer. Flushes, does not commit; returns how many rows
    it wrote."""
    horizon = today + timedelta(days=SUGGEST_DAYS)
    due = {
        day: name
        for year in {today.year, horizon.year}
        for day, name in holidays(region, year)
        if today <= day <= horizon
    }
    if not due:
        return 0
    taken = set(
        session.scalars(
            select(SpecialDay.date).where(
                SpecialDay.tenant_id == tenant_id, SpecialDay.date.in_(list(due))
            )
        )
    )
    rows = [
        {
            "tenant_id": tenant_id,
            "date": day,
            "note": name,
            "status": SUGGESTED,
            "like_weekday": SUNDAY,
        }
        for day, name in sorted(due.items())
        if day not in taken
    ]
    if not rows:
        return 0
    # A second pass at the same moment writes nothing (`uq_special_days_suggestion`).
    written = session.execute(
        insert(SpecialDay)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "date"],
            index_where=SpecialDay.status != SET,
        )
        .returning(SpecialDay.id)
    ).all()
    return len(written)


@dataclass(frozen=True)
class Suggestion:
    id: uuid.UUID
    date: date
    name: str
    like_weekday: int


def open_suggestions(
    session: Session, tenant_id: uuid.UUID, today: date
) -> list[Suggestion]:
    """The suggestions the team has not answered, from today on, by date."""
    rows = session.scalars(
        select(SpecialDay)
        .where(
            SpecialDay.tenant_id == tenant_id,
            SpecialDay.status == SUGGESTED,
            SpecialDay.date >= today,
        )
        .order_by(SpecialDay.date)
    ).all()
    return [
        Suggestion(row.id, row.date, row.note or "", row.like_weekday or SUNDAY)
        for row in rows
    ]


def answer(
    session: Session,
    tenant_id: uuid.UUID,
    special_day_id: uuid.UUID,
    choice: str,
    actor: str = ACTOR_TABLET,
) -> SpecialDay:
    """The team's answer to a suggestion. Tapped twice or on two tablets: the
    first answer holds and one entry is written. Commits."""
    if choice not in ANSWERS:
        raise InvalidInput(f"Unknown answer {choice!r} to a holiday suggestion")
    day = session.scalars(
        select(SpecialDay)
        .where(SpecialDay.id == special_day_id, SpecialDay.tenant_id == tenant_id)
        .with_for_update()
    ).first()
    if day is None:
        raise NotFound(f"Special day {special_day_id} not found")
    if day.status == SUGGESTED:
        if choice == OPEN:
            day.status = DISMISSED
        else:
            day.status = SET
            if choice == CLOSED:
                day.closed, day.like_weekday = True, None
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor,
                action=ACTION_ANSWERED,
                entity="special_day",
                entity_id=day.id,
                payload={
                    "date": day.date.isoformat(),
                    "note": day.note,
                    "answer": choice,
                    "like_weekday": day.like_weekday,
                },
            )
        )
    # Also without a change: the commit releases the lock.
    session.commit()
    return day
