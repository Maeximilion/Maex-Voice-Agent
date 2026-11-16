"""Display and form reading of the tab "Zeiten" (#571, docs/06 §4): German
words, and the typed fields turned into what `domain/status` takes.

Every hour shown comes from the database through `domain/status/weekly.py`
and `staff_changes.py`; this file formats them and reads the forms.
"""

from datetime import date
from datetime import time as dt_time
from typing import Any

from sqlalchemy.orm import Session

from api.core.time import business_day
from api.domain.reservations.spoken import WEEKDAYS
from api.domain.status import staff_changes as staff
from api.domain.status import weekly
from api.gui.figures_view import DASH
from api.gui.figures_view import WEEKDAYS as SHORT
from api.models import Tenant

# The columns of the week, in the order of the mockup.
SERVICES = (("dinein", "Im Haus"), ("pickup", "Abholung"), ("delivery", "Lieferung"))
CLOSED = "geschlossen"
# What a field echoes back at most: "HH:MM" and a little of a typing error.
ECHO = 8


def hhmm(text: str) -> dt_time:
    """HH:MM only: a stored second or offset would undo to another time."""
    if len(text) != 5:
        raise ValueError(text)
    # "1000Z" is five characters and 10:00 with an offset: a time with a zone
    # cannot be compared with the stored ones.
    parsed = dt_time.fromisoformat(text)
    if parsed.tzinfo is not None:
        raise ValueError(text)
    return parsed


def typed_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _cell(spans: list[weekly.Span]) -> str:
    return ", ".join(f"{s.opens:%H:%M}{DASH}{s.closes:%H:%M}" for s in spans) or CLOSED


def week_rows(session: Session, tenant: Tenant) -> list[dict[str, Any]]:
    week = weekly.week(session, tenant.id)
    return [
        {
            "weekday": weekday,
            "name": WEEKDAYS[weekday],
            "cells": [(label, _cell(week[weekday][key])) for key, label in SERVICES],
        }
        for weekday in range(7)
    ]


def day_form(
    session: Session, tenant: Tenant, weekday: int, typed: dict[str, str] | None = None
) -> dict[str, Any]:
    """The form of one weekday: what holds, with one empty row per service to
    add a span, or what was typed and refused (`typed`). The token is always
    the one of the hours in the database now."""
    day = weekly.week(session, tenant.id)[weekday]
    groups = []
    for key, label in SERVICES:
        if typed is None:
            rows = [
                {
                    "von": f"{span.opens:%H:%M}",
                    "bis": f"{span.closes:%H:%M}",
                    "nachts": span.overnight,
                }
                for span in day[key]
            ]
            if len(rows) < weekly.MAX_SPANS:
                rows.append({"von": "", "bis": "", "nachts": False})
        else:
            rows = [
                {
                    "von": typed.get(f"{key}-von-{i}", "")[:ECHO],
                    "bis": typed.get(f"{key}-bis-{i}", "")[:ECHO],
                    "nachts": bool(typed.get(f"{key}-nachts-{i}")),
                }
                for i in _rows(typed, key)
            ]
        groups.append({"key": key, "label": label, "rows": rows})
    return {
        "weekday": weekday,
        "name": WEEKDAYS[weekday],
        "groups": groups,
        "seen": weekly.token(day),
    }


def _rows(form: dict[str, str], key: str) -> range:
    """The rows of one service a form sent, counted from 0 without a gap; one
    more than the domain takes, so a row too many is refused there by name."""
    count = 0
    while count <= weekly.MAX_SPANS and f"{key}-von-{count}" in form:
        count += 1
    return range(count)


def typed_spans(form: dict[str, str]) -> weekly.Day:
    """The spans a weekday's form sent. A row with both fields empty is no
    span; a service the form does not name is left out and keeps its hours.
    Raises ValueError on a field that is no HH:MM."""
    spans: weekly.Day = {}
    for key, _label in SERVICES:
        rows = _rows(form, key)
        if not rows:
            continue
        spans[key] = []
        for i in rows:
            von = form.get(f"{key}-von-{i}", "").strip()
            bis = form.get(f"{key}-bis-{i}", "").strip()
            if not von and not bis:
                continue
            overnight = bool(form.get(f"{key}-nachts-{i}"))
            spans[key].append(weekly.Span(hhmm(von), hhmm(bis), overnight))
    return spans


def echo(form: dict[str, str]) -> dict[str, Any]:
    """What a refused form for one date shows again: only what was typed."""
    return {
        "datum": form.get("datum", "")[:10],
        "von": form.get("von", "")[:ECHO],
        "bis": form.get("bis", "")[:ECHO],
        "nachts": bool(form.get("nachts")),
    }


def _when(day: date) -> str:
    return f"{SHORT[day.weekday()]}, {day:%d.%m.%Y}"


def day_rows(session: Session, tenant: Tenant) -> list[dict[str, Any]]:
    today = business_day(tz_name=tenant.timezone)
    return [
        {
            "day": day.isoformat(),
            "when": _when(day),
            "text": hours.text(),
            "note": hours.note or "",
        }
        for day, hours in staff.coming_days(session, tenant.id, today)
    ]


def guest_rows(session: Session, tenant: Tenant) -> list[dict[str, Any]]:
    """The confirmed tables no dine-in window holds, on the restaurant's clock."""
    return [
        {
            "when": f"{SHORT[b.at.weekday()]}, {b.at:%d.%m.} {b.at:%H:%M} Uhr",
            "name": b.guest_name,
            "party": b.party_size,
            "phone": b.phone,
            "costly": b.costly,
        }
        for b in weekly.tables_outside_hours(session, tenant.id)
    ]


def page(session: Session, tenant: Tenant) -> dict[str, Any]:
    return {
        "hours": week_rows(session, tenant),
        "days": day_rows(session, tenant),
        "guests": guest_rows(session, tenant),
        "today": business_day(tz_name=tenant.timezone).isoformat(),
    }
