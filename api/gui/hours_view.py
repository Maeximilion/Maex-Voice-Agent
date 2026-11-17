"""Display and form reading of the tab "Zeiten" (#571, docs/06 §4): German
words, and the typed fields turned into what `domain/status` takes.

Every hour shown comes from the database through `domain/status/weekly.py`
and `staff_changes.py`; this file formats them and reads the forms.
"""

import re
from datetime import date
from datetime import time as dt_time
from typing import Any

from sqlalchemy.orm import Session

from api.core.time import business_day
from api.domain.reservations.spoken import WEEKDAYS
from api.domain.status import staff_changes as staff
from api.domain.status import weekly
from api.gui import form_view
from api.gui.figures_view import DASH
from api.gui.figures_view import WEEKDAYS as SHORT
from api.models import Tenant
from api.models.tenants import SERVICES as MODEL_SERVICES

# The columns of the week: every service the model knows, with its word on
# the tablet. A service added to the model without a word fails here, at the
# start, instead of being saved by a form that never showed it.
LABELS = {"dinein": "Im Haus", "pickup": "Abholung", "delivery": "Lieferung"}
SERVICES = tuple((key, LABELS[key]) for key in MODEL_SERVICES)
CLOSED = "geschlossen"
# What a field echoes back at most: "HH:MM" and a little of a typing error.
ECHO = 8
# The length of `weekly.token`.
TOKEN = 16
# The most rows of one service a form is read for: a bound on the loop,
# far above what a restaurant stores (a span per half hour of the day).
ROWS = 48
HHMM = re.compile(r"[0-9]{2}:[0-9]{2}")


def hhmm(text: str) -> dt_time:
    """HH:MM only, digits and a colon: a stored second or offset would undo
    to another time, and `fromisoformat` alone reads far more than a clock
    time. "10.30" is 10:00 and three tenths of a second to it, so the German
    way to write half past ten would open the restaurant half an hour early;
    "1000Z" is 10:00 with an offset, "T1000" a time with a prefix."""
    if not HHMM.fullmatch(text):
        raise ValueError(text)
    return dt_time.fromisoformat(text)


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
    session: Session,
    tenant: Tenant,
    weekday: int,
    typed: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The form of one weekday: what holds, with one empty row per service to
    add a span and the token of these hours; or what was typed and refused
    (`typed`), with the token it was typed against. A refused form never gets
    a fresh token: its rows come from the old hours, and with a new token the
    corrected save would write over what another device changed meanwhile.
    `errors`: the faults by row ("pickup-1") or service ("pickup")."""
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
    errors = errors or {}
    return {
        "weekday": weekday,
        "name": WEEKDAYS[weekday],
        "groups": groups,
        "errors": errors,
        "summary": form_view.summary(errors, _row_names(groups)),
        "seen": weekly.token(day)
        if typed is None
        else typed.get("gesehen", "")[:TOKEN],
    }


def _rows(form: dict[str, str], key: str) -> range:
    """The rows of one service a form sent, counted from 0 without a gap. All
    of them, also more than the domain takes: a service stored with more
    spans than the form adds (provisioning, SQL) must come back whole, or it
    would read as changed and block the weekday; a row too many that the
    owner typed is refused in the domain by name. `ROWS` only bounds the loop."""
    count = 0
    while count < ROWS and f"{key}-von-{count}" in form:
        count += 1
    return range(count)


def typed_spans(form: dict[str, str]) -> tuple[weekly.Day, list[str]]:
    """The spans a weekday's form sent, and every row ("pickup-1") with a
    field that is no HH:MM; with one of those, nothing is to be saved. A row
    with both fields empty is no span; a service the form does not name is
    left out and keeps its hours."""
    spans: weekly.Day = {}
    bad = []
    for key, _label in SERVICES:
        rows = _rows(form, key)
        if not rows:
            continue
        spans[key] = []
        for i in rows:
            von, bis = _typed_row(form, key, i)
            if not von and not bis:
                continue
            overnight = bool(form.get(f"{key}-nachts-{i}"))
            try:
                spans[key].append(weekly.Span(hhmm(von), hhmm(bis), overnight))
            except ValueError:
                bad.append(f"{key}-{i}")
    return spans, bad


def row_errors(form: dict[str, str], faults: dict[str, str]) -> dict[str, str]:
    """The domain's faults ("pickup 18:00-01:00", or "pickup" for the whole
    service) by the rows of the form that typed them ("pickup-1"); a span
    typed twice marks both rows."""
    errors: dict[str, str] = {}
    for key, say in faults.items():
        service, _, span = key.partition(" ")
        rows = [
            f"{service}-{i}"
            for i in _rows(form, service)
            if span and "-".join(_typed_row(form, service, i)) == span
        ]
        for row in rows or [service]:
            errors.setdefault(row, say)
    return errors


def _typed_row(form: dict[str, str], key: str, i: int) -> tuple[str, str]:
    return form.get(f"{key}-von-{i}", "").strip(), form.get(
        f"{key}-bis-{i}", ""
    ).strip()


def _row_names(groups: list[dict[str, Any]]) -> form_view.Fields:
    """Each row and each service by its words in the list of faults; a link
    jumps to the row's first field."""
    names: form_view.Fields = {}
    for group in groups:
        key, label = group["key"], group["label"]
        names[key] = (f"f-{key}-von-0", label)
        for i in range(len(group["rows"])):
            names[f"{key}-{i}"] = (f"f-{key}-von-{i}", f"{label}, Zeile {i + 1}")
    return names


def typed_hours(form: dict[str, str]) -> staff.DayHours:
    """What a form for one date asks for by its button (`art`): its two times,
    closed, or the weekly hours. Raises ValueError on a time that is no HH:MM."""
    choice = form.get("art", "")
    if choice == "zeiten":
        return staff.DayHours(
            opens=hhmm(form.get("von", "")), closes=hhmm(form.get("bis", ""))
        )
    return staff.DayHours(closed=choice == "geschlossen")


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


def _week_part(session: Session, tenant: Tenant) -> dict[str, Any]:
    return {"hours": week_rows(session, tenant)}


def _days_part(session: Session, tenant: Tenant) -> dict[str, Any]:
    return {
        "days": day_rows(session, tenant),
        "today": business_day(tz_name=tenant.timezone).isoformat(),
    }


# What each part of the tab reads; a write answers with its own part only.
PARTS = {"woche": _week_part, "tage": _days_part}


def page(session: Session, tenant: Tenant) -> dict[str, Any]:
    return {
        **_week_part(session, tenant),
        **_days_part(session, tenant),
        "guests": guest_rows(session, tenant),
    }
